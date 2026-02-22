"""
llm_client.py — LLM API client with retry logic and JSON response parsing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import TypedDict

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL: str = "https://api.anthropic.com/v1/messages"
# haiku is fast and cheap for this; sonnet would be overkill
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-3-haiku-20240307")
ANTHROPIC_VERSION: str = "2023-06-01"
_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "60"))
_MAX_ATTEMPTS: int = int(os.getenv("LLM_MAX_ATTEMPTS", "3"))
_BACKOFF_BASE: float = float(os.getenv("LLM_BACKOFF_BASE", "2"))


class LLMError(RuntimeError):
    """Base class for llm_client errors."""

class LLMConfigError(LLMError):
    """Raised when required configuration (e.g. API key) is missing."""

class LLMTimeoutError(LLMError):
    """Raised when the LLM request times out after all retries."""

class LLMResponseError(LLMError):
    """Raised when the LLM returns an HTTP error that is not retryable."""

class LLMParseError(LLMError):
    """Raised when the model's output cannot be parsed into the expected schema."""


class SummaryResult(TypedDict):
    summary: str
    technologies: list[str]
    structure: str


_SYSTEM_PROMPT = """\
You are a senior software engineer. Analyse a GitHub repository and return a structured JSON summary.

Respond with ONLY a valid JSON object — no markdown, no code fences, no prose.
Your entire response must be parseable by json.loads().

Schema:
{
  "summary":      string,   // one concise paragraph: what the project does, who it is for, notable features
  "technologies": string[], // every meaningful language, framework, library, tool identified
  "structure":    string    // one paragraph: directory layout and how the code is divided
}

Constraints:
- Output ONLY the JSON object. Nothing before '{' or after '}'.
- "summary"      — plain text, ≤ 200 words.
- "technologies" — short name strings (e.g. "Python", "FastAPI"). No version numbers. No duplicates.
- "structure"    — plain text, ≤ 150 words.
- Do NOT add extra keys. Do NOT wrap in a code block.
"""

_USER_PROMPT_TEMPLATE = """\
Analyse the repository context below and return the JSON summary.

Remember: respond with ONLY the JSON object, nothing else.

{context}

JSON response:
"""


def _api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise LLMConfigError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Add it to your .env file or export it in your shell."
        )
    return key


def _call_api(context: str, api_key: str) -> str:
    """
    POST to the Anthropic Messages endpoint.
    Returns the text content of the first response block.
    Raises LLMTimeoutError / LLMResponseError on failure.
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1024,
        "system": _SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": _USER_PROMPT_TEMPLATE.format(context=context)},
        ],
    }

    last_exc: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = httpx.post(
                ANTHROPIC_API_URL,
                headers=headers,
                json=payload,
                timeout=_TIMEOUT,
            )
        except httpx.TimeoutException as exc:
            last_exc = exc
            logger.warning("LLM request timed out (attempt %d/%d)", attempt, _MAX_ATTEMPTS)
        except httpx.RequestError as exc:
            last_exc = exc
            logger.warning("LLM network error (attempt %d/%d): %s", attempt, _MAX_ATTEMPTS, exc)
        else:
            # --- HTTP error handling ---
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", _BACKOFF_BASE * (2 ** attempt)))
                logger.warning("LLM rate limited (attempt %d/%d). Retry-After: %.1fs", attempt, _MAX_ATTEMPTS, retry_after)
                last_exc = LLMResponseError(f"Rate limited by Anthropic API (429). Retry after {retry_after}s.")
                time.sleep(retry_after)
                continue

            if resp.status_code in {500, 502, 503, 504, 529}:
                last_exc = LLMResponseError(f"Anthropic API server error {resp.status_code}.")
                logger.warning("LLM server error %d (attempt %d/%d)", resp.status_code, attempt, _MAX_ATTEMPTS)
            elif resp.status_code == 401:
                raise LLMConfigError("Anthropic API returned 401 Unauthorized. Check your ANTHROPIC_API_KEY.")
            elif not resp.is_success:
                raise LLMResponseError(f"Anthropic API returned unexpected status {resp.status_code}: {resp.text[:300]}")
            else:
                data = resp.json()
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block["text"]
                raise LLMResponseError("Anthropic API returned no text content in response.")

        if attempt < _MAX_ATTEMPTS:
            time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
        continue

    if isinstance(last_exc, httpx.TimeoutException):
        raise LLMTimeoutError(f"LLM request timed out after {_MAX_ATTEMPTS} attempt(s).") from last_exc
    raise LLMResponseError(f"LLM request failed after {_MAX_ATTEMPTS} attempt(s).") from last_exc


def _validate_result(obj: dict) -> SummaryResult:
    missing = {"summary", "technologies", "structure"} - obj.keys()
    if missing:
        raise LLMParseError(f"LLM response missing required key(s): {missing}")

    summary = obj["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise LLMParseError(f'"summary" must be a non-empty string')

    techs = obj["technologies"]
    if isinstance(techs, str):
        techs = [t.strip() for t in techs.split(",") if t.strip()]
    elif isinstance(techs, list):
        techs = list(dict.fromkeys(str(t).strip() for t in techs if t))
    else:
        raise LLMParseError(f'"technologies" must be an array, got {type(techs).__name__}')

    structure = obj["structure"]
    if not isinstance(structure, str) or not structure.strip():
        raise LLMParseError(f'"structure" must be a non-empty string')

    return SummaryResult(summary=summary.strip(), technologies=techs, structure=structure.strip())


def _extract_json_block(text: str) -> str:
    """Isolate the outermost JSON object from prose or markdown fences."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _parse_response(raw: str) -> SummaryResult:
    # json.loads first — most models return clean JSON ~90% of the time
    for candidate in (raw, _extract_json_block(raw)):
        try:
            return _validate_result(json.loads(candidate))
        except (json.JSONDecodeError, LLMParseError):
            continue

    raise LLMParseError(
        f"LLM response could not be parsed. Raw (first 300 chars): {raw[:300]}"
    )


def get_summary(context: str) -> SummaryResult:
    """
    Send repository context to the LLM and return a structured summary.

    Raises:
      LLMConfigError    – API key missing or invalid.
      LLMTimeoutError   – all attempts timed out.
      LLMResponseError  – non-retryable HTTP error.
      LLMParseError     – model returned unparseable output.
    """
    api_key = _api_key()
    raw = _call_api(context, api_key)
    return _parse_response(raw)
