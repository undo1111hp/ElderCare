#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Idempotently add the /device/ location (Elder Care :8005) to nginx.conf,
mirroring the /v2/ block. Backs up first. Does NOT touch any existing block."""
import sys, os, datetime, re

CONF = "/home/USER/nginx/nginx.conf"
src = open(CONF, encoding="utf-8").read()

if "location /device/" in src:
    print("[nginx] /device/ already present — no change")
    sys.exit(0)

BLOCK = (
    "        # ── PTalk Signature — Elder Care (/device, host port 8005, WebSocket) ──\n"
    "        location /device/ {\n"
    "            proxy_pass http://host.docker.internal:8005/;\n"
    "            proxy_set_header Host $host;\n"
    "            proxy_set_header X-Real-IP $remote_addr;\n"
    "            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
    "            proxy_set_header X-Forwarded-Proto $scheme;\n"
    "\n"
    "            # WebSocket support\n"
    "            proxy_http_version 1.1;\n"
    "            proxy_set_header Upgrade $http_upgrade;\n"
    "            proxy_set_header Connection \"upgrade\";\n"
    "        }\n\n"
)

# Insert right before the catch-all `location / {` (the last, most-generic route).
m = re.search(r"^[ \t]*location / \{", src, flags=re.M)
if not m:
    print("[nginx] ERROR: could not find catch-all 'location / {' anchor; aborting")
    sys.exit(2)

new = src[:m.start()] + BLOCK + src[m.start():]

ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = f"{CONF}.bak.{ts}"
open(bak, "w", encoding="utf-8").write(src)
open(CONF, "w", encoding="utf-8").write(new)
print(f"[nginx] backup  -> {bak}")
print(f"[nginx] inserted /device/ block before catch-all (offset {m.start()})")
