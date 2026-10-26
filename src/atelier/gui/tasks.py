"""Running blocking work off the UI thread and handing the result back on it.

Every call into the backend can block, so the window submits it here. Catalog work runs on one serial thread, which
keeps it in order (open a project, then list its sources); Blender conversions run on their own small pool, so a slow
conversion never holds up a click in the tree. The callbacks always run on the UI thread, through a queued signal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

logger = logging.getLogger(__name__)

# Concurrent Blender conversions; each one is a whole Blender process.
_CONVERSION_WORKERS = 2


class TaskRunner(QObject):
    """Runs functions on background threads and calls back on the thread that owns this object."""

    _finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._catalog_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catalog")
        self._conversion_executor = ThreadPoolExecutor(max_workers=_CONVERSION_WORKERS, thread_name_prefix="convert")
        self._finished.connect(self._deliver)

    def run_catalog(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """Run catalog work after any submitted before it."""
        self._submit(self._catalog_executor, work, on_done, on_error)

    def run_conversion(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """Run a Blender conversion alongside other work."""
        self._submit(self._conversion_executor, work, on_done, on_error)

    def shutdown(self) -> None:
        """Stop accepting work and drop anything not yet started."""
        self._catalog_executor.shutdown(wait=False, cancel_futures=True)
        self._conversion_executor.shutdown(wait=False, cancel_futures=True)

    def _submit(
        self,
        executor: ThreadPoolExecutor,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        future = executor.submit(work)
        # Emitted from the worker thread; the queued connection runs _deliver on this object's thread.
        future.add_done_callback(lambda finished: self._finished.emit((finished, on_done, on_error)))

    @Slot(object)
    def _deliver(self, payload: tuple[Future[Any], Callable[[Any], None], Callable[[Exception], None]]) -> None:
        future, on_done, on_error = payload
        if future.cancelled():
            logger.debug("[Tasks] A cancelled task was dropped")
            return
        error = future.exception()
        if isinstance(error, Exception):
            on_error(error)
            return
        if error is not None:
            raise error
        on_done(future.result())
