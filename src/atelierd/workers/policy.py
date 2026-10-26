"""The scope policy: which of a worker's tool calls run without asking, and which are escalated to the reviewer.

One function, `evaluate`, decides for both places Claude Code consults Atelier: the `PreToolUse` hook (`hook.py`),
which sees every call, and the permission-prompt tool (`mcp.py`), which sees what Claude Code would otherwise ask
about. They can therefore never disagree. The rules are the table in `docs/workers.md`:

- Reads anywhere in the project run, and so do reads in its state folder (where feedback and transcripts live) and in
  Claude Code's own configuration folder (`~/.claude`, which the user's own instructions send every agent to read).
- Writes under the source's own folder, and to its discovered files wherever they are, run.
- Its own Blender MCP, Atelier's worker tools and `blender -b` runs run.
- Everything else escalates: writes outside the scope, writes to `shared` paths (even inside the source's folder),
  anything under the Unreal `Content/` tree, network access, and commands the policy cannot read.

A shell command is parsed (with `bashlex`), never matched as text: a pipeline of reads, a `blender -b` rebuild, and
`mkdir`/`cp`/`mv`/`rm`/`touch` or a `>` redirect inside the scope run; anything it cannot account for escalates. When
in doubt the policy escalates, because asking costs a click and a wrong allow can cost the reviewer's work.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath
from typing import Any

import bashlex
from pydantic import BaseModel, ConfigDict, Field
from wcmatch import glob

from atelierd.catalog.discovery import GLOB_FLAGS
from atelierd.catalog.models import Source
from atelierd.config import load_config
from atelierd.constants import UNREAL_CONTENT_FOLDER, WORKER_BLENDER_SERVER_NAME, WORKER_MCP_SERVER_NAME
from atelierd.project import Project

logger = logging.getLogger(__name__)

# Claude Code's own tools that neither touch files nor reach the network themselves. A subagent's calls are each
# checked again by the hook, so starting one is harmless too.
_HARMLESS_TOOLS = frozenset(
    {
        "Agent",
        "BashOutput",
        "EnterPlanMode",
        "ExitPlanMode",
        "KillBash",
        "KillShell",
        "ListMcpResourcesTool",
        "ReadMcpResourceTool",
        "Skill",
        "SlashCommand",
        "Task",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
        "TodoWrite",
        "ToolSearch",
    }
)
_NETWORK_TOOLS = frozenset({"WebFetch", "WebSearch"})

# Each read tool's argument naming the file or folder it reads.
_READ_PATH_ARGUMENT_BY_TOOL = {
    "Read": "file_path",
    "NotebookRead": "notebook_path",
    "Glob": "path",
    "Grep": "path",
    "LS": "path",
}
# Each write tool's argument naming the file it writes.
_WRITE_PATH_ARGUMENT_BY_TOOL = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

# Shell programs that only read: their path arguments must be readable.
_READ_ONLY_PROGRAMS = frozenset(
    {
        "basename",
        "cat",
        "cd",
        "diff",
        "dirname",
        "du",
        "echo",
        "false",
        "file",
        "find",
        "grep",
        "head",
        "identify",
        "jq",
        "ls",
        "md5sum",
        "paste",
        "printf",
        "pwd",
        "realpath",
        "rg",
        "sha256sum",
        "sleep",
        "sort",
        "stat",
        "tail",
        "test",
        "tr",
        "tree",
        "true",
        "uniq",
        "wc",
        "which",
    }
)
# `find` actions that write, delete or run something.
_FIND_WRITE_ACTIONS = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
)
# Shell programs that write: every path argument must be inside the write scope.
_WRITE_PROGRAMS = frozenset({"cp", "mkdir", "mv", "rm", "rmdir", "touch"})
# Shell programs that reach the network.
_NETWORK_PROGRAMS = frozenset(
    {"curl", "ftp", "git", "nc", "ncat", "npm", "npx", "pip", "pip3", "rsync", "scp", "sftp", "ssh", "telnet", "wget"}
)
_BLENDER_PROGRAM = "blender"
_BLENDER_BACKGROUND_FLAGS = frozenset({"-b", "--background"})
# Redirect targets that write nothing.
_NULL_DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})


class ScopePolicy(BaseModel):
    """What one worker may do without asking: written once per worker as `policy.json` for its hook to read."""

    model_config = ConfigDict(frozen=True)

    project_root: Path = Field(description="Absolute and resolved; also the worker's working directory.")
    state_dir: Path = Field(description="Atelier's state folder, readable for feedback and transcripts.")
    read_folders: tuple[Path, ...] = Field(
        default=(),
        description="Absolute folders outside the project that are read freely: Claude Code's configuration folder.",
    )
    source: str
    write_folders: tuple[str, ...] = Field(description="Project-relative folders the worker writes under freely.")
    write_files: tuple[str, ...] = Field(
        default=(),
        description="Project-relative files the worker writes freely wherever they are: the source's own outputs.",
    )
    shared_globs: tuple[str, ...] = Field(default=(), description="`shared` from atelier.toml; writes escalate.")
    shared_files: tuple[str, ...] = Field(
        default=(),
        description="Files shared though no `shared` glob names them: a builder outside the source's folder.",
    )


class PolicyDecision(BaseModel):
    """Whether a tool call runs without asking, and why."""

    model_config = ConfigDict(frozen=True)

    is_allowed: bool
    reason: str


def build_scope_policy(project: Project, source: Source) -> ScopePolicy:
    """Return the scope of a worker on `source`: its folder and its own files, with the project's shared paths.

    A builder outside the source's folder (a README's rebuild command running a project-wide script) is shared, the
    same rule restore follows.
    """
    config = load_config(project.config_path)
    folder = PurePosixPath(source.path).parent
    write_files = [source.path, *source.exports, *source.renders]
    if source.readme:
        write_files.append(source.readme)
    shared_files: list[str] = []
    if source.builder and PurePosixPath(source.builder).parent == folder:
        write_files.append(source.builder)
    elif source.builder:
        shared_files.append(source.builder)
    claude_config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").resolve()
    return ScopePolicy(
        project_root=project.root,
        state_dir=project.state_dir,
        read_folders=(claude_config_dir,),
        source=source.path,
        write_folders=(folder.as_posix(),),
        write_files=tuple(write_files),
        shared_globs=config.shared,
        shared_files=tuple(shared_files),
    )


def evaluate(policy: ScopePolicy, tool_name: str, tool_input: dict[str, Any]) -> PolicyDecision:
    """Decide one tool call."""
    if tool_name.startswith("mcp__"):
        server_name = tool_name.split("__")[1]
        if server_name in (WORKER_BLENDER_SERVER_NAME, WORKER_MCP_SERVER_NAME):
            return PolicyDecision(is_allowed=True, reason=f"Its own {server_name} MCP server")
        return PolicyDecision(is_allowed=False, reason=f"An MCP server outside the worker's own ({server_name})")

    if tool_name in _HARMLESS_TOOLS:
        return PolicyDecision(is_allowed=True, reason="Touches no files")
    if tool_name in _NETWORK_TOOLS:
        return PolicyDecision(is_allowed=False, reason="Network access")

    if tool_name in _READ_PATH_ARGUMENT_BY_TOOL:
        # Glob, Grep and LS default to the working directory, the project root.
        target = str(tool_input.get(_READ_PATH_ARGUMENT_BY_TOOL[tool_name]) or ".")
        decision = _check_read(policy, target)
        pattern = str(tool_input.get("pattern") or "")
        if decision.is_allowed and tool_name == "Glob" and pattern.startswith("/"):
            decision = _check_read(policy, _static_prefix(pattern))
        return decision

    if tool_name in _WRITE_PATH_ARGUMENT_BY_TOOL:
        target = str(tool_input.get(_WRITE_PATH_ARGUMENT_BY_TOOL[tool_name]) or "")
        if not target:
            return PolicyDecision(is_allowed=False, reason=f"{tool_name} without a file path")
        return _check_write(policy, target)

    if tool_name == "Bash":
        return _check_command(policy, str(tool_input.get("command") or ""))

    return PolicyDecision(is_allowed=False, reason=f"{tool_name} is not a tool the scope policy knows")


# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------


def _resolve(policy: ScopePolicy, target: str) -> Path:
    """Resolve a path the way the worker's tools do: relative to the project root, `~` expanded, symlinks followed."""
    return (policy.project_root / os.path.expanduser(target)).resolve()


