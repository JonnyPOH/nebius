'''Processing GitHub repo and building a context string for LLM input'''


#------------------ Imports ------------------#
import fnmatch
import logging
import os
from github_fetcher import fetch_file_contents


#------------------ Variables ------------------#
logger = logging.getLogger(__name__)

# hardcoded
TOTAL_CHAR_BUDGET = 80_000
_FILE_CAP = 15_000
_TREE_CAP = 6_000
_MAX_BLOB_BYTES = 200_000
_MAX_SOURCE_FILES = 6
_SOURCE_TIER_THRESHOLD = 5

_PRIORITY_RULES = [
    ("README*", 0, 20_000),
    ("readme*", 0, 20_000),
    ("pyproject.toml", 1, 5_000),
    ("setup.py", 1, 5_000),
    ("setup.cfg", 1, 4_000),
    ("requirements*.txt", 1, 3_000),
    ("package.json", 1, 5_000),
    ("go.mod", 1, 3_000),
    ("Cargo.toml", 1, 5_000),
    ("Gemfile", 1, 3_000),
    ("pom.xml", 1, 5_000),
    ("build.gradle*", 1, 5_000),
    ("*.csproj", 1, 5_000),
    ("Dockerfile", 2, 4_000),
    ("Dockerfile.*", 2, 4_000),
    ("docker-compose*.yml", 2, 4_000),
    ("docker-compose*.yaml", 2, 4_000),
    ("Makefile", 2, 4_000),
    (".env.example", 3, 2_000),
    (".env.sample", 3, 2_000),
    (".github/workflows/*.yml", 3, 3_000),
    ("*.toml", 3, 4_000),
    ("*.yaml", 3, 4_000),
    ("*.yml", 3, 4_000),
    ("*.py", 5, 10_000),
    ("*.ts", 5, 10_000),
    ("*.tsx", 5, 10_000),
    ("*.js", 5, 10_000),
    ("*.go", 5, 10_000),
    ("*.rs", 5, 10_000),
    ("*.java", 5, 10_000),
    ("*.rb", 5, 10_000),
    ("*.cs", 5, 10_000),
    ("*.cpp", 5, 10_000),
    ("*.c", 5, 10_000),
    ("*.kt", 5, 10_000),
    ("*.swift", 5, 10_000),
    ("package-lock.json", 90, 0),
    ("yarn.lock", 90, 0),
    ("Cargo.lock", 90, 0),
    ("Pipfile.lock", 90, 0),
    ("poetry.lock", 90, 0),
    ("go.sum", 90, 0),
    ("*.lock", 90, 0),
    ("*.min.js", 90, 0),
    ("*.min.css", 90, 0),
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

_BINARY_EXTENSIONS = {
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
}

_GENERATED_PATTERNS = (
    "*.snap", "*.snapshot",
    "*.pb.go", "*_grpc.pb.go", "*_pb2.py", "*_pb2_grpc.py", "*.pb.ts", "*.pb.js",
    "*.generated.ts", "*.generated.js", "*_generated.go", "*generated*.go",
    "**/migrations/[0-9]*.py",
    "*.bundle.js", "*.chunk.js",
)


#------------------ Functions ------------------#

# skip paths inside vendor/build/cache dirs
def _is_excluded(path):
    return any(path.startswith(p) or f"/{p}" in path for p in _EXCLUDED_DIRS)

# skip image, font, archive, compiled and other non-text files
def _is_binary(path):
    return any(path.lower().endswith(ext) for ext in _BINARY_EXTENSIONS)

# skip auto-generated files like protobuf outputs, snapshots, bundles
def _is_generated(path):
    basename = os.path.basename(path)
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(basename, p) for p in _GENERATED_PATTERNS)

# returns (priority_score, char_cap) — lower score means higher priority
def _score_file(path):
    basename = os.path.basename(path)
    best_score, best_cap = 99, _FILE_CAP  # 99 = unmatched, low priority
    for pattern, score, cap in _PRIORITY_RULES:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern):
            if score < best_score:
                best_score = score
                best_cap = cap
    return best_score, best_cap


# scores and splits blobs into guaranteed files and source candidates, caps source at _MAX_SOURCE_FILES
def _select_files(blobs):
    guaranteed = []      # readmes, manifests, configs — always include
    source_candidates = []  # source files — capped at _MAX_SOURCE_FILES

    for entry in blobs:
        path = entry["path"]
        size = entry.get("size", 0)

        if _is_excluded(path) or _is_binary(path) or _is_generated(path):
            continue

        # skip empty or huge files
        if size == 0 or size > _MAX_BLOB_BYTES:
            continue

        score, cap = _score_file(path)

        if cap == 0:
            continue

        row = (score, path.count("/"), path, cap)

        if score < _SOURCE_TIER_THRESHOLD:
            guaranteed.append(row)
        else:
            source_candidates.append(row)

    guaranteed.sort()
    source_candidates.sort()
    return guaranteed + source_candidates[:_MAX_SOURCE_FILES]


# builds an indented directory listing, truncated if it gets too long
def _render_tree(tree):
    lines = ["<directory_tree>"]

    for entry in tree:
        if entry["type"] != "blob":
            continue
        path = entry["path"]
        if _is_excluded(path) or _is_binary(path):
            continue
        # indent by depth so it looks like a real tree
        indent = "  " * path.count("/")
        lines.append(indent + os.path.basename(path))

    lines.append("</directory_tree>")
    result = "\n".join(lines)

    # truncate if too long
    if len(result) > _TREE_CAP:
        result = result[:_TREE_CAP] + "\n...(tree truncated)"

    return result


# main entry point — takes repo_data from github_fetcher, returns a single context string for the LLM
def build_context(repo_data):
    owner = repo_data["owner"]
    repo = repo_data["repo"]
    ref = repo_data["ref"]
    tree = repo_data["tree"]

    # filter down to files only and score/select the most useful ones
    blobs = [e for e in tree if e["type"] == "blob"]
    selected = _select_files(blobs)
    logger.info("selected %d files for context", len(selected))

    # fetch all file contents in one batch
    paths = [path for _, _, path, _ in selected]
    raw_contents = fetch_file_contents(owner, repo, paths, ref)

    # build the repo header
    header_parts = [f"Repository: {owner}/{repo}", f"Default branch: {repo_data['branch']}"]
    if repo_data.get("description"):
        header_parts.append(f"Description: {repo_data['description']}")
    if repo_data.get("language"):
        header_parts.append(f"Primary language: {repo_data['language']}")
    if repo_data.get("topics"):
        header_parts.append(f"Topics: {', '.join(repo_data['topics'])}")

    sections = [
        "<repo_info>\n" + "\n".join(header_parts) + "\n</repo_info>",
        _render_tree(tree),
    ]
    chars_used = len(sections[0]) + len(sections[1])

    # add files one by one until we hit the character budget
    for _, _, path, char_cap in selected:
        if chars_used >= TOTAL_CHAR_BUDGET:
            break
        content = raw_contents.get(path)
        if not content:
            continue
        cap = min(char_cap, TOTAL_CHAR_BUDGET - chars_used)
        body = content[:cap] + f"\n...(truncated at {cap} chars)" if len(content) > cap else content
        block = f"<file path=\"{path}\">\n{body}\n</file>"
        sections.append(block)
        chars_used += len(block)

    return "\n\n".join(sections)
