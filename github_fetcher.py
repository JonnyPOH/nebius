"""github_fetcher.py — GitHub URL parsing, REST API calls, file tree + content fetching."""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timezone
from typing import TypedDict

import httpx


class GitHubError(RuntimeError): pass
class GitHubURLError(ValueError, GitHubError): pass
class GitHubNotFoundError(GitHubError): pass
class GitHubPrivateRepoError(GitHubError): pass
class GitHubNetworkError(GitHubError): pass

class GitHubRateLimitError(GitHubError):
    def __init__(self, message: str, reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class RepoData(TypedDict):
    owner: str
    repo: str
    branch: str
    ref: str
    description: str | None
    language: str | None
    topics: list[str]
    tree: list[dict]
    token: str | None


GITHUB_API = "https://api.github.com"
_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/?#]+?)(?:\.git)?(?:/.*)?$"
)


def _headers(token: str | None) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(client: httpx.Client, url: str, token: str | None, **kwargs) -> httpx.Response:
    try:
        resp = client.get(url, headers=_headers(token), timeout=20, **kwargs)
    except httpx.TimeoutException as exc:
        raise GitHubNetworkError(f"Request timed out: {url}") from exc
    except httpx.ConnectError as exc:
        raise GitHubNetworkError(f"Could not connect to GitHub API. ({exc})") from exc
    except httpx.RequestError as exc:
        raise GitHubNetworkError(f"Network error: {exc}") from exc

    if resp.status_code == 401:
        raise GitHubPrivateRepoError("GitHub returned 401 Unauthorized. Check your GITHUB_TOKEN.")

    if resp.status_code == 403:
        remaining = resp.headers.get("X-RateLimit-Remaining", "1")
        retry_after = resp.headers.get("Retry-After")
        if remaining == "0" or retry_after:
            raw = resp.headers.get("X-RateLimit-Reset")
            reset_at = datetime.fromtimestamp(int(raw), tz=timezone.utc) if raw else None
            reset_str = reset_at.strftime("%Y-%m-%d %H:%M:%S UTC") if reset_at else "unknown time"
            wait_hint = f" Retry after {retry_after}s." if retry_after else ""
            raise GitHubRateLimitError(
                f"Rate limit exceeded, resets at {reset_str}.{wait_hint} "
                "Set GITHUB_TOKEN to get 5,000 req/hr instead of 60.",
                reset_at=reset_at,
            )
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        raise GitHubPrivateRepoError(
            f"Access denied (403): {body.get('message', 'Forbidden')}. "
            "Repo may be private or your token may need 'repo' scope."
        )

    if resp.status_code == 404:
        hint = " If private, set GITHUB_TOKEN with 'repo' scope." if not token else ""
        raise GitHubNotFoundError(f"Not found (404): {url}.{hint}")

    if resp.status_code == 422:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        raise GitHubError(
            f"GitHub couldn't process request (422): {body.get('message', 'Unprocessable Entity')}. "
            "Repo may be empty."
        )

    if resp.status_code == 451:
        raise GitHubNotFoundError(f"Repo unavailable for legal reasons (451): {url}.")

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GitHubError(f"Unexpected GitHub API error {resp.status_code}: {exc}") from exc

    return resp


def fetch_repo(url: str) -> RepoData:
    m = _URL_RE.match(url.strip())
    if not m:
        raise GitHubURLError(f"Invalid GitHub URL: '{url}'. Expected: https://github.com/<owner>/<repo>")

    owner, repo_name = m.group("owner"), m.group("repo")
    token = os.getenv("GITHUB_TOKEN")

    with httpx.Client(follow_redirects=True) as client:
        meta = _get(client, f"{GITHUB_API}/repos/{owner}/{repo_name}", token).json()
        branch = meta["default_branch"]

        branch_data = _get(client, f"{GITHUB_API}/repos/{owner}/{repo_name}/branches/{branch}", token).json()
        ref = branch_data["commit"]["sha"]

        tree_resp = _get(
            client,
            f"{GITHUB_API}/repos/{owner}/{repo_name}/git/trees/{ref}",
            token,
            params={"recursive": "1"},
        ).json()

    return RepoData(
        owner=owner,
        repo=repo_name,
        branch=branch,
        ref=ref,
        description=meta.get("description"),
        language=meta.get("language"),
        topics=meta.get("topics", []),
        tree=tree_resp.get("tree", []),
        token=token,
    )


def fetch_file_contents(
    owner: str,
    repo: str,
    paths: list[str],
    ref: str,
    token: str | None = None,
) -> dict[str, str]:
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
                continue
            except (GitHubRateLimitError, GitHubPrivateRepoError):
                raise
            except Exception:
                continue

            encoding = data.get("encoding", "")
            content_b64 = data.get("content", "")
            if encoding != "base64" or not content_b64:
                continue

            try:
                results[path] = base64.b64decode(content_b64).decode("utf-8", errors="replace")
            except Exception:
                continue

    return results
