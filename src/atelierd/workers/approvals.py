"""Escalated tool calls, held until the reviewer answers them.

A worker's permission-prompt tool call blocks inside the worker-side MCP until its `Approval` is answered here. The
broker is the daemon's, across every project, because the reviewer answers from one badge whichever source asked.
Only `answer` resolves an approval as allowed or denied, and only the GUI's RPC calls it: a manager cannot grant
permissions (see `docs/mcp.md`). An approval still pending when its turn ends (interrupted, the worker stopped) is
cancelled, which the worker hears as a denial.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC, datetime

from atelierd.workers.models import Approval, ApprovalEvent, ApprovalStatus

logger = logging.getLogger(__name__)


class ApprovalBroker:
    """Every pending approval in the daemon, with the future its worker waits on."""

    def __init__(
        self,
        on_event: Callable[[ApprovalEvent], None],
        notify: Callable[[Approval], None],
    ) -> None:
        """Create the broker.

        Args:
            on_event: called (on any thread) when an approval is raised or resolved.
            notify: raises a desktop notification for a new approval.
        """
        self._on_event = on_event
        self._notify = notify
        self._guard = threading.Lock()
        self._pending: dict[str, tuple[Approval, Future[Approval]]] = {}

    def request(
        self,
        project: str,
        source: str,
        tool_name: str,
        tool_summary: str,
        tool_input: dict[str, object],
        reason: str,
    ) -> tuple[Approval, Future[Approval]]:
        """Raise an approval and return it with the future that resolves when it is answered or cancelled."""
        approval = Approval(
            id=uuid.uuid4().hex,
            project=project,
            source=source,
            tool_name=tool_name,
            tool_summary=tool_summary,
            tool_input=tool_input,
            reason=reason,
            created_at=datetime.now(UTC).isoformat(),
        )
        future: Future[Approval] = Future()
        with self._guard:
            self._pending[approval.id] = (approval, future)
        logger.info(f"[Approvals] {project} {source}: {tool_name} escalated ({reason})")
        self._on_event(ApprovalEvent(approval=approval))
        try:
            self._notify(approval)
        except OSError as error:
            logger.warning(f"[Approvals] Could not raise a desktop notification: {error}")
        return approval, future

    def answer(self, approval_id: str, is_allowed: bool, message: str = "") -> Approval:
        """Allow or deny a pending approval.

        Raises:
            KeyError: no approval with that id is pending (it was answered, cancelled, or never existed).
        """
        status = ApprovalStatus.ALLOWED if is_allowed else ApprovalStatus.DENIED
        return self._resolve(approval_id, status, message)

    def cancel(self, project: str, source: str, message: str) -> list[Approval]:
        """Cancel every approval pending for one source's worker, and return them."""
        with self._guard:
            approval_ids = [
                approval.id
                for approval, _ in self._pending.values()
                if approval.project == project and approval.source == source
            ]
        cancelled: list[Approval] = []
        for approval_id in approval_ids:
            try:
                cancelled.append(self._resolve(approval_id, ApprovalStatus.CANCELLED, message))
            except KeyError:
                logger.debug(f"[Approvals] {approval_id} was answered while being cancelled")
        return cancelled

    def list_pending(self, project: str | None = None) -> list[Approval]:
        """Return the pending approvals, oldest first, for one project or all of them."""
        with self._guard:
            approvals = [approval for approval, _ in self._pending.values()]
        matching = [approval for approval in approvals if project is None or approval.project == project]
        return sorted(matching, key=lambda approval: approval.created_at)

    def _resolve(self, approval_id: str, status: ApprovalStatus, message: str) -> Approval:
        with self._guard:
            entry = self._pending.pop(approval_id, None)
        if entry is None:
            raise KeyError(f"No pending approval {approval_id}")
        approval, future = entry
        resolved = approval.model_copy(update={"status": status, "message": message})
        logger.info(f"[Approvals] {approval.source}: {approval.tool_name} {status.value}")
        future.set_result(resolved)
        self._on_event(ApprovalEvent(approval=resolved))
        return resolved
