"""
github_fetcher.py
-----------------
Responsible for:
  1. Parsing and validating a GitHub repository URL.
  2. Fetching repository metadata from the GitHub REST API.
  3. Fetching the full recursive file tree.
  4. Providing a helper to fetch raw content of specific files.

Public surface used by the rest of the app:
  fetch_repo(url: str) -> RepoData
  fetch_file_contents(owner, repo, paths, ref, token) -> dict[str, str]
"""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timezone
from typing import TypedDict

import httpx


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class GitHubError(RuntimeError):
    """Base class for all GitHub fetcher errors."""


class GitHubURLError(ValueError, GitHubError):
    """Raised when the supplied URL is not a valid GitHub repository URL."""


class GitHubNotFoundError(GitHubError):
    """Raised when the repository or resource does not exist (HTTP 404)."""


class GitHubPrivateRepoError(GitHubError):
    """Raised when the token lacks permission to access a private resource (HTTP 403, not rate-limited)."""


class GitHubRateLimitError(GitHubError):
    """Raised when the GitHub API rate limit is exceeded."""
    def __init__(self, message: str, reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class GitHubNetworkError(GitHubError):
    """Raised on connection, timeout, or other network-level failures."""

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class TreeEntry(TypedDict):
    path: str
    type: str   # "blob" | "tree"
    size: int   # 0 for trees


class RepoData(TypedDict):
    owner: str
    repo: str
    branch: str          # default branch name  (e.g. "main")
    ref: str             # default branch SHA   (used for tree API calls)
    description: str | None
    language: str | None
    topics: list[str]
    tree: list[TreeEntry]
    token: str | None    # passed through so repo_processor can call fetch_file_contents


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
# Regex accepts:
#   https://github.com/owner/repo
#   https://github.com/owner/repo/
#   https://github.com/owner/repo/tree/branch
#   http variant
_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/?#]+?)(?:\.git)?(?:/.*)?$"
)
# GitHub's maximum tree size before it truncates; we warn but continue
_MAX_TREE_ENTRIES = 100_000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _headers(token: str | None) -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _parse_reset_time(headers: httpx.Headers) -> datetime | None:
    """Convert the X-RateLimit-Reset unix timestamp header to a UTC datetime."""
    raw = headers.get("X-RateLimit-Reset")
    if raw:
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc)
        except (ValueError, OSError):
            pass
    return None


def _get(client: httpx.Client, url: str, token: str | None, **kwargs) -> httpx.Response:
    """GET with comprehensive error handling for GitHub API responses."""
    try:
        resp = client.get(url, headers=_headers(token), timeout=20, **kwargs)
    except httpx.TimeoutException as exc:
        raise GitHubNetworkError(
            f"Request timed out while contacting GitHub API: {url}"
        ) from exc
    except httpx.ConnectError as exc:
        raise GitHubNetworkError(
            f"Could not connect to GitHub API. Check your network connection. ({exc})"
        ) from exc
    except httpx.RequestError as exc:
        raise GitHubNetworkError(
            f"Network error while contacting GitHub API: {exc}"
        ) from exc

    # --- 401: bad / expired token ---
    if resp.status_code == 401:
        raise GitHubPrivateRepoError(
            "GitHub API returned 401 Unauthorized. "
            "Provide a valid token via the GITHUB_TOKEN environment variable."
        )

    # --- 403: rate-limit OR insufficient permissions ---
    if resp.status_code == 403:
        remaining = resp.headers.get("X-RateLimit-Remaining", "1")
        retry_after = resp.headers.get("Retry-After")  # secondary rate limit

        if remaining == "0" or retry_after:
            reset_at = _parse_reset_time(resp.headers)
            reset_str = (
                reset_at.strftime("%Y-%m-%d %H:%M:%S UTC") if reset_at else "unknown time"
            )
            wait_hint = f" Retry after {retry_after}s." if retry_after else ""
            raise GitHubRateLimitError(
                f"GitHub API rate limit exceeded. Limit resets at {reset_str}.{wait_hint} "
                "Set GITHUB_TOKEN to increase your quota (5 000 req/hr authenticated vs 60 unauthenticated).",
                reset_at=reset_at,
            )

        # Authenticated but forbidden — private repo or org SSO enforcement
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        reason = body.get("message", "Forbidden")
        raise GitHubPrivateRepoError(
            f"Access denied (403): {reason}. "
            "The repository may be private, or your token may lack the required scopes "
            "(needs at least 'repo' scope for private repos)."
        )

    # --- 404: repo/resource missing or private (unauthenticated) ---
    if resp.status_code == 404:
        # GitHub returns 404 (not 403) for private repos hit without a token
        hint = (
            " If this is a private repository, set GITHUB_TOKEN with 'repo' scope."
            if not token
            else ""
        )
        raise GitHubNotFoundError(
            f"Repository or resource not found (404): {url}.{hint}"
        )

    # --- 422: valid request but GitHub can't process (e.g. empty repo) ---
    if resp.status_code == 422:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        reason = body.get("message", "Unprocessable Entity")
        raise GitHubError(
            f"GitHub could not process request (422): {reason}. "
            "The repository may be empty or its Git data may be unavailable."
        )

    # --- 451: legal / DMCA takedown ---
    if resp.status_code == 451:
        raise GitHubNotFoundError(
            f"Repository unavailable for legal reasons (451): {url}."
        )

    # --- everything else (5xx etc.) ---
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GitHubError(
            f"Unexpected GitHub API error {resp.status_code} for {url}: {exc}"
        ) from exc

    return resp


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_github_url(url: str) -> tuple[str, str]:
    """
    Extract (owner, repo) from a GitHub URL.

    Raises GitHubURLError for non-GitHub or malformed URLs.
    """
    m = _URL_RE.match(url.strip())
    if not m:
        raise GitHubURLError(
            f"Invalid GitHub URL: '{url}'. "
            "Expected format: https://github.com/<owner>/<repo>"
        )
    return m.group("owner"), m.group("repo")


