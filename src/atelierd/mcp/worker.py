"""The worker-side `atelier` MCP server: the permission-prompt tool every worker runs with.

Each worker is started with `--permission-prompt-tool mcp__atelier__approve`, so Claude Code calls `approve` whenever
it would otherwise ask a person about a tool call, including every call the scope policy's hook answered `ask`. The
call carries the worker's secret token in a header, which says which worker it is.

`approve` applies the scope policy again (the same `evaluate` the hook runs), so a call inside the scope is allowed
at once. Anything else becomes an approval card for the reviewer, and the call waits, however long it takes, until
the card is answered or the turn ends. The answer goes back in the shape Claude Code expects of a permission-prompt
tool: one text block holding `{"behavior": "allow", "updatedInput": ...}` or `{"behavior": "deny", "message": ...}`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from atelierd.constants import WORKER_APPROVAL_TOOL, WORKER_MCP_SERVER_NAME, WORKER_TOKEN_HEADER
from atelierd.daemon.core import AtelierCore
from atelierd.workers import ApprovalStatus, Worker

logger = logging.getLogger(__name__)

_INSTRUCTIONS = (
    "Atelier, the studio reviewing this worker's source. Its only tool decides whether a tool call may run; Claude "
    "Code calls it by itself, so never call it directly."
)


def find_worker(core: AtelierCore, token: str) -> Worker | None:
    """Return the worker a token belongs to, in any open project."""
    for runtime in core.open_projects():
        worker = runtime.workers.find_by_token(token)
        if worker:
            return worker
    return None


def build_worker_mcp(core: AtelierCore) -> MCPServer:
    """Return the worker-side MCP server over `core`."""
    server: MCPServer = MCPServer(WORKER_MCP_SERVER_NAME, instructions=_INSTRUCTIONS)

    # Unstructured, so the result is exactly one text block: Claude Code rejects a permission answer with more.
    @server.tool(name=WORKER_APPROVAL_TOOL, structured_output=False)
    async def approve(
        tool_name: str,
        input: dict[str, Any],
        ctx: Context,
        tool_use_id: str | None = None,
    ) -> str:
        """Decide whether a worker's tool call may run, asking the reviewer when the scope policy escalates it."""
        request = ctx.request_context.request
        token = request.headers.get(WORKER_TOKEN_HEADER, "") if request is not None else ""
        worker = find_worker(core, token)
        if worker is None:
            logger.warning(f"[Worker MCP] An approval request from no known worker, for {tool_name}")
            return json.dumps({"behavior": "deny", "message": "Atelier does not know this worker"})

        decision = await asyncio.to_thread(worker.decide, tool_name, input)
        if decision.is_allowed:
            return json.dumps({"behavior": "allow", "updatedInput": input})

        future = await asyncio.to_thread(worker.escalate, tool_name, input, decision.reason)
        approval = await asyncio.wrap_future(future)
        if approval.status is ApprovalStatus.ALLOWED:
            return json.dumps({"behavior": "allow", "updatedInput": input})
        if approval.status is ApprovalStatus.DENIED:
            message = "The reviewer denied this call"
            if approval.message:
                message += f": {approval.message}"
            return json.dumps({"behavior": "deny", "message": message})
        return json.dumps({"behavior": "deny", "message": approval.message or "The call was cancelled"})

    return server
