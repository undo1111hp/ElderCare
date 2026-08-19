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
from .audio_io import Recorder, Player


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
        self._recorder = None
        self._player = Player(self._out_dev, self.sr, self.ch,
                              gain=a.get("output_gain", 0.6))
        self._accepting = False
        self._talking = False
        self._closing = False

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

    async def _talk_start(self):
        if self._talking or self.ws is None:
            return
        self._talking = True
        self._accepting = True
        self._player.start()
        try:
            await self.ws.send("START_PCM_OUT")
        except Exception as e:
            self._emit(f"ERR:{e}")
            self._talking = False
            return

        def on_frame(buf):
            try:
                packed = pack_frame(self._enc.encode(buf))
            except Exception:
                return
            self._loop.call_soon_threadsafe(self._sendq.put_nowait, packed)

        self._recorder = Recorder(self._in_dev, self.sr, self.ch,
                                  self.frame_bytes, on_frame)
        try:
            self._recorder.start()
        except Exception as e:
            self._emit(f"ERR:mic:{e}")

    async def _talk_stop(self):
        if not self._talking:
            return
        self._talking = False
        if self._recorder:
            await self._loop.run_in_executor(None, self._recorder.stop)
            self._recorder = None
        # queued opus frames flush first (FIFO), then END
        await self._sendq.put("END")

    async def _cancel(self):
        self._accepting = False
        self._talking = False
        if self._recorder:
            await self._loop.run_in_executor(None, self._recorder.stop)
            self._recorder = None
        self._player.flush()
        self._player.stop()
        self._emit("CANCELLED")

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
        if self._recorder:
            try:
                self._recorder.stop()
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
