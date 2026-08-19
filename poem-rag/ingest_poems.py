#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ingest Phan Ngoc Lan's poems into an ISOLATED Qdrant collection for Elder Care RAG.
CPU-only embedding (bge-m3) -> zero GPU contention with production services.
Creates two collections (unique 'eldercare_' prefix, does not touch edu/law collections):
  - eldercare_poems       : one point per whole poem (recital + thematic retrieval)
  - eldercare_poem_lines  : sliding-window line chunks (pinpoint a remembered line)
"""
import json, os, sys
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from sentence_transformers import SentenceTransformer

POEMS     = os.environ.get("POEMS_JSON", "/home/USER/eldercare/poems_structured.json")
THEMES    = os.environ.get("THEMES_JSON", "/home/USER/eldercare/poem_themes.json")  # optional enrichment
COLL_POEM = "eldercare_poems"
COLL_LINE = "eldercare_poem_lines"
DIM       = 1024
QHOST     = os.environ.get("QDRANT_HOST", "localhost")
QPORT     = int(os.environ.get("QDRANT_PORT", "6333"))

def log(*a):
    print(*a, flush=True)

poems = json.load(open(POEMS, encoding="utf-8"))
log(f"[ingest] loaded {len(poems)} poems from {POEMS}")

# optional theme enrichment (produced by the background workflow); merge if present
themes_by_idx = {}
if os.path.exists(THEMES):
    try:
        td = json.load(open(THEMES, encoding="utf-8"))
        rows = td["poems"] if isinstance(td, dict) and "poems" in td else td
        for r in rows:
            themes_by_idx[int(r["index"])] = r
        log(f"[ingest] merged theme metadata for {len(themes_by_idx)} poems")
    except Exception as e:
        log(f"[ingest] WARN could not read themes ({e}); continuing without")
else:
    log("[ingest] no themes file yet; ingesting core fields only (payload can be enriched later)")

log("[ingest] loading BAAI/bge-m3 on CPU (no GPU touch)...")
model = SentenceTransformer("BAAI/bge-m3", device="cpu")

def embed(texts, bs=8):
    return model.encode(texts, normalize_embeddings=True, batch_size=bs, show_progress_bar=False)

client = QdrantClient(host=QHOST, port=QPORT, timeout=120)

for coll in (COLL_POEM, COLL_LINE):
    try:
        client.delete_collection(coll)
        log(f"[ingest] dropped existing {coll}")
    except Exception:
        pass
    client.create_collection(coll, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
    log(f"[ingest] created {coll} (dim={DIM}, cosine)")

# ---------- whole-poem points ----------
poem_texts = []
for p in poems:
    parts = [p.get("title") or ""]
    if p.get("dedication"):
        parts.append(p["dedication"])
    parts.append(p.get("body") or "")
    poem_texts.append("\n".join(x for x in parts if x))
log(f"[ingest] embedding {len(poem_texts)} whole poems on CPU (this is the slow part)...")
pvecs = embed(poem_texts)
ppoints = []
for i, p in enumerate(poems):
    pl = {
        "poem_id": i,
        "title": p.get("title"),
        "author": p.get("author"),
        "dedication": p.get("dedication"),
        "date": p.get("date"),
        "body": p.get("body"),
        "n_lines": p.get("n_lines"),
        "granularity": "poem",
    }
    t = themes_by_idx.get(i)
    if t:
        pl["themes"]     = t.get("themes")
        pl["addressee"]  = t.get("addressee")
        pl["occasion"]   = t.get("occasion")
        pl["mood"]       = t.get("mood")
        pl["summary_vi"] = t.get("summary_vi")
    ppoints.append(PointStruct(id=i, vector=pvecs[i].tolist(), payload=pl))
client.upsert(COLL_POEM, points=ppoints)
log(f"[ingest] upserted {len(ppoints)} whole-poem points -> {COLL_POEM}")

# ---------- line-chunk points ----------
def chunk_lines(lines, window=6, step=4):
    n = len(lines)
    if n <= window:
        return [(0, n)]
    out, i = [], 0
    while i < n:
        out.append((i, min(i + window, n)))
        if i + window >= n:
            break
        i += step
    return out

line_texts, line_meta = [], []
for pid, p in enumerate(poems):
    lines = [l for l in (p.get("body") or "").split("\n") if l.strip()]
    for (a, b) in chunk_lines(lines):
        line_texts.append("\n".join(lines[a:b]))
        line_meta.append((pid, a, b))
log(f"[ingest] embedding {len(line_texts)} line-chunks on CPU...")
lvecs = embed(line_texts)
lpoints = []
for j, (pid, a, b) in enumerate(line_meta):
    lpoints.append(PointStruct(id=j, vector=lvecs[j].tolist(), payload={
        "poem_id": pid,
        "title": poems[pid].get("title"),
        "chunk_index": j,
        "line_start": a,
        "line_end": b,
        "text": line_texts[j],
        "granularity": "chunk",
    }))
client.upsert(COLL_LINE, points=lpoints)
log(f"[ingest] upserted {len(lpoints)} line-chunk points -> {COLL_LINE}")

log("[ingest] DONE. Counts:")
for coll in (COLL_POEM, COLL_LINE):
    log(f"   {coll}: {client.count(coll).count}")
