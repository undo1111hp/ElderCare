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
import os
import threading
import time as _time

from .opus_codec import OpusEncoder
from .protocol import pack_frame, unpack_frames
from .audio_io import SharedMic, Player
from .wakeword import WakeWord
from .vad import Endpointer
from . import tts

_DEBUG = os.environ.get("PTALK_WAKE_DEBUG") == "1"
_DEBUG_PATH = os.environ.get("PTALK_WAKE_LOG", "/tmp/ptalk_wake.log")


def _dlog(msg):
    if not _DEBUG:
        return
    try:
        with open(_DEBUG_PATH, "a") as f:
            f.write("%s %s" % (_time.strftime("%H:%M:%S"), msg) + chr(10))
    except Exception:
        pass


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

        # ---- hands-free wake word ("Bi oi") ----
        w = dict(cfg.get("wakeword", {}) or {})
        self._ww_cfg = w
        self._hands_free = False     # is the CURRENT turn hands-free?
        self._ending = False         # endpointer already asked to stop?
        self._local_reply = False    # a local (espeak) reply is playing
        self._frames_sent = 0
        self._enc_errs = 0
        self._turn_id = 0
        self._wake = None
        self._always_on = False
        self._endpoint = Endpointer(
            frame_ms=a.get("frame_ms", 20),
            silence_ms=w.get("silence_ms", 1300),
            lead_ms=w.get("lead_ms", 6000),
            max_ms=w.get("max_ms", 13000))
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
        # Open capture AND playback once, here, and keep both for the whole
        # session. Opening/closing them mid-session is what renegotiates this
        # USB device and wedges it.
        self._player.start()
        self._loop.call_soon(self._rearm_wake)

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
        """Return the UI to idle only once the answer has really finished.

        The old ~10 s cap was shorter than a long reply, so the screen went idle
        while Ngân was still talking. Wait for the whole queue, then a little
        longer for aplay's own ALSA buffer to finish sounding.
        """
        waited = 0.0
        while self._player.pending() > 0 and waited < 600.0:
            await asyncio.sleep(0.05)
            waited += 0.05
        await asyncio.sleep(0.7)          # aplay/ALSA still holds ~0.5 s
        self._emit("PLAYBACK_DONE")
        self._rearm_wake()                # only now is it safe to listen again

    def _emit(self, ev):
        try:
            self.on_event(ev)
        except Exception:
            pass

    # ---------------- talk control (any thread) ----------------
    def talk_start(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._talk_start(), self._loop)

    def talk_stop(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._talk_stop(), self._loop)

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
    def _ensure_streams(self):
        """Both streams must be open. alive() checks the reader thread, not just
        a flag — a dead thread with the flag set once made turns hang silently."""
        if not self._mic.alive():
            try:
                self._mic.stop()
            except Exception:
                pass
            self._mic.start()
            _dlog("[turn] mic (re)started alive=%s" % self._mic.alive())
        self._player.start()          # no-op if already running

    def _rearm_wake(self):
        """Back to idle: point the shared mic at the wake detector.

        With hands-free off this is a no-op and the mic is only opened while the
        talk button is held — exactly the 0.6.1 behaviour."""
        if self._local_reply:
            return                    # a local spoken reply is still playing
        self._ending = False
        self._hands_free = False
        if self._always_on and self._wake and self._wake.available() and not self._talking:
            self._endpoint.reset()
            self._wake.reset()
            self._wake.mute(False)
            self._ensure_streams()
            self._mic.set_consumer(self._wake_consumer)

    # ---------------- mic consumers (called on the mic thread) ----------------
    def _wake_consumer(self, buf):
        if self._wake:
            self._wake.feed(buf)

    def _uplink_consumer(self, buf):
        try:
            packed = pack_frame(self._enc.encode(buf))
        except Exception as e:
            packed = None
            if self._enc_errs < 3:
                self._enc_errs += 1
                _dlog("[turn]   ENCODE FAILED: %r" % (e,))
        if packed is not None:
            self._loop.call_soon_threadsafe(self._sendq.put_nowait, packed)
            self._frames_sent += 1
            if self._frames_sent in (1, 50, 150):
                _dlog("[turn]   uplink frames=%d" % self._frames_sent)
        if self._hands_free and not self._ending:
            reason = self._endpoint.feed(buf)
            if reason:
                self._ending = True
                _dlog("[turn]   endpoint -> %s (frames=%d)" % (reason, self._frames_sent))
                if reason == "no_speech":
                    asyncio.run_coroutine_threadsafe(self._abort_no_speech(), self._loop)
                else:
                    asyncio.run_coroutine_threadsafe(self._talk_stop(), self._loop)

    # ---------------- wake -> hands-free turn ----------------
    def _on_wake_detected(self, score):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._begin_hands_free(score), self._loop)

    async def _begin_hands_free(self, score):
        _dlog("[turn] begin_hands_free score=%.3f talking=%s" % (score, self._talking))
        if self._talking or self._closing or self.ws is None:
            return
        if self._wake:
            self._wake.mute(True)
        self._mic.set_consumer(None)
        self._emit("WAKE")
        # Play the blip through the player we already own. A second aplay client
        # would reconfigure this USB device and kill the capture stream.
        try:
            self._player.start()
            self._player.write(tts.ack_pcm(self.sr))
        except Exception as e:
            _dlog("[turn]   ack failed: %r" % (e,))
        # Capture after the blip so it is not heard as the first words. await,
        # not call_later: a callback that raises would vanish into the loop's
        # exception handler and the turn would hang forever.
        await asyncio.sleep(0.5)
        try:
            await self._do_talk_start(True)
        except Exception as e:
            _dlog("[turn]   do_talk_start RAISED: %r" % (e,))
            self._talking = False
            self._emit("ERR:wake:%s" % e)
            self._rearm_wake()

    async def _abort_no_speech(self):
        """Called by name with nothing after it.

        Do not ship the silence to the server. The protocol has no CANCEL, but a
        fresh START clears the server's buffer, so START+END abandons the turn
        using only commands it knows. Answer locally so being called always gets
        a reply."""
        if not self._talking:
            return
        self._talking = False
        self._hands_free = False
        self._mic.set_consumer(None)
        self._local_reply = True
        if self._wake:
            self._wake.mute(True)
        await self._sendq.put("START_PCM_OUT")
        await self._sendq.put("END")
        self._emit("WAKE_NO_SPEECH")
        msg = "Da, ba can gi thi noi voi chau nhe"
        try:
            pcm = await self._loop.run_in_executor(None, tts.speak_pcm, msg, self.sr)
            if pcm:
                self._player.write(pcm)      # reuse the one output client
        except Exception as e:
            _dlog("[turn]   local reply failed: %r" % (e,))
        await asyncio.sleep(3.0)
        self._local_reply = False
        self._rearm_wake()

    async def _turn_watchdog(self, turn_id):
        """Backstop: the endpointer only runs while mic frames keep arriving, so
        if that ever stalls the device would sit in 'listening' forever."""
        await asyncio.sleep(self._ww_cfg.get("max_ms", 13000) / 1000.0 + 3.0)
        if self._talking and self._hands_free and self._turn_id == turn_id:
            _dlog("[turn]   WATCHDOG: stuck (frames=%d) -> forcing END" % self._frames_sent)
            self._ending = True
            await self._talk_stop()

    # ---------------- start / stop a turn ----------------
    async def _talk_start(self):
        await self._do_talk_start(False)

    async def _do_talk_start(self, hands_free):
        _dlog("[turn] talk_start hands_free=%s talking=%s" % (hands_free, self._talking))
        if self._talking or self.ws is None:
            return
        self._talking = True
        self._accepting = True
        self._hands_free = hands_free
        self._ending = False
        self._frames_sent = 0
        if self._wake:
            self._wake.mute(True)
        if hands_free:
            self._endpoint.reset()
        self._ensure_streams()
        # Queue the command; never send on the socket from two places at once.
        await self._sendq.put("START_PCM_OUT")
        self._mic.set_consumer(self._uplink_consumer)
        _dlog("[turn]   uplink armed (mic alive=%s)" % self._mic.alive())
        if hands_free:
            self._turn_id += 1
            self._loop.create_task(self._turn_watchdog(self._turn_id))

    async def _talk_stop(self):
        if not self._talking:
            return
        self._talking = False
        self._mic.set_consumer(None)      # stop the uplink; streams stay open
        await self._sendq.put("END")

    async def _cancel(self):
        self._accepting = False
        self._talking = False
        self._ending = False
        self._mic.set_consumer(None)
        # Dropping the queue silences it within the ~0.5 s already inside aplay.
        # Deliberately do NOT stop the player.
        self._player.flush()
        try:
            await self._sendq.put("START_PCM_OUT")
            await self._sendq.put("END")
        except Exception:
            pass
        self._emit("CANCELLED")
        self._rearm_wake()

    # ---------------- wake toggle (from Settings) ----------------
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
                # Keep the stream OPEN even when hands-free is off: closing it is
                # what damages this device. An idle stream costs nothing.

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
