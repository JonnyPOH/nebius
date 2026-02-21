"""
llm_client.py
-------------
Responsible for:
  1. Loading / validating the Anthropic API key from the environment.
  2. Calling the Anthropic Messages API (claude-3-5-sonnet by default).
  3. Enforcing a strict JSON output schema: {summary, technologies[], structure}.
  4. Retrying on transient failures with exponential back-off.
  5. Falling back to best-effort JSON extraction when the model returns
     text that is not perfectly valid JSON.

Public surface used by main.py:
  get_summary(context: str) -> SummaryResult
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

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment variables)
# ---------------------------------------------------------------------------

ANTHROPIC_API_URL: str = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")
ANTHROPIC_VERSION: str = "2023-06-01"
# Request timeout in seconds (connect + read)
_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "60"))
# Number of total attempts (1 = no retry)
_MAX_ATTEMPTS: int = int(os.getenv("LLM_MAX_ATTEMPTS", "3"))
# Base back-off in seconds between retries (doubled each attempt)
_BACKOFF_BASE: float = float(os.getenv("LLM_BACKOFF_BASE", "2"))

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

class SummaryResult(TypedDict):
    summary: str
    technologies: list[str]
    structure: str

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a senior software engineer. Your task is to analyse a GitHub repository \
and return a structured JSON summary.

You MUST respond with ONLY a valid JSON object — no markdown, no code fences, \
no prose, no extra keys, no explanation. Your entire response must be parseable \
by json.loads().

The JSON object must conform EXACTLY to this schema:

{
  "summary":      string,   // one concise paragraph: what the project does, who it is for, notable features
  "technologies": string[], // every meaningful language, framework, library, tool identified in the files
  "structure":    string    // one paragraph: directory layout and how the code is divided
}

Constraints:
- Output ONLY the JSON object. No text before '{' or after '}'.
- "summary"      — plain text, ≤ 200 words.
- "technologies" — array of short name strings (e.g. "Python", "FastAPI", "PostgreSQL").
                   No version numbers unless architecturally significant. No duplicates.
- "structure"    — plain text, ≤ 150 words.
- Do NOT add any key other than the three above.
- Do NOT wrap the JSON in a code block or backticks.
"""

_USER_PROMPT_TEMPLATE = """\
Analyse the repository context below and return the JSON summary.

Remember: respond with ONLY the JSON object, nothing else.

{context}

JSON response:
"""

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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
            _maybe_backoff(attempt)
            continue
        except httpx.RequestError as exc:
            last_exc = exc
            logger.warning("LLM network error (attempt %d/%d): %s", attempt, _MAX_ATTEMPTS, exc)
            _maybe_backoff(attempt)
            continue

        # --- HTTP error handling ---
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", _BACKOFF_BASE * (2 ** attempt)))
            logger.warning(
                "LLM rate limited (attempt %d/%d). Retry-After: %.1fs",
                attempt, _MAX_ATTEMPTS, retry_after,
            )
            last_exc = LLMResponseError(f"Rate limited by Anthropic API (429). Retry after {retry_after}s.")
            time.sleep(retry_after)
            continue

        if resp.status_code in {500, 502, 503, 504, 529}:  # 529 = Anthropic overloaded
            last_exc = LLMResponseError(f"Anthropic API server error {resp.status_code}.")
            logger.warning("LLM server error %d (attempt %d/%d)", resp.status_code, attempt, _MAX_ATTEMPTS)
            _maybe_backoff(attempt)
            continue

        if resp.status_code == 401:
            raise LLMConfigError(
                "Anthropic API returned 401 Unauthorized. Check your ANTHROPIC_API_KEY."
            )

        if not resp.is_success:
            raise LLMResponseError(
                f"Anthropic API returned unexpected status {resp.status_code}: {resp.text[:300]}"
            )

        data = resp.json()
        # Anthropic response: {"content": [{"type": "text", "text": "..."}], ...}
        for block in data.get("content", []):
            if block.get("type") == "text":
                return block["text"]

        raise LLMResponseError("Anthropic API returned no text content in response.")

    # All attempts exhausted
    if isinstance(last_exc, httpx.TimeoutException):
        raise LLMTimeoutError(
            f"LLM request timed out after {_MAX_ATTEMPTS} attempt(s) "
            f"(timeout={_TIMEOUT}s per request)."
        ) from last_exc
    raise LLMResponseError(
        f"LLM request failed after {_MAX_ATTEMPTS} attempt(s)."
    ) from last_exc

    # All attempts exhausted
    if isinstance(last_exc, httpx.TimeoutException):
        raise LLMTimeoutError(
            f"LLM request timed out after {_MAX_ATTEMPTS} attempt(s) "
            f"(timeout={_TIMEOUT}s per request)."
        ) from last_exc
    raise LLMResponseError(
        f"LLM request failed after {_MAX_ATTEMPTS} attempt(s)."
    ) from last_exc