def _project_relative(policy: ScopePolicy, path: Path) -> str | None:
    """The path relative to the project root, or None when it lies outside the project."""
    if not path.is_relative_to(policy.project_root):
        return None
    return path.relative_to(policy.project_root).as_posix()


def _is_under_content(relative_path: str) -> bool:
    return PurePosixPath(relative_path).parts[:1] == (UNREAL_CONTENT_FOLDER,)


def _check_read(policy: ScopePolicy, target: str) -> PolicyDecision:
    path = _resolve(policy, target)
    if path.is_relative_to(policy.state_dir):
        return PolicyDecision(is_allowed=True, reason="Reads Atelier's state (feedback, transcripts)")
    if any(path.is_relative_to(folder) for folder in policy.read_folders):
        return PolicyDecision(is_allowed=True, reason="Reads Claude Code's own configuration")
    relative_path = _project_relative(policy, path)
    if relative_path is None:
        return PolicyDecision(is_allowed=False, reason=f"Reads outside the project: {path}")
    if _is_under_content(relative_path):
        return PolicyDecision(is_allowed=False, reason=f"Touches the Unreal Content/ tree: {relative_path}")
    return PolicyDecision(is_allowed=True, reason="Reads inside the project")


def _check_write(policy: ScopePolicy, target: str) -> PolicyDecision:
    path = _resolve(policy, target)
    relative_path = _project_relative(policy, path)
    if relative_path is None or path.is_relative_to(policy.state_dir):
        return PolicyDecision(is_allowed=False, reason=f"Writes outside the source's scope: {path}")
    if _is_under_content(relative_path):
        return PolicyDecision(is_allowed=False, reason=f"Writes into the Unreal Content/ tree: {relative_path}")
    is_shared = relative_path in policy.shared_files or glob.globmatch(
        relative_path, list(policy.shared_globs), flags=GLOB_FLAGS
    )
    if is_shared:
        return PolicyDecision(
            is_allowed=False, reason=f"Writes a shared path other sources build from: {relative_path}"
        )

    relative = PurePosixPath(relative_path)
    is_in_folder = any(relative.is_relative_to(folder) for folder in policy.write_folders)
    if is_in_folder or relative_path in policy.write_files:
        return PolicyDecision(is_allowed=True, reason="Writes inside the source's scope")
    return PolicyDecision(is_allowed=False, reason=f"Writes outside the source's scope: {relative_path}")


