"""Groq client wiring.

Groq is OpenAI-compatible, which shapes two decisions here:

* **Structured output is negotiated, not assumed.** Groq model support for
  `json_schema` response format varies by model; `json_object` mode is near
  universal. `parse_structured` tries the strict path, falls back to JSON mode,
  and validates the result with Pydantic either way. The validation is the
  guarantee — the response format is just an optimization.

* **One retry on a validation failure, with the error fed back.** Smaller models
  occasionally emit a near-miss (a wrong enum value, a stray markdown fence).
  Retrying once with the specific complaint recovers most of those. It is capped
  at one: unbounded self-correction loops eat the clock and rarely converge.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

# Groq default. Override with GTM_MODEL. `gtm models` lists what this key can
# actually reach, so you are not guessing at an ID that may have been retired.
DEFAULT_MODEL = os.getenv("GTM_MODEL", "llama-3.3-70b-versatile")
MAX_TOKENS = int(os.getenv("GTM_MAX_TOKENS", "8000"))
TEMPERATURE = float(os.getenv("GTM_TEMPERATURE", "0"))

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class LLMUnavailable(RuntimeError):
    """No usable Groq credential or client.

    Distinct from other failures so callers can degrade gracefully: ingest and
    metrics work fine without an LLM, and the CLI should say "extraction is
    unavailable" rather than crash the run.
    """


def credentials_available() -> bool:
    return bool(os.getenv("GROQ_API_KEY"))


def get_client():
    if not credentials_available():
        raise LLMUnavailable("GROQ_API_KEY is not set — add it to .env")
    try:
        from groq import Groq
    except ImportError as exc:  # noqa: TRY003
        raise LLMUnavailable("the `groq` package is not installed — `uv pip install groq`") from exc
    try:
        return Groq(api_key=os.environ["GROQ_API_KEY"])
    except Exception as exc:  # noqa: BLE001
        raise LLMUnavailable(f"could not construct a Groq client: {exc}") from exc


def list_models(client=None) -> list[str]:
    """What this key can actually reach. Beats guessing at a model ID."""
    resolved = client or get_client()
    return sorted(m.id for m in resolved.models.list().data)


# --------------------------------------------------------------------------
# Schema handling
# --------------------------------------------------------------------------


def _harden(node: Any) -> Any:
    """Make a Pydantic JSON schema acceptable to strict `json_schema` mode.

    Strict mode requires every object to forbid extra properties and to list
    every property as required. Pydantic emits neither, so walk the tree and add
    them. Optional fields still accept null via their `anyOf`, so requiring the
    key does not make an optional field mandatory in substance.
    """
    if isinstance(node, dict):
        out = {k: _harden(v) for k, v in node.items()}
        if out.get("type") == "object" and isinstance(out.get("properties"), dict):
            out["additionalProperties"] = False
            out["required"] = list(out["properties"].keys())
        return out
    if isinstance(node, list):
        return [_harden(v) for v in node]
    return node


def _schema_prompt(schema: type[BaseModel]) -> str:
    return (
        "Return a single JSON object matching this JSON Schema exactly. "
        "Output JSON and nothing else — no prose, no markdown fences.\n\n"
        f"{json.dumps(_harden(schema.model_json_schema()), indent=2)}"
    )


def _loads(text: str) -> Any:
    """Parse model output that may be wrapped in a markdown fence or padded with
    a sentence of preamble."""
    cleaned = _FENCE.sub("", text or "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = min((i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1), default=-1)
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if start == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


# --------------------------------------------------------------------------


def _call(client, *, model: str, system: str, user: str, max_tokens: int,
          response_format: dict | None) -> str:
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": TEMPERATURE,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if response_format:
        kwargs["response_format"] = response_format
    completion = client.chat.completions.create(**kwargs)
    return completion.choices[0].message.content or ""


def _unsupported_format(exc: Exception) -> bool:
    """Distinguish 'this model cannot do json_schema' from a real failure, so we
    fall back on the former and surface the latter."""
    text = str(exc).lower()
    return any(s in text for s in
               ("response_format", "json_schema", "json schema", "not supported",
                "unsupported", "invalid_type"))


def parse_structured(
    client: Any,
    *,
    system: str,
    user: str,
    schema: type[T],
    model: str | None = None,
    max_tokens: int | None = None,
) -> T:
    """One structured call. Returns a validated instance of `schema`.

    Tries strict `json_schema`, falls back to `json_object`, and validates with
    Pydantic in both cases — so a model that ignores the format hint still
    cannot produce an invalid claim.
    """
    model = model or DEFAULT_MODEL
    max_tokens = max_tokens or MAX_TOKENS

    # JSON mode requires the word "json" to appear in the prompt.
    json_system = f"{system}\n\n{_schema_prompt(schema)}"

    attempts: list[tuple[str, dict | None]] = [
        (json_system, {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": _harden(schema.model_json_schema()),
                "strict": True,
            },
        }),
        (json_system, {"type": "json_object"}),
    ]

    last_error: Exception | None = None
    raw = ""

    for system_text, response_format in attempts:
        try:
            raw = _call(client, model=model, system=system_text, user=user,
                        max_tokens=max_tokens, response_format=response_format)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if _unsupported_format(exc):
                continue  # try the next, looser format
            raise

        try:
            return schema.model_validate(_loads(raw))
        except (ValidationError, json.JSONDecodeError) as exc:
            last_error = exc
            # One repair attempt, with the specific complaint fed back.
            repair = (
                f"{user}\n\nYour previous reply could not be parsed:\n{exc}\n\n"
                "Return corrected JSON matching the schema. JSON only."
            )
            try:
                raw = _call(client, model=model, system=system_text, user=repair,
                            max_tokens=max_tokens, response_format=response_format)
                return schema.model_validate(_loads(raw))
            except Exception as exc2:  # noqa: BLE001
                last_error = exc2
                continue

    raise LLMUnavailable(
        f"could not obtain valid structured output from {model}: {last_error}\n"
        f"last raw response: {raw[:400]!r}"
    )
