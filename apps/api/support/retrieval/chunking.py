"""Heading-aware chunking of versioned policy Markdown.

Design decisions, all of which are reportable in the README:

* Sections are split on heading boundaries, not on a fixed token window, so a
  chunk is always a coherent unit of policy with a readable heading path to
  cite.
* A rule and its exceptions must stay together. "Time limit and its exceptions"
  is useless split in half: a retrieved chunk saying "within 60 days" without
  the duplicate-charge carve-out would ground a wrong answer. Sections are
  therefore only split when they genuinely exceed the budget, and then on
  paragraph boundaries with overlap.
* Short sections are kept intact rather than merged with neighbours, because
  merging dilutes the embedding of a precise rule such as a refund cap.
* Chunk ids are derived from (doc_key, version, ordinal, normalised text), so
  re-ingesting an unchanged corpus produces identical ids and existing
  citations remain valid.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import tiktoken
from markdown_it import MarkdownIt

# text-embedding-3 models use cl100k_base. Chosen explicitly so the token
# budget means the same thing here as it does at the provider.
_ENCODING = tiktoken.get_encoding("cl100k_base")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class PolicyMeta:
    doc_key: str
    title: str
    version: str
    tiers: tuple[str, ...]  # empty means global: retrievable for any tier
    active: bool
    effective: str
    source_path: str
    checksum: str

    @property
    def is_global(self) -> bool:
        return not self.tiers


@dataclass(frozen=True)
class Chunk:
    chunk_key: str
    heading_path: str
    text: str
    token_count: int
    ordinal: int


@dataclass(frozen=True)
class ParsedPolicy:
    meta: PolicyMeta
    chunks: tuple[Chunk, ...]


def count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


def _normalise(text: str) -> str:
    """Collapse whitespace for stable hashing.

    Reformatting a document without changing its words must not change chunk
    ids, or every stored citation would break.
    """
    return re.sub(r"\s+", " ", text).strip()


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError("document has no frontmatter block")

    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()

    return fields, raw[match.end() :]


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "yes", "1"}


def _as_tiers(value: str) -> tuple[str, ...]:
    # Absent or blank means global. Explicit list means tier-restricted.
    if not value.strip():
        return ()
    return tuple(t.strip().lower() for t in value.split(",") if t.strip())


@dataclass
class _Section:
    level: int
    heading: str
    path: tuple[str, ...]
    lines: list[str]

    def body(self) -> str:
        return "\n".join(self.lines).strip()


def _split_sections(body: str) -> list[_Section]:
    """Split Markdown into sections using the real heading structure.

    markdown-it gives authoritative heading positions, which avoids
    misreading a `#` inside a fenced code block as a heading.
    """
    md = MarkdownIt()
    tokens = md.parse(body)
    lines = body.splitlines()

    headings: list[tuple[int, int, str]] = []  # (line_index, level, text)
    for i, token in enumerate(tokens):
        if token.type != "heading_open" or token.map is None:
            continue
        level = int(token.tag[1:])
        text = tokens[i + 1].content.strip() if i + 1 < len(tokens) else ""
        headings.append((token.map[0], level, text))

    if not headings:
        return [_Section(level=1, heading="", path=(), lines=lines)]

    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []

    for idx, (line_no, level, text) in enumerate(headings):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))
        path = tuple(h for _, h in stack)

        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        sections.append(
            _Section(
                level=level,
                heading=text,
                path=path,
                lines=lines[line_no + 1 : end],
            )
        )

    return sections


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _split_oversized(
    text: str,
    target_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """Split on paragraph boundaries, carrying a bounded overlap.

    Never splits mid-paragraph: a half-sentence is not groundable evidence.
    """
    paras = _paragraphs(text)
    if not paras:
        return []

    out: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for para in paras:
        para_tokens = count_tokens(para)

        if current and current_tokens + para_tokens > target_tokens:
            out.append("\n\n".join(current))

            # Carry trailing paragraphs back as overlap, up to the budget.
            carried: list[str] = []
            carried_tokens = 0
            for prev in reversed(current):
                prev_tokens = count_tokens(prev)
                if carried_tokens + prev_tokens > overlap_tokens:
                    break
                carried.insert(0, prev)
                carried_tokens += prev_tokens

            current = [*carried, para]
            current_tokens = carried_tokens + para_tokens
        else:
            current.append(para)
            current_tokens += para_tokens

    if current:
        out.append("\n\n".join(current))
    return out


def parse_policy(
    path: Path,
    *,
    target_tokens: int = 300,
    overlap_tokens: int = 50,
) -> ParsedPolicy:
    raw = path.read_text(encoding="utf-8")
    fields, body = _parse_frontmatter(raw)

    for required in ("doc_key", "title", "version"):
        if not fields.get(required):
            raise ValueError(f"{path.name}: frontmatter missing '{required}'")

    meta = PolicyMeta(
        doc_key=fields["doc_key"],
        title=fields["title"],
        version=fields["version"],
        tiers=_as_tiers(fields.get("tiers", "")),
        active=_as_bool(fields.get("active", "true")),
        effective=fields.get("effective", ""),
        source_path=path.name,
        # Checksum over the whole file: any edit triggers reindexing of the doc.
        checksum=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32],
    )

    chunks: list[Chunk] = []
    ordinal = 0

    for section in _split_sections(body):
        content = section.body()
        if not content:
            # A heading with no body of its own (a parent of subsections)
            # carries no evidence, so it is not a chunk.
            continue

        # Drop a leading H1 that merely repeats the document title, so a
        # citation reads "Per-request refund cap" rather than
        # "Refund Policy - Premium Plan > Refund Policy - Premium Plan > ...".
        heading_parts = section.path
        if heading_parts and heading_parts[0].strip() == meta.title.strip():
            heading_parts = heading_parts[1:]
        heading_path = " > ".join(heading_parts) if heading_parts else meta.title
        # Prefix the heading path into the embedded text: a bare "$25.00 per
        # request" embeds poorly, the same line under "Refund Policy - Premium
        # Plan > Per-request refund cap" embeds precisely.
        prefix = f"{meta.title} > {heading_path}" if heading_path != meta.title else meta.title

        pieces = (
            [content]
            if count_tokens(content) <= target_tokens
            else _split_oversized(content, target_tokens, overlap_tokens)
        )

        for piece in pieces:
            text = f"{prefix}\n\n{piece}"
            digest = hashlib.sha256(
                f"{meta.doc_key}|{meta.version}|{ordinal}|{_normalise(piece)}".encode()
            ).hexdigest()[:12]
            chunks.append(
                Chunk(
                    chunk_key=f"{meta.doc_key}@{meta.version}#{ordinal:02d}:{digest}",
                    heading_path=heading_path,
                    text=text,
                    token_count=count_tokens(text),
                    ordinal=ordinal,
                )
            )
            ordinal += 1

    if not chunks:
        raise ValueError(f"{path.name}: produced no chunks")

    return ParsedPolicy(meta=meta, chunks=tuple(chunks))


def parse_corpus(
    policies_dir: Path,
    *,
    target_tokens: int = 300,
    overlap_tokens: int = 50,
) -> list[ParsedPolicy]:
    paths = sorted(policies_dir.glob("*.md"))
    if not paths:
        raise FileNotFoundError(f"no .md policies found in {policies_dir}")

    parsed = [
        parse_policy(p, target_tokens=target_tokens, overlap_tokens=overlap_tokens) for p in paths
    ]

    # Guard against two active versions of the same document, which would make
    # retrieval nondeterministic and could surface contradictory caps.
    seen: dict[str, str] = {}
    for policy in parsed:
        if not policy.meta.active:
            continue
        key = policy.meta.doc_key
        if key in seen:
            raise ValueError(
                f"two active versions of '{key}': {seen[key]} and {policy.meta.version}"
            )
        seen[key] = policy.meta.version

    return parsed
