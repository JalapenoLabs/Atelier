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
