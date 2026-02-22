import logging
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl

from github_fetcher import (
    fetch_repo,
    GitHubURLError,
    GitHubNotFoundError,
    GitHubPrivateRepoError,
    GitHubRateLimitError,
    GitHubNetworkError,
)
from repo_processor import build_context
from llm_client import (
    get_summary,
    LLMConfigError,
    LLMTimeoutError,
    LLMResponseError,
    LLMParseError,
)

# Load .env file if present (no-op in production where vars are set directly)
load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


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

    model_config = {
        "json_schema_extra": {
            "example": {"status": "error", "message": "Repository not found (404)."}
        }
    }


app = FastAPI(
    title="GitHub Repository Summarizer",
    description=(
        "Accepts a GitHub repository URL, fetches the repository contents, "
        "and returns an LLM-generated summary."
    ),
    version="0.1.0",
)

# every error goes through these handlers so the shape is always {"status": "error", ...}
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(message=str(exc.detail)).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Flatten Pydantic validation errors into a single readable string.
    errors = "; ".join(
        f"{' -> '.join(str(loc) for loc in e['loc'])}: {e['msg']}"
        for e in exc.errors()
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(message=errors).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(message=f"Unexpected error: {exc}").model_dump(),
    )


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health_check() -> HealthResponse:
    """Liveness probe. Returns service version alongside status."""
    return HealthResponse(version=app.version)


@app.post(
    "/summarize",
    response_model=SummarizeSuccessResponse,   # errors always go through exception handlers
    responses={
        200: {"model": SummarizeSuccessResponse, "description": "Summary generated successfully."},
        400: {"model": ErrorResponse, "description": "Bad request."},
        403: {"model": ErrorResponse, "description": "Access denied (private repo or bad token)."},
        404: {"model": ErrorResponse, "description": "Repository not found."},
        422: {"model": ErrorResponse, "description": "Invalid GitHub URL."},
        429: {"model": ErrorResponse, "description": "GitHub or LLM rate limit exceeded."},
        502: {"model": ErrorResponse, "description": "Upstream error (GitHub API or LLM)."},
        503: {"model": ErrorResponse, "description": "LLM not configured (missing API key)."},
        504: {"model": ErrorResponse, "description": "LLM request timed out."},
    },
    tags=["summarize"],
    summary="Summarise a GitHub repository",
)
def summarize_repo(payload: SummarizeRequest) -> SummarizeSuccessResponse:
    """Validate input → fetch repo → build context → call LLM → return summary."""
    url = str(payload.github_url)
    logger.info("[summarize] START url=%s", url)

    # ── Step 1: fetch repository metadata + file tree ────────────────────────
    try:
        repo_data = fetch_repo(url)
    except GitHubURLError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except GitHubNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except GitHubPrivateRepoError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except GitHubRateLimitError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    except GitHubNetworkError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception as exc:
        logger.exception("[summarize] unexpected error during fetch")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    logger.info(
        "[summarize] FETCH OK owner=%s repo=%s tree_entries=%d",
        repo_data["owner"], repo_data["repo"], len(repo_data["tree"]),
    )

    # ── Step 2: select + fetch important files and build LLM context ─────────
    context = build_context(repo_data)
    logger.info("[summarize] CONTEXT built chars=%d", len(context))

    # ── Step 3: call LLM and parse structured output ─────────────────────────
    try:
        result = get_summary(context)
    except LLMConfigError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except LLMTimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc))
    except (LLMResponseError, LLMParseError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception as exc:
        logger.exception("[summarize] unexpected error during LLM call")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    logger.info(
        "[summarize] LLM OK techs=%d summary_len=%d",
        len(result["technologies"]), len(result["summary"]),
    )

    # ── Step 4: return structured success response ────────────────────────────
    return SummarizeSuccessResponse(
        summary=result["summary"],
        technologies=result["technologies"],
        structure=result["structure"],
    )
