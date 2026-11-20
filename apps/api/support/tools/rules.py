"""Structured policy rules.

Separating enforcement from prose is deliberate. The Markdown corpus is what
the customer reads and what gets cited; this table is what the action service
enforces. Keeping them apart means the cap is never parsed out of English at
runtime — and a test asserts the two agree, so they cannot drift.

Rules are keyed by (doc_key, version, tier, action) so a refund committed today
records exactly which rule authorised it, and a later policy revision cannot
retroactively change what an existing receipt means.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from support.config import get_settings
from support.contracts.enums import Tier


@dataclass(frozen=True)
class RefundRule:
    doc_key: str
    version: str
    tier: Tier
    per_request_cap_minor: int
    currency: str
    requires_active_contract: bool
    time_limit_days: int
    time_limit_exceptions: tuple[str, ...]
    eligible_reasons: tuple[str, ...]
    active: bool

    @property
    def cap_display(self) -> str:
        return f"${self.per_request_cap_minor / 100:.2f} {self.currency}"


def rules_path() -> Path:
    settings = get_settings()
    if settings.fixtures_dir:
        return Path(settings.fixtures_dir) / "policy_rules.json"
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "fixtures" / "policy_rules.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Could not locate fixtures/policy_rules.json")


@lru_cache
def load_refund_rules() -> tuple[RefundRule, ...]:
    data = json.loads(rules_path().read_text(encoding="utf-8"))
    out: list[RefundRule] = []
    for row in data["rules"]:
        if row.get("action") != "refund":
            continue
        out.append(
            RefundRule(
                doc_key=row["doc_key"],
                version=row["version"],
                tier=Tier(row["tier"]),
                per_request_cap_minor=int(row["per_request_cap_minor"]),
                currency=row["currency"],
                requires_active_contract=bool(row["requires_active_contract"]),
                time_limit_days=int(row["time_limit_days"]),
                time_limit_exceptions=tuple(row.get("time_limit_exceptions", ())),
                eligible_reasons=tuple(row.get("eligible_reasons", ())),
                # Defaults to active; the superseded row sets it false.
                active=bool(row.get("active", True)),
            )
        )
    return tuple(out)


def active_refund_rule(tier: Tier) -> RefundRule:
    """The one active refund rule for a tier.

    Raises if zero or more than one match: an ambiguous ruleset must fail
    closed rather than pick arbitrarily, because picking wrong means approving
    a refund under the wrong cap.
    """
    matches = [r for r in load_refund_rules() if r.tier is tier and r.active]
    if not matches:
        raise LookupError(f"no active refund rule for tier {tier.value}")
    if len(matches) > 1:
        versions = ", ".join(f"{r.doc_key}@{r.version}" for r in matches)
        raise LookupError(f"ambiguous active refund rules for {tier.value}: {versions}")
    return matches[0]
