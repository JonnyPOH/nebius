"""Obtain repository metadata and file contents from GitHub using the REST API"""


#------------------ Imports ------------------#
import base64
import os
import re
import httpx


#------------------ Classes ------------------#
class GitHubError(RuntimeError): pass
class GitHubURLError(GitHubError): pass
class GitHubNotFoundError(GitHubError): pass


#------------------ Variables ------------------#
GITHUB_API = "https://api.github.com"
_URL_RE = re.compile(r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/?#]+?)(?:\.git)?(?:/.*)?$")


#------------------ Functions ------------------#
# makes GET requests, injects auth header if token present
def _get(client, url, **kwargs):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    resp = client.get(url, headers=headers, timeout=20, **kwargs)
    resp.raise_for_status()
    return resp


# parses the url. 3 api calls to get repo metadata, default branch, and full file tree
def fetch_repo(url):
    m = _URL_RE.match(url.strip())
    # url must match pattern
    if not m:
        raise GitHubURLError(f"Invalid GitHub URL: '{url}'")

    owner, repo_name = m.group("owner"), m.group("repo")

    with httpx.Client(follow_redirects=True) as client:
        # repo metadata
        meta = _get(client, f"{GITHUB_API}/repos/{owner}/{repo_name}").json()
        branch = meta["default_branch"]

        # need the latest commit SHA to fetch the tree
        branch_data = _get(client, f"{GITHUB_API}/repos/{owner}/{repo_name}/branches/{branch}").json()
        ref = branch_data["commit"]["sha"]

        # full recursive file tree
        tree_resp = _get(
            client,
            f"{GITHUB_API}/repos/{owner}/{repo_name}/git/trees/{ref}",
            params={"recursive": "1"},
        ).json()

    return {
        "owner": owner,
        "repo": repo_name,
        "branch": branch,
        "ref": ref,
        "description": meta.get("description"),
        "language": meta.get("language"),
        "topics": meta.get("topics", []),
        "tree": tree_resp.get("tree", []),
    }

# fetches raw text content. Returns a dict of path (decoded content)
def fetch_file_contents(owner, repo, paths, ref):
    results = {}

    with httpx.Client(follow_redirects=True) as client:
        for path in paths:
            try:
                data = _get(
                    client,
                    f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
                    params={"ref": ref},
                ).json()
            except Exception:
                continue  # file disappeared or network error - skiop

            # github returns file content as base64
            encoding = data.get("encoding", "")
            content_b64 = data.get("content", "")
            if encoding != "base64" or not content_b64:
                continue  # binary or empty - skip

            try:
                results[path] = base64.b64decode(content_b64).decode("utf-8", errors="replace")
            except Exception:
                continue

    return results
