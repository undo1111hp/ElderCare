"""Real-time voice engine — port of the Flutter StreamingVoiceClient.

Protocol (validated against ws://YOUR_L40S_HOST:8000/v2/ws):
  connect -> send JSON {device_id, firmware_version}
  per turn: send "START_PCM_OUT" -> stream Opus frames -> send "END"
  server text events: LISTENING, PROCESSING, THINKING, SPEAKING, STREAM_DONE, IDLE
  server binary: raw PCM16 frames (uint16 LE length prefix) -> play directly

Runs its own asyncio loop in a background thread. Public talk_start/talk_stop/
cancel are thread-safe and can be called from the Qt UI thread.
"""
import asyncio
import json
import threading

from .opus_codec import OpusEncoder
from .protocol import pack_frame, unpack_frames
from .audio_io import SharedMic, Player
from .wakeword import WakeWord
from .vad import Endpointer
from . import tts


class VoiceEngine:
    def __init__(self, cfg, on_event=lambda e: None):
        self.cfg = cfg
        self.on_event = on_event
        a = cfg["audio"]
        self.sr = a["sample_rate"]
        self.ch = a["channels"]
        self.frame_bytes = cfg.pcm_frame_bytes
        self._in_dev = a["input_device"]
        self._out_dev = a["output_device"]
        self._enc = OpusEncoder(self.sr, self.ch, a.get("opus_bitrate", 24000))

        self._loop = None
        self._thread = None
        self.ws = None
        self._sendq = None
        self._mic = SharedMic(self._in_dev, self.sr, self.ch, self.frame_bytes)
        self._player = Player(self._out_dev, self.sr, self.ch,
                              gain=a.get("output_gain", 0.6))
        self._accepting = False
        self._talking = False
        self._closing = False

        # ---- hands-free wake word ("Bi ơi") ----
        w = dict(cfg.get("wakeword", {}) or {})
        self._ww_cfg = w
        self._hands_free = False        # is the *current* turn hands-free?
        self._ending = False            # endpointer already asked to stop?
        self._wake = None
        self._always_on = False
        self._endpoint = Endpointer(
            frame_ms=a.get("frame_ms", 20),
            silence_ms=w.get("silence_ms", 1300),
            lead_ms=w.get("lead_ms", 6000),
            max_ms=w.get("max_ms", 13000),
        )
        if w.get("enabled"):
            self._wake = self._make_wake()
            self._always_on = self._wake.available()

    def _make_wake(self):
        w = self._ww_cfg
        return WakeWord(
            w.get("model_path"), self._on_wake_detected,
            threshold=w.get("threshold", 0.9), src_rate=self.sr,
            refractory_ms=w.get("refractory_ms", 1800),
            trigger_hits=w.get("trigger_hits", 2))

    # ---------------- lifecycle ----------------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._sendq = asyncio.Queue()
        self._loop.create_task(self._main())
        try:
            self._loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(self._loop)
                for t in pending:
                    t.cancel()
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self._loop.close()

    async def _main(self):
        while not self._closing:
            try:
                await self._connect()
                await self._reader()          # returns when ws closes
            except Exception as e:
                self._emit(f"ERR:{e}")
            if self._closing:
                break
            await asyncio.sleep(2)            # reconnect backoff

    async def _connect(self):
        import websockets
        s = self.cfg["server"]
        self.ws = await websockets.connect(
            s["ws_url"], open_timeout=10, max_size=None, ping_interval=None)
        await self.ws.send(json.dumps({
            "device_id": s["device_id"],
            "firmware_version": s["firmware_version"],
        }))
        self._emit("CONNECTED")
        self._loop.create_task(self._sender())
        self._loop.call_soon(self._rearm_wake)   # start listening for "Bi ơi"

    async def _sender(self):
        while True:
            item = await self._sendq.get()
            if item is None:
                continue
            try:
                await self.ws.send(item)
            except Exception:
                break

    async def _reader(self):
        async for msg in self.ws:
            if isinstance(msg, (bytes, bytearray)):
                if self._accepting:
                    try:
                        for pcm in unpack_frames(bytes(msg)):
                            self._player.write(pcm)
                    except Exception:
                        pass
            else:
                ev = msg.strip()
                # /device chat frames: {"type":"transcript"|"answer","text":...}
                if ev[:1] == "{":
                    try:
                        obj = json.loads(ev)
                    except Exception:
                        obj = None
                    if isinstance(obj, dict) and obj.get("type") in ("transcript", "answer"):
                        pre = "CHAT_T:" if obj["type"] == "transcript" else "CHAT_A:"
                        self._emit(pre + (obj.get("text") or ""))
                        continue
                self._emit(ev)
                if ev.upper() == "IDLE":
                    self._accepting = False
                    self._loop.create_task(self._drain_then_idle())
        self._emit("DISCONNECTED")

    async def _drain_then_idle(self):
        # let the speaker buffer empty so the UI returns to idle when sound ends
        for _ in range(200):  # cap ~10s
            if self._player.pending() == 0:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        self._emit("PLAYBACK_DONE")
        self._rearm_wake()               # turn finished -> listen for "Bi ơi" again

    def _emit(self, ev):
        try:
            self.on_event(ev)
        except Exception:
            pass

    # ---------------- talk control (any thread) ----------------
    def talk_start(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._do_talk_start(False), self._loop)

    def talk_stop(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._do_talk_stop(), self._loop)

    def cancel(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._cancel(), self._loop)

    def set_output_gain(self, g):
        """Live-adjust speaker output gain (0..1) from Settings."""
        try:
            self._player.gain = float(g)
        except Exception:
            pass

    # ---------------- mic sharing / wake arming ----------------
    def _ensure_mic(self):
        if not self._mic.running():
            try:
                self._mic.start()
            except Exception as e:
                self._emit(f"ERR:mic:{e}")

    def _rearm_wake(self):
        """Idle again: point the shared mic at the wake detector (hands-free on).
        When hands-free is off this is a no-op and the mic stays closed until a
        button press — identical to the original push-to-talk behaviour."""
        self._ending = False
        self._hands_free = False
        if self._always_on and self._wake and self._wake.available() and not self._talking:
            self._endpoint.reset()
            self._wake.reset()
            self._wake.mute(False)
            self._ensure_mic()
            self._mic.set_consumer(self._wake_consumer)

    # ---------------- mic consumers (run on the mic thread) ----------------
    def _wake_consumer(self, buf):
        if self._wake:
            self._wake.feed(buf)

    def _uplink_consumer(self, buf):
        try:
            packed = pack_frame(self._enc.encode(buf))
        except Exception:
            packed = None
        if packed is not None:
            self._loop.call_soon_threadsafe(self._sendq.put_nowait, packed)
        if self._hands_free and not self._ending:
            reason = self._endpoint.feed(buf)
            if reason:                                   # speech ended / timed out
                self._ending = True
                asyncio.run_coroutine_threadsafe(self._do_talk_stop(), self._loop)

    # ---------------- wake -> hands-free turn ----------------
    def _on_wake_detected(self, score):
        # called on the mic thread; hop onto the event loop
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._begin_hands_free(score), self._loop)

    async def _begin_hands_free(self, score):
        if self._talking or self._closing or self.ws is None:
            return
        if self._wake:
            self._wake.mute(True)
        self._mic.set_consumer(None)                     # stop feeding the detector
        self._emit("WAKE")                               # UI: "cháu nghe đây..."
        try:
            tts.ack()                                    # gentle acknowledgement blip
        except Exception:
            pass
        # start capturing *after* the ack so it isn't heard as the first words
        self._loop.call_later(
            0.5, lambda: asyncio.ensure_future(self._do_talk_start(True)))

    # ---------------- unified start / stop ----------------
    async def _do_talk_start(self, hands_free):
        if self._talking or self.ws is None:
            return
        self._talking = True
        self._accepting = True
        self._hands_free = hands_free
        self._ending = False
        if self._wake:
            self._wake.mute(True)
        self._player.start()
        if hands_free:
            self._endpoint.reset()
        try:
            await self.ws.send("START_PCM_OUT")
        except Exception as e:
            self._emit(f"ERR:{e}")
            self._talking = False
            return
        self._ensure_mic()
        self._mic.set_consumer(self._uplink_consumer)

    async def _do_talk_stop(self):
        if not self._talking:
            return
        self._talking = False
        self._mic.set_consumer(None)                     # stop uplink; wake stays muted
        if not self._always_on and self._mic.running():
            await self._loop.run_in_executor(None, self._mic.stop)
        # queued opus frames flush first (FIFO), then END
        await self._sendq.put("END")

    async def _cancel(self):
        self._accepting = False
        self._talking = False
        self._ending = False
        self._mic.set_consumer(None)
        if not self._always_on and self._mic.running():
            await self._loop.run_in_executor(None, self._mic.stop)
        self._player.flush()
        self._player.stop()
        self._emit("CANCELLED")
        self._rearm_wake()

    # ---------------- wake-word runtime toggle (from Settings) ----------------
    def wake_status(self):
        return {
            "enabled": bool(self._always_on),
            "available": bool(self._wake and self._wake.available()),
            "configured": bool(self._ww_cfg.get("model_path")),
        }

    def set_wakeword_enabled(self, on):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._set_ww(bool(on)), self._loop)

    async def _set_ww(self, on):
        if on:
            if self._wake is None:
                self._wake = self._make_wake()
            self._always_on = self._wake.available()
            self._rearm_wake()
        else:
            self._always_on = False
            if self._wake:
                self._wake.mute(True)
            if not self._talking:
                self._mic.set_consumer(None)
                if self._mic.running():
                    await self._loop.run_in_executor(None, self._mic.stop)

    def shutdown(self):
        self._closing = True
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._begin_shutdown)

    def _begin_shutdown(self):
        # runs on the loop thread: stop audio, close ws, cancel tasks, stop loop
        try:
            self._player.stop()
        except Exception:
            pass
        try:
            self._mic.stop()
        except Exception:
            pass

        async def _close():
            try:
                if self.ws:
                    await self.ws.close()
            except Exception:
                pass
            cur = asyncio.current_task()
            for t in asyncio.all_tasks(self._loop):
                if t is not cur:
                    t.cancel()
            self._loop.stop()

        asyncio.ensure_future(_close())
