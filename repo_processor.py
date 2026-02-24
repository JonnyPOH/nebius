"""repo_processor.py — file prioritisation, context budget management, and LLM context assembly."""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass, field

from github_fetcher import RepoData, fetch_file_contents

logger = logging.getLogger(__name__)

TOTAL_CHAR_BUDGET = int(os.getenv("CONTEXT_CHAR_BUDGET", 80_000))
_FILE_CAP = 15_000
_TREE_CAP = 6_000
_MAX_BLOB_BYTES = int(os.getenv("MAX_BLOB_BYTES", 200_000))
_MAX_SOURCE_FILES = int(os.getenv("MAX_SOURCE_FILES", 6))
_SOURCE_TIER_THRESHOLD = 5

_PRIORITY_RULES: list[tuple[str, int, int | None]] = [
    ("README*",                 0,  20_000),
    ("readme*",                 0,  20_000),
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
    ("Dockerfile",              2,  4_000),
    ("Dockerfile.*",            2,  4_000),
    ("docker-compose*.yml",     2,  4_000),
    ("docker-compose*.yaml",    2,  4_000),
    ("Makefile",                2,  4_000),
    (".env.example",            3,  2_000),
    (".env.sample",             3,  2_000),
    (".github/workflows/*.yml", 3,  3_000),
    ("*.toml",                  3,  4_000),
    ("*.yaml",                  3,  4_000),
    ("*.yml",                   3,  4_000),
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

_EXCLUDED_DIRS = (
    "node_modules/", "vendor/", "third_party/", "third-party/",
    "extern/", "externals/", "bower_components/",
    ".git/", ".svn/", ".hg/",
    "dist/", "build/", "out/", "target/", ".next/", ".nuxt/",
    ".output/", "storybook-static/", "public/build/",
    ".venv/", "venv/", "env/", "__pycache__/", ".pytest_cache/",
    ".mypy_cache/", ".ruff_cache/", ".tox/", "site-packages/",
    "htmlcov/", ".nyc_output/", "coverage/",
    "__snapshots__/", "__mocks__/", "testdata/", "test_data/",
    "fixtures/data/", "spec/fixtures/",
    ".idea/", ".vscode/", ".eggs/", "*.egg-info/",
)

_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff",
    ".svg", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".zst",
    ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".wasm",
    ".pyc", ".pyo", ".class", ".jar", ".war", ".ear",
    ".db", ".sqlite", ".sqlite3",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".webm", ".ogg",
    ".min.js", ".min.css", ".map",
    ".parquet", ".pickle", ".pkl", ".npy", ".npz", ".h5", ".hdf5", ".proto",
})

_GENERATED_PATTERNS = (
    "*.snap", "*.snapshot",
    "*.pb.go", "*_grpc.pb.go", "*_pb2.py", "*_pb2_grpc.py", "*.pb.ts", "*.pb.js",
    "*.generated.ts", "*.generated.js", "*_generated.go", "*generated*.go",
    "**/migrations/[0-9]*.py",
    "*.bundle.js", "*.chunk.js",
)


@dataclass(order=True)
class _ScoredFile:
    score: int
    depth: int
    path: str = field(compare=False)
    char_cap: int = field(compare=False)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _is_excluded(path: str) -> bool:
    return any(path.startswith(p) or f"/{p}" in path for p in _EXCLUDED_DIRS)


def _is_binary(path: str) -> bool:
    return any(path.lower().endswith(ext) for ext in _BINARY_EXTENSIONS)


def _is_generated(path: str) -> bool:
    basename = _basename(path)
    return any(
        fnmatch.fnmatch(path, p) or fnmatch.fnmatch(basename, p)
        for p in _GENERATED_PATTERNS
    )


def _score_file(path: str) -> tuple[int, int]:
    basename = _basename(path)
    best_score, best_cap = 99, _FILE_CAP
    for pattern, score, cap in _PRIORITY_RULES:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern):
            if score < best_score:
                best_score = score
                best_cap = cap if cap is not None else _FILE_CAP
    return best_score, best_cap


def _select_files(blobs: list[dict]) -> list[_ScoredFile]:
    guaranteed, source_candidates = [], []
    for entry in blobs:
        path, size = entry["path"], entry.get("size", 0)
        if _is_excluded(path) or _is_binary(path) or _is_generated(path):
            continue
        if size == 0 or size > _MAX_BLOB_BYTES:
            continue
        score, cap = _score_file(path)
        if cap == 0:
            continue
        sf = _ScoredFile(score=score, depth=path.count("/"), path=path, char_cap=cap)
        (guaranteed if score < _SOURCE_TIER_THRESHOLD else source_candidates).append(sf)

    return sorted(guaranteed) + sorted(source_candidates)[:_MAX_SOURCE_FILES]


def _render_tree(tree: list[dict]) -> str:
    lines = ["<directory_tree>"]
    for entry in tree:
        if entry["type"] == "blob" and not _is_excluded(entry["path"]) and not _is_binary(entry["path"]):
            depth = entry["path"].count("/")
            lines.append("  " * depth + _basename(entry["path"]))
    lines.append("</directory_tree>")
    result = "\n".join(lines)
    return result if len(result) <= _TREE_CAP else result[:_TREE_CAP] + "\n… (tree truncated)"


def _truncate(content: str, cap: int, path: str) -> str:
    if len(content) <= cap:
        return content
    return content[:cap] + f"\n… (truncated at {cap} chars: {path})"


def build_context(repo_data: RepoData) -> str:
    owner, repo, ref, token, tree = (
        repo_data["owner"], repo_data["repo"], repo_data["ref"],
        repo_data["token"], repo_data["tree"],
    )

    blobs = [e for e in tree if e["type"] == "blob"]
    selected = _select_files(blobs)
    logger.info("selected %d files for context", len(selected))

    raw_contents = fetch_file_contents(owner, repo, [f.path for f in selected], ref, token)

    header_parts = [f"Repository: {owner}/{repo}", f"Default branch: {repo_data['branch']}"]
    for key, label in [("description", "Description"), ("language", "Primary language")]:
        if repo_data.get(key):
            header_parts.append(f"{label}: {repo_data[key]}")
    if repo_data.get("topics"):
        header_parts.append(f"Topics: {', '.join(repo_data['topics'])}")

    sections = [
        "<repo_info>\n" + "\n".join(header_parts) + "\n</repo_info>",
        _render_tree(tree),
    ]
    chars_used = sum(len(s) for s in sections)

    for scored in selected:
        if chars_used >= TOTAL_CHAR_BUDGET:
            break
        content = raw_contents.get(scored.path)
        if not content:
            continue
        remaining = TOTAL_CHAR_BUDGET - chars_used
        if remaining <= 0:
            break
        body = _truncate(content, min(scored.char_cap, remaining), scored.path)
        block = f"<file path=\"{scored.path}\">\n{body}\n</file>"
        sections.append(block)
        chars_used += len(block)

    return "\n\n".join(sections)