def _static_prefix(pattern: str) -> str:
    """The leading folders of a glob that hold no pattern characters."""
    static_parts: list[str] = []
    for part in PurePosixPath(pattern).parts:
        if glob.is_magic(part, flags=GLOB_FLAGS):
            break
        static_parts.append(part)
    return "/".join(static_parts) or "/"


# -----------------------------------------------------------------------------
# Shell commands
# -----------------------------------------------------------------------------


def _check_command(policy: ScopePolicy, command: str) -> PolicyDecision:
    """Allow a command only when every simple command and redirect in it is accounted for."""
    if not command.strip():
        return PolicyDecision(is_allowed=False, reason="An empty shell command")
    try:
        trees = bashlex.parse(command)
    except (bashlex.errors.ParsingError, NotImplementedError, IndexError, TypeError) as error:
        logger.debug(f"[Policy] Could not parse the command, escalating it: {error}")
        return PolicyDecision(is_allowed=False, reason="A shell command the scope policy cannot read")

    collector = _CommandCollector()
    for tree in trees:
        collector.visit(tree)
    if collector.unsupported:
        return PolicyDecision(
            is_allowed=False,
            reason=f"A shell command with {collector.unsupported[0]}, which the scope policy cannot account for",
        )

    programs = {PurePosixPath(words[0]).name for words in collector.commands}
    is_redirecting = any(target not in _NULL_DEVICES for target in collector.redirect_targets)
    is_writing = is_redirecting or bool(programs & _WRITE_PROGRAMS)
    if "cd" in programs and is_writing:
        # Paths are checked against the project root, so a write after a `cd` could land somewhere unchecked.
        return PolicyDecision(is_allowed=False, reason="A shell command that changes directory and then writes")

    for words in collector.commands:
        decision = _check_simple_command(policy, words)
        if not decision.is_allowed:
            return decision
    for target in collector.redirect_targets:
        if target in _NULL_DEVICES:
            continue
        if "$" in target or "`" in target:
            return PolicyDecision(is_allowed=False, reason=f"Writes to a computed path: {target}")
        decision = _check_write(policy, target)
        if not decision.is_allowed:
            return decision
    return PolicyDecision(is_allowed=True, reason="Reads, `blender -b` runs and writes inside the source's scope")


