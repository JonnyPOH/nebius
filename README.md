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
NEBIUS_API_KEY=your_key_here

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

`meta-llama/Llama-3.3-70B-Instruct` via Nebius Token Factory — reliably follows structured JSON instructions and has a large enough context window for repository contents.

## How repo content is processed

Files are prioritised by type:

- Always included: READMEs, dependency manifests, Dockerfiles, CI config, directory tree
- Up to 6 files: source code (*.py, *.ts, *.go etc.)
- Skipped: node_modules, build output, binary files, lock files, generated files, files over 200KB

Total context is capped at 80,000 characters — enough to understand the project without wasting tokens on noise.
