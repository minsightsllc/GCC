'''Safe, retryable email delivery for operational messages.

Business records are written first. Email delivery is an outbox concern so a
mail-provider failure can never undo a client invite or another workflow
mutation. Invite URLs are intentionally kept in the protected outbox and
are never written to logs.
'''

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import ClientInvite, EmployeeInvite, EmailOutbox


logger = logging.getLogger(__name__)


def queue_email(
    *,
    recipient,
    subject,
    body,
    actor=None,
    project=None,
    client=None,
    idempotency_key=None,
):
    '''Create one protected outbox item without sending during the write.'''
    if not recipient:
        return None
    if idempotency_key:
        existing = EmailOutbox.objects.filter(
            idempotency_key=str(idempotency_key)
        ).first()
        if existing:
            return existing
    try:
        with transaction.atomic():
            return EmailOutbox.objects.create(
                recipient=str(recipient).strip().lower(),
                subject=str(subject).strip()[:220],
                body=str(body),
                project=project,
                client=client,
                created_by=actor,
                idempotency_key=str(idempotency_key) if idempotency_key else None,
            )
    except IntegrityError:
        if idempotency_key:
            existing = EmailOutbox.objects.filter(
                idempotency_key=str(idempotency_key)
            ).first()
            if existing:
                return existing
        raise


def queue_client_invite_email(invite: ClientInvite, invite_url: str, *, actor=None):
    '''Queue the one-time client portal URL exactly once for an invite.'''
    client = invite.client
    if not client.email:
        return None
    subject = 'Your Grand Coast Construction client portal invitation'
    body = (
        f'Hello {client.name}\n\n'
        'Grand Coast Construction has created a secure client portal for your project. '
        'Use the one-time link below to create your portal access:\n\n'
        f'{invite_url}\n\n'
        'This link expires in seven days and can only be used once. '
        'If you did not expect this invitation, contact Grand Coast Construction.\n\n'
        'Grand Coast Construction'
    )
    return queue_email(
        recipient=client.email,
        subject=subject,
        body=body,
        actor=actor,
        client=client,
        idempotency_key=f'client-portal-invite:{invite.pk}',
    )


def queue_employee_invite_email(invite: EmployeeInvite, invite_url: str, *, actor=None):
    '''Queue the one-time employee onboarding URL exactly once for an invite.'''
    if invite.purpose != EmployeeInvite.Purpose.ONBOARDING or not invite.email:
        return None
    subject = 'Your Grand Coast Construction employee invitation'
    name = ' '.join(part for part in (invite.first_name, invite.last_name) if part).strip()
    greeting = f'Hello {name}' if name else 'Hello'
    body = (
        f'{greeting},\n\n'
        'Grand Coast Construction has invited you to create your employee account. '
        'Use the one-time link below to choose your sign-in details:\n\n'
        f'{invite_url}\n\n'
        'This link expires in seven days and can only be used once. '
        'If you did not expect this invitation, contact Grand Coast Construction.\n\n'
        'Grand Coast Construction'
    )
    return queue_email(
        recipient=invite.email,
        subject=subject,
        body=body,
        actor=actor,
        idempotency_key=f'employee-onboarding-invite:{invite.pk}',
    )


def _refresh_outbox(outbox):
    return EmailOutbox.objects.get(pk=outbox.pk)


def _mark_failed(outbox, error, *, retry_minutes=5):
    message = str(error).strip()[:500] or error.__class__.__name__
    EmailOutbox.objects.filter(pk=outbox.pk, status=EmailOutbox.Status.SENDING).update(
        status=EmailOutbox.Status.FAILED,
        next_attempt_at=timezone.now() + timedelta(minutes=retry_minutes),
        last_error=message,
    )
    logger.exception('Unable to deliver email outbox item %s.', outbox.pk)
    return _refresh_outbox(outbox)


def deliver_email_outbox(outbox):
    '''Attempt one due outbox item and return its current persisted state.'''
    if outbox is None:
        return None
    if not getattr(settings, 'GCC_EMAIL_DELIVERY_ENABLED', False):
        return _refresh_outbox(outbox)

    now = timezone.now()
    with transaction.atomic():
        item = EmailOutbox.objects.select_for_update().get(pk=outbox.pk)
        if item.status == EmailOutbox.Status.SENT:
            return item
        if item.next_attempt_at and item.next_attempt_at > now:
            return item
        from_email = (getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '').strip()
        if not from_email:
            item.status = EmailOutbox.Status.FAILED
            item.attempt_count += 1
            item.next_attempt_at = now + timedelta(minutes=5)
            item.last_error = 'DEFAULT_FROM_EMAIL is not configured.'
            item.save(update_fields=['status', 'attempt_count', 'next_attempt_at', 'last_error'])
            logger.error('DEFAULT_FROM_EMAIL is not configured for email outbox item %s.', item.pk)
            return item
        item.status = EmailOutbox.Status.SENDING
        item.attempt_count += 1
        item.next_attempt_at = now + timedelta(minutes=15)
        item.last_error = ''
        item.save(update_fields=['status', 'attempt_count', 'next_attempt_at', 'last_error'])

    try:
        sent_count = send_mail(
            item.subject,
            item.body,
            from_email,
            [item.recipient],
            fail_silently=False,
        )
        if not sent_count:
            raise RuntimeError('The configured email backend accepted no recipients.')
    except Exception as exc:
        return _mark_failed(item, exc)

    completed_at = timezone.now()
    EmailOutbox.objects.filter(pk=item.pk, status=EmailOutbox.Status.SENDING).update(
        status=EmailOutbox.Status.SENT,
        sent_at=completed_at,
        next_attempt_at=completed_at,
        last_error='',
    )
    return _refresh_outbox(item)


def dispatch_pending_email_outbox(*, limit=100):
    '''Attempt due pending, failed, or expired sending items.'''
    now = timezone.now()
    items = list(
        EmailOutbox.objects.filter(
            Q(status=EmailOutbox.Status.PENDING)
            | Q(status=EmailOutbox.Status.FAILED)
            | Q(status=EmailOutbox.Status.SENDING),
            next_attempt_at__lte=now,
        ).order_by('next_attempt_at', 'created_at')[:max(1, int(limit))]
    )
    counts = {'attempted': 0, 'sent': 0, 'failed': 0, 'pending': 0}
    for item in items:
        counts['attempted'] += 1
        result = deliver_email_outbox(item)
        if result.status == EmailOutbox.Status.SENT:
            counts['sent'] += 1
        elif result.status == EmailOutbox.Status.FAILED:
            counts['failed'] += 1
        else:
            counts['pending'] += 1
    return counts
