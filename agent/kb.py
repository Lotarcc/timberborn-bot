#!/usr/bin/env python3
"""Static KB retrieval for the Timberborn player agent."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KB_PATHS = (
    REPO_ROOT / "docs" / "kb",
    REPO_ROOT / "docs" / "knowledge" / "survival-basics.md",
)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.+-]*", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "vs",
    "what",
    "when",
    "with",
}


@dataclass(frozen=True)
class Chunk:
    """One markdown-heading chunk from the static KB."""

    source: str
    title: str
    heading_path: str
    text: str

    def as_dict(self, score: float = 0.0) -> dict:
        return {
            "score": round(score, 6),
            "source": self.source,
            "title": self.title,
            "heading_path": self.heading_path,
            "text": self.text,
        }


def _tokenize(text: str) -> list[str]:
    return [
        token.lower()
        for token in TOKEN_RE.findall(text)
        if len(token) > 1 and token.lower() not in STOPWORDS
    ]


def _markdown_files(paths: Iterable[Path] = DEFAULT_KB_PATHS) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.md")))
        elif path.is_file():
            files.append(path)
    return files


def _split_markdown(path: Path) -> list[Chunk]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    chunks: list[Chunk] = []
    current_title: str | None = None
    current_level = 0
    current_lines: list[str] = []
    headings: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal current_lines, current_title
        if current_title is None:
            return
        body = "\n".join(current_lines).strip()
        if not body:
            return
        heading_path = " > ".join(title for _, title in headings)
        chunks.append(
            Chunk(
                source=str(path.relative_to(REPO_ROOT)),
                title=current_title,
                heading_path=heading_path,
                text=body,
            )
        )

    for line in lines:
        match = HEADING_RE.match(line)
        if match:
            flush()
            current_level = len(match.group(1))
            current_title = match.group(2).strip()
            headings = [(level, title) for level, title in headings if level < current_level]
            headings.append((current_level, current_title))
            current_lines = [line]
        else:
            if current_title is None:
                if line.strip():
                    current_title = path.stem
                    current_level = 1
                    headings = [(current_level, current_title)]
                    current_lines = [line]
            else:
                current_lines.append(line)

    flush()
    return chunks


def load_chunks(paths: Iterable[Path] = DEFAULT_KB_PATHS) -> list[Chunk]:
    """Load KB files and split them into markdown-heading concept chunks."""

    chunks: list[Chunk] = []
    for path in _markdown_files(paths):
        chunks.extend(_split_markdown(path))
    return chunks


def _tf(tokens: list[str]) -> dict[str, float]:
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    total = max(len(tokens), 1)
    return {token: count / total for token, count in counts.items()}


def _keyword_scores(query: str, chunks: list[Chunk]) -> list[tuple[float, Chunk]]:
    query_tokens = _tokenize(query)
    if not query_tokens or not chunks:
        return []

    chunk_tokens = [_tokenize(f"{chunk.heading_path}\n{chunk.text}") for chunk in chunks]
    dfs: dict[str, int] = {}
    for tokens in chunk_tokens:
        for token in set(tokens):
            dfs[token] = dfs.get(token, 0) + 1

    n = len(chunks)
    query_counts: dict[str, int] = {}
    for token in query_tokens:
        query_counts[token] = query_counts.get(token, 0) + 1

    scores: list[tuple[float, Chunk]] = []
    query_text = " ".join(query_tokens)
    for chunk, tokens in zip(chunks, chunk_tokens):
        term_freq = _tf(tokens)
        score = 0.0
        for token, query_count in query_counts.items():
            idf = math.log((n + 1) / (dfs.get(token, 0) + 1)) + 1.0
            score += term_freq.get(token, 0.0) * idf * query_count

        title_tokens = set(_tokenize(chunk.heading_path))
        score += 0.08 * len(title_tokens.intersection(query_counts))
        lowered = chunk.text.lower()
        if query.strip().lower() in lowered:
            score += 0.25
        if query_text and query_text in " ".join(tokens):
            score += 0.15
        for token in query_counts:
            if re.search(rf"\bRULE:\s+.*\b{re.escape(token)}\b", chunk.text, re.IGNORECASE):
                score += 0.03

        if score > 0:
            scores.append((score, chunk))

    return sorted(scores, key=lambda item: item[0], reverse=True)


def _ollama_embedding(text: str, timeout: float = 1.5) -> list[float] | None:
    payload = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_URL.rstrip('/')}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    embedding = body.get("embedding")
    if not isinstance(embedding, list) or not embedding:
        return None
    try:
        return [float(value) for value in embedding]
    except (TypeError, ValueError):
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def _embedding_scores(query: str, chunks: list[Chunk]) -> list[tuple[float, Chunk]] | None:
    query_embedding = _ollama_embedding(query)
    if query_embedding is None:
        return None

    scored: list[tuple[float, Chunk]] = []
    for chunk in chunks:
        chunk_embedding = _ollama_embedding(f"{chunk.heading_path}\n{chunk.text}", timeout=3.0)
        if chunk_embedding is None:
            return None
        score = _cosine(query_embedding, chunk_embedding)
        if score > 0:
            scored.append((score, chunk))
    return sorted(scored, key=lambda item: item[0], reverse=True)


def lookup(
    query: str,
    k: int = 3,
    *,
    use_embeddings: bool = True,
    paths: Iterable[Path] = DEFAULT_KB_PATHS,
) -> list[dict]:
    """Return the top-k KB chunks relevant to query."""

    if k <= 0:
        return []
    chunks = load_chunks(paths)
    if not query.strip() or not chunks:
        return []

    scored = _embedding_scores(query, chunks) if use_embeddings else None
    if scored is None:
        scored = _keyword_scores(query, chunks)
    return [chunk.as_dict(score) for score, chunk in scored[:k]]


def _excerpt(text: str, max_chars: int = 900) -> str:
    compact = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retrieve chunks from the Timberborn KB.")
    parser.add_argument("query", nargs="*", help="Search query, e.g. 'how much water for a drought'")
    parser.add_argument("-k", "--top-k", type=int, default=3, help="Number of chunks to return")
    parser.add_argument(
        "--no-embeddings",
        action="store_true",
        help="Skip Ollama embeddings and use local keyword scoring only",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON results")
    args = parser.parse_args(argv)

    query = " ".join(args.query).strip()
    if not query:
        parser.print_usage(sys.stderr)
        return 0

    results = lookup(query, args.top_k, use_embeddings=not args.no_embeddings)
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0

    if not results:
        print("No KB chunks found.")
        return 0

    for index, result in enumerate(results, start=1):
        print(f"{index}. {result['title']} [{result['source']}] score={result['score']}")
        print(_excerpt(result["text"]))
        if index != len(results):
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
