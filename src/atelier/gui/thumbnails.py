"""Small copies of the review grid's images, decoded to tile size once and kept under the project's state folder.

A delivery's images are full-size renders and sheets, and a queue can hold well over a hundred tickets: decoding every
one at full size each time the grid shows would stall it. Each image is instead decoded once, straight to thumbnail
size (`QImageReader.setScaledSize`, which lets a JPEG decode at a fraction of its size), and saved as a PNG named by
the image's path, size and modification time, so a changed file gets a new thumbnail and an unchanged one is never
decoded again. The work is blocking and runs off the UI thread; QImage is safe to use there.

The images are untrusted tracker data: one that cannot be decoded has no thumbnail, and the tile says so.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImageReader

logger = logging.getLogger(__name__)

# The longest side of a thumbnail, in pixels: a tile is about 240 logical pixels wide, twice that on a HiDPI screen.
THUMBNAIL_SIZE = 480

# Bumped when the way thumbnails are made changes, so older ones are made again.
_THUMBNAIL_VERSION = 1


class ThumbnailCache:
    """Makes and finds thumbnails in one folder."""

    def __init__(self, folder: Path, size: int = THUMBNAIL_SIZE) -> None:
        self._folder = folder
        self._size = size

    def thumbnail(self, image_path: Path) -> Path | None:
        """Return a thumbnail of an image, making it on a miss; None when the image cannot be read.

        Blocking: call it off the UI thread.
        """
        try:
            image_stat = image_path.stat()
        except OSError as error:
            logger.debug(f"[Thumbnails] {image_path} is not readable: {error}")
            return None
        key = f"{_THUMBNAIL_VERSION}|{self._size}|{image_path}|{image_stat.st_size}|{image_stat.st_mtime_ns}"
        thumbnail_path = self._folder / f"{hashlib.sha256(key.encode()).hexdigest()}.png"
        if thumbnail_path.is_file():
            return thumbnail_path

        reader = QImageReader(str(image_path))
        reader.setAutoTransform(True)
        full_size = reader.size()
        if full_size.isValid() and (full_size.width() > self._size or full_size.height() > self._size):
            bounds = QSize(self._size, self._size)
            reader.setScaledSize(full_size.scaled(bounds, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            logger.debug(f"[Thumbnails] Could not decode {image_path}: {reader.errorString()}")
            return None

        # Written under a name of its own and renamed into place, so a reader never sees half a file, and two tickets
        # sharing an image (a source's standard render) never write over each other's partial file.
        self._folder.mkdir(parents=True, exist_ok=True)
        partial_path = thumbnail_path.with_name(f".{thumbnail_path.stem}.{uuid.uuid4().hex}.partial.png")
        if not image.save(str(partial_path)):
            logger.warning(f"[Thumbnails] Could not write a thumbnail of {image_path} to {self._folder}")
            partial_path.unlink(missing_ok=True)
            return None
        partial_path.replace(thumbnail_path)
        return thumbnail_path
