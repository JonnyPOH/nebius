# GitHub Repository Summarizer

A FastAPI service that takes a GitHub repository URL and returns an LLM-generated summary of what the project does, the technologies it uses, and how it's structured.

## Setup

Either unzip the archive or clone the repo:

```bash
git clone https://github.com/JonnyPOH/nebius.git
```

Then:

```bash
cd nebius
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
NEBIUS_API_KEY=XXXXXXXXXXXXXXX

# optional — raises GitHub rate limit from 60 to 5000 requests/hr
GITHUB_TOKEN=ghp_...
```

## Running

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Usage

```bash
curl -X POST http://localhost:8000/summarize \
  -H "Content-Type: application/json" \
  -d '{"github_url": "https://github.com/psf/requests"}'
```

Check the service is running at `http://localhost:8000/health`, or open `http://localhost:8000/docs` for the interactive API.

## Model

`meta-llama/Llama-3.3-70B-Instruct` via Nebius Token Factory — reliably follows structured JSON instructions, has a large enough context window for repository contents, and is readily available on Nebius with low latency.

## How repo content is processed

Sending an entire repo to an LLM is impractical, so files are prioritised by type. READMEs, dependency manifests, Dockerfiles and CI config are always included. Source files (*.py, *.ts, *.go etc.) are included up to a cap of 6. Anything in node_modules, build output, binary files, lock files, generated files, or files over 200KB gets skipped. A directory tree is always included. Total context is capped at 80,000 characters — enough to understand the project without wasting tokens on noise.
