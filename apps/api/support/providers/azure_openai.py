"""Azure OpenAI adapter.

All provider access goes through the protocols in this module, so the agent and
retrieval layers never import the vendor SDK directly and can be driven by
deterministic fakes in tests.

Authentication is keyless by default: a managed identity in deployment, the
developer's Entra credentials locally. An API key is honoured only when
explicitly configured, as a local fallback.

Note on roles: a subscription Owner still cannot call the inference API.
Azure's Owner role grants control-plane `actions` but no `dataActions`, so the
"Cognitive Services OpenAI User" role must be assigned separately. Both the
app identity and the operator get it in infra/identity.tf.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

from support.config import Settings, get_settings
from support.providers.errors import translate

_COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


@dataclass(frozen=True)
class Usage:
    """Token usage as reported by the provider.

    Fields are optional because not every response carries usage; the drawer
    labels anything missing as unavailable rather than guessing.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    usage: Usage


@dataclass(frozen=True)
class CompletionChunk:
    text: str
    usage: Usage | None = None


@runtime_checkable
class EmbeddingProvider(Protocol):
    async def embed(self, texts: Sequence[str]) -> EmbeddingResult: ...


@runtime_checkable
class ChatProvider(Protocol):
    async def stream(
        self,
        *,
        messages: Sequence[dict[str, object]],
        tools: Sequence[dict[str, object]] | None = None,
        temperature: float = 0.0,
    ) -> AsyncIterator[CompletionChunk]: ...


def _build_client(settings: Settings) -> tuple[AsyncAzureOpenAI, DefaultAzureCredential | None]:
    """Build a client and return the credential alongside it.

    The credential owns an aiohttp session that must be closed explicitly, so
    callers need a handle on it. Returning it here keeps ownership obvious
    rather than leaking "Unclosed client session" at interpreter exit.
    """
    if not settings.azure_openai_endpoint:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT is not configured")

    if settings.azure_openai_api_key:
        # Local fallback only. Deployment uses managed identity.
        client = AsyncAzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
        )
        return client, None

    credential = DefaultAzureCredential(
        managed_identity_client_id=settings.azure_client_id,
        exclude_interactive_browser_credential=True,
    )
    client = AsyncAzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        azure_ad_token_provider=get_bearer_token_provider(credential, _COGNITIVE_SCOPE),
        api_version=settings.azure_openai_api_version,
    )
    return client, credential


class AzureOpenAIEmbeddings:
    """Embeddings via a configured Azure OpenAI deployment.

    Dimensionality is pinned from settings and asserted against what the
    service returns: a model swap that silently changes width would corrupt
    the index and must fail loudly instead.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client, self._credential = _build_client(self._settings)

    @property
    def deployment(self) -> str:
        return self._settings.azure_openai_embedding_deployment

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], usage=Usage())

        expected = self._settings.embedding_dimensions
        try:
            response = await self._client.embeddings.create(
                model=self.deployment,
                input=list(texts),
                dimensions=expected,
            )
        # Broad on purpose: this is the adapter boundary, and an
        # unclassified vendor exception must still arrive as a typed one.
        except Exception as exc:
            raise translate(exc) from exc

        # The API does not guarantee response order; sort by index.
        ordered = sorted(response.data, key=lambda d: d.index)
        vectors = [list(d.embedding) for d in ordered]

        for vec in vectors:
            if len(vec) != expected:
                raise RuntimeError(
                    f"Embedding width {len(vec)} != configured {expected}. "
                    "Rebuild the corpus into a fresh index before changing models."
                )

        usage = Usage(
            input_tokens=getattr(response.usage, "prompt_tokens", None) if response.usage else None
        )
        return EmbeddingResult(vectors=vectors, usage=usage)

    async def aclose(self) -> None:
        await self._client.close()
        if self._credential is not None:
            await self._credential.close()


@dataclass(frozen=True)
class ToolCallRequest:
    """A tool call the model asked for. Arguments are untrusted."""

    call_id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ChatTurn:
    """One model response: either tool calls, or final text."""

    text: str
    tool_calls: tuple[ToolCallRequest, ...]
    usage: Usage


class AzureOpenAIChat:
    """Chat completions with tool calling.

    Deliberately NOT streamed to the client token-by-token. The final answer is
    buffered here so citations can be validated before any text is released --
    see support.agent.runner. Early feedback comes from tool status events,
    which fire within a few hundred milliseconds.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client, self._credential = _build_client(self._settings)

    @property
    def model(self) -> str:
        return self._settings.azure_openai_chat_deployment

    async def complete(
        self,
        *,
        messages: Sequence[dict[str, object]],
        tools: Sequence[dict[str, object]] | None = None,
        temperature: float = 0.0,
        response_format: dict[str, object] | None = None,
    ) -> ChatTurn:
        kwargs: dict[str, object] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = "auto"
        if response_format:
            kwargs["response_format"] = response_format

        # The SDK's create() is heavily overloaded on `stream`; a kwargs dict
        # cannot resolve against those overloads. The call is correct at
        # runtime, so the suppression is scoped to this one vendor boundary.
        try:
            response = await self._client.chat.completions.create(**kwargs)  # type: ignore[call-overload]  # type: ignore[arg-type]
        # Broad on purpose: this is the adapter boundary, and an
        # unclassified vendor exception must still arrive as a typed one.
        except Exception as exc:
            # Every vendor exception is classified here and nowhere else, so
            # the agent loop decides whether to retry from three facts rather
            # than from an SDK class hierarchy.
            raise translate(exc) from exc
        choice = response.choices[0]

        calls: list[ToolCallRequest] = []
        for call in choice.message.tool_calls or []:
            if call.type != "function":
                continue
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                # Malformed arguments are the model's error, not a crash. Pass
                # an empty dict so schema validation produces a clean refusal.
                args = {}
            calls.append(
                ToolCallRequest(
                    call_id=call.id,
                    name=call.function.name,
                    arguments=args if isinstance(args, dict) else {},
                )
            )

        usage = Usage(
            input_tokens=getattr(response.usage, "prompt_tokens", None) if response.usage else None,
            output_tokens=(
                getattr(response.usage, "completion_tokens", None) if response.usage else None
            ),
        )
        return ChatTurn(
            text=choice.message.content or "",
            tool_calls=tuple(calls),
            usage=usage,
        )

    async def aclose(self) -> None:
        await self._client.close()
        if self._credential is not None:
            await self._credential.close()
