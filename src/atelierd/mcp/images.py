"""Images for the manager MCP: renders scaled down to a size a model's context can afford, and side-by-side compares.

Standard renders are 1024 px PNGs on a flat backdrop. A manager judges them as well at 512 px, and as JPEG they cost
a fraction of the bytes, so every image the manager MCP returns goes through here. A compare is one image per view,
revision A on the left and B on the right, each labelled in its corner, so a model never has to pair up two images
it saw in different places.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_JPEG_QUALITY = 85
# The flat grey a missing side of a compare is filled with: close to the renders' own backdrop.
_PLACEHOLDER_GREY = (96, 96, 96)
_LABEL_MARGIN = 8
_LABEL_PADDING = 4


def scaled_jpeg(path: Path, edge: int) -> bytes:
    """Return an image file as JPEG bytes, scaled so its longer side is at most `edge` pixels."""
    with Image.open(path) as image:
        picture = image.convert("RGB")
    picture.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    return _encode_jpeg(picture)


def side_by_side_jpeg(
    left: Path | None,
    right: Path | None,
    edge: int,
    left_label: str,
    right_label: str,
) -> bytes:
    """Return two images side by side as one JPEG, each scaled into an `edge` pixel square and labelled.

    A side with no image (that revision has no render of the view) is a grey square that says so.
    """
    canvas = Image.new("RGB", (edge * 2, edge), _PLACEHOLDER_GREY)
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=max(12, edge // 24))
    for offset, path, label in ((0, left, left_label), (edge, right, right_label)):
        if path is None:
            label += " (no render)"
        else:
            with Image.open(path) as image:
                picture = image.convert("RGB")
            picture.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            # Centred in its half, in case a render is not square.
            canvas.paste(picture, (offset + (edge - picture.width) // 2, (edge - picture.height) // 2))
        left_edge, top_edge, right_edge, bottom_edge = draw.textbbox(
            (offset + _LABEL_MARGIN, _LABEL_MARGIN), label, font=font
        )
        draw.rectangle(
            (
                left_edge - _LABEL_PADDING,
                top_edge - _LABEL_PADDING,
                right_edge + _LABEL_PADDING,
                bottom_edge + _LABEL_PADDING,
            ),
            fill=(0, 0, 0),
        )
        draw.text((offset + _LABEL_MARGIN, _LABEL_MARGIN), label, fill=(255, 255, 255), font=font)
    # A thin divider, so two similar renders never read as one wide image.
    draw.line(((edge, 0), (edge, edge)), fill=(0, 0, 0), width=2)
    return _encode_jpeg(canvas)


def _encode_jpeg(picture: Image.Image) -> bytes:
    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    logger.debug(f"[Manager MCP] Encoded a {picture.width}x{picture.height} JPEG of {buffer.tell()} bytes")
    return buffer.getvalue()
