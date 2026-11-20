"""Tier-filtered retrieval and the grounding decision.

Two things are kept deliberately separate:

* **Similarity** measures retrieval relevance only. It is a cosine similarity,
  computed as ``1 - cosine_distance``, so higher is always closer. The
  conversion is written once, here, because mistaking a distance for a
  similarity inverts the whole guardrail.
* **Evidence coverage** — how many chunks cleared the bar — is recorded
  separately from the top score. One marginal hit is not the same as four
  strong ones, and the drawer shows both.

Filtering happens *before* ranking: inactive versions and other tiers are
excluded in SQL, not filtered out afterwards. A superseded policy must never
be able to win on score and then be dropped, because the next-best chunk would
already have been displaced.

Fails closed: no evidence, or evidence below the threshold, yields WEAK or
UNSUPPORTED, and neither permits a policy claim or an action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from support.config import Settings, get_settings
from support.contracts.enums import GroundingDecision, Tier
from support.providers.azure_openai import EmbeddingProvider
from support.storage.models import PolicyChunk, PolicyDocument


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_key: str
    doc_key: str
    document_title: str
    document_version: str
    heading_path: str
    text: str
    similarity: float


@dataclass(frozen=True)
class RetrievalResult:
    decision: GroundingDecision
    threshold: float
    top_similarity: float | None
    accepted: tuple[RetrievedChunk, ...]
    considered: tuple[RetrievedChunk, ...]

    @property
    def evidence_count(self) -> int:
        return len(self.accepted)

    @property
    def permits_policy_claim(self) -> bool:
        """Only GROUNDED evidence may back a policy statement or an action."""
        return self.decision is GroundingDecision.GROUNDED

    def citation_keys(self) -> frozenset[str]:
        """The only chunk ids an answer may cite."""
        return frozenset(c.chunk_key for c in self.accepted)


async def retrieve(
    db: AsyncSession,
    *,
    query: str,
    tier: Tier | None,
    embeddings: EmbeddingProvider,
    settings: Settings | None = None,
    top_k: int | None = None,
) -> RetrievalResult:
    """Retrieve policy evidence for a query.

    `tier` is the *verified* tier from server state. None means the session is
    unverified, which restricts retrieval to globally-applicable documents --
    public troubleshooting guidance stays available without verification.
    """
    settings = settings or get_settings()
    k = top_k or settings.retrieval_top_k

    embedded = await embeddings.embed([query])
    if not embedded.vectors:
        return RetrievalResult(
            decision=GroundingDecision.UNSUPPORTED,
            threshold=settings.grounding_threshold,
            top_similarity=None,
            accepted=(),
            considered=(),
        )
    query_vector = embedded.vectors[0]

    distance = PolicyChunk.embedding.cosine_distance(query_vector).label("distance")

    # A document with an empty tiers array is global.
    is_global = func.cardinality(PolicyDocument.tiers) == 0
    tier_predicate = (
        is_global if tier is None else or_(is_global, PolicyDocument.tiers.contains([tier.value]))
    )

    stmt = (
        select(PolicyChunk, PolicyDocument, distance)
        .join(PolicyDocument, PolicyChunk.document_id == PolicyDocument.id)
        .where(PolicyDocument.is_active.is_(True))
        .where(tier_predicate)
        .order_by(distance)
        .limit(k)
    )

    # The labelled distance column leaves the select untyped, so the row shape
    # is asserted once here rather than silently degrading to Any downstream.
    rows = cast(
        "list[tuple[PolicyChunk, PolicyDocument, float]]",
        (await db.execute(stmt)).all(),
    )

    considered = tuple(
        RetrievedChunk(
            chunk_key=chunk.chunk_key,
            doc_key=document.doc_key,
            document_title=document.title,
            document_version=document.version,
            heading_path=chunk.heading_path,
            text=chunk.text,
            # The single place distance becomes similarity.
            similarity=1.0 - float(dist),
        )
        for chunk, document, dist in rows
    )

    return decide(considered, settings=settings)


def decide(
    considered: tuple[RetrievedChunk, ...],
    *,
    settings: Settings | None = None,
) -> RetrievalResult:
    """Apply the threshold. Pure, so calibration can sweep it offline."""
    settings = settings or get_settings()
    threshold = settings.grounding_threshold
    weak_floor = threshold - settings.grounding_weak_margin

    if not considered:
        return RetrievalResult(
            decision=GroundingDecision.UNSUPPORTED,
            threshold=threshold,
            top_similarity=None,
            accepted=(),
            considered=(),
        )

    top = considered[0].similarity
    accepted = tuple(c for c in considered if c.similarity >= threshold)

    if accepted:
        decision = GroundingDecision.GROUNDED
    elif top >= weak_floor:
        # Near the bar: ask a clarifying question rather than guess or refuse.
        decision = GroundingDecision.WEAK
    else:
        decision = GroundingDecision.UNSUPPORTED

    return RetrievalResult(
        decision=decision,
        threshold=threshold,
        top_similarity=top,
        accepted=accepted,
        considered=considered,
    )


def validate_citations(
    cited: list[str],
    result: RetrievalResult,
) -> tuple[bool, list[str]]:
    """Check model-supplied citations against the accepted evidence.

    Returns (ok, invalid_keys). A citation outside the accepted set means the
    model invented or mis-copied a source, so the answer must not be released.
    """
    allowed = result.citation_keys()
    invalid = [key for key in cited if key not in allowed]
    return (not invalid, invalid)
