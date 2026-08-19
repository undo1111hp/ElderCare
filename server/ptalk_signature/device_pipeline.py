# -*- coding: utf-8 -*-
"""
ptalk_signature/device_pipeline.py — Elder Care /device orchestrator.

STT (CPU ZipFormer) + poem-RAG (bge-m3 -> Qdrant eldercare_poems) + Elder prompt,
pushing to the SHARED llm_worker (Gemma) / tts_worker (OmniVoice) UNCHANGED.

Two answer paths:
  • RECITE bypass — when bà asks to read a specific poem we can confidently match,
    push the poem LINE BY LINE straight to STREAM_TTS (each line = its own TTS
    chunk → natural pause between lines, 100% verbatim, no LLM paraphrase).
  • LLM path — chat / list-by-theme / Q&A: inject grounding + real time into the
    Elder system_prompt and push to STREAM_LLM.

Chat events for the device panel: {"type":"TRANSCRIPT"} and {"type":"ANSWER"}.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.redis_bus import (
    get_redis, xadd_json, resp_stream_name, stream_results,
    STREAM_LLM, STREAM_TTS,
)
import ptalk_signature.settings as cfg

# ── Lazy singletons (all CPU — never touch the GPU) ───────────────────────────
_stt = None
_embedder = None
_qdrant = None


def _get_stt():
    global _stt
    if _stt is None:
        os.environ.setdefault("STT_PROVIDER", "cpu")
        from shared.stt_engine import STTEngine
        _stt = STTEngine()
    return _stt


def _get_embedder():
    global _embedder
    if _embedder is None:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        from sentence_transformers import SentenceTransformer
        print("🔤 [device] loading bge-m3 (CPU) for poem RAG...")
        _embedder = SentenceTransformer("BAAI/bge-m3", device="cpu")
    return _embedder


def _get_qdrant():
    global _qdrant
    if _qdrant is None:
        from qdrant_client import QdrantClient
        _qdrant = QdrantClient(host=cfg.QDRANT_HOST, port=cfg.QDRANT_PORT, timeout=10)
    return _qdrant


def warm():
    for fn in (_get_stt, _get_embedder):
        try:
            fn()
        except Exception as e:
            print(f"⚠️ [device] warm failed: {e}")


def transcribe(wav_path: str) -> str:
    try:
        return _get_stt().transcribe(wav_path) or ""
    except Exception as e:
        print(f"⚠️ [device] STT failed: {e}")
        return ""


# ── Real time (server is UTC; give bà correct Vietnam time) ───────────────────
_WD_VI = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]


def _now_vn():
    from datetime import datetime, timedelta, timezone
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=7)))


def _time_block() -> str:
    d = _now_vn()
    return ("\n\n[THỜI GIAN THỰC — khi bà hỏi mấy giờ / hôm nay ngày mấy / thứ mấy, "
            "TRẢ LỜI ĐÚNG theo đây, TUYỆT ĐỐI không tự đoán giờ ngày khác]\n"
            f"Bây giờ là {d.hour} giờ {d.minute:02d} phút, {_WD_VI[d.weekday()]}, "
            f"ngày {d.day} tháng {d.month} năm {d.year}.")


# ── Poem search + grounding (poems-first waterfall) ───────────────────────────
REL_MIN = float(os.getenv("ELDER_RAG_MIN_SCORE", "0.40"))    # relevant-enough to ground
RECITE_MIN = float(os.getenv("ELDER_RECITE_MIN_SCORE", "0.52"))  # confident enough to recite


def _search(query: str, limit: int = 6):
    if not query.strip():
        return []
    try:
        qv = _get_embedder().encode([query], normalize_embeddings=True)[0].tolist()
        client = _get_qdrant()
        try:
            pts = client.query_points(cfg.POEM_COLLECTION, query=qv, limit=limit, with_payload=True).points
        except Exception:
            pts = client.search(cfg.POEM_COLLECTION, query_vector=qv, limit=limit)
        return [{"score": float(getattr(p, "score", 0.0) or 0.0), "payload": p.payload or {}} for p in pts]
    except Exception as e:
        print(f"⚠️ [device] poem search failed (ignored): {e}")
        return []


def _format_grounding(hits) -> str:
    if not hits or hits[0]["score"] < REL_MIN:
        return ""
    top = hits[0]["payload"]
    out = ["[BÀI THƠ LIÊN QUAN NHẤT — NGUYÊN VĂN, dùng để đọc/ngâm nếu bà yêu cầu]"]
    dt = f" (ngày {top.get('date')})" if top.get("date") else ""
    out.append(f"Tên bài: {top.get('title') or ''}{dt}")
    if top.get("summary_vi"):
        out.append(f"Tóm tắt: {top['summary_vi']}")
    out.append("Nội dung nguyên văn:")
    out.append(top.get("body") or "")
    others = hits[1:]
    if others:
        out.append("\n[CÁC BÀI THƠ KHÁC CÓ LIÊN QUAN — dùng để liệt kê / trả lời theo chủ đề]")
        for h in others:
            p = h["payload"]
            th = ", ".join(p.get("themes") or [])
            line = f"- {p.get('title')}"
            if p.get("date"):
                line += f" (ngày {p.get('date')})"
            if th:
                line += f" — chủ đề: {th}"
            if p.get("summary_vi"):
                line += f" — {p['summary_vi']}"
            out.append(line)
    return "\n".join(out)


def retrieve_grounding(query: str) -> str:  # kept for external callers
    return _format_grounding(_search(query, 6))


def _is_recite(q: str) -> bool:
    ql = q.lower()
    return (("đọc" in ql) or ("ngâm" in ql)) and (("thơ" in ql) or ("bài" in ql))


def build_system_prompt(rag_context: str = "") -> str:
    sysp = cfg.ROLE_PROMPT
    if rag_context:
        sysp += ("\n\n<KHO_THƠ>\n"
                 "Dưới đây là dữ liệu thơ của bà mà hệ thống tra cứu tự động (bà KHÔNG tự gửi). "
                 "CHỈ dùng phần này khi bà hỏi về thơ. Khi bà bảo đọc/ngâm một bài mà ở đây có "
                 "nguyên văn, hãy ĐỌC ĐÚNG TỪNG DÒNG y như bản gốc, không sửa, không thêm bớt. "
                 "TUYỆT ĐỐI không bịa thêm thơ ngoài phần này.\n"
                 f"{rag_context}\n</KHO_THƠ>")
    sysp += _time_block()
    return sysp


# ── Shared stream consumer: surface ANSWER text + pass audio chunks through ────
async def _consume(r, resp_stream, joiner=" "):
    parts = []
    async for ev in stream_results(r, resp_stream, timeout=cfg.PIPELINE_TIMEOUT):
        et = ev.get("type", "")
        if et == "TTS_CHUNK":
            rt = ev.get("response_text", "")
            if rt:
                parts.append(rt)
                yield {"type": "ANSWER", "text": joiner.join(parts).strip()}
            yield ev
        elif et in ("TTS_END", "NO_INPUT"):
            yield ev
            return
        else:
            yield ev


async def _push_recite(r, resp_stream, request_id, session_id, device_id, title, lines):
    """Push a poem to TTS line-by-line (each line = its own chunk → natural pauses,
    verbatim, no LLM). Reuses the shared tts_worker unchanged."""
    intro = f"Dạ bà, cháu xin đọc bài {title} cho bà nghe ạ."
    chunks = [intro] + lines
    n = len(chunks)
    for i, text in enumerate(chunks):
        await xadd_json(r, STREAM_TTS, {
            "request_id": request_id,
            "session_id": session_id,
            "text": text,
            "emotion": "00",
            "chunk_index": i,
            "is_last": (i == n - 1),
            "resp_stream": resp_stream,
            "input_text": "",
            "device_id": device_id,
            "skip_moderation": True,   # verbatim poem — do not re-screen
        })


async def process_stream(audio_input_path: str = None, session_id: str = "default",
                         request_id: str = None, device_id: str = "",
                         user_name: str = None, text_override: str = None):
    request_id = request_id or uuid.uuid4().hex[:12]

    # 1) transcript
    if text_override is not None:
        transcript = (text_override or "").strip()
    elif audio_input_path:
        transcript = (await asyncio.to_thread(transcribe, audio_input_path)).strip()
    else:
        transcript = ""

    yield {"type": "TRANSCRIPT", "text": transcript}
    if not transcript:
        yield {"type": "NO_INPUT", "request_id": request_id}
        return

    # 2) poem search (off the event loop)
    hits = await asyncio.to_thread(_search, transcript, 6)
    r = await get_redis()
    resp_stream = resp_stream_name(request_id)

    # 3a) RECITE bypass — verbatim, line-by-line pacing
    if _is_recite(transcript) and hits and hits[0]["score"] >= RECITE_MIN:
        top = hits[0]["payload"]
        lines = [l.strip() for l in (top.get("body") or "").split("\n") if l.strip()]
        if lines:
            print(f"🎙️ [device] RECITE '{top.get('title')}' ({len(lines)} dòng, score={hits[0]['score']:.3f})")
            await _push_recite(r, resp_stream, request_id, session_id, device_id,
                               top.get("title") or "", lines)
            try:
                async for x in _consume(r, resp_stream, joiner="\n"):
                    yield x
            finally:
                try:
                    await r.delete(resp_stream)
                except Exception:
                    pass
            return

    # 3b) LLM path — chat / list / Q&A (grounding + real time in the prompt)
    rag_context = _format_grounding(hits)
    if rag_context:
        print(f"📚 [device] grounding hit ({len(rag_context)} chars)")
    sysp = build_system_prompt(rag_context)
    job = {
        "request_id": request_id,
        "session_id": session_id,
        "device_id": device_id or "elder_device",
        "resp_stream": resp_stream,
        "text": transcript,
        "llm_config": {
            "model": cfg.OPENAI_MODEL,
            "system_prompt": sysp,
            "safety_prompt": cfg.SAFETY_PROMPT,
            "temperature": cfg.LLM_TEMPERATURE,
            "max_tokens": cfg.LLM_MAX_TOKENS,
            "top_p": cfg.LLM_TOP_P,
            "frequency_penalty": cfg.LLM_FREQ_PENALTY,
            "presence_penalty": cfg.LLM_PRESENCE_PENALTY,
            "user_name": user_name or cfg.USER_NAME,
            "location_name": cfg.LOCATION_NAME,
        },
    }
    print(f"\n🧓 [device] START req={request_id} | text={transcript!r} | grounded={bool(rag_context)}")
    await xadd_json(r, STREAM_LLM, job)
    try:
        async for x in _consume(r, resp_stream, joiner=" "):
            yield x
    finally:
        try:
            await r.delete(resp_stream)
        except Exception:
            pass


async def answer_text(text: str, session_id: str = None, device_id: str = "elder_device") -> dict:
    session_id = session_id or f"ask_{uuid.uuid4().hex[:8]}"
    transcript, answer = text, ""
    async for ev in process_stream(text_override=text, session_id=session_id, device_id=device_id):
        et = ev.get("type", "")
        if et == "TRANSCRIPT":
            transcript = ev.get("text", text)
        elif et == "ANSWER":
            answer = ev.get("text", answer)
    return {"question": text, "transcript": transcript, "answer": answer, "session_id": session_id}
