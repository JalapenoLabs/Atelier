"""The content-addressed object store, `<state>/objects/<sha256>`: every snapshotted file's bytes, stored once.

A file is hashed while it is copied, in one read, into a temporary name in the store and then renamed to its hash, so
a reader never sees a partial object, and an object that already exists is never written again. Objects are made
read-only: nothing in Atelier changes one after it is written.

Recording a revision stores its objects before it inserts the manifest naming them, so a prune running at the same
moment could delete an object that no manifest names yet. Writers therefore hold a shared lock on the store and a
prune holds it exclusively. The lock is an `flock` on `<objects>/.lock`, so it also holds between processes, such as
`atelier prune` beside an open window.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import shutil
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

_CHUNK_BYTES = 1 << 20
_LOCK_FILE_NAME = ".lock"
_PARTIAL_SUFFIX = ".partial"
_READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH


class ObjectStore:
    """Read and write one project's object store."""

    def __init__(self, objects_dir: Path) -> None:
        self.objects_dir = objects_dir

    def path_of(self, sha256: str) -> Path:
        """Return where the object with this hash lives (whether or not it exists)."""
        return self.objects_dir / sha256

    def has(self, sha256: str) -> bool:
        """Whether the object with this hash is stored."""
        return self.path_of(sha256).is_file()

    def store(self, file: Path) -> tuple[str, int]:
        """Copy a file into the store under its hash, unless it is already there, and return its hash and size.

        Call it holding `shared_lock()`.
        """
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        partial_path = self.objects_dir / f"{uuid.uuid4().hex}{_PARTIAL_SUFFIX}"
        digest = hashlib.sha256()
        size = 0
        try:
            with file.open("rb") as source_file, partial_path.open("wb") as partial_file:
                while chunk := source_file.read(_CHUNK_BYTES):
                    digest.update(chunk)
                    partial_file.write(chunk)
                    size += len(chunk)

            sha256 = digest.hexdigest()
            object_path = self.path_of(sha256)
            if object_path.is_file():
                logger.debug(f"[Objects] {file.name} is already stored as {sha256[:12]}")
            else:
                partial_path.chmod(_READ_ONLY)
                partial_path.replace(object_path)
                logger.debug(f"[Objects] Stored {file.name} as {sha256[:12]} ({size} bytes)")
        finally:
            partial_path.unlink(missing_ok=True)
        return sha256, size

    def write_to(self, sha256: str, target: Path) -> None:
        """Write an object's bytes to `target`, atomically: a reader sees the old file or the new one, never half.

        The new file keeps the permissions of the file it replaces.

        Raises:
            FileNotFoundError: the store has no such object.
        """
        object_path = self.path_of(sha256)
        if not object_path.is_file():
            raise FileNotFoundError(f"Object {sha256} is not in {self.objects_dir}")

        target.parent.mkdir(parents=True, exist_ok=True)
        # A name no catalog convention matches (not `.fbx`, and `.blend` only inside it), so a scan that runs
        # while the copy is under way never takes it for a source's file.
        partial_path = target.with_name(f"{target.name}.atelier-restore-{uuid.uuid4().hex[:8]}")
        try:
            shutil.copyfile(object_path, partial_path)
            if target.exists():
                shutil.copymode(target, partial_path)
            else:
                partial_path.chmod(0o644)
            partial_path.replace(target)
        finally:
            partial_path.unlink(missing_ok=True)

    def delete(self, sha256: str) -> int:
        """Delete an object and return the bytes freed. Call it holding `exclusive_lock()`."""
        object_path = self.path_of(sha256)
        size = object_path.stat().st_size
        object_path.unlink()
        return size

    def list_hashes(self) -> set[str]:
        """Return the hash of every stored object."""
        if not self.objects_dir.is_dir():
            return set()
        return {
            entry.name
            for entry in self.objects_dir.iterdir()
            if entry.is_file() and not entry.name.startswith(".") and not entry.name.endswith(_PARTIAL_SUFFIX)
        }

    @contextmanager
    def shared_lock(self) -> Iterator[None]:
        """Hold the store against a prune while objects are stored and their manifest is written."""
        with self._lock(fcntl.LOCK_SH):
            yield

    @contextmanager
    def exclusive_lock(self) -> Iterator[None]:
        """Hold the store alone, while a prune deletes unreferenced objects."""
        with self._lock(fcntl.LOCK_EX):
            yield

    @contextmanager
    def _lock(self, operation: int) -> Iterator[None]:
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        lock_descriptor = os.open(self.objects_dir / _LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock_descriptor, operation)
            yield
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)
