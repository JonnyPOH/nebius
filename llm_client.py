"""llm_client.py — calls Anthropic, parses the JSON back out."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import TypedDict

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-haiku-20240307")
ANTHROPIC_VERSION = "2023-06-01"
_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))
_MAX_ATTEMPTS = int(os.getenv("LLM_MAX_ATTEMPTS", "3"))
_BACKOFF_BASE = float(os.getenv("LLM_BACKOFF_BASE", "2"))


class LLMError(RuntimeError): pass
class LLMConfigError(LLMError): pass
class LLMTimeoutError(LLMError): pass
class LLMResponseError(LLMError): pass
class LLMParseError(LLMError): pass


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


def _api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise LLMConfigError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Add it to your .env file or export it in your shell."
        )
    return key


def _call_api(context: str, api_key: str) -> str:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1024,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": f"Analyse the repository context below and return the JSON summary.\n\n{context}\n\nJSON response:"}],
    }

    last_exc: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = httpx.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=_TIMEOUT)
        except httpx.TimeoutException as exc:
            last_exc = exc
            logger.warning("LLM timeout (attempt %d/%d)", attempt, _MAX_ATTEMPTS)
        except httpx.RequestError as exc:
            last_exc = exc
            logger.warning("LLM network error (attempt %d/%d): %s", attempt, _MAX_ATTEMPTS, exc)
        else:
            if resp.status_code == 429:
                wait = float(resp.headers.get("retry-after", _BACKOFF_BASE * (2 ** attempt)))
                logger.warning("rate limited, waiting %.1fs", wait)
                last_exc = LLMResponseError(f"Rate limited (429), retry after {wait}s.")
                time.sleep(wait)
                continue

            if resp.status_code in {500, 502, 503, 504, 529}:
                last_exc = LLMResponseError(f"Anthropic server error {resp.status_code}.")
                logger.warning("server error %d (attempt %d/%d)", resp.status_code, attempt, _MAX_ATTEMPTS)
            elif resp.status_code == 401:
                raise LLMConfigError("Anthropic returned 401 — check your ANTHROPIC_API_KEY.")
            elif not resp.is_success:
                raise LLMResponseError(f"Unexpected status {resp.status_code}: {resp.text[:300]}")
            else:
                for block in resp.json().get("content", []):
                    if block.get("type") == "text":
                        return block["text"]
                raise LLMResponseError("No text content in Anthropic response.")

        if attempt < _MAX_ATTEMPTS:
            time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))

    if isinstance(last_exc, httpx.TimeoutException):
        raise LLMTimeoutError(f"Timed out after {_MAX_ATTEMPTS} attempt(s).") from last_exc
    raise LLMResponseError(f"Failed after {_MAX_ATTEMPTS} attempt(s).") from last_exc


def _extract_json(text: str) -> str:
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


def _validate_result(obj: dict) -> SummaryResult:
    missing = {"summary", "technologies", "structure"} - obj.keys()
    if missing:
        raise LLMParseError(f"Missing key(s): {missing}")

    summary = obj["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise LLMParseError('"summary" must be a non-empty string')

    techs = obj["technologies"]
    if isinstance(techs, str):
        techs = [t.strip() for t in techs.split(",") if t.strip()]
    elif isinstance(techs, list):
        techs = list(dict.fromkeys(str(t).strip() for t in techs if t))
    else:
        raise LLMParseError(f'"technologies" must be an array, got {type(techs).__name__}')

    structure = obj["structure"]
    if not isinstance(structure, str) or not structure.strip():
        raise LLMParseError('"structure" must be a non-empty string')

    return SummaryResult(summary=summary.strip(), technologies=techs, structure=structure.strip())


def _parse_response(raw: str) -> SummaryResult:
    for candidate in (raw, _extract_json(raw)):
        try:
            return _validate_result(json.loads(candidate))
        except (json.JSONDecodeError, LLMParseError):
            continue
    raise LLMParseError(f"Could not parse LLM response. Raw (first 300 chars): {raw[:300]}")


def get_summary(context: str) -> SummaryResult:
    return _parse_response(_call_api(context, _api_key()))
