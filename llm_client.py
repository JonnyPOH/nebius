"""Client for calling the Anthropic API and parsing the response"""


#------------------ Imports ------------------#
import json
import logging
import os
import re
import time
import httpx


#------------------ Classes ------------------#
class LLMError(RuntimeError): pass
class LLMConfigError(LLMError): pass
class LLMTimeoutError(LLMError): pass
class LLMResponseError(LLMError): pass
class LLMParseError(LLMError): pass


#------------------ Variables ------------------#
logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# hardcoded
ANTHROPIC_MODEL = "claude-3-haiku-20240307"
_TIMEOUT = 60
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 2

_SYSTEM_PROMPT = """\
You are a senior software engineer reviewing a GitHub repository.

Return ONLY a valid JSON object — no markdown, no code fences, nothing outside the braces.

{
  "summary":      string,   // what the project does and who it's for, one paragraph (≤ 200 words)
  "technologies": string[], // languages, frameworks, libraries, tools — short names, no versions
  "structure":    string    // how the repo is laid out, one paragraph (≤ 150 words)
}

No extra keys. No duplicates in technologies.
"""


#------------------ Functions ------------------#

# sends context to anthropic, retries on failure, returns raw text
def _call_api(context):
    # check api key exists
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise LLMConfigError("ANTHROPIC_API_KEY is not set.")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1024,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": f"Analyse this repository and return the JSON summary.\n\n{context}\n\nJSON response:"}],
    }

    # try up to _MAX_ATTEMPTS times
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = httpx.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=_TIMEOUT)
        except httpx.TimeoutException:
            logger.warning("timeout on attempt %d/%d", attempt, _MAX_ATTEMPTS)
            time.sleep(_BACKOFF_BASE * attempt)
            continue
        except httpx.RequestError as exc:
            raise LLMResponseError(f"Network error: {exc}")

        if resp.status_code == 401:
            raise LLMConfigError("Got 401 — check your ANTHROPIC_API_KEY.")
        if resp.status_code == 429:
            # rate limited — wait and retry
            wait = float(resp.headers.get("retry-after", _BACKOFF_BASE * attempt))
            logger.warning("rate limited, waiting %.1fs", wait)
            time.sleep(wait)
            continue
        if not resp.is_success:
            logger.warning("server error %d (attempt %d/%d)", resp.status_code, attempt, _MAX_ATTEMPTS)
            time.sleep(_BACKOFF_BASE * attempt)
            continue

        # pull text out of the response
        for block in resp.json().get("content", []):
            if block.get("type") == "text":
                return block["text"]
        raise LLMResponseError("No text content in response.")

    raise LLMTimeoutError(f"Failed after {_MAX_ATTEMPTS} attempt(s).")


# strips markdown fences if the model wraps the response in them
def _extract_json(text):
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


# checks the parsed json has the right keys, cleans up technologies if needed
def _validate_result(obj):
    missing = {"summary", "technologies", "structure"} - obj.keys()
    if missing:
        raise LLMParseError(f"Missing key(s): {missing}")

    techs = obj["technologies"]
    if isinstance(techs, str):
        # sometimes the model returns a comma-separated string instead of an array
        techs = [t.strip() for t in techs.split(",") if t.strip()]
    else:
        techs = list(dict.fromkeys(str(t).strip() for t in techs if t))

    return {"summary": obj["summary"].strip(), "technologies": techs, "structure": obj["structure"].strip()}


# tries to parse raw response, falls back to extracting json if the first attempt fails
def _parse_response(raw):
    try:
        return _validate_result(json.loads(raw))
    except (json.JSONDecodeError, LLMParseError):
        pass

    # try again after stripping any markdown fences
    try:
        return _validate_result(json.loads(_extract_json(raw)))
    except (json.JSONDecodeError, LLMParseError):
        pass

    raise LLMParseError(f"Could not parse LLM response: {raw[:300]}")


# main entry point
def get_summary(context):
    return _parse_response(_call_api(context))
