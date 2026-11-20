"""Model-supplied tool arguments.

Everything here arrives from the model and is therefore untrusted. Two rules:

1. **Schema-validated before use.** `extra="forbid"` so a hallucinated extra
   field is a hard error, not silently ignored.
2. **No authority fields.** There is no `tier`, no `customer_id`, no
   `contract_status`, no verification secret in any of these models. Those come
   from server state only. A model cannot assert who the customer is or what
   they are entitled to, because there is nowhere to put the claim.

Note what `verify_identity` takes: nothing. The account is bound to the session
by the UI sign-in control, so the demo verification secret never enters model
context or traces.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from support.contracts.enums import EscalationReason


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VerifyIdentityArgs(_Args):
    """No arguments by design.

    The verified account is resolved from the session binding. A chat message
    claiming an account or tier is not evidence of anything.
    """


class RetrievePolicyArgs(_Args):
    topic: str = Field(
        min_length=3,
        max_length=400,
        description=(
            "A complete natural-language question, as a customer would ask it. "
            "The tier used for filtering comes from server state, not from here."
        ),
    )
    """Query shape matters more than it looks.

    The grounding threshold is calibrated on fixtures/eval/queries.json, which
    holds natural-language questions. A terse keyword topic embeds differently
    and scores materially lower against the same documents -- measured on the
    Premium refund policy: 'refund double charge' 0.356 (below tau, 0 chunks
    accepted) against 0.499 for 'Can I get a refund for a double charge on
    invoice INV-1001?' (5 chunks). So the tool definition instructs the model
    to pass a question, which keeps production queries in the distribution the
    threshold was actually calibrated on.

    This is deliberately not enforced with a length floor or a rejection: a
    poor query degrades to WEAK and the turn escalates, which is the correct
    fail-closed behaviour rather than a tool error. Appending the customer's
    raw utterance was measured as an alternative and rejected -- it reaches
    only 0.405 with 2 chunks, well short of a properly phrased question.
    """


class ExecuteMockRefundArgs(_Args):
    invoice_ref: str = Field(
        min_length=3,
        max_length=32,
        description="The customer-visible invoice reference, e.g. INV-1001.",
    )
    amount_minor: int = Field(
        gt=0,
        le=100_000_000,
        description=(
            "Refund amount in integer minor units (US cents). 1500 means $15.00. Never a decimal."
        ),
    )
    reason: str = Field(
        min_length=3,
        max_length=64,
        description="Eligible refund reason, e.g. billing_error, duplicate_charge.",
    )

    @field_validator("invoice_ref")
    @classmethod
    def _normalise_ref(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("reason")
    @classmethod
    def _normalise_reason(cls, v: str) -> str:
        return v.strip().lower().replace(" ", "_").replace("-", "_")


class CreateEscalationTicketArgs(_Args):
    reason: EscalationReason = Field(description="Structured escalation reason code.")
    summary: str = Field(
        min_length=10,
        max_length=1000,
        description="Safe summary of the request for the human specialist.",
    )
