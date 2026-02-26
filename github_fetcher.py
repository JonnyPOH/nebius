"""Obtain repository metadata and file contents from GitHub using the REST API"""


#------------------ Imports ------------------#
import base64
import os
import re
import httpx


#------------------ Classes ------------------#
class GitHubError(RuntimeError): pass
class GitHubURLError(GitHubError): pass


#------------------ Variables ------------------#
GITHUB_API = "https://api.github.com"
_URL_RE = re.compile(r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/?#]+?)(?:\.git)?(?:/.*)?$")


#------------------ Functions ------------------#

# makes GET requests with required github headers, optional token raises rate limit from 60 to 5000/hr
def _get(client, url, token=None, **kwargs):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = client.get(url, headers=headers, timeout=20, **kwargs)
    resp.raise_for_status()
    return resp


# parses the url, makes 3 api calls to get repo metadata, default branch, and full file tree
def fetch_repo(url):
    m = _URL_RE.match(url.strip())
    if not m:
        raise GitHubURLError(f"Invalid GitHub URL: '{url}'")

    owner, repo_name = m.group("owner"), m.group("repo")
    token = os.getenv("GITHUB_TOKEN")

    with httpx.Client(follow_redirects=True) as client:
        # repo metadata — description, language, topics, default branch
        meta = _get(client, f"{GITHUB_API}/repos/{owner}/{repo_name}", token).json()
        branch = meta["default_branch"]

        # need the latest commit SHA to fetch the tree
        branch_data = _get(client, f"{GITHUB_API}/repos/{owner}/{repo_name}/branches/{branch}", token).json()
        ref = branch_data["commit"]["sha"]

        # full recursive file tree
        tree_resp = _get(
            client,
            f"{GITHUB_API}/repos/{owner}/{repo_name}/git/trees/{ref}",
            token,
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
        "token": token,
    }


# fetches raw text content for a list of paths, returns a dict of path -> decoded content
def fetch_file_contents(owner, repo, paths, ref, token=None):
    results = {}

    with httpx.Client(follow_redirects=True) as client:
        for path in paths:
            try:
                data = _get(
                    client,
                    f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
                    token,
                    params={"ref": ref},
                ).json()
            except Exception:
                continue  # file disappeared or network blip, skip it

            # github returns file content as base64
            encoding = data.get("encoding", "")
            content_b64 = data.get("content", "")
            if encoding != "base64" or not content_b64:
                continue  # binary or empty, skip

            try:
                results[path] = base64.b64decode(content_b64).decode("utf-8", errors="replace")
            except Exception:
                continue

    return results
