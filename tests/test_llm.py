"""Tests for the Groq structured-output layer.

Groq is OpenAI-compatible and model capabilities vary, so the negotiation
between strict `json_schema` and plain `json_object` is real logic that can
silently degrade. These tests pin it with a fake client — no key, no network.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from gtm.llm import LLMUnavailable, _harden, _loads, parse_structured


class Item(BaseModel):
    name: str
    note: str | None = None


class Payload(BaseModel):
    items: list[Item] = []


# --------------------------------------------------------------- fake client


class FakeCompletions:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.calls.append(kwargs)
        fmt = (kwargs.get("response_format") or {}).get("type")
        if fmt in self.owner.unsupported:
            raise RuntimeError("400: response_format json_schema is not supported by this model")
        reply = self.owner.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return type("C", (), {"choices": [
            type("Ch", (), {"message": type("M", (), {"content": reply})()})()
        ]})()


class FakeGroq:
    def __init__(self, replies, unsupported=()):
        self.replies = list(replies)
        self.unsupported = set(unsupported)
        self.calls: list[dict] = []
        self.chat = type("Chat", (), {"completions": FakeCompletions(self)})()


def _parse(client):
    return parse_structured(client, system="s", user="u", schema=Payload, model="m")


# ------------------------------------------------------------------- schema


def test_harden_makes_schema_strict_mode_acceptable():
    """Strict mode rejects objects that allow extra keys or omit `required`."""
    hardened = _harden(Payload.model_json_schema())
    assert hardened["additionalProperties"] is False
    assert set(hardened["required"]) == set(hardened["properties"])

    item = hardened["$defs"]["Item"]
    assert item["additionalProperties"] is False
    # An optional field is still listed as required — it accepts null via anyOf,
    # so requiring the key does not make the field mandatory in substance.
    assert set(item["required"]) == {"name", "note"}


def test_harden_leaves_non_objects_alone():
    assert _harden({"type": "string"}) == {"type": "string"}
    assert _harden([{"type": "integer"}]) == [{"type": "integer"}]


# -------------------------------------------------------------------- parse


def test_plain_json_is_parsed():
    client = FakeGroq(['{"items": [{"name": "a", "note": null}]}'])
    assert _parse(client).items[0].name == "a"


@pytest.mark.parametrize("wrapped", [
    '```json\n{"items": []}\n```',
    '```\n{"items": []}\n```',
    'Here is the JSON you asked for:\n{"items": []}',
    '{"items": []}\n\nLet me know if you need anything else.',
])
def test_markdown_fences_and_preamble_are_stripped(wrapped):
    """Small models wrap JSON in prose no matter how firmly you ask them not to."""
    assert _loads(wrapped) == {"items": []}


def test_falls_back_to_json_object_when_schema_mode_unsupported():
    client = FakeGroq(['{"items": [{"name": "b"}]}'], unsupported={"json_schema"})
    result = _parse(client)
    assert result.items[0].name == "b"
    # First attempt was strict and rejected; second used the looser format.
    assert client.calls[0]["response_format"]["type"] == "json_schema"
    assert client.calls[1]["response_format"]["type"] == "json_object"


def test_invalid_output_triggers_exactly_one_repair_attempt():
    client = FakeGroq(['{"items": [{"note": "missing name"}]}', '{"items": [{"name": "c"}]}'])
    assert _parse(client).items[0].name == "c"
    assert len(client.calls) == 2
    assert "could not be parsed" in client.calls[1]["messages"][1]["content"]


def test_gives_up_with_a_useful_error_rather_than_looping():
    client = FakeGroq(["not json"] * 4)
    with pytest.raises(LLMUnavailable) as exc:
        _parse(client)
    assert "could not obtain valid structured output" in str(exc.value)
    # Two formats x (attempt + one repair) — bounded, never unbounded.
    assert len(client.calls) == 4


def test_real_api_errors_are_raised_not_swallowed():
    """A 401 must surface as itself, not be mistaken for a format problem."""
    client = FakeGroq([RuntimeError("401 invalid api key")])
    with pytest.raises(RuntimeError, match="401"):
        _parse(client)


def test_json_word_present_for_json_mode():
    """Groq's JSON mode requires the word 'json' somewhere in the prompt."""
    client = FakeGroq(['{"items": []}'])
    _parse(client)
    assert "json" in client.calls[0]["messages"][0]["content"].lower()
