#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test-query the Elder Care poem RAG (CPU embed). Demonstrates retrieval quality
before we wire it into the /device endpoint. Read-only; touches nothing else.
Usage:
  python query_poems.py                 # runs a built-in Vietnamese demo set
  python query_poems.py "your query"    # ad-hoc query
"""
import sys, os
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

COLL_POEM = "eldercare_poems"
COLL_LINE = "eldercare_poem_lines"
QHOST = os.environ.get("QDRANT_HOST", "localhost")
QPORT = int(os.environ.get("QDRANT_PORT", "6333"))

DEMO = [
    "những bài thơ nhớ thương chồng đã mất",
    "thơ về ngày Tết sum vầy",
    "bài thơ tặng cháu nhân dịp sinh nhật",
    "thơ ca ngợi người chiến sĩ công an",
    "nỗi nhớ và tình yêu quê hương đất nước",
    "100 ngày khóc anh",
]

queries = sys.argv[1:] or DEMO

def log(*a): print(*a, flush=True)

log("[query] loading BAAI/bge-m3 on CPU...")
model = SentenceTransformer("BAAI/bge-m3", device="cpu")
client = QdrantClient(host=QHOST, port=QPORT, timeout=60)

def search(coll, qv, limit):
    # query_points is the modern API; fall back to search on older clients
    try:
        return client.query_points(coll, query=qv, limit=limit).points
    except Exception:
        return client.search(coll, query_vector=qv, limit=limit)

for q in queries:
    qv = model.encode([q], normalize_embeddings=True)[0].tolist()
    log("\n" + "=" * 70)
    log("Q:", q)
    log("-- top poems (whole-poem semantic match) --")
    for r in search(COLL_POEM, qv, 5):
        p = r.payload
        themes = p.get("themes")
        extra = f"  [{', '.join(themes)}]" if themes else ""
        log(f"  {r.score:.3f}  {p.get('title')}   (ngày {p.get('date')}){extra}")
    log("-- best line-chunk match --")
    for r in search(COLL_LINE, qv, 1):
        p = r.payload
        snip = (p.get("text") or "").replace("\n", " / ")
        log(f"  {r.score:.3f}  [{p.get('title')}]  “{snip[:90]}”")
