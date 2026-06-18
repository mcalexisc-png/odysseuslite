# services/memory_hybrid.py
"""
Hybrid semantic memory for Odysseus Lite (Phase 2).

Replacing ChromaDB with SQLite FTS5 + BM25 made memory keyword-only, which
weakens the "agent evolves over time" promise: paraphrased recall stops working.
This module restores *cheap* semantic recall within the lite RAM budget:

  - dense vectors stored in **sqlite-vec** — a single SQLite extension, no server,
    so the single-container rule holds;
  - embeddings from a **tiny static model** (model2vec, default
    `minishlab/potion-base-8M`, ~30MB) — near-zero CPU, no ONNX runtime, fits the
    2-4GB tier. A fastembed/MiniLM model can be swapped in via EMBEDDING_MODEL
    when there's RAM headroom.

It implements the same interface as src.memory_vector.MemoryVectorStore
(.healthy, .add, .remove, .search, .find_similar, .rebuild, .count, .get_stats),
so src/chat_processor.py's existing BM25 + vector fusion lights up unchanged when
MEMORY_BACKEND=hybrid. The keyword (BM25) half of the hybrid lives in
chat_processor; this class supplies the dense half. Fusion in chat_processor is a
weighted blend; this module additionally exposes reciprocal-rank-fusion helpers
for callers that want pure RRF.

Graceful degradation: if sqlite-vec or the embedding model can't load (e.g. the
host has <4GB free), the store reports healthy=False and the initializer falls
back to keyword-only `sqlite_fts` automatically — it never crashes the app.
"""

from __future__ import annotations

import logging
import math
import os
import struct
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "minishlab/potion-base-8M"


def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v is not None and v.strip() else default


def _pack_floats(vec: List[float]) -> bytes:
    """sqlite-vec accepts float32 vectors as a raw little-endian blob."""
    return struct.pack("<%sf" % len(vec), *vec)


# ---------------------------------------------------------------------------
# Embedders — local model2vec (default) or OpenAI, behind EMBEDDING_PROVIDER.
# ---------------------------------------------------------------------------
class _Model2VecEmbedder:
    """Static-embedding backend (model2vec). Tiny RAM, no GPU, no ONNX."""

    def __init__(self, model_name: str):
        from model2vec import StaticModel  # imported lazily so it's optional
        # StaticModel.from_pretrained downloads + caches under HF cache (data/).
        self._model = StaticModel.from_pretrained(model_name)
        self.name = model_name
        # Probe dimension once.
        probe = self._model.encode(["probe"])
        self.dim = int(len(probe[0]))

    def encode(self, texts: List[str]) -> List[List[float]]:
        vecs = self._model.encode(list(texts))
        return [list(map(float, v)) for v in vecs]


