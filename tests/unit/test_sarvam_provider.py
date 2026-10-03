"""Reading Sarvam's response — the shape, not the network.

`sarvam-105b` is a reasoning model. It writes a chain of thought into `reasoning_content` and
only then writes the answer into `content`, and both are billed as completion tokens. Two things
follow, and both shipped wrong first:

- `content` is **null, not absent**, when the budget ran out mid-reasoning. `body[...]["content"]`
  returned None, `text or ""` turned it into an empty string, and the endpoint returned HTTP 200
  with a full token bill and an empty answer box. The one thing worse than no answer is no
  answer that looks like one.
- The reasoning is discarded rather than shown. It carries no citations and is not grounded in
  the passages the way the answer is required to be, so presenting it beside a cited answer
  invites it being read as one.

httpx is stubbed rather than called: what is under test is the parsing, and a test that needs
an API key and a network is a test that gets skipped.
"""

import pytest

from src.core import config
from src.modules.generation.provider import GenerationError, SarvamProvider


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def reply(content=None, reasoning=None, finish="stop", prompt=100, completion=400):
    return {
        "choices": [{
            "finish_reason": finish,
            "message": {
                "role": "assistant",
                "content": content,
                "reasoning_content": reasoning,
                "refusal": None,
                "tool_calls": None,
            },
        }],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(config.settings, "SARVAM_API_KEY", "test-key")
    p = SarvamProvider(api_key="test-key")
    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent["url"] = url
        sent["payload"] = json
        sent["headers"] = headers
        return sent["response"]

    import src.modules.generation.provider as mod
    monkeypatch.setattr(mod.httpx, "post", fake_post)
    p._sent = sent
    return p


# ==============================================================================
# The empty-answer bug
# ==============================================================================

def test_a_null_content_is_an_error_not_an_empty_answer(provider):
    """The bug as it shipped: HTTP 200, 1,024 completion tokens billed, and nothing in the
    answer box."""
    provider._sent["response"] = FakeResponse(
        reply(content=None, reasoning="Let me think about this at length...",
              finish="length", completion=1024)
    )

    with pytest.raises(GenerationError) as e:
        provider.complete("sys", "user")

    assert "budget" in str(e.value).lower()
    assert "GENERATION_MAX_TOKENS" in str(e.value), "the message must name the fix"


def test_an_empty_answer_without_truncation_is_also_an_error(provider):
    provider._sent["response"] = FakeResponse(reply(content="", finish="stop"))

    with pytest.raises(GenerationError):
        provider.complete("sys", "user")


def test_a_real_answer_comes_back_with_its_token_counts(provider):
    provider._sent["response"] = FakeResponse(
        reply(content="The extension is 36 months [1].",
              reasoning="thinking...", prompt=4373, completion=419)
    )

    text, in_tokens, out_tokens = provider.complete("sys", "user")

    assert text == "The extension is 36 months [1]."
    assert (in_tokens, out_tokens) == (4373, 419)


def test_the_reasoning_is_not_returned_as_the_answer(provider):
    """It carries no citations and is not grounded in the passages. Returned beside a cited
    answer it would read as one."""
    provider._sent["response"] = FakeResponse(
        reply(content="36 months [1].", reasoning="The user wants... I should check...")
    )

    text, _i, _o = provider.complete("sys", "user")

    assert "The user wants" not in text
    assert text == "36 months [1]."


# ==============================================================================
# What is asked for
# ==============================================================================

def test_reasoning_effort_is_sent(provider, monkeypatch):
    """Not a cosmetic setting: at default effort the model spent 2,682 completion tokens to
    write the same fourteen-character answer it wrote with 419 at "low"."""
    monkeypatch.setattr(config.settings, "GENERATION_REASONING_EFFORT", "low")
    provider._sent["response"] = FakeResponse(reply(content="ok"))

    provider.complete("sys", "user")

    assert provider._sent["payload"]["reasoning_effort"] == "low"


def test_both_auth_headers_are_sent(provider):
    """Sarvam is OpenAI-shaped but not OpenAI-compatible: it wants the key in its own header as
    well as the bearer token, and omitting either is a 401."""
    provider._sent["response"] = FakeResponse(reply(content="ok"))

    provider.complete("sys", "user")

    h = provider._sent["headers"]
    assert h["Authorization"] == "Bearer test-key"
    assert h["api-subscription-key"] == "test-key"


def test_no_api_key_is_a_clear_refusal(monkeypatch):
    """Retrieval still works without a key, and the message should say so rather than read as
    a broken deployment."""
    monkeypatch.setattr(config.settings, "SARVAM_API_KEY", "")
    p = SarvamProvider(api_key="")

    with pytest.raises(GenerationError) as e:
        p.complete("sys", "user")

    assert "retrieval still works" in str(e.value).lower()


# ==============================================================================
# Cost
# ==============================================================================

def test_cost_is_present_in_the_serialised_answer():
    """A bare @property is invisible to pydantic, so this was absent from every API response
    while looking correct in Python — the UI read it as missing and showed nothing."""
    from src.modules.generation.models import GeneratedAnswer

    payload = GeneratedAnswer(question="q", input_tokens=4373, output_tokens=419).model_dump()

    assert "cost_inr" in payload
    assert payload["cost_inr"] == pytest.approx((4373 / 1e6) * 29.28 + (419 / 1e6) * 73.2)
