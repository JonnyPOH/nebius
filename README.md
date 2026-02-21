# GitHub Repository Summarizer

A FastAPI service that accepts a GitHub repository URL, fetches its contents, and returns an LLM-generated summary of what the project does, the technologies it uses, and how it is structured.

## Prerequisites

- Python 3.11 or higher
- An [Anthropic API key](https://console.anthropic.com/) with available credits
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
# ANTHROPIC_MODEL=claude-3-5-sonnet-20241022   # default
# CONTEXT_CHAR_BUDGET=80000                    # default: 80 000 chars sent to LLM
# MAX_BLOB_BYTES=200000                        # default: skip files larger than 200 KB
# MAX_SOURCE_FILES=6                           # default: max source files included
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
{"github_url": "https://github.com/psf/requests"\}
```

**Success response (200)**

```json
{
  "summary": "Requests is a popular Python HTTP library ...",
  "technologies": ["Python", "urllib3", "certifi", "charset-normalizer"],
  "structure": "The main source code lives in src/requests/, tests in tests/, and docs in docs/."
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
| `llm_client.py` | Anthropic API client, retry logic, strict JSON parsing |
| `requirements.txt` | Python dependencies |

## How Repo Content Is Processed

Sending an entire repository to an LLM is impractical — too many files, too many tokens. The processor applies a tiered priority system:

1. **Tier 0** — `README*` (up to 20 KB) — the single most informative file
2. **Tier 1** — Manifest files (`pyproject.toml`, `requirements*.txt`, `package.json`, `Cargo.toml`, etc.)
3. **Tier 2** — Build / ops files (`Dockerfile`, `docker-compose*`, `Makefile`)
4. **Tier 3** — CI / config files (`*.yml`, `*.toml`, `.env.example`)
5. **Tier 5** — Source files (`*.py`, `*.ts`, `*.go`, `*.rs`, …) — capped at 6 files total
6. **Excluded** — `node_modules/`, `.git/`, `dist/`, vendored dirs, binary files, lock files, generated files, files > 200 KB

A directory tree is always included regardless of budget. Total context sent to the LLM is capped at 80,000 characters by default.
