#!/usr/bin/env python3
"""
ptalk_signature/main.py — PTalk Signature (Elder Care) /device WebSocket server.

Isolated clone of ptalk_v2. SAME Opus/PCM audio protocol as v2 (so the device's
voice keeps working identically), but:
  - runs its own CPU STT + Elder persona + poem-RAG grounding (device_pipeline),
    pushing to the SHARED llm_worker/tts_worker (no worker changes, no GPU cost);
  - additionally emits JSON text frames for the on-device chat window:
        {"type":"transcript","text":...}   {"type":"answer","text":...}
    These start with '{' so the device tells them apart from the ALL-CAPS state
    strings (LISTENING/PROCESSING/SPEAKING/STREAM_DONE/IDLE) and 2-digit emotions.
Production /v2 /eldercare /v1 are never touched.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import soundfile as sf
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.opus_codec import (
    OpusDecoder, OpusEncoder,
    decode_ws_message, encode_pcm_to_opus_frames,
    OPUS_SAMPLE_RATE, OPUS_FRAME_SIZE,
)
from shared.resample import resample_48k_to_16k
from ptalk_signature.device_pipeline import process_stream, answer_text
import ptalk_signature.settings as cfg

# ── Paths ────────────────────────────────────────────────────
AUDIO_FILES_DIR = Path(os.getenv("AUDIO_FILES_DIR", "audio_files")).resolve()
VOICESTART_DIR = Path(os.getenv("VOICESTART_DIR", str(AUDIO_FILES_DIR / "voicestart"))).resolve()
BUFFER_GLOB = os.getenv("BUFFER_GLOB", "buffer_wait_*.wav")

# ── Audio settings ───────────────────────────────────────────
INTERNAL_SR = 16000
MIN_AUDIO_MS = int(os.getenv("MIN_AUDIO_MS", "300"))
MIN_AUDIO_BYTES = int(INTERNAL_SR * (MIN_AUDIO_MS / 1000.0) * 2)

# ── Opus streaming settings ──────────────────────────────────
OPUS_BATCH_SIZE = 10
OPUS_FRAME_DURATION = 0.02
TARGET_BUFFER_SEC = 3.0

_device_ws: Dict[str, WebSocket] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "=" * 60)
    print("🧓 PTalk Signature — Elder Care /device server")
    print(f"   Audio Dir:  {AUDIO_FILES_DIR}")
    print(f"   Model:      {cfg.OPENAI_MODEL}")
    print("=" * 60 + "\n")
    # pre-load CPU STT + bge-m3 in the background so the first turn isn't slow
    import threading
    from ptalk_signature.device_pipeline import warm
    threading.Thread(target=warm, daemon=True).start()
    yield


app = FastAPI(title="PTalk Signature (Elder Care)", lifespan=lifespan)


# ── Audio helpers (verbatim from ptalk_v2) ────────────────────
def _save_audio_to_wav(pcm_bytes: bytes, session_id: str) -> str:
    import wave
    AUDIO_FILES_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    uid = uuid.uuid4().hex[:6]
    path = AUDIO_FILES_DIR / f"device_{session_id}_{ts}_{uid}.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(INTERNAL_SR)
        wf.writeframes(pcm_bytes)
    return str(path)


def _load_wav_as_pcm_48k(wav_path: str) -> Optional[np.ndarray]:
    try:
        wav, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
        wav = wav.mean(axis=1) if wav.shape[1] > 1 else wav[:, 0]
        wav = np.clip(wav, -1.0, 1.0)
        pcm = (wav * 32767.0).astype(np.int16)
        if sr == 16000:
            from shared.resample import resample_16k_to_48k
            pcm = resample_16k_to_48k(pcm)
        elif sr == 24000:
            from shared.resample import resample_24k_to_48k
            pcm = resample_24k_to_48k(pcm)
        elif sr != OPUS_SAMPLE_RATE:
            new_len = int(len(wav) * OPUS_SAMPLE_RATE / sr)
            wav = np.interp(np.linspace(0, 1, new_len), np.linspace(0, 1, len(wav)), wav).astype("float32")
            pcm = (wav * 32767.0).astype(np.int16)
        return pcm
    except Exception as e:
        print(f"❌ Failed to load WAV: {e}")
        return None


async def _stream_wav_as_format(websocket, wav_path, encoder, req_gen, get_gen, should_stop, is_pcm_out_mode=False):
    if not wav_path or not wav_path.exists():
        return
    try:
        pcm_48k = _load_wav_as_pcm_48k(str(wav_path))
        if pcm_48k is None or len(pcm_48k) == 0:
            return
        await websocket.send_text("SPEAKING")
        t0 = time.time()
        if is_pcm_out_mode:
            import struct
            pcm_bytes = pcm_48k.tobytes()
            CHUNK_SIZE = 1920
            offset = 0
            frames = []
            while offset < len(pcm_bytes):
                chunk = pcm_bytes[offset:offset + CHUNK_SIZE]
                if len(chunk) < CHUNK_SIZE:
                    chunk += b'\x00' * (CHUNK_SIZE - len(chunk))
                frames.append(struct.pack('<H', len(chunk)) + chunk)
                offset += CHUNK_SIZE
        else:
            encoder.reset()
            frames = encode_pcm_to_opus_frames(encoder, pcm_48k)
            if not frames:
                return
        for i in range(0, len(frames), OPUS_BATCH_SIZE):
            if should_stop() or req_gen != get_gen():
                break
            batch = frames[i:i + OPUS_BATCH_SIZE]
            try:
                await websocket.send_bytes(b''.join(batch))
            except Exception:
                break
            elapsed = time.time() - t0
            expected = (i + len(batch) - 15) * OPUS_FRAME_DURATION
            sleep_t = expected - elapsed - 0.01
            if sleep_t > 0:
                await asyncio.sleep(sleep_t)
    except (asyncio.CancelledError, Exception):
        return


async def _send_json(websocket, obj):
    try:
        await websocket.send_text(json.dumps(obj, ensure_ascii=False))
        return True
    except Exception:
        return False


# ── WebSocket endpoint ────────────────────────────────────────
@app.websocket("/ws")
@app.websocket("/voice/ws")
@app.websocket("/device/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()

    device_id = "unknown"
    raw_handshake = ""
    try:
        first = await websocket.receive()
        text = first.get("text", "") or ""
        try:
            data = json.loads(text)
            device_id = data.get("device_id", text) if isinstance(data, dict) else text
        except Exception:
            device_id = text
        raw_handshake = text
    except Exception:
        pass

    print(f"\n🔌 Elder device connected: {raw_handshake or device_id}")
    _device_ws[device_id] = websocket

    session_id = f"{device_id}_{int(time.time())}"
    session_user_name = cfg.USER_NAME
    is_button_active = False
    is_processing = False
    should_stop_streaming = False
    is_pcm_out_mode = False
    current_gen = 0
    pcm_acc_48k = np.zeros((0,), dtype=np.int16)
    from shared.redis_bus import get_redis, mark_cancelled, resp_stream_name

    opus_decoder = OpusDecoder()
    opus_encoder = OpusEncoder()

    connection_alive = True
    current_task: Optional[asyncio.Task] = None
    current_req_id: Optional[str] = None
    streaming_stopped = asyncio.Event()
    streaming_stopped.set()

    from shared.tts_engine import TTSEngine
    _tts_engine = TTSEngine()

    _wait_messages = [
        "Bà đợi cháu một chút nhé, cháu đang tìm hiểu ạ.",
        "Bà chờ cháu một xíu nhé.",
        "Để cháu nghĩ một chút bà nhé.",
    ]

    async def handle_pipeline(req_gen: int, sess: str, full_audio_16k: Optional[bytes],
                              req_id: str, text_override: Optional[str] = None):
        nonlocal connection_alive, should_stop_streaming, is_processing
        thinking_task = None
        wait_audio_done = asyncio.Event()
        wait_audio_done.set()
        streaming_stopped.clear()
        try:
            wav_path = None
            if text_override is None:
                wav_path = await asyncio.to_thread(_save_audio_to_wav, full_audio_16k, sess)

            first_chunk = True
            stream_t0 = None
            stream_frame_offset = 0

            async def _send_wait_if_slow():
                await asyncio.sleep(8)
                if not first_chunk or req_gen != current_gen or should_stop_streaming:
                    return
                import random
                msg = random.choice(_wait_messages)
                try:
                    await websocket.send_text("THINKING")
                except Exception:
                    return
                try:
                    wav = await asyncio.to_thread(_tts_engine.synthesize, msg)
                    if first_chunk and req_gen == current_gen and not should_stop_streaming:
                        wait_audio_done.clear()
                        await _stream_wav_as_format(
                            websocket, Path(wav), opus_encoder, req_gen,
                            lambda: current_gen, lambda: should_stop_streaming,
                            is_pcm_out_mode=is_pcm_out_mode)
                except Exception as e:
                    print(f"❌ [Wait] TTS failed: {e}")
                finally:
                    wait_audio_done.set()

            thinking_task = asyncio.create_task(_send_wait_if_slow())

            opus_encoder.reset()
            pcm_remainder = np.array([], dtype=np.int16)

            async for event in process_stream(audio_input_path=wav_path, session_id=sess,
                                               request_id=req_id, device_id=device_id,
                                               user_name=session_user_name, text_override=text_override):
                if req_gen != current_gen or should_stop_streaming:
                    break

                et = event.get("type")

                # ── chat text frames for the device's conversation panel ──
                if et == "TRANSCRIPT":
                    await _send_json(websocket, {"type": "transcript", "text": event.get("text", "")})
                    continue
                if et == "ANSWER":
                    await _send_json(websocket, {"type": "answer", "text": event.get("text", "")})
                    continue

                if et == "TTS_CHUNK":
                    if first_chunk:
                        if thinking_task and not thinking_task.done():
                            if wait_audio_done.is_set():
                                thinking_task.cancel()
                                try: await thinking_task
                                except: pass
                            else:
                                try:
                                    await asyncio.wait_for(thinking_task, timeout=30)
                                except (asyncio.TimeoutError, Exception):
                                    pass
                        emotion = event.get("emotion_details", event.get("emotion", "00"))
                        try:
                            await websocket.send_text(emotion)
                            await websocket.send_text("SPEAKING")
                        except Exception:
                            connection_alive = False
                            return
                        first_chunk = False

                    output_audio = event.get("audio_output_path") or event.get("output_audio")
                    if output_audio and os.path.exists(str(output_audio)):
                        try:
                            pcm_48k = _load_wav_as_pcm_48k(str(output_audio))
                            if pcm_48k is None or len(pcm_48k) == 0:
                                continue
                            if stream_t0 is None:
                                stream_t0 = time.time()
                                stream_frame_offset = 0
                            if len(pcm_remainder) > 0:
                                pcm_48k = np.concatenate((pcm_remainder, pcm_48k))
                            num_frames = len(pcm_48k) // OPUS_FRAME_SIZE
                            encode_len = num_frames * OPUS_FRAME_SIZE
                            pcm_remainder = pcm_48k[encode_len:]
                            pcm_48k = pcm_48k[:encode_len]
                            if len(pcm_48k) == 0:
                                continue

                            if is_pcm_out_mode:
                                import struct
                                pcm_bytes = pcm_48k.tobytes()
                                CHUNK_SIZE = 1920
                                offset = 0
                                pcm_t0 = time.time()
                                pcm_frames_sent = 0
                                while offset < len(pcm_bytes):
                                    if should_stop_streaming or req_gen != current_gen:
                                        break
                                    chunk = pcm_bytes[offset:offset + CHUNK_SIZE]
                                    if len(chunk) < CHUNK_SIZE:
                                        chunk += b"\x00" * (CHUNK_SIZE - len(chunk))
                                    frame = struct.pack('<H', len(chunk)) + chunk
                                    try:
                                        await websocket.send_bytes(frame)
                                    except Exception:
                                        connection_alive = False
                                        break
                                    stream_frame_offset += 1
                                    pcm_frames_sent += 1
                                    offset += CHUNK_SIZE
                                    elapsed = time.time() - pcm_t0
                                    expected = (pcm_frames_sent - 15) * 0.02
                                    sleep_t = expected - elapsed
                                    if sleep_t > 0:
                                        await asyncio.sleep(sleep_t)
                            else:
                                frames = encode_pcm_to_opus_frames(opus_encoder, pcm_48k)
                                if not frames:
                                    continue
                                for i in range(0, len(frames), OPUS_BATCH_SIZE):
                                    if should_stop_streaming or req_gen != current_gen:
                                        break
                                    batch = frames[i:i + OPUS_BATCH_SIZE]
                                    try:
                                        await websocket.send_bytes(b''.join(batch))
                                    except Exception:
                                        connection_alive = False
                                        break
                                    stream_frame_offset += len(batch)
                                    elapsed = time.time() - stream_t0
                                    total_audio_sec = stream_frame_offset * OPUS_FRAME_DURATION
                                    expected = total_audio_sec - TARGET_BUFFER_SEC
                                    sleep_t = expected - elapsed
                                    if sleep_t > 0:
                                        await asyncio.sleep(sleep_t)
                                    else:
                                        await asyncio.sleep(0.100)
                        except Exception as e:
                            print(f"❌ Stream error: {e}")
                            connection_alive = False
                    else:
                        print("⚠️ Output audio missing for chunk")

                elif et in ("TTS_END", "NO_INPUT"):
                    break

            # No TTS chunks — fallback voice line
            if first_chunk and connection_alive and req_gen == current_gen:
                fallback_text = "Cháu chưa nghe rõ ạ, bà nói lại giúp cháu một lần nữa nhé."
                try:
                    fb = await asyncio.to_thread(_tts_engine.synthesize, fallback_text)
                    if fb and os.path.exists(str(fb)):
                        await _send_json(websocket, {"type": "answer", "text": fallback_text})
                        await websocket.send_text("00")
                        await websocket.send_text("SPEAKING")
                        first_chunk = False
                        await _stream_wav_as_format(
                            websocket, Path(fb), opus_encoder, req_gen,
                            lambda: current_gen, lambda: should_stop_streaming,
                            is_pcm_out_mode=is_pcm_out_mode)
                except Exception as e:
                    print(f"❌ Fallback TTS failed: {e}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"❌ Pipeline error: {e}")
        finally:
            if thinking_task and not thinking_task.done():
                thinking_task.cancel()
                try: await thinking_task
                except: pass
            is_processing = False
            streaming_stopped.set()
            if req_gen == current_gen:
                should_stop_streaming = False
            if connection_alive and req_gen == current_gen:
                try:
                    # opus-silence tail is an ESP32 I2S-DMA workaround; on PCM_OUT (Pi)
                    # it would be decoded as a noise burst right before STREAM_DONE.
                    if not is_pcm_out_mode:
                        silence_pcm = np.zeros(OPUS_FRAME_SIZE * 3, dtype=np.int16)
                        silence_frames = encode_pcm_to_opus_frames(opus_encoder, silence_pcm)
                        await websocket.send_bytes(b''.join(silence_frames))
                    await websocket.send_text("STREAM_DONE")
                    if stream_t0 is not None and stream_frame_offset > 0:
                        expected_duration = stream_frame_offset * OPUS_FRAME_DURATION
                        elapsed = time.time() - stream_t0
                        time_to_wait = expected_duration - elapsed
                        if time_to_wait > 0:
                            await asyncio.sleep(min(time_to_wait + 6.0, 25.0))
                        else:
                            await asyncio.sleep(3)
                    else:
                        await asyncio.sleep(3)
                    if req_gen == current_gen and connection_alive:
                        await websocket.send_text("IDLE")
                except Exception:
                    connection_alive = False

    async def _start_text_turn(user_text: str):
        nonlocal is_processing, current_req_id, current_task, current_gen, should_stop_streaming
        req_gen = current_gen
        try:
            await websocket.send_text("PROCESSING")
        except Exception:
            return
        current_req_id = uuid.uuid4().hex[:12]
        current_task = asyncio.create_task(
            handle_pipeline(req_gen, session_id, None, current_req_id, text_override=user_text))

    try:
        while True:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                print(f"🔌 Disconnected: {device_id}")
                break
            except RuntimeError as e:
                if "disconnect" in str(e).lower():
                    break
                raise

            if "text" in message and message["text"] is not None:
                cmd = message["text"]

                # JSON command (typed chat / test) — {"type":"text","text":"..."}
                if cmd.startswith("{"):
                    try:
                        obj = json.loads(cmd)
                    except Exception:
                        obj = None
                    if isinstance(obj, dict) and obj.get("type") == "text":
                        user_text = (obj.get("text") or "").strip()
                        if user_text:
                            current_gen += 1
                            should_stop_streaming = True
                            if current_task and not current_task.done():
                                current_task.cancel()
                                try: await asyncio.wait_for(current_task, timeout=2.0)
                                except Exception: pass
                            should_stop_streaming = False
                            await _start_text_turn(user_text)
                        continue

                if cmd in ("START", "START_PCM", "START_PCM_OUT"):
                    is_pcm_out_mode = (cmd == "START_PCM_OUT")
                    # ESP32 (opus downlink) needs an opus-silence primer to clear its
                    # I2S DMA. The Pi uses PCM_OUT (raw PCM16 downlink) — opus bytes on
                    # that channel get decoded as LOUD NOISE at the moment voice starts.
                    if not is_pcm_out_mode:
                        silence_pcm = np.zeros(OPUS_FRAME_SIZE * 3, dtype=np.int16)
                        silence_frames = encode_pcm_to_opus_frames(opus_encoder, silence_pcm)
                        try: await websocket.send_bytes(b''.join(silence_frames))
                        except: pass

                    if current_req_id:
                        async def _cancel_req(req_to_cancel):
                            try:
                                r = await get_redis()
                                await mark_cancelled(r, req_to_cancel)
                                await r.delete(resp_stream_name(req_to_cancel))
                            except Exception as e:
                                print(f"⚠️ cancel old req failed: {e}")
                        asyncio.create_task(_cancel_req(current_req_id))

                    is_pcm_out_mode = (cmd == "START_PCM_OUT")
                    current_req_id = None
                    current_gen += 1
                    should_stop_streaming = True
                    if current_task and not current_task.done():
                        current_task.cancel()
                        try:
                            await asyncio.wait_for(current_task, timeout=2.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                            pass
                    streaming_stopped.set()
                    is_button_active = True
                    is_processing = False
                    should_stop_streaming = False
                    pcm_acc_48k = np.zeros((0,), dtype=np.int16)
                    opus_decoder.reset()
                    opus_encoder.reset()
                    try:
                        await websocket.send_text("LISTENING")
                    except Exception:
                        connection_alive = False
                        break
                    continue

                if cmd == "END":
                    is_button_active = False
                    if len(pcm_acc_48k) == 0:
                        try: await websocket.send_text("IDLE")
                        except Exception:
                            connection_alive = False
                            break
                        continue
                    is_processing = True
                    req_gen = current_gen
                    try:
                        await websocket.send_text("PROCESSING")
                    except Exception:
                        connection_alive = False
                        break
                    pcm_16k = resample_48k_to_16k(pcm_acc_48k)
                    full_audio = pcm_16k.tobytes()
                    pcm_acc_48k = np.zeros((0,), dtype=np.int16)
                    if len(full_audio) < MIN_AUDIO_BYTES:
                        is_processing = False
                        try:
                            await websocket.send_text("STREAM_DONE")
                            await websocket.send_text("IDLE")
                        except Exception:
                            connection_alive = False
                            break
                        continue
                    current_req_id = uuid.uuid4().hex[:12]
                    current_task = asyncio.create_task(
                        handle_pipeline(req_gen, session_id, full_audio, current_req_id))
                    continue

                if cmd == "CLEAR_SESSION":
                    session_id = f"{device_id}_{int(time.time())}"
                    try: await websocket.send_text("SESSION_CLEARED")
                    except: pass
                    continue

            if "bytes" in message and message["bytes"] is not None:
                if is_button_active and not is_processing:
                    # Device uplink = batched Opus frames; decode -> PCM @ 48kHz
                    pcm_block = decode_ws_message(opus_decoder, message["bytes"])
                    if len(pcm_block) > 0:
                        pcm_acc_48k = np.concatenate((pcm_acc_48k, pcm_block))

    except Exception as e:
        print(f"❌ WS error: {e}")
    finally:
        print(f"👋 Connection closed: {device_id}")
        _device_ws.pop(device_id, None)
        if current_task and not current_task.done():
            current_task.cancel()
        if current_req_id:
            try:
                r = await get_redis()
                await mark_cancelled(r, current_req_id)
                await r.delete(resp_stream_name(current_req_id))
            except Exception as e:
                print(f"⚠️ cancel on disconnect failed: {e}")


# ── HTTP ──────────────────────────────────────────────────────
@app.get("/health")
@app.get("/device/health")
async def health():
    return JSONResponse({"status": "ok", "service": "ptalk-signature-eldercare", "model": cfg.OPENAI_MODEL})


@app.post("/ask")
@app.post("/device/ask")
async def device_ask(req: Request):
    """Text-only test endpoint (no audio). Curl-friendly; also usable as a typed
    chat backend. Runs the SAME Elder pipeline via the shared LLM workers."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "body phải là JSON"}, status_code=400)
    q = (body.get("question") or body.get("text") or body.get("q") or "").strip()
    if not q:
        return JSONResponse({"error": "thiếu 'question'"}, status_code=400)
    try:
        res = await answer_text(q, session_id=body.get("session_id"))
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return JSONResponse(res)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ptalk_signature.main:app", host="0.0.0.0", port=8005, reload=False)
