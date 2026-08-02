"""Vault index + retrieval. sqlite-vec, with a numpy fallback because
some stock python3 builds ship without loadable-extension support and
that is not worth losing a day to on skeleton day.
"""
import json
import sqlite3
from pathlib import Path

import numpy as np

from . import config, llm

_VEC = None


def _connect():
    global _VEC
    con = sqlite3.connect(config.VAULT_DB)
    try:
        import sqlite_vec
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        _VEC = True
    except Exception as e:  # noqa: BLE001
        print(f"[vault] sqlite-vec unavailable ({e}); using numpy fallback")
        _VEC = False
    con.execute("CREATE TABLE IF NOT EXISTS chunks "
                "(id INTEGER PRIMARY KEY, path TEXT, text TEXT, emb TEXT)")
    return con


def chunk(text: str, size: int = 900) -> list[str]:
    paras, buf, out = text.split("\n\n"), "", []
    for p in paras:
        if len(buf) + len(p) > size and buf:
            out.append(buf.strip())
            buf = ""
        buf += p + "\n\n"
    if buf.strip():
        out.append(buf.strip())
    return out


def index(vault_dir: str, embed_fn=None) -> int:
    embed_fn = embed_fn or llm.embed
    con = _connect()
    con.execute("DELETE FROM chunks")
    rows = []
    for p in sorted(Path(vault_dir).rglob("*.md")):
        for c in chunk(p.read_text(encoding="utf-8", errors="ignore")):
            rows.append((str(p), c))
    if not rows:
        print(f"[vault] no .md files under {vault_dir}")
        return 0
    embs = embed_fn([t for _, t in rows])
    con.executemany("INSERT INTO chunks (path, text, emb) VALUES (?,?,?)",
                    # strict: a short embedding batch would silently pair each
                    # chunk with the wrong vector rather than fail.
                    [(p, t, json.dumps(e))
                     for (p, t), e in zip(rows, embs, strict=True)])
    con.commit()
    con.close()
    print(f"[vault] indexed {len(rows)} chunks from {vault_dir}")
    return len(rows)


def retrieve(question: str, k: int = 3, embed_fn=None) -> list[dict]:
    embed_fn = embed_fn or llm.embed
    con = _connect()
    rows = con.execute("SELECT path, text, emb FROM chunks").fetchall()
    con.close()
    if not rows:
        return []
    # Not every endpoint that serves chat also serves embeddings — a LiteLLM
    # proxy in front of vLLM answers /chat/completions and 501s /embeddings.
    # Same reasoning as the dimension check below: notes are framing, so a
    # missing embedder costs tone, not correctness. Warn once and answer
    # without them rather than failing the whole request.
    try:
        q = np.array(embed_fn([question])[0], dtype=np.float32)
    except llm.LLMError as e:
        print(f"[vault] embeddings unavailable ({str(e)[:120]}) — skipping "
              f"notes. Set FD_EMBED_MODEL to an embedding model this endpoint "
              f"serves, or clear the index to silence this.")
        return []
    M = np.array([json.loads(r[2]) for r in rows], dtype=np.float32)

    # Changing embedding model changes vector width, and the index on disk
    # still holds the old one. Left alone this is a numpy matmul error deep
    # in a worker thread — an opaque crash for what is really a stale cache.
    # The vault supplies framing only, never figures, so dropping it costs
    # tone rather than correctness: warn, degrade, keep answering.
    if M.ndim != 2 or M.shape[1] != q.shape[0]:
        print(f"[vault] index is {M.shape[1] if M.ndim == 2 else '?'}-dim but "
              f"{config.EMBED_MODEL} returns {q.shape[0]}-dim — skipping notes. "
              f"Re-index with: python -c \"from filingdesk import vault, "
              f"config; vault.index(config.VAULT_DIR)\"")
        return []

    sims = (M @ q) / (np.linalg.norm(M, axis=1) * np.linalg.norm(q) + 1e-9)
    top = np.argsort(-sims)[:k]
    out = [{"path": rows[i][0], "text": rows[i][1], "score": float(sims[i])}
           for i in top]
    print(f"[vault] top-{k} scores: {[round(o['score'], 3) for o in out]}")
    return out
