import logging
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from github_fetcher import fetch_repo, GitHubURLError
from repo_processor import build_context
from llm_client import get_summary, LLMConfigError, LLMTimeoutError

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="GitHub Repo Summariser", version="0.1.0")


class SummarizeRequest(BaseModel):
    github_url: str


@app.get("/health")
def health():
    return {"status": "ok", "version": app.version}


@app.post("/summarize")
def summarize_repo(body: SummarizeRequest):
    github_url = body.github_url
    logger.info("summarize request: %s", github_url)

    try:
        repo_data = fetch_repo(github_url)
    except GitHubURLError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(502, str(e))

    context = build_context(repo_data)

    try:
        return get_summary(context)
    except LLMConfigError as e:
        raise HTTPException(503, str(e))
    except LLMTimeoutError as e:
        raise HTTPException(504, str(e))
    except Exception as e:
        raise HTTPException(502, str(e))
