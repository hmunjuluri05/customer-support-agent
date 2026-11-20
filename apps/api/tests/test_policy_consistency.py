"""Consistency between enforced rules and the prose customers are shown.

The structured ruleset in fixtures/policy_rules.json is what the action
service enforces; the Markdown corpus is what gets cited and quoted. If those
drift, the agent cites a $25 cap while enforcing $10 — a failure that no
amount of model quality can fix and that no normal test would catch.

These tests make the two files each other's assertion.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from support.contracts.enums import Tier
from support.retrieval.ingest import policies_dir
from support.tools.rules import active_refund_rule, load_refund_rules


def _doc_text(doc_key: str, version: str) -> str:
    for path in sorted(policies_dir().glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        if f"doc_key: {doc_key}" in raw and f"version: {version}" in raw:
            return raw
    raise AssertionError(f"no policy document for {doc_key}@{version}")


@pytest.mark.parametrize("rule", load_refund_rules(), ids=lambda r: f"{r.doc_key}@{r.version}")
def test_cap_matches_prose(rule) -> None:
    """The enforced cap must appear verbatim in the document."""
    text = _doc_text(rule.doc_key, rule.version)
    expected = f"${rule.per_request_cap_minor / 100:.2f} {rule.currency} per request"
    assert expected in text, (
        f"{rule.doc_key}@{rule.version} enforces {expected} but the document does not say so"
    )


@pytest.mark.parametrize("rule", load_refund_rules(), ids=lambda r: f"{r.doc_key}@{r.version}")
def test_time_limit_matches_prose(rule) -> None:
    text = _doc_text(rule.doc_key, rule.version)
    assert f"{rule.time_limit_days} days" in text


@pytest.mark.parametrize("rule", load_refund_rules(), ids=lambda r: f"{r.doc_key}@{r.version}")
def test_contract_requirement_matches_prose(rule) -> None:
    if not rule.requires_active_contract:
        return
    text = _doc_text(rule.doc_key, rule.version).lower()
    assert "active" in text and "contract" in text


def test_exactly_one_active_rule_per_tier() -> None:
    """Ambiguity here would mean approving refunds under an arbitrary cap."""
    for tier in Tier:
        rule = active_refund_rule(tier)
        assert rule.active
        assert rule.tier is tier


def test_premium_cap_exceeds_basic_cap() -> None:
    """The fixture design depends on this ordering.

    If it inverted, the headline demo -- same amount, different outcome by
    tier -- would stop discriminating.
    """
    premium = active_refund_rule(Tier.PREMIUM)
    basic = active_refund_rule(Tier.BASIC)
    assert premium.per_request_cap_minor > basic.per_request_cap_minor


def test_superseded_rule_is_inactive_and_more_permissive() -> None:
    """The regression canary.

    The superseded Premium rule allows $50 against the current $25. If the
    active-version filter breaks, a $40 approval becomes possible, and this
    documents why that number is the tell.
    """
    superseded = [r for r in load_refund_rules() if not r.active]
    assert superseded, "the canary rule has been removed"
    for rule in superseded:
        active = active_refund_rule(rule.tier)
        assert rule.per_request_cap_minor > active.per_request_cap_minor
        assert rule.version != active.version


def test_outage_credit_is_premium_only() -> None:
    """Asserted in both the rules and the prose."""
    premium = active_refund_rule(Tier.PREMIUM)
    basic = active_refund_rule(Tier.BASIC)

    assert "service_outage" in premium.eligible_reasons
    assert "service_outage" not in basic.eligible_reasons

    basic_text = _doc_text(basic.doc_key, basic.version).lower()
    assert "not" in basic_text and "outage" in basic_text


def test_every_policy_document_has_required_frontmatter() -> None:
    required = ("doc_key", "title", "version", "active")
    for path in sorted(policies_dir().glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        head = raw.split("---")[1] if raw.startswith("---") else ""
        for field in required:
            assert re.search(rf"^{field}:", head, re.MULTILINE), f"{path.name} missing {field}"


def test_only_one_active_version_per_doc_key() -> None:
    """Two active versions would make retrieval nondeterministic."""
    active: dict[str, str] = {}
    for path in sorted(policies_dir().glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        if "active: true" not in raw:
            continue
        doc_key = re.search(r"^doc_key:\s*(\S+)", raw, re.MULTILINE)
        version = re.search(r"^version:\s*(\S+)", raw, re.MULTILINE)
        assert doc_key and version
        key, ver = doc_key.group(1), version.group(1)
        assert key not in active, f"{key} active at both {active[key]} and {ver}"
        active[key] = ver


def test_heldout_topic_absent_from_corpus() -> None:
    """Shipping hardware to a new address must not be answerable.

    If someone adds a shipping policy, the unsupported-query demo silently
    stops demonstrating anything.
    """
    for path in sorted(policies_dir().glob("*.md")):
        text = path.read_text(encoding="utf-8").lower()
        assert "shipping address" not in text
        assert "ship a replacement" not in text


def test_path_resolution_finds_corpus(tmp_path: Path) -> None:
    assert policies_dir().is_dir()
    assert list(policies_dir().glob("*.md"))
