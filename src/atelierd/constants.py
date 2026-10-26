"""Names, defaults and file conventions shared across the daemon core.

The defaults here are the design of record in `docs/catalog.md` and `docs/architecture.md`; change them together.
"""

from __future__ import annotations

# ========================================================================================
# Project layout
# ========================================================================================

CONFIG_FILE_NAME = "atelier.toml"
STATE_DIR_NAME = ".atelier"
STATE_DB_NAME = "state.db"

# The ignore entry added to the project's version control, and the files it may go in, most preferred first.
STATE_IGNORE_ENTRY = f"{STATE_DIR_NAME}/"
DIVERSION_IGNORE_FILE = ".dvignore"
GIT_IGNORE_FILE = ".gitignore"

# ========================================================================================
# atelier.toml defaults
# ========================================================================================

DEFAULT_ROOTS = ("SourceArt",)
DEFAULT_SHARED = ("SourceArt/_lib/**", "SourceArt/**/_scripts/*.py", "SourceArt/Palette/**")
DEFAULT_REFERENCE_GLOBS = ("SourceArt/References/**", "SourceArt/Concepts/**", "References/**", "docs/reference/**")
DEFAULT_REFERENCE_DROP_FOLDER = "SourceArt/References"

# ========================================================================================
# Discovery conventions
# ========================================================================================

SOURCE_SUFFIX = ".blend"
EXPORT_SUFFIX = ".fbx"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".exr"})
README_FILE_NAME = "README.md"
BUILDER_PREFIX = "build_"

# Folders beside a .blend whose whole tree belongs to it.
OUTPUT_FOLDER_NAMES = ("Export", "Exports", "Review", "Renders")
# Of those, the folders whose images are renders rather than textures or masks.
RENDER_FOLDER_NAMES = frozenset({"Review", "Renders"})
# Image names that are renders wherever they sit in a source's files.
RENDER_NAME_GLOBS = ("*_preview*", "*_contact_sheet*")

# Folders never walked into: private tooling (`_lib`, `_scripts`), and through the same prefix `__pycache__`.
PRIVATE_FOLDER_PREFIX = "_"

# ========================================================================================
# Machine registry
# ========================================================================================

REGISTRY_DIR_NAME = "atelier"
REGISTRY_FILE_NAME = "projects.toml"

# ========================================================================================
# File watcher
# ========================================================================================

# Quiet period a batch of changes must settle for before the catalog rescans. Blender saves through a temporary
# file and a rename, and builders write many exports in a burst, so this has to cover a whole save or build step.
WATCH_SETTLE_MS = 1500
# Upper bound on how long a continuous stream of changes is grouped before the catalog rescans anyway.
WATCH_MAX_BATCH_MS = 30_000

# ========================================================================================
# Exports and their converted meshes
# ========================================================================================

# Unreal's convention for collision hulls exported beside a mesh. The viewer hides them unless asked.
COLLISION_PREFIX = "UCX_"

# `<state>/cache/` holds every export converted to `.glb` with its stats, named by the FBX's sha256.
CACHE_DIR_NAME = "cache"

# The Blender command run headless; `ATELIER_BLENDER` overrides it with a path or another command name.
BLENDER_COMMAND = "blender"
BLENDER_COMMAND_ENV = "ATELIER_BLENDER"
# Headless Blender runs at this niceness, so a conversion never competes with the window or an open editor.
BLENDER_NICENESS = 10
# A conversion still running after this long is treated as hung and killed.
BLENDER_TIMEOUT_S = 300

# ========================================================================================
# Revisions
# ========================================================================================

# Standard renders running at once, each a whole headless Blender (itself held to a few threads, at low priority).
STANDARD_RENDER_WORKERS = 2
# Revisions per source `atelier prune` keeps unless told otherwise.
DEFAULT_PRUNE_KEEP = 20

# ========================================================================================
# The daemon and its front doors
# ========================================================================================

# The GUI's JSON-RPC socket, inside `$XDG_RUNTIME_DIR`; `ATELIER_SOCKET` (or `--socket`) puts it elsewhere.
SOCKET_FILE_NAME = "atelier.sock"
SOCKET_PATH_ENV = "ATELIER_SOCKET"
# The HTTP front door on 127.0.0.1: the worker-side MCP each worker calls, and the manager MCP.
DEFAULT_HTTP_PORT = 47817
# The systemd user unit `atelierd install` writes; the GUI starts the daemon through it when it is installed.
SYSTEMD_UNIT_NAME = "atelierd.service"
# Where a daemon the GUI starts by itself (no unit installed) writes its log, inside `$XDG_STATE_HOME`.
DAEMON_LOG_FILE_NAME = "atelierd.log"

# ========================================================================================
# Workers
# ========================================================================================

# The Claude Code CLI a worker runs, and the Blender MCP client its `blender` server runs; both can be overridden
# with a path, which is also how the tests substitute stubs.
CLAUDE_COMMAND = "claude"
CLAUDE_COMMAND_ENV = "ATELIER_CLAUDE"
BLENDER_MCP_COMMAND = "blender-mcp"
BLENDER_MCP_COMMAND_ENV = "ATELIER_BLENDER_MCP"
# A worker's background Blender renders on this many threads, so several workers never take the whole machine.
WORKER_BLENDER_THREADS = 4
# How long a worker waits for its background Blender to start listening before the turn fails.
WORKER_BLENDER_START_TIMEOUT_S = 60
# How long a stopping worker's processes get to exit on their own before they are killed.
WORKER_STOP_TIMEOUT_S = 10
# The worker-side MCP server's name in each worker's `--mcp-config`, and so the prefix of its tools.
WORKER_MCP_SERVER_NAME = "atelier"
WORKER_BLENDER_SERVER_NAME = "blender"
WORKER_APPROVAL_TOOL = "approve"
# The header carrying a worker's secret token on every call it makes to the worker-side MCP.
WORKER_TOKEN_HEADER = "X-Atelier-Worker"
# The Unreal content tree; anything under it escalates (see docs/workers.md).
UNREAL_CONTENT_FOLDER = "Content"

# Folders inside a project's state folder, beside `objects/`, `renders/` and `cache/`.
WORKERS_DIR_NAME = "workers"
TRANSCRIPTS_DIR_NAME = "transcripts"
FEEDBACK_DIR_NAME = "feedback"
