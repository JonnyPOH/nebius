"""Client for calling the Anthropic API and parsing the response"""


#------------------ Imports ------------------#
import json
import os
import re
import time
import httpx


#------------------ Classes ------------------#
class LLMError(RuntimeError): pass
class LLMTimeoutError(LLMError): pass


#------------------ Variables ------------------#
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# hardcoded
ANTHROPIC_MODEL = "claude-3-haiku-20240307"
_TIMEOUT = 60
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 2

_SYSTEM_PROMPT = """\
You are analysing a GitHub repository to help a developer quickly understand what it does.

Return ONLY a valid JSON object with exactly these keys:

{
  "summary": "what the project does, who it's for, and why someone would use it (max 200 words)",
  "technologies": ["languages", "frameworks", "and tools used — short names only, no versions"],
  "structure": "how the codebase is organised and where the important parts live (max 150 words)"
}

Be specific and practical. Avoid vague phrases like 'this project provides' or 'this repository contains'.
No markdown, no code fences, no extra keys, no duplicate technologies.
"""


#------------------ Functions ------------------#

# sends context to anthropic, retries on failure, returns raw text
def _call_api(context):
    # check api key exists
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY is not set.")

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
            time.sleep(_BACKOFF_BASE * attempt)
            continue
        except httpx.RequestError as exc:
            raise LLMError(f"Network error: {exc}")

        if not resp.is_success:
            time.sleep(_BACKOFF_BASE * attempt)
            continue

        for block in resp.json().get("content", []):
            if block.get("type") == "text":
                return block["text"]

    raise LLMTimeoutError(f"Failed after {_MAX_ATTEMPTS} attempt(s).")


# extracts and parses the json from the raw response
def _parse_response(raw):
    # strip markdown fences if the model wrapped the response
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = raw.find("{"), raw.rfind("}")
        text = raw[start:end + 1] if start != -1 and end > start else raw

    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        raise LLMError(f"Could not parse LLM response: {raw[:300]}")

    missing = {"summary", "technologies", "structure"} - obj.keys()
    if missing:
        raise LLMError(f"Missing key(s): {missing}")

    techs = obj["technologies"]
    if isinstance(techs, str):
        # sometimes the model returns a comma-separated string instead of an array
        techs = [t.strip() for t in techs.split(",") if t.strip()]

    return {"summary": obj["summary"].strip(), "technologies": techs, "structure": obj["structure"].strip()}


# main entry point
def get_summary(context):
    return _parse_response(_call_api(context))
