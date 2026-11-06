from __future__ import annotations

import hashlib
import html
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import MessageTranslationCache, MessageTranslationUsage


logger = logging.getLogger("operations.message_translations")

# Keep a 10,000-character margin under Google's 500,000-character monthly
# allowance so unrelated usage and character-counting edge cases cannot push
# this app to the provider's paid tier.
MONTHLY_CHARACTER_LIMIT = 490_000
MAX_TEXT_CHARACTERS_PER_REQUEST = 30_000
TRANSLATE_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"


def _language_code(value):
    return str(value or "").strip().lower().replace("_", "-").split("-", 1)[0]


def _target_language(value):
    return "es" if _language_code(value) == "es" else "en"


def _source_hash(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _character_count(body):
    # UTF-16 code units conservatively count characters outside the BMP twice.
    return max(len(body), len(body.encode("utf-16-le", errors="surrogatepass")) // 2)


def _valid_cache_for(messages, target_language, hashes):
    message_ids = [message.pk for message in messages]
    if not message_ids:
        return {}

    cache_rows = MessageTranslationCache.objects.filter(
        message_id__in=message_ids,
        target_language=target_language,
    )
    return {
        cache.message_id: cache
        for cache in cache_rows
        if hashes.get(cache.message_id) == cache.source_hash
    }


def _reserve_monthly_characters(candidates):
    if not candidates:
        return []

    month_start = timezone.localdate().replace(day=1)
    with transaction.atomic():
        MessageTranslationUsage.objects.get_or_create(month_start=month_start)
        usage = MessageTranslationUsage.objects.select_for_update().get(
            month_start=month_start
        )
        remaining = max(
            MONTHLY_CHARACTER_LIMIT - usage.characters_reserved,
            0,
        )

        # Translate the newest messages first when the remaining allowance
        # cannot cover every uncached message in the conversation.
        selected = []
        for message, source_hash in reversed(candidates):
            characters = _character_count(message.body)
            if characters and characters <= MAX_TEXT_CHARACTERS_PER_REQUEST and characters <= remaining:
                selected.append((message, source_hash, characters))
                remaining -= characters

        reserved = sum(characters for _message, _hash, characters in selected)
        if reserved:
            usage.characters_reserved += reserved
            usage.save(update_fields=["characters_reserved", "updated_at"])

    selected.reverse()
    return [(message, source_hash) for message, source_hash, _count in selected]


def _request_translation_batch(items, target_language):
    api_key = settings.GCC_GOOGLE_TRANSLATE_API_KEY
    if not api_key or not items:
        return None

    form_data = [
        ("target", target_language),
        ("format", "text"),
    ]
    form_data.extend(("q", message.body) for message, _source_hash in items)
    request = Request(
        TRANSLATE_ENDPOINT,
        data=urlencode(form_data).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        logger.warning("Google message translation request failed (HTTP %s).", error.code)
        return None
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Google message translation request failed.")
        return None

    translations = payload.get("data", {}).get("translations", [])
    if len(translations) != len(items):
        logger.warning("Google message translation returned an incomplete response.")
        return None
    return translations


def _request_translations(items, target_language):
    results = []
    batch = []
    batch_characters = 0

    for item in items:
        characters = _character_count(item[0].body)
        if batch and (
            len(batch) >= 128
            or batch_characters + characters > MAX_TEXT_CHARACTERS_PER_REQUEST
        ):
            batch_results = _request_translation_batch(batch, target_language)
            results.extend(
                batch_results if batch_results is not None else [None] * len(batch)
            )
            batch = []
            batch_characters = 0
        batch.append(item)
        batch_characters += characters

    if batch:
        batch_results = _request_translation_batch(batch, target_language)
        results.extend(
            batch_results if batch_results is not None else [None] * len(batch)
        )

    return results


def apply_message_translations(messages, display_language):
    """Attach cached/provider-translated text without changing original messages."""
    messages = list(messages or [])
    for message in messages:
        message.display_body = message.body

    api_key = settings.GCC_GOOGLE_TRANSLATE_API_KEY
    if not messages or not api_key:
        return

    target_language = _target_language(display_language)
    hashes = {
        message.pk: _source_hash(message.body)
        for message in messages
    }
    cached = _valid_cache_for(messages, target_language, hashes)
    pending = [
        (message, hashes[message.pk])
        for message in messages
        if message.pk not in cached and message.body
    ]
    reserved = _reserve_monthly_characters(pending)

    if reserved:
        translations = _request_translations(reserved, target_language)
        cache_rows = []
        for (message, source_hash), result in zip(reserved, translations):
            if not isinstance(result, dict):
                continue
            translated_body = result.get("translatedText")
            if not isinstance(translated_body, str):
                continue
            cache_rows.append(
                MessageTranslationCache(
                    message=message,
                    target_language=target_language,
                    source_hash=source_hash,
                    source_language=_language_code(
                        result.get("detectedSourceLanguage")
                    )[:35],
                    translated_body=html.unescape(translated_body),
                )
            )
        if cache_rows:
            MessageTranslationCache.objects.bulk_create(
                cache_rows,
                ignore_conflicts=True,
            )
        cached = _valid_cache_for(messages, target_language, hashes)

    for message in messages:
        translation = cached.get(message.pk)
        if translation is None:
            continue
        if translation.source_language and translation.source_language == target_language:
            message.display_body = message.body
        else:
            message.display_body = translation.translated_body
