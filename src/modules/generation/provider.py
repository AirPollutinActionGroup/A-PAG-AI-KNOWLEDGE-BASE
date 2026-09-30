"""Answer providers — the boundary where text leaves the deployment.

Every other stage of this system runs on hardware A-PAG controls. This one does not: the question
and the retrieved passages are sent to Sarvam's API. That is a deliberate choice and the reason
Sarvam was chosen over OpenAI or Anthropic — an Indian provider, with the data staying in India,
for an Indian policy organisation. It is still worth naming plainly, because `RESTRICTED`
documents can reach this boundary and `GENERATION_INCLUDE_RESTRICTED` is the switch that decides
whether they do.

`AnswerProvider` exists for the same reason `EmbeddingProvider` does: the model is a configuration
decision. Sarvam deprecated both Sarvam-M and Sarvam-30B within a year, so the provider being
swappable is not hypothetical — it already happened once before a line of this was written.
"""

import logging
from abc import ABC, abstractmethod

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)


class GenerationError(RuntimeError):
    """The model could not be reached or refused the request."""


class AnswerProvider(ABC):
    """Turns a prompt into text. Knows nothing about documents, retrieval or citations."""

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @abstractmethod
    def complete(self, system: str, user: str) -> tuple[str, int, int]:
        """Returns (text, input_tokens, output_tokens)."""


class SarvamProvider(AnswerProvider):
    """Sarvam's chat completions API.

    Two headers, not one: `Authorization: Bearer` *and* `api-subscription-key`. The request body
    follows OpenAI's shape closely enough to read like it, which is a trap — it is a separate
    implementation, so an OpenAI SDK is not a drop-in and is deliberately not used here.
    """

    ENDPOINT = "https://api.sarvam.ai/v1/chat/completions"

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str | None = None,
        timeout: float | None = None,
    ):
        self._api_key = api_key or settings.SARVAM_API_KEY
        self._model = model_name or settings.SARVAM_MODEL
        self._timeout = timeout or settings.SARVAM_TIMEOUT_SECONDS

    @property
    def model_name(self) -> str:
        return self._model

    def complete(self, system: str, user: str) -> tuple[str, int, int]:
        if not self._api_key:
            raise GenerationError(
                "SARVAM_API_KEY is not set. Answer generation is disabled without it; "
                "retrieval still works and returns passages."
            )

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Low, not zero. This task is extraction and summary over supplied text, where
            # invention is the failure mode; there is nothing to gain from sampling variety.
            "temperature": settings.GENERATION_TEMPERATURE,
            "max_tokens": settings.GENERATION_MAX_TOKENS,
            # sarvam-105b reasons before it answers, and the reasoning is billed. Left at the
            # default it spent 2,682 completion tokens to produce a fourteen-character answer;
            # at "low" it spent 419 for the same one. See GENERATION_REASONING_EFFORT.
            "reasoning_effort": settings.GENERATION_REASONING_EFFORT,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "api-subscription-key": self._api_key,
            "Content-Type": "application/json",
        }

        try:
            response = httpx.post(
                self.ENDPOINT, json=payload, headers=headers, timeout=self._timeout
            )
        except httpx.RequestError as e:
            raise GenerationError(f"Could not reach Sarvam: {e}") from e

        if response.status_code == 401:
            raise GenerationError("Sarvam rejected the API key.")
        if response.status_code == 429:
            raise GenerationError("Sarvam rate limit reached. Try again shortly.")
        if response.status_code >= 400:
            raise GenerationError(
                f"Sarvam returned {response.status_code}: {response.text[:200]}"
            )

        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError) as e:
            raise GenerationError(f"Unexpected response shape from Sarvam: {e}") from e

        # `content` is null, not absent, when the model ran out of budget mid-reasoning.
        text = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        finish = choice.get("finish_reason")
        usage = body.get("usage") or {}
        completion = int(usage.get("completion_tokens", 0))

        if not text:
            # This shipped once as a silent empty string: a 200 response, a full token bill, and
            # an answer box with nothing in it. An empty answer must be an error, because the
            # one thing worse than no answer is no answer that looks like one.
            if finish == "length":
                raise GenerationError(
                    f"The model used its entire {settings.GENERATION_MAX_TOKENS}-token budget "
                    f"reasoning and never wrote an answer. Raise GENERATION_MAX_TOKENS or lower "
                    f"GENERATION_REASONING_EFFORT."
                )
            raise GenerationError(
                f"Sarvam returned no answer text (finish_reason={finish!r})."
            )

        # The reasoning is the model's scratchpad and is deliberately discarded: it is not
        # grounded in the passages the way the answer is required to be, it carries no
        # citations, and showing it beside a cited answer invites it being read as one.
        if reasoning and completion:
            logger.info(
                "Sarvam: %d completion tokens, of which ~%d were reasoning (%d chars) for a "
                "%d-char answer",
                completion, max(0, completion - len(text) // 4), len(reasoning), len(text),
            )

        return text, int(usage.get("prompt_tokens", 0)), completion
