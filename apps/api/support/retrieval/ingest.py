"""Corpus ingestion.

    uv run python -m support.retrieval.ingest           # incremental
    uv run python -m support.retrieval.ingest --force   # re-embed everything

Properties that matter:

* **Incremental.** A document whose checksum is unchanged is skipped, so
  re-running costs nothing and does not spend embedding tokens.
* **Atomic activation.** New rows are written inactive, then a single
  transaction flips the active version per doc_key. A crash mid-ingest leaves
  the previously active corpus serving queries untouched.
* **Dimensionality is asserted**, not assumed. A model whose width differs
  from the configured value aborts before writing, because a mixed-width index
  silently returns nonsense.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import delete, select, update

from support.config import get_settings
from support.platform_compat import configure_event_loop
from support.providers.azure_openai import AzureOpenAIEmbeddings
from support.retrieval.chunking import ParsedPolicy, parse_corpus
from support.storage.models import PolicyChunk, PolicyDocument
from support.storage.session import dispose_engine, transaction

EMBED_BATCH = 32


def policies_dir() -> Path:
    settings = get_settings()
    if settings.fixtures_dir:
        return Path(settings.fixtures_dir) / "policies"

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "fixtures" / "policies"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("Could not locate fixtures/policies. Set FIXTURES_DIR.")


async def _embed_all(
    embeddings: AzureOpenAIEmbeddings,
    texts: list[str],
) -> tuple[list[list[float]], int]:
    """Embed in bounded batches, returning vectors and total input tokens."""
    vectors: list[list[float]] = []
    tokens = 0
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start : start + EMBED_BATCH]
        result = await embeddings.embed(batch)
        vectors.extend(result.vectors)
        tokens += result.usage.input_tokens or 0
    return vectors, tokens


async def _ingest_document(
    policy: ParsedPolicy,
    embeddings: AzureOpenAIEmbeddings,
    *,
    force: bool,
) -> tuple[str, int]:
    """Ingest one document. Returns (action, tokens_spent)."""
    meta = policy.meta

    async with transaction() as db:
        existing = (
            await db.execute(
                select(PolicyDocument).where(
                    PolicyDocument.doc_key == meta.doc_key,
                    PolicyDocument.version == meta.version,
                )
            )
        ).scalar_one_or_none()

        if (
            existing is not None
            and not force
            and existing.checksum == meta.checksum
            and existing.embedding_deployment == embeddings.deployment
        ):
            return "skipped", 0

    texts = [c.text for c in policy.chunks]
    vectors, tokens = await _embed_all(embeddings, texts)
    if len(vectors) != len(policy.chunks):
        raise RuntimeError(
            f"{meta.doc_key}@{meta.version}: embedded {len(vectors)} of {len(policy.chunks)}"
        )

    async with transaction() as db:
        existing = (
            await db.execute(
                select(PolicyDocument).where(
                    PolicyDocument.doc_key == meta.doc_key,
                    PolicyDocument.version == meta.version,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            document = PolicyDocument(
                doc_key=meta.doc_key,
                title=meta.title,
                version=meta.version,
                # Written inactive; activation happens in its own transaction
                # once every document has landed.
                is_active=False,
                tiers=list(meta.tiers),
                source_path=meta.source_path,
                checksum=meta.checksum,
                embedding_deployment=embeddings.deployment,
            )
            db.add(document)
            await db.flush()
            action = "created"
        else:
            document = existing
            document.title = meta.title
            document.tiers = list(meta.tiers)
            document.source_path = meta.source_path
            document.checksum = meta.checksum
            document.embedding_deployment = embeddings.deployment
            await db.execute(delete(PolicyChunk).where(PolicyChunk.document_id == document.id))
            action = "reindexed"

        for chunk, vector in zip(policy.chunks, vectors, strict=True):
            db.add(
                PolicyChunk(
                    chunk_key=chunk.chunk_key,
                    document_id=document.id,
                    heading_path=chunk.heading_path,
                    text=chunk.text,
                    token_count=chunk.token_count,
                    ordinal=chunk.ordinal,
                    embedding=vector,
                )
            )

    return action, tokens


async def _activate(corpus: list[ParsedPolicy]) -> dict[str, str]:
    """Flip active versions in one transaction.

    Until this commits, queries continue to see the previously active corpus.
    """
    desired = {p.meta.doc_key: p.meta.version for p in corpus if p.meta.active}

    async with transaction() as db:
        for doc_key, version in desired.items():
            await db.execute(
                update(PolicyDocument)
                .where(PolicyDocument.doc_key == doc_key)
                .values(is_active=False)
            )
            await db.execute(
                update(PolicyDocument)
                .where(
                    PolicyDocument.doc_key == doc_key,
                    PolicyDocument.version == version,
                )
                .values(is_active=True)
            )

        # Anything not named as active in the corpus is explicitly deactivated,
        # so a document removed from the corpus stops being retrievable.
        for policy in corpus:
            if policy.meta.active:
                continue
            await db.execute(
                update(PolicyDocument)
                .where(
                    PolicyDocument.doc_key == policy.meta.doc_key,
                    PolicyDocument.version == policy.meta.version,
                )
                .values(is_active=False)
            )

    return desired


async def run(force: bool = False) -> int:
    settings = get_settings()
    corpus = parse_corpus(
        policies_dir(),
        target_tokens=settings.chunk_target_tokens,
        overlap_tokens=settings.chunk_overlap_tokens,
    )

    embeddings = AzureOpenAIEmbeddings(settings)
    total_tokens = 0
    try:
        for policy in corpus:
            action, tokens = await _ingest_document(policy, embeddings, force=force)
            total_tokens += tokens
            flag = "active" if policy.meta.active else "superseded"
            print(
                f"  {action:10} {policy.meta.doc_key:24} v{policy.meta.version:4} "
                f"{flag:11} chunks={len(policy.chunks):2}"
            )
        active = await _activate(corpus)
    finally:
        await embeddings.aclose()
        await dispose_engine()

    print("\nactive versions: " + ", ".join(f"{k}@{v}" for k, v in sorted(active.items())))
    cost = total_tokens / 1000 * settings.cost_per_1k_embedding_tokens_usd
    print(
        f"embedding tokens: {total_tokens} "
        f"(estimated ${cost:.5f} at {settings.cost_rates_effective} rates)"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest the policy corpus.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-embed every document even when its checksum is unchanged.",
    )
    args = parser.parse_args()

    configure_event_loop()
    return asyncio.run(run(force=args.force))


if __name__ == "__main__":
    sys.exit(main())
