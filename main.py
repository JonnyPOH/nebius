from typing import Literal, Union

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SummarizeRequest(BaseModel):
    github_url: HttpUrl = Field(
        ...,
        description="Public GitHub repository URL to summarize.",
        examples=["https://github.com/psf/requests"],
    )


class SummarizeSuccessResponse(BaseModel):
    summary: str = Field(..., description="Human-readable summary of the repository.")
    technologies: list[str] = Field(
        default_factory=list,
        description="Technologies / languages inferred from the repository.",
    )
    structure: str = Field(
        ..., description="High-level description of how the repository is organised."
    )


class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    message: str = Field(..., description="Description of what went wrong.")


SummarizeResponse = Union[SummarizeSuccessResponse, ErrorResponse]

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="GitHub Repository Summarizer",
    description=(
        "Accepts a GitHub repository URL, fetches the repository contents, "
        "and returns an LLM-generated summary."
    ),
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["meta"])
def health_check() -> dict[str, str]:
    """Quick liveness probe."""
    return {"status": "ok"}


@app.post(
    "/summarize",
    response_model=SummarizeResponse,
    tags=["summarize"],
    summary="Summarise a GitHub repository",
)
def summarize_repo(payload: SummarizeRequest) -> SummarizeResponse:
    """
    1. Fetch repository contents via the GitHub API.
    2. Select the most informative files (README, tree, key sources, configs).
    3. Send the curated context to the LLM.
    4. Parse and return the structured summary.
    """
    from github_fetcher import fetch_repo
    from repo_processor import build_context
    from llm_client import get_summary

    url = str(payload.github_url)

    try:
        repo_data = fetch_repo(url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    context = build_context(repo_data)

    try:
        result = get_summary(context)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    return SummarizeSuccessResponse(
        summary=result["summary"],
        technologies=result.get("technologies", []),
        structure=result["structure"],
    )