def fetch_repo(url: str) -> RepoData:
    """
    Entry point called by main.py.

    Fetches repository metadata + full recursive file tree.
    Returns a RepoData dict; does NOT fetch individual file contents
    (that is deferred to repo_processor so it can select files first).
    """
    owner, repo_name = parse_github_url(url)
    token = os.getenv("GITHUB_TOKEN")

    with httpx.Client(follow_redirects=True) as client:
        # 1. Repo metadata
        meta = _get(
            client,
            f"{GITHUB_API}/repos/{owner}/{repo_name}",
            token,
        ).json()

        branch: str = meta["default_branch"]
        description: str | None = meta.get("description")
        language: str | None = meta.get("language")
        topics: list[str] = meta.get("topics", [])

        # 2. Latest commit SHA for the default branch (used for tree endpoint)
        branch_data = _get(
            client,
            f"{GITHUB_API}/repos/{owner}/{repo_name}/branches/{branch}",
            token,
        ).json()
        ref: str = branch_data["commit"]["sha"]

        # 3. Recursive file tree
        tree_resp = _get(
            client,
            f"{GITHUB_API}/repos/{owner}/{repo_name}/git/trees/{ref}",
            token,
            params={"recursive": "1"},
        ).json()

        if tree_resp.get("truncated"):
            # Repository has > 100k entries; we still proceed with what we have
            pass

        tree: list[TreeEntry] = [
            TreeEntry(
                path=entry["path"],
                type=entry["type"],       # "blob" or "tree"
                size=entry.get("size", 0),
            )
            for entry in tree_resp.get("tree", [])
        ]

    return RepoData(
        owner=owner,
        repo=repo_name,
        branch=branch,
        ref=ref,
        description=description,
        language=language,
        topics=topics,
        tree=tree,
        token=token,
    )


def fetch_file_contents(
    owner: str,
    repo: str,
    paths: list[str],
    ref: str,
    token: str | None = None,
) -> dict[str, str]:
    """
    Fetch raw text content for a list of file paths.

    Returns a dict mapping path -> decoded content.
    Silently skips files that are binary, too large, or return errors.
    Called by repo_processor.build_context after it has selected files.
    """
    results: dict[str, str] = {}

    with httpx.Client(follow_redirects=True) as client:
        for path in paths:
            try:
                data = _get(
                    client,
                    f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
                    token,
                    params={"ref": ref},
                ).json()
            except (GitHubNotFoundError, GitHubNetworkError):
                continue  # file disappeared or network blip — skip silently
            except (GitHubRateLimitError, GitHubPrivateRepoError):
                raise   # propagate auth/rate errors immediately
            except Exception:
                continue

            # GitHub returns base64-encoded content for blobs
            encoding = data.get("encoding", "")
            content_b64 = data.get("content", "")

            if encoding != "base64" or not content_b64:
                continue  # binary, symlink, or empty — skip

            try:
                decoded = base64.b64decode(content_b64).decode("utf-8", errors="replace")
            except Exception:
                continue

            results[path] = decoded

    return results
