"""Threshold calibration against the labelled evaluation set.

    uv run python -m support.retrieval.calibrate
    uv run python -m support.retrieval.calibrate --holdout

Why this exists: a hardcoded similarity cutoff is an unjustified magic number.
This sweeps candidate thresholds over labelled queries and reports the
trade-off, so the chosen value and its measured behaviour can both go in the
README.

Embeddings are computed once per query and reused across every candidate
threshold, because `decide()` is pure. A full sweep therefore costs one
embedding pass, not one per threshold.

Tier correctness is reported separately and is threshold-independent: it asks
whether the top-ranked document is the right one for the verified tier, which
is a property of filtering, not of the cutoff.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from support.config import Settings, get_settings
from support.contracts.enums import GroundingDecision, Tier
from support.platform_compat import configure_event_loop
from support.providers.azure_openai import AzureOpenAIEmbeddings
from support.retrieval.search import RetrievedChunk, decide, retrieve
from support.storage.session import dispose_engine, get_sessionmaker

# Queries whose id ends in an even digit form the held-out set, so tuning and
# verification never use the same examples.
HOLDOUT_CATEGORIES = {"supported_premium_paraphrase", "tier_overlap", "out_of_scope_heldout"}


@dataclass
class Case:
    id: str
    query: str
    tier: Tier | None
    expect: str
    expect_doc_key: str | None
    category: str
    considered: tuple[RetrievedChunk, ...] = ()


def eval_path() -> Path:
    settings = get_settings()
    if settings.fixtures_dir:
        return Path(settings.fixtures_dir) / "eval" / "queries.json"
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "fixtures" / "eval" / "queries.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Could not locate fixtures/eval/queries.json")


def load_cases() -> list[Case]:
    data = json.loads(eval_path().read_text(encoding="utf-8"))
    cases: list[Case] = []
    for row in data["queries"]:
        cases.append(
            Case(
                id=row["id"],
                query=row["query"],
                tier=Tier(row["tier"]) if row.get("tier") else None,
                expect=row["expect"],
                expect_doc_key=row.get("expect_doc_key"),
                category=row["category"],
            )
        )
    return cases


def _correct(expect: str, decision: GroundingDecision) -> bool:
    if expect == "grounded":
        return decision is GroundingDecision.GROUNDED
    if expect == "unsupported":
        return decision is GroundingDecision.UNSUPPORTED
    if expect == "weak_or_grounded":
        return decision in (GroundingDecision.WEAK, GroundingDecision.GROUNDED)
    raise ValueError(f"unknown expectation {expect!r}")


@dataclass
class Score:
    threshold: float
    margin: float
    supported_accepted: int
    supported_total: int
    unsupported_rejected: int
    unsupported_total: int
    ambiguous_ok: int
    ambiguous_total: int

    @property
    def recall(self) -> float:
        return self.supported_accepted / self.supported_total if self.supported_total else 0.0

    @property
    def rejection(self) -> float:
        return self.unsupported_rejected / self.unsupported_total if self.unsupported_total else 0.0

    @property
    def ambiguous_rate(self) -> float:
        return self.ambiguous_ok / self.ambiguous_total if self.ambiguous_total else 1.0

    @property
    def balanced(self) -> float:
        """Equal weight on answering what we should and refusing what we must.

        A grounding guardrail that accepts everything and one that refuses
        everything are both useless; the mean punishes either collapse.

        Ambiguous cases are deliberately excluded here -- they have two
        acceptable answers, so they must not trade against correctness. They
        break ties instead, via `selection_key`.
        """
        return (self.recall + self.rejection) / 2

    @property
    def selection_key(self) -> tuple[float, float, float]:
        """Ordering for picking a configuration.

        1. Safety-critical score first.
        2. Then ambiguous handling -- this is what the margin is actually for,
           and ignoring it let margin be chosen by noise.
        3. Then the stricter threshold, since an unsupported policy claim is a
           worse failure than an unnecessary clarifying question.
        """
        return (self.balanced, self.ambiguous_rate, self.threshold)


def score_at(cases: list[Case], threshold: float, margin: float, settings: Settings) -> Score:
    tuned = settings.model_copy(
        update={"grounding_threshold": threshold, "grounding_weak_margin": margin}
    )

    s = Score(threshold, margin, 0, 0, 0, 0, 0, 0)
    for case in cases:
        result = decide(case.considered, settings=tuned)
        ok = _correct(case.expect, result.decision)
        if case.expect == "grounded":
            s.supported_total += 1
            s.supported_accepted += int(ok)
        elif case.expect == "unsupported":
            s.unsupported_total += 1
            s.unsupported_rejected += int(ok)
        else:
            s.ambiguous_total += 1
            s.ambiguous_ok += int(ok)
    return s


async def _gather(cases: list[Case]) -> None:
    """Embed and retrieve once per case; thresholds are swept offline."""
    embeddings = AzureOpenAIEmbeddings()
    sessionmaker = get_sessionmaker()
    try:
        # Force a permissive threshold so every candidate row is captured and
        # the sweep can re-decide without re-querying.
        permissive = get_settings().model_copy(
            update={"grounding_threshold": 0.0, "grounding_weak_margin": 0.0}
        )
        for case in cases:
            async with sessionmaker() as db:
                result = await retrieve(
                    db,
                    query=case.query,
                    tier=case.tier,
                    embeddings=embeddings,
                    settings=permissive,
                )
            case.considered = result.considered
    finally:
        await embeddings.aclose()
        await dispose_engine()


async def run(holdout: bool) -> int:
    settings = get_settings()
    all_cases = load_cases()

    tune = [c for c in all_cases if c.category not in HOLDOUT_CATEGORIES]
    held = [c for c in all_cases if c.category in HOLDOUT_CATEGORIES]

    print(f"evaluation set: {len(all_cases)} queries ({len(tune)} tuning, {len(held)} held out)")
    print("retrieving (one embedding pass)...")
    await _gather(all_cases)

    # Two different questions, often confused:
    #
    #   recall@k  -- did the authoritative document reach the evidence set at
    #                all? This is what governs grounding, because the agent
    #                reads every accepted chunk, not just the first.
    #   top-1     -- did it also rank first? Useful as a precision signal, but
    #                a miss here is not automatically a grounding failure.
    #
    # A wrong-tier document appearing at ANY rank is a real defect, so that is
    # checked separately and must be zero.
    tier_checked = [c for c in all_cases if c.expect_doc_key]

    in_set = sum(
        1 for c in tier_checked if any(ch.doc_key == c.expect_doc_key for ch in c.considered)
    )
    top1 = sum(
        1 for c in tier_checked if c.considered and c.considered[0].doc_key == c.expect_doc_key
    )
    print(
        f"\nauthoritative doc in retrieved set (recall@k): "
        f"{in_set}/{len(tier_checked)} = {in_set / len(tier_checked):.0%}"
    )
    print(
        f"authoritative doc ranked first (top-1):        "
        f"{top1}/{len(tier_checked)} = {top1 / len(tier_checked):.0%}"
    )
    for c in tier_checked:
        if not any(ch.doc_key == c.expect_doc_key for ch in c.considered):
            print(f"    ABSENT  {c.id:28} expected {c.expect_doc_key} never retrieved")
        elif c.considered and c.considered[0].doc_key != c.expect_doc_key:
            print(
                f"    rank>1  {c.id:28} expected {c.expect_doc_key:24} "
                f"outranked by {c.considered[0].doc_key}"
            )

    # Tier leakage: a document scoped to the other tier must never appear.
    other = {Tier.PREMIUM: "basic", Tier.BASIC: "premium"}
    leaks = [
        (c.id, ch.doc_key)
        for c in all_cases
        if c.tier
        for ch in c.considered
        if ch.doc_key.endswith(other[c.tier])
    ]
    print(f"wrong-tier documents retrieved (must be 0):    {len(leaks)}")
    for case_id, doc in leaks:
        print(f"    LEAK {case_id}: {doc}")

    # Superseded versions must never be retrievable.
    stale = [
        (c.id, f"{ch.doc_key}@{ch.document_version}")
        for c in all_cases
        for ch in c.considered
        if ch.doc_key == "refund-policy-premium" and ch.document_version != "2.0"
    ]
    print(f"superseded versions retrieved (must be 0):     {len(stale)}")
    for case_id, doc in stale:
        print(f"    STALE {case_id}: {doc}")

    # Sweep both dimensions. The threshold decides accept vs not; the margin
    # decides whether a near miss becomes a clarifying question (WEAK) or an
    # escalation (UNSUPPORTED). They interact, so tuning one at a time
    # understates what is achievable.
    print("\nthreshold x margin sweep on the tuning subset (balanced score):")
    margins = [0.04, 0.08, 0.12, 0.16]
    print("  thresh " + "".join(f"  m={m:.2f}" for m in margins))
    candidates: list[Score] = []
    for i in range(24, 53, 2):
        threshold = i / 100
        row = []
        for margin in margins:
            s = score_at(tune, threshold, margin, settings)
            candidates.append(s)
            row.append(f"  {s.balanced:.3f}")
        print(f"  {threshold:.2f}  " + "".join(row))

    # Tie-break toward the HIGHER threshold: where two settings score equally,
    # the stricter one is preferred, because an unsupported policy claim is a
    # worse failure than an unnecessary clarifying question.
    best = max(candidates, key=lambda s: s.selection_key)
    print(
        f"\nbest on tuning subset: threshold={best.threshold:.2f} "
        f"margin={best.margin:.2f} balanced={best.balanced:.3f} "
        f"(recall={best.recall:.0%} reject={best.rejection:.0%} "
        f"ambiguous={best.ambiguous_rate:.0%})"
    )
    margin = best.margin

    if holdout:
        h = score_at(held, best.threshold, margin, settings)
        print(
            f"held-out check at {best.threshold:.2f}: "
            f"recall={h.recall:.0%} ({h.supported_accepted}/{h.supported_total}) "
            f"reject={h.rejection:.0%} ({h.unsupported_rejected}/{h.unsupported_total})"
        )

    overall = score_at(all_cases, best.threshold, margin, settings)
    print(
        f"\nfull set at threshold={best.threshold:.2f}, margin={margin:.2f}:\n"
        f"  supported accepted   {overall.supported_accepted}/{overall.supported_total}\n"
        f"  unsupported rejected {overall.unsupported_rejected}/{overall.unsupported_total}\n"
        f"  ambiguous handled    {overall.ambiguous_ok}/{overall.ambiguous_total}"
    )

    print("\nremaining errors at the selected threshold:")
    tuned = settings.model_copy(
        update={"grounding_threshold": best.threshold, "grounding_weak_margin": margin}
    )
    errors = 0
    for case in all_cases:
        result = decide(case.considered, settings=tuned)
        if not _correct(case.expect, result.decision):
            errors += 1
            top = result.top_similarity or 0.0
            print(
                f"    {case.id:28} expected {case.expect:17} "
                f"got {result.decision.value:11} top={top:.4f}"
            )
    if not errors:
        print("    none")

    print(f"\nset GROUNDING_THRESHOLD={best.threshold:.2f} in .env and infra variables.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate the grounding threshold.")
    parser.add_argument(
        "--holdout",
        action="store_true",
        help="Also report the held-out subset at the selected threshold.",
    )
    args = parser.parse_args()
    configure_event_loop()
    return asyncio.run(run(holdout=args.holdout))


if __name__ == "__main__":
    sys.exit(main())