class _OpenAIEmbedder:
    """Optional OpenAI embeddings (EMBEDDING_PROVIDER=openai). Needs OPENAI_API_KEY."""

    def __init__(self, model_name: str):
        import httpx  # already a lite dep
        self._httpx = httpx
        self.name = model_name or "text-embedding-3-small"
        self._key = os.getenv("OPENAI_API_KEY", "")
        if not self._key:
            raise RuntimeError("EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is not set")
        self._base = _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        # dims known for the common small model; probe otherwise.
        self.dim = 1536 if "small" in self.name else len(self.encode(["probe"])[0])

    def encode(self, texts: List[str]) -> List[List[float]]:
        resp = self._httpx.post(
            f"{self._base}/embeddings",
            headers={"Authorization": f"Bearer {self._key}"},
            json={"model": self.name, "input": list(texts)},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return [list(map(float, d["embedding"])) for d in data]


def _build_embedder():
    """Return an embedder per EMBEDDING_PROVIDER, or raise on misconfig."""
    provider = _env("EMBEDDING_PROVIDER", "local").lower()
    model = _env("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    if provider == "none":
        raise RuntimeError("EMBEDDING_PROVIDER=none — semantic memory disabled")
    if provider == "openai":
        return _OpenAIEmbedder(model)
    # default: local static embeddings
    return _Model2VecEmbedder(model)


# ---------------------------------------------------------------------------
# sqlite-vec backed store
# ---------------------------------------------------------------------------
class SqliteVecMemoryStore:
    """Dense vector memory backed by sqlite-vec. Mirrors MemoryVectorStore."""

    def __init__(self, data_dir: str, embedding_model=None):
        self.data_dir = data_dir
        self._healthy = False
        self._db = None
        self._embedder = None
        self.db_path = os.path.join(data_dir, "memory_vec.db")
        try:
            self._initialize()
            self._healthy = True
        except Exception as e:
            logger.warning("Hybrid memory (sqlite-vec) DEGRADED, falling back to keyword: %s", e)
            self._healthy = False

    # -- setup --------------------------------------------------------------
    def _initialize(self):
        import sqlite3
        try:
            import sqlite_vec  # the SQLite extension package
        except Exception as e:
            raise RuntimeError(f"sqlite-vec not installed: {e}")

        self._embedder = _build_embedder()
        dim = self._embedder.dim

        os.makedirs(self.data_dir, exist_ok=True)
        db = sqlite3.connect(self.db_path, check_same_thread=False)
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)

        # vec0 virtual table keyed by rowid; a side table maps memory_id<->rowid.
        db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS mem_vec USING vec0(embedding float[{dim}])"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS mem_ids ("
            "  rowid INTEGER PRIMARY KEY,"
            "  memory_id TEXT UNIQUE,"
            "  text TEXT"
            ")"
        )
        db.commit()
        self._db = db
        self._dim = dim
        logger.info(
            "Hybrid memory ready: sqlite-vec @ %s, embedder=%s dim=%d",
            self.db_path, self._embedder.name, dim,
        )

    # -- interface (mirrors src.memory_vector.MemoryVectorStore) ------------
    @property
    def healthy(self) -> bool:
        return self._healthy

    def count(self) -> int:
        if not self._healthy:
            return 0
        try:
            row = self._db.execute("SELECT COUNT(*) FROM mem_ids").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _rowid_for(self, memory_id: str) -> Optional[int]:
        row = self._db.execute(
            "SELECT rowid FROM mem_ids WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        return int(row[0]) if row else None

    def add(self, memory_id: str, text: str):
        if not self._healthy or not text:
            return
        try:
            vec = self._embedder.encode([text])[0]
            blob = _pack_floats(vec)
            existing = self._rowid_for(memory_id)
            if existing is not None:
                self._db.execute("DELETE FROM mem_vec WHERE rowid = ?", (existing,))
                self._db.execute(
                    "INSERT INTO mem_vec(rowid, embedding) VALUES (?, ?)", (existing, blob)
                )
                self._db.execute(
                    "UPDATE mem_ids SET text = ? WHERE rowid = ?", (text, existing)
                )
            else:
                cur = self._db.execute(
                    "INSERT INTO mem_ids(memory_id, text) VALUES (?, ?)", (memory_id, text)
                )
                rowid = cur.lastrowid
                self._db.execute(
                    "INSERT INTO mem_vec(rowid, embedding) VALUES (?, ?)", (rowid, blob)
                )
            self._db.commit()
        except Exception as e:
            logger.warning("hybrid add failed for %s: %s", memory_id, e)

    def remove(self, memory_id: str):
        if not self._healthy:
            return
        try:
            rowid = self._rowid_for(memory_id)
            if rowid is not None:
                self._db.execute("DELETE FROM mem_vec WHERE rowid = ?", (rowid,))
                self._db.execute("DELETE FROM mem_ids WHERE rowid = ?", (rowid,))
                self._db.commit()
        except Exception as e:
            logger.warning("hybrid remove failed for %s: %s", memory_id, e)

    def search(self, query: str, k: int = 8) -> List[Dict]:
        """Return [{"memory_id": str, "score": float}] — score in ~0..1 (higher better)."""
        if not self._healthy or not query.strip():
            return []
        try:
            qvec = self._embedder.encode([query])[0]
            blob = _pack_floats(qvec)
            rows = self._db.execute(
                "SELECT v.rowid, v.distance, m.memory_id "
                "FROM mem_vec v JOIN mem_ids m ON m.rowid = v.rowid "
                "WHERE v.embedding MATCH ? AND k = ? "
                "ORDER BY v.distance",
                (blob, int(k)),
            ).fetchall()
            out = []
            for _rowid, distance, memory_id in rows:
                # vec0 default metric is L2; convert distance -> similarity in (0,1].
                score = 1.0 / (1.0 + float(distance))
                out.append({"memory_id": memory_id, "score": score})
            return out
        except Exception as e:
            logger.warning("hybrid search failed: %s", e)
            return []

    def find_similar(self, text: str, threshold: float = 0.92) -> Optional[str]:
        """Return a near-duplicate memory_id if one exists above `threshold`."""
        hits = self.search(text, k=1)
        if hits and hits[0]["score"] >= threshold:
            return hits[0]["memory_id"]
        return None

    def rebuild(self, memories: List[Dict]):
        """Re-index existing memories (migration path on first hybrid boot)."""
        if not self._healthy:
            return
        try:
            self._db.execute("DELETE FROM mem_vec")
            self._db.execute("DELETE FROM mem_ids")
            self._db.commit()
        except Exception as e:
            logger.warning("hybrid rebuild clear failed: %s", e)
        indexed = 0
        for mem in memories or []:
            mid = mem.get("id") or mem.get("memory_id")
            text = mem.get("text") or mem.get("content") or ""
            if mid and text:
                self.add(str(mid), text)
                indexed += 1
        logger.info("Hybrid memory re-indexed %d existing memories", indexed)

    def get_stats(self) -> Dict:
        return {
            "healthy": self.healthy,
            "backend": "sqlite_vec",
            "embedder": getattr(self._embedder, "name", None),
            "count": self.count(),
            "db_path": self.db_path,
        }


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion helper (for callers that prefer pure RRF over the
# weighted blend in chat_processor).
# ---------------------------------------------------------------------------
def reciprocal_rank_fusion(
    keyword_ranking: List[str],
    vector_ranking: List[str],
    k: int = 60,
) -> List[tuple]:
    """Fuse two ranked id lists via RRF. Returns [(id, score)] sorted desc."""
    scores: Dict[str, float] = {}
    for ranking in (keyword_ranking, vector_ranking):
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
