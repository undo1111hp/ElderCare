#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Demonstrate PRECISE 'list poems about theme X' via payload filter on themes.
This is the intent-driven 'liệt kê các bài thơ về chủ đề ...' behavior — no
embedding needed, pure metadata filter, so it is exact and instant."""
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchAny

client = QdrantClient(host="localhost", port=6333, timeout=30)
COLL = "eldercare_poems"

def list_theme(label, tags):
    flt = Filter(should=[FieldCondition(key="themes", match=MatchAny(any=tags))])
    pts, _ = client.scroll(COLL, scroll_filter=flt, limit=100, with_payload=True)
    pts = sorted(pts, key=lambda p: p.payload.get("poem_id", 0))
    print(f"\n### {label}  (tags={tags})  -> {len(pts)} bài")
    for p in pts:
        print(f"   • {p.payload['title']}   ({p.payload.get('mood')})")

list_theme("Thơ nhớ thương CHỒNG đã mất", ["nhớ thương chồng","khóc chồng","khóc thương chồng","mất chồng","chồng qua đời"])
list_theme("Thơ về TẾT", ["Tết","Tết Tân Sửu","Tết Nhâm Dần","chợ hoa xuân"])
list_theme("Thơ tặng/ mừng CHÁU", ["tình bà cháu","cháu nội","cháu gái","cháu dâu","đích tôn"])
list_theme("Thơ về nghề DIỄN XUẤT / phim ảnh", ["đam mê diễn xuất","đóng phim diễn xuất","nghề diễn","đoàn làm phim","vai bà nội"])
list_theme("Thơ YÊU NƯỚC / quê hương", ["yêu nước","quê hương","tình yêu quê hương","tự hào dân tộc","thống nhất đất nước"])