def _check_simple_command(policy: ScopePolicy, words: list[str]) -> PolicyDecision:
    program_word = words[0]
    if "$" in program_word or "`" in program_word:
        return PolicyDecision(is_allowed=False, reason=f"Runs a computed program: {program_word}")
    program = PurePosixPath(program_word).name
    arguments = words[1:]

    if program == _BLENDER_PROGRAM:
        if _BLENDER_BACKGROUND_FLAGS.intersection(arguments):
            return PolicyDecision(is_allowed=True, reason="A `blender -b` run")
        return PolicyDecision(is_allowed=False, reason="Opens a windowed Blender; only `blender -b` runs freely")
    if program in _NETWORK_PROGRAMS:
        return PolicyDecision(is_allowed=False, reason=f"Network access ({program})")
    if program == "find" and _FIND_WRITE_ACTIONS.intersection(arguments):
        return PolicyDecision(is_allowed=False, reason="`find` that writes, deletes or runs commands")

    path_arguments = [argument for argument in arguments if not argument.startswith("-")]
    if program in _READ_ONLY_PROGRAMS:
        for argument in path_arguments:
            if not _looks_like_path(argument):
                continue
            if "$" in argument or "`" in argument:
                return PolicyDecision(is_allowed=False, reason=f"Reads a computed path: {argument}")
            decision = _check_read(policy, argument)
            if not decision.is_allowed:
                return decision
        return PolicyDecision(is_allowed=True, reason="Reads inside the project")
    if program in _WRITE_PROGRAMS:
        if not path_arguments:
            return PolicyDecision(is_allowed=False, reason=f"`{program}` with no paths")
        for argument in path_arguments:
            if "$" in argument or "`" in argument:
                return PolicyDecision(is_allowed=False, reason=f"Writes a computed path: {argument}")
            decision = _check_write(policy, argument)
            if not decision.is_allowed:
                return decision
        return PolicyDecision(is_allowed=True, reason="Writes inside the source's scope")
    return PolicyDecision(
        is_allowed=False,
        reason=f"Runs `{program}`, which is not a read, a write inside the scope or a `blender -b` run",
    )


def _looks_like_path(argument: str) -> bool:
    """Whether a read's argument names a file rather than a pattern or a count. Paths are what reads are checked on."""
    return "/" in argument or argument.startswith((".", "~"))


class _CommandCollector(bashlex.ast.nodevisitor):  # type: ignore[misc]
    """Collect every simple command's words and every output redirect's target from a parsed command line.

    Command substitutions are walked too (they are nodes inside a word), so `PATH=$(... | grep -v mise) blender -b`
    checks the `echo`, `tr` and `grep` inside it as well as the `blender` run, and so are `( ... )` and `{ ...; }`
    groups. Loops, conditionals and functions are reported as unsupported, and the call escalates.
    """

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.redirect_targets: list[str] = []
        self.unsupported: list[str] = []

    def visitcommand(self, node: Any, parts: list[Any]) -> None:
        words = [part.word for part in parts if part.kind == "word"]
        if words:
            self.commands.append(words)

    def visitredirect(self, node: Any, input: Any, redirect_type: str, output: Any, heredoc: Any) -> None:
        if redirect_type not in (">", ">>", ">|", "&>", ">&"):
            return
        # `2>&1` names a descriptor, not a file.
        if isinstance(output, int) or output is None:
            return
        target = getattr(output, "word", "")
        if target.isdigit() or target.startswith("&"):
            return
        self.redirect_targets.append(target)

    def visitif(self, node: Any, parts: Any) -> None:
        self.unsupported.append("a conditional")

    def visitfor(self, node: Any, parts: Any) -> None:
        self.unsupported.append("a loop")

    def visitwhile(self, node: Any, parts: Any) -> None:
        self.unsupported.append("a loop")

    def visituntil(self, node: Any, parts: Any) -> None:
        self.unsupported.append("a loop")

    def visitfunction(self, node: Any, name: Any, body: Any, parts: Any) -> None:
        self.unsupported.append("a function definition")

    def visitprocesssubstitution(self, node: Any, command: Any) -> None:
        self.unsupported.append("a process substitution")
