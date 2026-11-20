"""Azure OpenAI Realtime, for the voice path.

Two calls, in this order:

1. ``POST {endpoint}/openai/v1/realtime/client_secrets`` with an Entra bearer
   token mints an ephemeral key (``ek_…``) and fixes the session's
   configuration -- instructions, tools, voice, transcription, turn detection.
2. ``POST {endpoint}/openai/v1/realtime/calls`` with *that ephemeral key* and
   ``Content-Type: application/sdp`` exchanges the browser's SDP offer for an
   answer. After this the browser talks to Azure directly over WebRTC.

Both were verified against the live resource before any of this was written.
The important finding is the second one's auth:

    Entra bearer  -> 401 "This operation requires ephemeral tokens for
                     authentication."
    ephemeral key -> 400 "Offer did not have an audio media section."

so the SDP exchange *only* accepts an ephemeral key, and a wrong URL is
distinguishable because it 404s instead of complaining about the offer.

The preview surface (``/openai/realtime/...`` with an ``api-version``) is gone
and returns 404; this module uses the GA ``/openai/v1/...`` paths, which take
no api-version parameter.

**Why the SDP exchange is proxied rather than done from the browser.** The
ephemeral key is a bearer credential for our Azure resource. Handing it to the
browser would let a visitor open their own realtime session against it, with
their own instructions and their own tool list, billed to us. Proxying keeps
it server-side: the browser sends an offer, receives an answer, and never holds
anything it could replay. The media path is still browser-to-Azure, so this
costs one request at setup and nothing per utterance.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from azure.identity.aio import DefaultAzureCredential

from support.config import Settings, get_settings

logger = logging.getLogger("support.realtime")

_COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"

# Generous: SDP exchange is a single round trip, but it sets up media.
_TIMEOUT_S = 20.0


class RealtimeUnavailable(RuntimeError):
    """The realtime service refused. Carries a safe message for the UI."""

    def __init__(self, safe_message: str, status: int | None = None) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.status = status


class AzureRealtime:
    """Mints ephemeral sessions and proxies SDP.

    Holds its own credential and HTTP client because the OpenAI SDK does not
    cover the WebRTC endpoints; both are closed by ``aclose``.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        if not self._settings.azure_openai_endpoint:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT is not configured")

        self._endpoint = self._settings.azure_openai_endpoint.rstrip("/")
        self._client = httpx.AsyncClient(timeout=_TIMEOUT_S)
        self._api_key = self._settings.azure_openai_api_key
        self._credential = (
            None
            if self._api_key
            else DefaultAzureCredential(
                managed_identity_client_id=self._settings.azure_client_id,
                exclude_interactive_browser_credential=True,
            )
        )

    @property
    def deployment(self) -> str:
        return self._settings.azure_openai_realtime_deployment

    async def _authorization(self) -> dict[str, str]:
        """Entra bearer, or an api-key header in local development."""
        if self._api_key:
            return {"api-key": self._api_key}
        assert self._credential is not None
        token = await self._credential.get_token(_COGNITIVE_SCOPE)
        return {"Authorization": f"Bearer {token.token}"}

    async def mint_client_secret(self, session_config: dict[str, Any]) -> dict[str, Any]:
        """Create an ephemeral realtime session.

        `session_config` is applied server-side at mint time so the browser
        cannot choose the agent's instructions or widen its tool list. The
        executor would refuse an illegal call anyway, but there is no reason to
        let the client propose one.
        """
        headers = {**(await self._authorization()), "Content-Type": "application/json"}
        body = {"session": {"type": "realtime", "model": self.deployment, **session_config}}

        try:
            response = await self._client.post(
                f"{self._endpoint}/openai/v1/realtime/client_secrets",
                headers=headers,
                json=body,
            )
        except httpx.HTTPError as exc:
            logger.warning("realtime client_secrets transport error: %s", exc)
            raise RealtimeUnavailable("Could not reach the voice service.") from exc

        if response.status_code != 200:
            # The body can echo configuration, so it is logged and not returned.
            logger.warning(
                "realtime client_secrets failed status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            raise RealtimeUnavailable(
                "The voice service refused to start a session.", response.status_code
            )

        payload: dict[str, Any] = response.json()
        secret = payload.get("value")
        if not isinstance(secret, str) or not secret:
            raise RealtimeUnavailable("The voice service returned no session key.")
        return payload

    async def exchange_sdp(self, offer_sdp: str, *, ephemeral_key: str) -> str:
        """Swap the browser's SDP offer for Azure's answer.

        Only an ephemeral key is accepted here; an Entra token returns 401.
        """
        try:
            response = await self._client.post(
                f"{self._endpoint}/openai/v1/realtime/calls",
                headers={
                    "Authorization": f"Bearer {ephemeral_key}",
                    "Content-Type": "application/sdp",
                },
                content=offer_sdp.encode("utf-8"),
            )
        except httpx.HTTPError as exc:
            logger.warning("realtime SDP transport error: %s", exc)
            raise RealtimeUnavailable("Could not reach the voice service.") from exc

        if response.status_code not in (200, 201):
            logger.warning(
                "realtime SDP exchange failed status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            raise RealtimeUnavailable(
                "The voice service rejected the connection offer.", response.status_code
            )

        answer = response.text
        if "m=audio" not in answer:
            # A 200 with no audio section would fail silently in the browser as
            # a connection that never carries sound.
            raise RealtimeUnavailable("The voice service returned an unusable answer.")
        return answer

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._credential is not None:
            await self._credential.close()
