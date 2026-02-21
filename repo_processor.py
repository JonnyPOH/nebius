"""
repo_processor.py
-----------------
Responsible for:
  1. Scoring / prioritising files in the repository tree.
  2. Selecting the most informative files within a token budget.
  3. Fetching the content of selected files via github_fetcher.
  4. Building a single context string ready to hand to the LLM.

Public surface used by main.py:
  build_context(repo_data: RepoData) -> str
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field

from github_fetcher import RepoData, fetch_file_contents

# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

# Conservative character budget — leaves headroom for the LLM prompt itself.
# Roughly 80 k chars ≈ 20 k tokens at ~4 chars/token.
TOTAL_CHAR_BUDGET: int = int(os.getenv("CONTEXT_CHAR_BUDGET", 80_000))

# Per-file caps so no single file consumes the whole budget.
_FILE_CAP: int = 15_000   # truncate any file beyond this
_TREE_CAP: int = 6_000    # max chars for the rendered directory tree

# Files larger than this (bytes) in the GitHub tree are skipped entirely —
# they are almost always generated, vendored, or binary-adjacent.
_MAX_BLOB_BYTES: int = int(os.getenv("MAX_BLOB_BYTES", 200_000))

# ---------------------------------------------------------------------------
# File-selection rules
# ---------------------------------------------------------------------------
# Each entry is (glob_pattern, priority_score, per_file_char_cap | None).
# Lower score = higher priority. Files not matching any rule get score 99.
# Git-ignored / vendor dirs are filtered out before scoring.

_PRIORITY_RULES: list[tuple[str, int, int | None]] = [
    # ── Tier 0: project overview (GUARANTEED) ───────────────────────────────
    ("README*",                 0,  20_000),
    ("readme*",                 0,  20_000),

    # ── Tier 1: dependency / build manifests (GUARANTEED) ───────────────────
    ("pyproject.toml",          1,  5_000),
    ("setup.py",                1,  5_000),
    ("setup.cfg",               1,  4_000),
    ("requirements*.txt",       1,  3_000),
    ("package.json",            1,  5_000),
    ("go.mod",                  1,  3_000),
    ("Cargo.toml",              1,  5_000),
    ("Gemfile",                 1,  3_000),
    ("pom.xml",                 1,  5_000),
    ("build.gradle*",           1,  5_000),
    ("*.csproj",                1,  5_000),

    # ── Tier 2: container / build tooling (GUARANTEED) ──────────────────────
    ("Dockerfile",              2,  4_000),
    ("Dockerfile.*",            2,  4_000),
    ("docker-compose*.yml",     2,  4_000),
    ("docker-compose*.yaml",    2,  4_000),
    ("Makefile",                2,  4_000),

    # ── Tier 3: top-level config & CI (GUARANTEED) ──────────────────────────
    (".env.example",            3,  2_000),
    (".env.sample",             3,  2_000),
    (".github/workflows/*.yml", 3,  3_000),
    ("*.toml",                  3,  4_000),   # catches pyproject.toml fallback and others
    ("*.yaml",                  3,  4_000),
    ("*.yml",                   3,  4_000),

    # ── Tier 5: source files (capped at _MAX_SOURCE_FILES total) ────────────
    # Depth tiebreaker inside _select_files means top-level files win.
    ("*.py",                    5, 10_000),
    ("*.ts",                    5, 10_000),
    ("*.tsx",                   5, 10_000),
    ("*.js",                    5, 10_000),
    ("*.go",                    5, 10_000),
    ("*.rs",                    5, 10_000),
    ("*.java",                  5, 10_000),
    ("*.rb",                    5, 10_000),
    ("*.cs",                    5, 10_000),
    ("*.cpp",                   5, 10_000),
    ("*.c",                     5, 10_000),
    ("*.kt",                    5, 10_000),
    ("*.swift",                 5, 10_000),

    # ── Tier 90: lock files / generated noise — excluded entirely ───────────
    ("package-lock.json",       90, 0),
    ("yarn.lock",               90, 0),
    ("Cargo.lock",              90, 0),
    ("Pipfile.lock",            90, 0),
    ("poetry.lock",             90, 0),
    ("go.sum",                  90, 0),
    ("*.lock",                  90, 0),
    ("*.min.js",                90, 0),
    ("*.min.css",               90, 0),
]

# Directories / path prefixes to exclude entirely.
_EXCLUDED_DIRS: tuple[str, ...] = (
    # ── package managers / vendored deps ──────────────────────────────────
    "node_modules/",
    "vendor/",
    "third_party/",
    "third-party/",
    "extern/",
    "externals/",
    "bower_components/",
    # ── VCS ────────────────────────────────────────────────────────────────
    ".git/",
    ".svn/",
    ".hg/",
    # ── build / dist output ────────────────────────────────────────────────
    "dist/",
    "build/",
    "out/",
    "target/",           # Rust / Maven
    ".next/",
    ".nuxt/",
    ".output/",
    "storybook-static/",
    "public/build/",
    # ── Python env / cache ─────────────────────────────────────────────────
    ".venv/",
    "venv/",
    "env/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    "site-packages/",
    "htmlcov/",          # coverage HTML report
    ".nyc_output/",      # JS coverage
    "coverage/",
    # ── test snapshots & fixtures ──────────────────────────────────────────
    "__snapshots__/",
    "__mocks__/",
    "testdata/",
    "test_data/",
    "fixtures/data/",
    "spec/fixtures/",
    # ── misc generated / IDE ───────────────────────────────────────────────
    ".idea/",
    ".vscode/",
    ".eggs/",
    "*.egg-info/",
)

# File extensions that are definitively binary / non-informative.
_BINARY_EXTENSIONS: frozenset[str] = frozenset({
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff",
    # vector / fonts
    ".svg", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    # documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    # archives
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".zst",
    # compiled / native
    ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".wasm",
    # JVM
    ".pyc", ".pyo", ".class", ".jar", ".war", ".ear",
    # databases
    ".db", ".sqlite", ".sqlite3",
    # media
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".webm", ".ogg",
    # generated web artifacts
    ".min.js", ".min.css", ".map",
    # data dumps
    ".parquet", ".pickle", ".pkl", ".npy", ".npz", ".h5", ".hdf5",
    ".proto",  # protobuf definitions are text but rarely explain the project
})

# Glob patterns for generated / snapshot files excluded regardless of extension.
# Matched against the full path using fnmatch.
_GENERATED_PATTERNS: tuple[str, ...] = (
    # test snapshots
    "*.snap",
    "*.snapshot",
    # protobuf / gRPC generated
    "*.pb.go",
    "*_grpc.pb.go",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.pb.ts",
    "*.pb.js",
    # OpenAPI / GraphQL generated
    "*.generated.ts",
    "*.generated.js",
    "*_generated.go",
    "*generated*.go",
    # migration auto-generated
    "**/migrations/[0-9]*.py",
    # bundler output left in source
    "*.bundle.js",
    "*.chunk.js",
)

# Max source files included across ALL source extensions combined (tier 5+).
# Keeps the payload focused: 3 files minimum, 8 files maximum.
_MAX_SOURCE_FILES: int = int(os.getenv("MAX_SOURCE_FILES", 6))

# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

@dataclass(order=True)
class _ScoredFile:
    score: int
    depth: int           # number of '/' in path — shallower files sort first within a tier
    path: str = field(compare=False)
    char_cap: int = field(compare=False)


def _is_excluded(path: str) -> bool:
    """Return True if the file lives under an excluded directory."""
    for prefix in _EXCLUDED_DIRS:
        if path.startswith(prefix) or f"/{prefix}" in path:
            return True
    return False


def _is_binary(path: str) -> bool:
    """Return True if the file extension is in the binary exclusion list."""
    lower = path.lower()
    for ext in _BINARY_EXTENSIONS:
        if lower.endswith(ext):
            return True
    return False


def _is_generated(path: str) -> bool:
    """Return True if the file matches a known generated-file pattern."""
    basename = _basename(path)
    for pattern in _GENERATED_PATTERNS:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern):
            return True
    return False


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _score_file(path: str) -> tuple[int, int]:
    """
    Return (priority_score, char_cap) for a file path.
    Matches against _PRIORITY_RULES using fnmatch on the basename first,
    then the full path for glob rules that include directories.
    """
    basename = _basename(path)
    best_score = 99
    best_cap = _FILE_CAP

    for pattern, score, cap in _PRIORITY_RULES:
        # Match on full path (for .github/workflows/*.yml etc.) and basename
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern):
            if score < best_score:
                best_score = score
                best_cap = cap if cap is not None else _FILE_CAP

    return best_score, best_cap


# Score threshold that separates "guaranteed" files (always included if present)
# from "source" files (subject to _MAX_SOURCE_FILES cap).
_SOURCE_TIER_THRESHOLD: int = 5


def _select_files(tree_blobs: list[dict]) -> list[_ScoredFile]:
    """
    Score all blobs and return a priority-sorted list.

    Guaranteed files (tiers 0–3): all included if present.
    Source files (tier 5+): prefer shallower paths; cap at _MAX_SOURCE_FILES total.
    """
    guaranteed: list[_ScoredFile] = []
    source_candidates: list[_ScoredFile] = []

    for entry in tree_blobs:
        path: str = entry["path"]
        size: int = entry.get("size", 0)

        if _is_excluded(path) or _is_binary(path) or _is_generated(path):
            continue
        if size == 0 or size > _MAX_BLOB_BYTES:
            continue

        score, cap = _score_file(path)

        if cap == 0:
            continue  # lock files / generated noise

        depth = path.count("/")
        sf = _ScoredFile(score=score, depth=depth, path=path, char_cap=cap)

        if score < _SOURCE_TIER_THRESHOLD:
            guaranteed.append(sf)
        else:
            source_candidates.append(sf)

    # Sort guaranteed files by (score, depth) — important manifests first, shallower first
    guaranteed.sort()

    # Sort source candidates by (score, depth) and take the top _MAX_SOURCE_FILES.
    # Depth tiebreaker means top-level files (main.py, app.py, index.ts) beat nested ones.
    source_candidates.sort()
    selected_sources = source_candidates[:_MAX_SOURCE_FILES]

    return guaranteed + selected_sources


# ---------------------------------------------------------------------------
# Tree renderer
# ---------------------------------------------------------------------------

def _render_tree(tree: list[dict]) -> str:
    """
    Render the file tree as an indented ASCII listing, capped at _TREE_CAP chars.
    Only shows blobs (files), using their paths to imply directory structure.
    """
    lines: list[str] = ["<directory_tree>"]
    for entry in tree:
        if entry["type"] == "blob" and not _is_excluded(entry["path"]) and not _is_binary(entry["path"]):
            depth = entry["path"].count("/")
            indent = "  " * depth
            lines.append(f"{indent}{_basename(entry['path'])}")

    lines.append("</directory_tree>")
    result = "\n".join(lines)
    if len(result) > _TREE_CAP:
        result = result[:_TREE_CAP] + "\n… (tree truncated)"
    return result


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def _truncate(content: str, cap: int, path: str) -> str:
    if len(content) <= cap:
        return content
    return content[:cap] + f"\n… (file truncated at {cap} chars: {path})"


def build_context(repo_data: RepoData) -> str:
    """
    Select important files, fetch their contents, and assemble a single
    context string for the LLM.

    Called by main.py with the RepoData returned by fetch_repo().
    """
    owner  = repo_data["owner"]
    repo   = repo_data["repo"]
    ref    = repo_data["ref"]
    token  = repo_data["token"]
    tree   = repo_data["tree"]

    blobs = [e for e in tree if e["type"] == "blob"]
    selected = _select_files(blobs)

    # Fetch all selected files in one batch
    paths_to_fetch = [f.path for f in selected]
    raw_contents = fetch_file_contents(owner, repo, paths_to_fetch, ref, token)

    # Build context sections respecting total budget
    sections: list[str] = []
    chars_used = 0

    # ── 1. Repo header ───────────────────────────────────────────────────────
    header_parts = [
        f"Repository: {owner}/{repo}",
        f"Default branch: {repo_data['branch']}",
    ]
    if repo_data.get("description"):
        header_parts.append(f"Description: {repo_data['description']}")
    if repo_data.get("language"):
        header_parts.append(f"Primary language: {repo_data['language']}")
    if repo_data.get("topics"):
        header_parts.append(f"Topics: {', '.join(repo_data['topics'])}")

    header = "<repo_info>\n" + "\n".join(header_parts) + "\n</repo_info>"
    sections.append(header)
    chars_used += len(header)

    # ── 2. Directory tree ────────────────────────────────────────────────────
    tree_str = _render_tree(tree)
    sections.append(tree_str)
    chars_used += len(tree_str)

    # ── 3. File contents in priority order ───────────────────────────────────
    for scored in selected:
        if chars_used >= TOTAL_CHAR_BUDGET:
            break

        content = raw_contents.get(scored.path)
        if not content:
            continue

        remaining = TOTAL_CHAR_BUDGET - chars_used
        effective_cap = min(scored.char_cap, remaining)
        if effective_cap <= 0:
            break

        body = _truncate(content, effective_cap, scored.path)
        block = f"<file path=\"{scored.path}\">\n{body}\n</file>"
        sections.append(block)
        chars_used += len(block)

    return "\n\n".join(sections)