def _maybe_backoff(attempt: int) -> None:
    """Sleep with exponential back-off unless this was the last attempt."""
    if attempt < _MAX_ATTEMPTS:
        delay = _BACKOFF_BASE * (2 ** (attempt - 1))
        logger.debug("Back-off %.1fs before attempt %d", delay, attempt + 1)
        time.sleep(delay)


# ---------------------------------------------------------------------------
# JSON parsing with fallback
# ---------------------------------------------------------------------------

_REQUIRED_KEYS: frozenset[str] = frozenset({"summary", "technologies", "structure"})


def _validate_result(obj: dict) -> SummaryResult:
    """
    Strictly type-check and coerce a parsed dict into a SummaryResult.
    Raises LLMParseError on any schema violation that cannot be safely coerced.
    """
    # --- required keys ---
    missing = _REQUIRED_KEYS - obj.keys()
    if missing:
        raise LLMParseError(f"LLM response missing required key(s): {missing}")

    # --- extra keys (warn but don't fail) ---
    extra = obj.keys() - _REQUIRED_KEYS
    if extra:
        logger.debug("LLM response contained unexpected keys (ignored): %s", extra)

    # --- summary: must be a non-empty string ---
    summary = obj["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise LLMParseError(
            f'"summary" must be a non-empty string, got {type(summary).__name__}: {str(summary)[:100]}'
        )

    # --- technologies: must be a list; coerce list of non-strings ---
    techs = obj["technologies"]
    if isinstance(techs, str):
        # Model returned a comma-separated string instead of an array
        techs = [t.strip() for t in techs.split(",") if t.strip()]
    elif isinstance(techs, list):
        coerced: list[str] = []
        for item in techs:
            if isinstance(item, str) and item.strip():
                coerced.append(item.strip())
            elif item is not None:
                coerced.append(str(item).strip())
        # deduplicate while preserving order
        seen: set[str] = set()
        techs = [t for t in coerced if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
    else:
        raise LLMParseError(
            f'"technologies" must be an array, got {type(techs).__name__}'
        )

    # --- structure: must be a non-empty string ---
    structure = obj["structure"]
    if not isinstance(structure, str) or not structure.strip():
        raise LLMParseError(
            f'"structure" must be a non-empty string, got {type(structure).__name__}: {str(structure)[:100]}'
        )

    return SummaryResult(
        summary=summary.strip(),
        technologies=techs,
        structure=structure.strip(),
    )


def _extract_json_block(text: str) -> str:
    """
    Strip markdown code fences and/or leading/trailing prose to isolate
    the outermost JSON object.
    """
    # 1. ```json ... ``` or ``` ... ```
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)

    # 2. Outermost { ... } span
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]

    return text


