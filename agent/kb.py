#!/usr/bin/env python3
"""Static KB retrieval for the Timberborn player agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KB_PATHS = (
    REPO_ROOT / "docs" / "kb",
    REPO_ROOT / "docs" / "knowledge" / "survival-basics.md",
)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3")
INDEX_PATH = Path(os.environ.get("KB_INDEX_PATH", REPO_ROOT / "agent" / "kb_index.json"))
try:
    BATCH_SIZE = max(int(os.environ.get("OLLAMA_EMBED_BATCH_SIZE", "16")), 1)
except ValueError:
    BATCH_SIZE = 16
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

    @property
    def key(self) -> str:
        hasher = hashlib.sha1()
        hasher.update(f"{self.source}{self.heading_path}{self.text}".encode("utf-8"))
        return hasher.hexdigest()

    def as_dict(self, score: float = 0.0, meta: Optional[dict[str, Any]] = None) -> dict:
        result = {
            "score": round(score, 6),
            "source": self.source,
            "title": self.title,
            "heading_path": self.heading_path,
            "text": self.text,
        }
        if meta:
            result["meta"] = meta
        return result


@dataclass(frozen=True)
class LoadedIndex:
    """Validated embedding vectors loaded from agent/kb_index.json."""

    model: str
    vectors_by_key: dict[str, list[float]]
    identity_by_key: dict[str, tuple[str, str]]


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
    current_title: Optional[str] = None
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


def _post_json(path: str, payload: dict[str, Any], timeout: float) -> Optional[dict[str, Any]]:
    encoded = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_URL.rstrip('/')}{path}",
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def _coerce_vector(values: Any) -> Optional[list[float]]:
    if not isinstance(values, list) or not values:
        return None
    try:
        return [float(value) for value in values]
    except (TypeError, ValueError):
        return None


def _ollama_embed_api(texts: list[str], timeout: float = 30.0) -> Optional[list[list[float]]]:
    body = _post_json("/api/embed", {"model": EMBED_MODEL, "input": texts}, timeout)
    if body is None:
        return None
    embeddings = body.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        vector = _coerce_vector(body.get("embedding"))
        if vector is not None and len(texts) == 1:
            return [vector]
        return None

    vectors: list[list[float]] = []
    for embedding in embeddings:
        vector = _coerce_vector(embedding)
        if vector is None:
            return None
        vectors.append(vector)
    return vectors


def _ollama_legacy_embedding(text: str, timeout: float = 30.0) -> Optional[list[float]]:
    body = _post_json("/api/embeddings", {"model": EMBED_MODEL, "prompt": text}, timeout)
    if body is None:
        return None
    return _coerce_vector(body.get("embedding"))


def _ollama_embeddings(
    texts: list[str],
    *,
    timeout: float = 30.0,
    batch_size: int = BATCH_SIZE,
    progress: bool = False,
) -> Optional[list[list[float]]]:
    if not texts:
        return []

    vectors: list[list[float]] = []
    used_batch_api = False
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        if progress:
            print(f"Embedding chunks {start + 1}-{start + len(batch)} of {len(texts)} via /api/embed...")
        batch_vectors = _ollama_embed_api(batch, timeout=timeout)
        if batch_vectors is None:
            vectors = []
            break
        used_batch_api = True
        vectors.extend(batch_vectors)

    if used_batch_api and len(vectors) == len(texts):
        return vectors

    if progress:
        print("/api/embed unavailable or incompatible; falling back to /api/embeddings per chunk...")
    vectors = []
    for index, text in enumerate(texts, start=1):
        if progress:
            print(f"Embedding chunk {index} of {len(texts)} via /api/embeddings...")
        vector = _ollama_legacy_embedding(text, timeout=timeout)
        if vector is None:
            return None
        vectors.append(vector)
    return vectors


def _ollama_embedding(text: str, timeout: float = 5.0) -> Optional[list[float]]:
    vectors = _ollama_embeddings([text], timeout=timeout, batch_size=1)
    if not vectors:
        return None
    return vectors[0]


def _chunk_embedding_text(chunk: Chunk) -> str:
    return f"{chunk.heading_path}\n{chunk.text}"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _load_index(path: Path = INDEX_PATH) -> Optional[LoadedIndex]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    model = body.get("model")
    entries = body.get("chunks")
    if not isinstance(model, str) or not isinstance(entries, list):
        return None

    vectors_by_key: dict[str, list[float]] = {}
    identity_by_key: dict[str, tuple[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        source = entry.get("source")
        heading_path = entry.get("heading_path")
        vector = _coerce_vector(entry.get("vector"))
        if (
            isinstance(key, str)
            and isinstance(source, str)
            and isinstance(heading_path, str)
            and vector is not None
        ):
            vectors_by_key[key] = vector
            identity_by_key[key] = (source, heading_path)
    return LoadedIndex(model=model, vectors_by_key=vectors_by_key, identity_by_key=identity_by_key)


def _index_stats(chunks: list[Chunk], index: Optional[LoadedIndex]) -> tuple[int, int]:
    if index is None:
        return 0, len(chunks)
    current_keys = {chunk.key for chunk in chunks}
    indexed_count = sum(1 for key in current_keys if key in index.vectors_by_key)
    stale_index_entries = len(set(index.vectors_by_key) - current_keys)
    missing_current_entries = len(current_keys) - indexed_count
    return indexed_count, stale_index_entries + missing_current_entries


def _write_index(chunks: list[Chunk], vectors: list[list[float]], path: Path = INDEX_PATH) -> None:
    payload = {
        "model": EMBED_MODEL,
        "chunks": [
            {
                "key": chunk.key,
                "source": chunk.source,
                "heading_path": chunk.heading_path,
                "vector": vector,
            }
            for chunk, vector in zip(chunks, vectors)
        ],
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def build_index(paths: Iterable[Path] = DEFAULT_KB_PATHS, path: Path = INDEX_PATH) -> int:
    chunks = load_chunks(paths)
    started = time.perf_counter()
    print(f"Loaded {len(chunks)} KB chunks.")
    if not chunks:
        return 1

    vectors = _ollama_embeddings(
        [_chunk_embedding_text(chunk) for chunk in chunks],
        timeout=120.0,
        batch_size=BATCH_SIZE,
        progress=True,
    )
    if vectors is None or len(vectors) != len(chunks):
        elapsed = time.perf_counter() - started
        print(f"Failed to build index: Ollama embedding API unavailable after {elapsed:.2f}s.", file=sys.stderr)
        return 1

    _write_index(chunks, vectors, path)
    elapsed = time.perf_counter() - started
    size_kib = path.stat().st_size / 1024.0
    print(f"Wrote {_display_path(path)} with {len(chunks)} chunks in {elapsed:.2f}s ({size_kib:.1f} KiB).")
    return 0


def check_index(paths: Iterable[Path] = DEFAULT_KB_PATHS, path: Path = INDEX_PATH) -> int:
    chunks = load_chunks(paths)
    index = _load_index(path)
    indexed_count, stale_count = _index_stats(chunks, index)
    reachable = _ollama_embedding("timberborn kb check", timeout=2.0) is not None
    print(f"chunk count: {len(chunks)}")
    print(f"indexed count: {indexed_count}")
    print(f"stale count: {stale_count}")
    print(f"Ollama reachable: {'y' if reachable else 'n'}")
    return 0


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


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

    keyword_scores = _keyword_scores(query, chunks)
    if not use_embeddings:
        return [chunk.as_dict(score) for score, chunk in keyword_scores[:k]]

    index = _load_index()
    if index is None or index.model != EMBED_MODEL:
        return [chunk.as_dict(score) for score, chunk in keyword_scores[:k]]

    query_embedding = _ollama_embedding(query, timeout=5.0)
    if query_embedding is None:
        return [chunk.as_dict(score) for score, chunk in keyword_scores[:k]]

    keyword_by_key = {chunk.key: score for score, chunk in keyword_scores}
    max_keyword = max(keyword_by_key.values()) if keyword_by_key else 0.0
    stale_identities = set(index.identity_by_key.values())

    scored: list[tuple[float, Chunk, Optional[dict[str, Any]]]] = []
    for chunk in chunks:
        key = chunk.key
        keyword_score = keyword_by_key.get(key, 0.0)
        normalized_keyword = keyword_score / max_keyword if max_keyword > 0 else 0.0
        vector = index.vectors_by_key.get(key)
        meta: Optional[dict[str, Any]] = None

        if vector is not None and len(vector) == len(query_embedding):
            cosine = _cosine(query_embedding, vector)
            score = (0.75 * cosine) + (0.25 * normalized_keyword)
            if score > 0:
                scored.append((score, chunk, meta))
            continue

        if keyword_score <= 0:
            continue
        score = normalized_keyword
        status = "stale" if (chunk.source, chunk.heading_path) in stale_identities else "missing"
        meta = {"retrieval": "keyword", "index": status, "stale": True}
        scored.append((score, chunk, meta))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk.as_dict(score, meta) for score, chunk, meta in scored[:k]]


def _excerpt(text: str, max_chars: int = 900) -> str:
    compact = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Retrieve chunks from the Timberborn KB.")
    parser.add_argument("query", nargs="*", help="Search query, e.g. 'how much water for a drought'")
    parser.add_argument("-k", "--top-k", type=int, default=3, help="Number of chunks to return")
    parser.add_argument(
        "--build-index",
        action="store_true",
        help=f"Embed all KB chunks and write {_display_path(INDEX_PATH)}",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print KB/index freshness and Ollama reachability.",
    )
    parser.add_argument(
        "--no-embeddings",
        action="store_true",
        help="Skip Ollama embeddings and use local keyword scoring only",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON results")
    args = parser.parse_args(argv)

    if args.build_index:
        return build_index()
    if args.check:
        return check_index()

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
