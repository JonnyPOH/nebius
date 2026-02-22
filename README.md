# GitHub Repository Summarizer

A FastAPI service that accepts a GitHub repository URL, fetches its contents, and returns an LLM-generated summary of what the project does, the technologies it uses, and how it is structured.

## Prerequisites

- Python 3.11 or higher
- An [Anthropic API key](https://console.anthropic.com/) — or a [Nebius Token Factory key](https://studio.nebius.ai/) for submission (set `NEBIUS_API_KEY` and update `llm_client.py`)
- (Optional) A [GitHub personal access token](https://github.com/settings/tokens) to raise the API rate limit from 60 to 5,000 requests/hour

## Installation

1. **Clone the repository**

   ```bash
   git clone https://github.com/JonnyPOH/nebius.git
   cd nebius
   ```

2. **Create and activate a virtual environment**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

## Configuration

Create a `.env` file in the project root with your credentials:

```dotenv
# Required
ANTHROPIC_API_KEY=sk-ant-api03-...

# Optional – raises GitHub rate limit from 60 to 5,000 req/hr
GITHUB_TOKEN=ghp_...

# Optional – tune processing behaviour
# ANTHROPIC_MODEL=claude-3-haiku-20240307  # default
# CONTEXT_CHAR_BUDGET=80000               # default: 80 000 chars sent to LLM
# MAX_BLOB_BYTES=200000                   # default: skip files larger than 200 KB
# MAX_SOURCE_FILES=6                      # default: max source files included
```

## Running the Server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

The server will be available at `http://localhost:8000`.

## API Reference

### `GET /health`

Returns service status.

**Response**

```json
{"status": "ok", "version": "0.1.0"}
```

---

### `POST /summarize`

Accepts a GitHub repository URL and returns an LLM-generated summary.

**Request body**

```json
{"github_url": "https://github.com/psf/requests"}
```

**Success response (200)**

```json
{
  "summary": "Requests is a simple, elegant HTTP library for Python. It abstracts the complexity of making HTTP requests behind a clean API, allowing users to send HTTP/1.1 requests with methods such as GET, POST, PUT, DELETE, and more. The library handles cookies, sessions, authentication, SSL verification, and connection pooling automatically, and is widely considered the de-facto standard for HTTP in Python.",
  "technologies": [
    "Python",
    "urllib3",
    "certifi",
    "charset-normalizer",
    "idna",
    "pytest"
  ],
  "structure": "The main source code is in src/requests/ and includes modules for sessions, adapters, auth, cookies, exceptions, and utils. Tests live in tests/. Documentation source is in docs/. Build and packaging configuration is in pyproject.toml and setup.cfg."
}
```

**Error response (4xx / 5xx)**

```json
{"status": "error", "message": "Description of what went wrong"}
```

| Status | Meaning |
|--------|---------|
| 400    | Malformed request body |
| 403    | Private repository |
| 404    | Repository not found |
| 422    | Invalid GitHub URL |
| 429    | GitHub rate limit exceeded |
| 502    | GitHub or LLM upstream error |
| 503    | LLM not configured (missing API key) |
| 504    | LLM request timed out |

## Testing

### Health check

```bash
curl http://localhost:8000/health
```

### Summarize a public repository

```bash
curl -X POST http://localhost:8000/summarize \
  -H "Content-Type: application/json" \
  -d '{"github_url": "https://github.com/psf/requests"}'
```

### Error scenarios

```bash
# 404 – repo does not exist
curl -X POST http://localhost:8000/summarize \
  -H "Content-Type: application/json" \
  -d '{"github_url": "https://github.com/doesnotexist99999/fakerepo"}'

# 422 – invalid URL
curl -X POST http://localhost:8000/summarize \
  -H "Content-Type: application/json" \
  -d '{"github_url": "https://example.com/not-github"}'
```

### Interactive API docs

Open `http://localhost:8000/docs` in a browser to explore the OpenAPI interface.

## Project Structure

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, request/response models, endpoint wiring |
| `github_fetcher.py` | GitHub URL parsing, REST API calls, file tree + content fetching |
| `repo_processor.py` | File prioritisation, exclusions, budget management, context building |
| `llm_client.py` | LLM API client (Anthropic / Nebius), retry logic, JSON parsing |
| `requirements.txt` | Python dependencies |

## Model Choice

**`meta-llama/Llama-3.3-70B-Instruct`** (default) via the Nebius Token Factory.
Llama 3.3 70B was chosen because it reliably follows structured JSON output instructions, has a large enough context window for repository contents, and is readily available on Nebius with low latency.

## How Repo Content Is Processed

Sending an entire repository to an LLM is impractical — too many files, too many tokens. The processor applies a tiered priority system:

1. **Tier 0** — `README*` (up to 20 KB) — the single most informative file
2. **Tier 1** — Manifest files (`pyproject.toml`, `requirements*.txt`, `package.json`, `Cargo.toml`, etc.)
3. **Tier 2** — Build / ops files (`Dockerfile`, `docker-compose*`, `Makefile`)
4. **Tier 3** — CI / config files (`*.yml`, `*.toml`, `.env.example`)
5. **Tier 5** — Source files (`*.py`, `*.ts`, `*.go`, `*.rs`, …) — capped at 6 files total
6. **Excluded** — `node_modules/`, `.git/`, `dist/`, vendored dirs, binary files, lock files, generated files, files > 200 KB

A directory tree is always included regardless of budget. Total context sent to the LLM is capped at 80,000 characters by default.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ModuleNotFoundError: No module named 'fastapi'` | Activate the venv and re-run `pip install -r requirements.txt` |
| `Address already in use` on port 8000 | Run `pkill -f 'uvicorn main:app'` then restart |
| `503 LLM not configured` | Check `.env` has `ANTHROPIC_API_KEY=sk-ant-...` with no surrounding quotes |
| `429 GitHub rate limit exceeded` | Set `GITHUB_TOKEN=ghp_...` in `.env` to raise the limit to 5,000 req/hr |
| `404 Repository not found` | Double-check the URL; for private repos add `GITHUB_TOKEN` with `repo` scope |
| `504 LLM request timed out` | Retry; reduce context by lowering `MAX_SOURCE_FILES` or `CONTEXT_CHAR_BUDGET` |