def _repair_json(text: str) -> str:
    """
    Best-effort repair of the most common LLM JSON mistakes so that
    json.loads() has a chance to succeed on a second try.

    Handles:
    - Trailing commas before } or ]         e.g. {"a": 1,}
    - Single-quoted strings                 e.g. {'key': 'value'}
    - Python-style True / False / None      e.g. {"ok": True}
    - Unquoted simple string values         e.g. {"summary": hello world}
    - Stray newlines inside string values   (normalise to \\n)
    """
    s = _extract_json_block(text)

    # 1. Trailing commas before a closing bracket/brace
    s = re.sub(r",\s*([}\]])", r"\1", s)

    # 2. Single-quoted strings → double-quoted
    #    Only replace quotes that act as string delimiters (not apostrophes)
    s = re.sub(r"(?<!\\)'", '"', s)

    # 3. Python literals → JSON literals
    s = re.sub(r"\bTrue\b",  "true",  s)
    s = re.sub(r"\bFalse\b", "false", s)
    s = re.sub(r"\bNone\b",  "null",  s)

    # 4. Remove C-style // comments (sometimes models add them)
    s = re.sub(r"//[^\n]*", "", s)

    return s


def _regex_extract_fields(text: str) -> dict | None:
    """
    Last-resort extraction: pull each required field individually via regex
    when the response cannot be made to parse as JSON at all.
    Returns a raw dict on success, None if any required field is missing.
    """
    result: dict = {}

    # --- summary: "summary": "..." (multiline value) ---
    m = re.search(r'["\']?summary["\']?\s*:\s*["\']([^"\'{\[]+)["\']', text, re.DOTALL)
    if m:
        result["summary"] = m.group(1).strip()

    # --- technologies: "technologies": ["a", "b", ...] ---
    m = re.search(r'["\']?technologies["\']?\s*:\s*\[([^\]]+)\]', text, re.DOTALL)
    if m:
        items = re.findall(r'["\']([^"\']+)["\']', m.group(1))
        if not items:  # unquoted items
            items = [t.strip() for t in m.group(1).split(",") if t.strip()]
        result["technologies"] = items

    # --- structure: "structure": "..." ---
    m = re.search(r'["\']?structure["\']?\s*:\s*["\']([^"\'{\[]+)["\']', text, re.DOTALL)
    if m:
        result["structure"] = m.group(1).strip()

    if _REQUIRED_KEYS.issubset(result.keys()):
        return result
    return None


def _parse_response(raw: str) -> SummaryResult:
    """
    Parse and strictly validate the model's raw output into a SummaryResult.

    Four-pass fallback strategy:
      1. json.loads(raw)                    → _validate_result
      2. json.loads(_extract_json_block)    → _validate_result
      3. json.loads(_repair_json)           → _validate_result
      4. _regex_extract_fields              → _validate_result  (last resort)
      5. Raise LLMParseError
    """
    last_parse_error: LLMParseError | None = None
    candidates = [
        raw,
        _extract_json_block(raw),
        _repair_json(raw),
    ]

    for attempt_text in candidates:
        try:
            obj = json.loads(attempt_text)
        except json.JSONDecodeError:
            continue
        try:
            return _validate_result(obj)
        except LLMParseError as exc:
            last_parse_error = exc
            continue

    # Pass 4: regex field extraction — no JSON parser involved
    fields = _regex_extract_fields(raw)
    if fields is not None:
        logger.warning(
            "LLM response required regex extraction fallback. "
            "Model may not be respecting response_format=json_object."
        )
        try:
            return _validate_result(fields)
        except LLMParseError as exc:
            last_parse_error = exc

    if last_parse_error:
        raise LLMParseError(
            f"{last_parse_error}. Raw response (first 500 chars): {raw[:500]}"
        )
    raise LLMParseError(
        f"LLM response could not be parsed by any strategy. "
        f"Raw response (first 500 chars): {raw[:500]}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_summary(context: str) -> SummaryResult:
    """
    Send the repository context to the Nebius LLM and return a structured summary.

    Raises:
      LLMConfigError    – NEBIUS_API_KEY missing or invalid.
      LLMTimeoutError   – all attempts timed out.
      LLMResponseError  – non-retryable HTTP error from the API.
      LLMParseError     – model returned unparseable output.
    """
    api_key = _api_key()
    raw = _call_api(context, api_key)
    return _parse_response(raw)
