"""Remove user-identifying metadata from uploaded photos and videos."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import warnings
from io import BytesIO
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError


MAX_SANITIZED_MEDIA_SIZE = 50 * 1024 * 1024
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}
SUPPORTED_MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
REJECTED_MEDIA_EXTENSIONS = {
    ".gif", ".heic", ".heif", ".webm", ".mkv", ".avi", ".m4v", ".3gp",
}
DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt",
}
_SANITIZED_MARKER = "_gcc_metadata_stripped"


def _photo_bytes(name, content):
    extension = Path(name).suffix.lower()
    expected_format = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}[extension]
    try:
        position = content.tell()
    except (AttributeError, OSError):
        position = 0
    try:
        content.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(content) as original:
                if original.format != expected_format:
                    raise ValidationError("The file contents do not match the selected photo type.")
                if getattr(original, "n_frames", 1) > 1:
                    raise ValidationError("Animated images are not supported. Upload an MP4 or MOV video instead.")
                original.load()
                cleaned = ImageOps.exif_transpose(original)
                if (
                    expected_format == "PNG"
                    and cleaned.mode == "P"
                    and "transparency" in cleaned.info
                ):
                    cleaned = cleaned.convert("RGBA")
                cleaned.info = {}

                save_options = {}
                if expected_format == "JPEG":
                    if cleaned.mode in {"RGBA", "LA", "P"}:
                        rgba = cleaned.convert("RGBA")
                        background = Image.new("RGB", rgba.size, "white")
                        background.paste(rgba, mask=rgba.getchannel("A"))
                        cleaned = background
                    elif cleaned.mode not in {"RGB", "L", "CMYK"}:
                        cleaned = cleaned.convert("RGB")
                    save_options.update(quality=95, optimize=True)
                elif expected_format == "PNG":
                    save_options["optimize"] = True
                else:
                    if cleaned.mode not in {"RGB", "RGBA", "L"}:
                        cleaned = cleaned.convert("RGBA" if "A" in cleaned.getbands() else "RGB")
                    save_options.update(lossless=True, method=4)

                output = BytesIO()
                cleaned.save(output, format=expected_format, **save_options)
                result = output.getvalue()
    except ValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError, UnidentifiedImageError) as exc:
        raise ValidationError("This photo could not be read. Choose a valid JPG, PNG, or WebP image.") from exc
    finally:
        try:
            content.seek(position)
        except (AttributeError, OSError):
            pass

    _ensure_output_size(result)
    return result


def _ffmpeg_executable():
    executable = shutil.which(os.getenv("IMAGEIO_FFMPEG_EXE", "ffmpeg"))
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError) as exc:
        raise ValidationError("Video cleanup is unavailable. Contact the site administrator.") from exc


def _video_bytes(name, content):
    extension = Path(name).suffix.lower()
    try:
        with tempfile.TemporaryDirectory(prefix="gcc-media-clean-") as directory:
            source = Path(directory) / f"source{extension}"
            output = Path(directory) / f"cleaned{extension}"
            try:
                content.seek(0)
            except (AttributeError, OSError):
                pass
            with source.open("wb") as source_file:
                for chunk in content.chunks():
                    source_file.write(chunk)

            # Re-mux just the picture and sound streams. This removes location,
            # device, timestamp, chapter, and other container metadata without
            # changing the encoded video/audio or retaining an original copy.
            command = [
                _ffmpeg_executable(),
                "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-protocol_whitelist", "file", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a?", "-sn", "-dn",
                "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1",
                "-c", "copy", "-movflags", "+faststart", "-fflags", "+bitexact",
                str(output),
            ]
            subprocess.run(command, check=True, capture_output=True, timeout=180)
            if not output.is_file():
                raise ValidationError("This video could not be processed. Choose an MP4 or MOV video.")
            result = output.read_bytes()
    except ValidationError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError("This video could not be processed. Choose a valid MP4 or MOV video.") from exc
    finally:
        try:
            content.seek(0)
        except (AttributeError, OSError):
            pass

    _ensure_output_size(result)
    return result


def _ensure_output_size(data):
    if not data or len(data) > MAX_SANITIZED_MEDIA_SIZE:
        raise ValidationError("The cleaned file must be no larger than 50 MB.")


def sanitize_media_bytes(name, content):
    extension = Path(str(name or "")).suffix.lower()
    input_size = getattr(content, "size", None)
    try:
        if input_size is not None and int(input_size) > MAX_SANITIZED_MEDIA_SIZE:
            raise ValidationError("Files must be 50 MB or smaller.")
    except (TypeError, ValueError):
        pass
    header = _read_header(content)
    if extension in REJECTED_MEDIA_EXTENSIONS:
        raise ValidationError("Use JPG, PNG, or WebP for photos and MP4 or MOV for videos.")
    if _looks_like_media_payload(header):
        if extension in SUPPORTED_MEDIA_EXTENSIONS:
            # Let the format-specific decoder return the useful mismatch error.
            pass
        else:
            raise ValidationError("Use JPG, PNG, or WebP for photos and MP4 or MOV for videos.")
    if extension in IMAGE_EXTENSIONS:
        return _photo_bytes(name, content)
    if extension in VIDEO_EXTENSIONS:
        return _video_bytes(name, content)
    if extension in DOCUMENT_EXTENSIONS:
        return None
    raise ValidationError("This file type is not supported. Use a supported photo, video, or document format.")


def _looks_like_media_payload(header):
    if (
        header.startswith((
            b"GIF87a", b"GIF89a", b"\x1a\x45\xdf\xa3", b"BM",
            b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n",
        ))
        or _looks_like_heic(header)
        or header.startswith((b"II*\x00", b"MM\x00*"))
        or (header[:4] == b"RIFF" and header[8:12] in {b"AVI ", b"WEBP"})
    ):
        return True
    if b"ftyp" in header[:32]:
        # ISO-BMFF content is handled only for the explicitly accepted MP4/MOV
        # suffixes; HEIC brands were detected above.
        return True
    return False


def _read_header(content, max_bytes=64):
    try:
        position = content.tell()
    except (AttributeError, OSError):
        position = 0
    try:
        content.seek(0)
        return content.read(max_bytes) or b""
    except (AttributeError, OSError):
        return b""
    finally:
        try:
            content.seek(position)
        except (AttributeError, OSError):
            pass


def _looks_like_heic(header):
    if len(header) < 12 or header[4:8] != b"ftyp":
        return False
    brands = {header[8:12], header[16:20], header[20:24], header[24:28], header[28:32]}
    return bool(brands & {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"})


def sanitize_uploaded_media(upload):
    """Replace an incoming upload's bytes before any permanent storage write."""
    if not upload or getattr(upload, _SANITIZED_MARKER, False):
        return upload
    data = sanitize_media_bytes(upload.name, upload)
    if data is None:
        return upload

    old_file = getattr(upload, "file", None)
    upload.file = ContentFile(data, name=upload.name)
    upload.size = len(data)
    setattr(upload, _SANITIZED_MARKER, True)
    upload.seek(0)
    if old_file is not None and old_file is not upload.file:
        try:
            old_file.close()
        except (AttributeError, OSError):
            pass
    return upload


def sanitized_storage_content(name, content):
    """Sanitize a FileField write even when a caller skipped form validation."""
    if getattr(content, _SANITIZED_MARKER, False):
        return content
    data = sanitize_media_bytes(name, content)
    if data is None:
        return content
    cleaned = ContentFile(data, name=name)
    setattr(cleaned, _SANITIZED_MARKER, True)
    return cleaned
