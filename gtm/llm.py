"""Anthropic client wiring.

Two deliberate choices:

* **Structured outputs, not hand-parsed JSON.** `client.messages.parse()` with a
  Pydantic schema means the model's output is validated before it reaches us.
  A malformed extraction fails loudly at the boundary instead of silently
  producing a claim with a missing quote.

* **The stable prefix is cached.** The extraction system prompt is byte-identical
  across every document in the portfolio, so it carries a cache breakpoint. With
  ~70 documents that is the difference between paying for the instructions once
  and paying for them seventy times.
"""

from __future__ import annotations

import os
from typing import Any, TypeVar

from pydantic import BaseModel

# Default to the most capable model. Extraction is high-volume, so a cheaper
# model is a defensible choice -- but that is a cost decision for the operator,
# not one to make silently on their behalf. Override with GTM_MODEL.
DEFAULT_MODEL = os.getenv("GTM_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.getenv("GTM_MAX_TOKENS", "16000"))

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """Raised when no Anthropic credential can be resolved.

    Kept distinct from other errors so callers can degrade gracefully: the
    ingest and metrics layers work fine without an LLM, and the CLI should say
    'extraction is unavailable' rather than crash the whole run.
    """


def get_client():
    """Build an Anthropic client.

    The SDK resolves credentials in order: ANTHROPIC_API_KEY, then
    ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile on disk. A bare
    constructor is therefore correct -- do not pass an explicit key.
    """
    try:
        import anthropic
    except ImportError as exc:  # noqa: TRY003
        raise LLMUnavailable(
            "the `anthropic` package is not installed -- `uv pip install anthropic`"
        ) from exc

    try:
        return anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001
        raise LLMUnavailable(f"could not construct an Anthropic client: {exc}") from exc


def credentials_available() -> bool:
    """Cheap pre-flight so the CLI can report a clear message instead of failing
    part-way through a fan-out."""
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return True
    from pathlib import Path

    config = Path(os.getenv("ANTHROPIC_CONFIG_DIR", Path.home() / ".config" / "anthropic"))
    return (config / "credentials").is_dir()


def parse_structured(
    client: Any,
    *,
    system: str,
    user: str,
    schema: type[T],
    model: str | None = None,
    max_tokens: int | None = None,
    cache_system: bool = True,
) -> T:
    """One structured-output call. Returns a validated instance of `schema`.

    `system` is sent as a cacheable block when `cache_system` is set, so a
    repeated prefix across many documents is billed once at write and then at
    read rates.
    """
    system_param: Any = system
    if cache_system:
        system_param = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]

    response = client.messages.parse(
        model=model or DEFAULT_MODEL,
        max_tokens=max_tokens or MAX_TOKENS,
        system=system_param,
        messages=[{"role": "user", "content": user}],
        output_format=schema,
    )

    # A refusal returns HTTP 200 with an empty/partial body. Reading
    # parsed_output blindly would surface as a confusing AttributeError.
    if getattr(response, "stop_reason", None) == "refusal":
        raise LLMUnavailable("the model declined this request")

    return response.parsed_output
