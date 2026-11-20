"""Persist a turn's retrieval result, and rebuild it later.

The chat loop holds its `RetrievalResult` in memory for the life of one turn,
because one turn is one request. Voice is not like that: each function call
from the realtime model arrives as its own HTTP request, so the evidence
gathered by `retrieve_policy` has to survive until `execute_mock_refund`
arrives -- possibly seconds later, possibly after a reconnect.

Without this the grounding gate would be unenforceable on the voice path,
since `ctx.evidence` would be None on every call and every action would be
refused as ungrounded.

**Only keys and scores are stored, never chunk text.** The text is rebuilt from
`policy_chunks` on load, so a turn cannot cite a stale copy of a document that
has since been re-ingested, and the row stays small. A chunk that has
disappeared from the corpus is dropped on rebuild rather than resurrected,
which fails closed: fewer accepted chunks can only make the gate stricter.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from support.contracts.enums import GroundingDecision
from support.retrieval.search import RetrievalResult, RetrievedChunk
from support.storage.models import PolicyChunk, PolicyDocument


def dump_evidence(result: RetrievalResult) -> dict[str, Any]:
    """Serialise a retrieval result for the `turns.evidence` column."""
    return {
        "version": 1,
        "decision": result.decision.value,
        "threshold": result.threshold,
        "top_similarity": result.top_similarity,
        "accepted": [
            {"chunk_key": c.chunk_key, "similarity": c.similarity} for c in result.accepted
        ],
        "considered": [
            {"chunk_key": c.chunk_key, "similarity": c.similarity} for c in result.considered
        ],
    }


async def load_evidence(db: AsyncSession, payload: dict[str, Any] | None) -> RetrievalResult | None:
    """Rebuild a retrieval result, re-reading chunk text from the corpus."""
    if not payload or payload.get("version") != 1:
        return None

    keys: list[str] = [
        str(entry["chunk_key"])
        for group in ("accepted", "considered")
        for entry in payload.get(group, [])
    ]
    chunks = await _chunks_by_key(db, keys)

    def rebuild(group: str) -> tuple[RetrievedChunk, ...]:
        out: list[RetrievedChunk] = []
        for entry in payload.get(group, []):
            key = str(entry["chunk_key"])
            found = chunks.get(key)
            if found is None:
                # Re-ingested or deleted since the turn began. Dropping it can
                # only narrow what may be cited or actioned.
                continue
            out.append(
                RetrievedChunk(
                    chunk_key=key,
                    doc_key=found.doc_key,
                    document_title=found.document_title,
                    document_version=found.document_version,
                    heading_path=found.heading_path,
                    text=found.text,
                    similarity=float(entry["similarity"]),
                )
            )
        return tuple(out)

    accepted = rebuild("accepted")
    decision = GroundingDecision(payload["decision"])
    if decision is GroundingDecision.GROUNDED and not accepted:
        # Every accepted chunk vanished. Claiming GROUNDED with no evidence
        # would let an action through on nothing at all.
        decision = GroundingDecision.UNSUPPORTED

    return RetrievalResult(
        decision=decision,
        threshold=float(payload["threshold"]),
        top_similarity=(
            None if payload.get("top_similarity") is None else float(payload["top_similarity"])
        ),
        accepted=accepted,
        considered=rebuild("considered"),
    )


async def _chunks_by_key(db: AsyncSession, keys: list[str]) -> dict[str, RetrievedChunk]:
    """Look up chunks by their stable citation key, in one query."""
    if not keys:
        return {}

    rows = (
        await db.execute(
            select(PolicyChunk, PolicyDocument)
            .join(PolicyDocument, PolicyChunk.document_id == PolicyDocument.id)
            .where(PolicyChunk.chunk_key.in_(set(keys)))
        )
    ).all()

    return {
        chunk.chunk_key: RetrievedChunk(
            chunk_key=chunk.chunk_key,
            doc_key=document.doc_key,
            document_title=document.title,
            document_version=document.version,
            heading_path=chunk.heading_path,
            text=chunk.text,
            # Replaced by the stored score; retrieval is not re-run.
            similarity=0.0,
        )
        for chunk, document in rows
    }
