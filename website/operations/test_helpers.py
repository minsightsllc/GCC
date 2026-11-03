"""Helpers for tests that bypass the interactive administrator sign-in flow."""

from types import SimpleNamespace
from functools import lru_cache
from pathlib import Path
import subprocess
import tempfile

from django.test import Client
from django.urls import reverse

from .security import is_active_admin, set_gate


class GatedTestClient(Client):
    """Client for feature tests that bypass interactive admin authentication."""

    def force_login(self, user, backend=None):
        super().force_login(user, backend=backend)
        if is_active_admin(user):
            session = self.session
            set_gate(SimpleNamespace(session=session), user, reverse("admin:index"))
            session.save()


def valid_test_image_bytes(image_format="PNG"):
    """Return a tiny real image for tests that exercise successful uploads."""
    from io import BytesIO

    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (2, 2), (40, 90, 150)).save(output, format=image_format)
    return output.getvalue()


@lru_cache(maxsize=1)
def valid_test_mp4_bytes():
    """Return a tiny, decodable MP4 fixture for successful upload tests."""
    import imageio_ffmpeg

    with tempfile.TemporaryDirectory(prefix="gcc-test-video-") as directory:
        output = Path(directory) / "fixture.mp4"
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=c=black:s=32x32:d=1", "-c:v", "mpeg4",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y",
                str(output),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return output.read_bytes()


def force_login_with_admin_gate(client, user, **kwargs):
    """Force-login a test user and establish the gate required by admin views."""
    client.force_login(user, **kwargs)
    if is_active_admin(user):
        session = client.session
        set_gate(SimpleNamespace(session=session), user, reverse("admin:index"))
        session.save()
    return client
