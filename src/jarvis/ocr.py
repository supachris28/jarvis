"""Reading text from a shared screenshot (a booking, a WhatsApp message) with Tesseract, on the server.

Phone screenshots are often light text on dark: with Pillow available the image is turned grey, inverted when it's
mostly dark and enlarged when small, which Tesseract reads far better. The text is then treated like shared text.
"""

from __future__ import annotations

import asyncio
import io
import shutil

MAX_IMAGE = 8 * 1024 * 1024


class OCRError(Exception):
    """A user-facing problem reading an image."""


def available() -> bool:
    return shutil.which("tesseract") is not None


def prepare(data: bytes) -> list[bytes]:
    """Versions of the image for Tesseract to try: grey (inverted when dark, enlarged when small), and the same
    turned pure black-and-white — message bubbles and coloured panels defeat Tesseract's own thresholding."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return [data]
    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image).convert("L")
    except Exception:  # noqa: BLE001 — not something Pillow can read: let Tesseract try
        return [data]
    histogram = image.histogram()
    mean = sum(i * n for i, n in enumerate(histogram)) / max(1, sum(histogram))
    if mean < 110:                      # dark mode: light text on a dark background
        image = ImageOps.invert(image)
    if image.width < 1400:
        scale = 1400 / image.width
        image = image.resize((1400, int(image.height * scale)), Image.LANCZOS)
    versions = []
    for version in (image.point(lambda p: 255 if p > 140 else 0), image):
        out = io.BytesIO()
        version.save(out, "PNG")
        versions.append(out.getvalue())
    return versions


async def _tesseract(image: bytes) -> str:
    process = await asyncio.create_subprocess_exec(
        "tesseract", "stdin", "stdout", "-l", "eng", "--psm", "3",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(image), timeout=45)
    except asyncio.TimeoutError:
        process.kill()
        raise OCRError("Reading the image took too long.") from None
    if process.returncode != 0:
        raise OCRError(f"Couldn't read the image ({err.decode(errors='replace').strip()[:120] or 'tesseract failed'}).")
    lines = [line.strip() for line in out.decode("utf-8", errors="replace").splitlines()]
    return "\n".join(line for line in lines if line).strip()


async def read_image(data: bytes) -> str:
    if not data:
        raise OCRError("No image.")
    if len(data) > MAX_IMAGE:
        raise OCRError("That image is too large (8 MB at most).")
    if not available():
        raise OCRError("Reading images isn't available on this server (Tesseract isn't installed).")
    best = ""
    for version in await asyncio.to_thread(prepare, data):
        text = await _tesseract(version)
        if sum(c.isalnum() for c in text) > sum(c.isalnum() for c in best):
            best = text
        if len(best) > 40:  # good enough — skip the slower second try
            break
    return best
