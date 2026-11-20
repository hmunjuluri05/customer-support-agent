"""Provider failures, translated at the adapter boundary.

The vendor SDK's exception types stop here. Everything above this line reasons
about three questions only -- is it worth retrying, how long should we wait,
and what may the customer be told -- which is all the runner needs and all the
UI can act on. It also means swapping a provider does not ripple into the
agent loop.

`safe_message` is customer-facing: no endpoint names, no request ids, no
account state.
"""

from __future__ import annotations

from typing import Any


class ProviderError(Exception):
    """A call to a model provider failed."""

    error_code = "provider_error"
    retryable = False

    def __init__(self, safe_message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.retry_after_s = retry_after_s


class ProviderTimeout(ProviderError):
    """The provider did not answer in time."""

    error_code = "llm_timeout"
    retryable = True


class ProviderRateLimited(ProviderError):
    """The provider rejected the call for rate or quota reasons.

    Carries the provider's own `Retry-After` when it sent one: guessing a
    backoff when the service has told us the number is how a client turns a
    throttle into an outage.
    """

    error_code = "llm_rate_limited"
    retryable = True


class ProviderUnavailable(ProviderError):
    """A transient server-side or connection failure."""

    error_code = "llm_unavailable"
    retryable = True


class ProviderRejected(ProviderError):
    """The provider refused the request itself -- a bad request, or a filter.

    Not retryable: the same call will be refused again. Sent as its own class
    so a retry loop cannot spin on a permanent condition.
    """

    error_code = "llm_rejected"
    retryable = False


_TIMEOUT_NAMES = {"APITimeoutError"}
_RATE_LIMIT_NAMES = {"RateLimitError"}
_TRANSIENT_NAMES = {
    "APIConnectionError",
    "APIConnectionTimeoutError",
    "InternalServerError",
}


def translate(exc: Exception) -> ProviderError:
    """Map an SDK exception onto this module's vocabulary.

    Matched by class name rather than by importing the SDK's exception
    hierarchy: the openai package reorganises those between majors, and an
    ImportError at module load would take down the whole API to report that a
    name moved. An unrecognised exception becomes a non-retryable
    ProviderError, which fails closed -- a retry loop that cannot classify a
    failure should not assume it is temporary.
    """
    name = type(exc).__name__
    retry_after = _retry_after(exc)

    if name in _TIMEOUT_NAMES:
        return ProviderTimeout(
            "The model took too long to respond. Nothing was committed.",
            retry_after_s=retry_after,
        )
    if name in _RATE_LIMIT_NAMES:
        return ProviderRateLimited(
            "The model service is rate limiting us right now. Nothing was committed.",
            retry_after_s=retry_after,
        )
    if name in _TRANSIENT_NAMES:
        return ProviderUnavailable(
            "The model service is temporarily unavailable. Nothing was committed.",
            retry_after_s=retry_after,
        )

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status == 429:
            return ProviderRateLimited(
                "The model service is rate limiting us right now. Nothing was committed.",
                retry_after_s=retry_after,
            )
        if status >= 500:
            return ProviderUnavailable(
                "The model service is temporarily unavailable. Nothing was committed.",
                retry_after_s=retry_after,
            )
        if 400 <= status < 500:
            return ProviderRejected("The model service refused that request.")

    return ProviderError("The model service failed on that request.")


def _retry_after(exc: Exception) -> float | None:
    """The provider's own backoff instruction, if it sent one."""
    response: Any = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        # Seconds form only. The HTTP-date form is legal but no provider in
        # use sends it, and a wrong parse here would mean a wrong sleep.
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return None
