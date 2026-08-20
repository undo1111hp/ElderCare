"""Mic capture and speaker playback via ALSA CLI (arecord / aplay).

Chosen over sounddevice/PortAudio because python3-sounddevice is not packaged
for Debian trixie, while alsa-utils is always present. arecord/aplay talk to
ALSA directly (or PipeWire's ALSA bridge), which is robust on the Pi.
"""
import queue
import subprocess
import threading
import time

import numpy as np


class Recorder:
    """Streams fixed-size PCM16 frames from the mic to on_frame(bytes)."""

    def __init__(self, device, sample_rate, channels, frame_bytes, on_frame):
        self.device = device
        self.sr = sample_rate
        self.ch = channels
        self.frame_bytes = frame_bytes
        self.on_frame = on_frame
        self._proc = None
        self._thread = None
        self._run = False

    def start(self):
        if self._run:
            return
        cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE",
               "-r", str(self.sr), "-c", str(self.ch), "-D", self.device]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        need = self.frame_bytes
        rd = self._proc.stdout
        while self._run:
            buf = rd.read(need)
            if not buf or len(buf) < need:
                break
            try:
                self.on_frame(buf)
            except Exception:
                pass

    def stop(self):
        self._run = False
        p = self._proc
        self._proc = None
        if p:
            try:
                p.terminate()
                p.wait(timeout=1)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


class Player:
    """Buffered PCM16 playback. write() enqueues; a thread pumps into aplay."""

    MAX_RESPAWNS = 3        # keep tiny: this USB device hates being reopened

    def __init__(self, device, sample_rate, channels, gain=1.0):
        self.device = device
        self.sr = sample_rate
        self.ch = channels
        self.gain = float(gain)
        self._proc = None
        self._q = None
        self._thread = None
        self._run = False

    def _level(self, pcm):
        """Cân bằng âm lượng tự động: hạ mức theo gain + limiter mềm (tanh) để
        loa khuếch đại cố định của ReSpeaker Lite không bị vỡ tiếng ('rè')."""
        if self.gain >= 0.999:
            return pcm
        try:
            x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * (1.0 / 32768.0)
            x *= self.gain
            thr = 0.8                      # ngưỡng bắt đầu nén mềm
            a = np.abs(x)
            m = a > thr
            if m.any():
                x[m] = np.sign(x[m]) * (thr + (1.0 - thr) * np.tanh((a[m] - thr) / (1.0 - thr)))
            np.clip(x, -0.997, 0.997, out=x)
            return (x * 32768.0).astype(np.int16).tobytes()
        except Exception:
            return pcm

    def _spawn(self):
        cmd = ["aplay", "-q", "-t", "raw", "-f", "S16_LE",
               "-r", str(self.sr), "-c", str(self.ch), "-D", self.device]
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL)
            return True
        except Exception as e:
            print("[player] cannot start aplay:", repr(e), flush=True)
            self._proc = None
            return False

    def _close_proc(self):
        p = self._proc
        self._proc = None
        if not p:
            return
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            p.terminate()
            p.wait(timeout=1)
        except Exception:
            try:
                p.kill()
                p.wait(timeout=1)
            except Exception:
                pass

    def start(self):
        if self._run:
            return
        if not self._spawn():
            return
        # The server streams a whole answer far faster than real time, so this
        # queue holds everything not yet spoken. 4000 chunks was only ~1 minute
        # and anything past it was silently discarded — that is why long replies
        # and poems got cut off part-way. Hold ~10 minutes instead, still bounded.
        self._q = queue.Queue(maxsize=30000)
        self._dropped = 0
        self._respawns = 0
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._run:
            try:
                chunk = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if chunk is None:
                break
            try:
                self._proc.stdin.write(chunk)
                self._proc.stdin.flush()
            except Exception as e:
                # aplay died mid-answer. Bailing out here left the rest of the
                # reply unplayed and the app silent for good. Try a couple of
                # gentle restarts — deliberately few and slow, because rapidly
                # reopening this USB audio device is what wedges it.
                if self._respawns >= self.MAX_RESPAWNS:
                    print("[player] output died and will not restart:", repr(e),
                          flush=True)
                    break
                self._respawns += 1
                print("[player] output died (%s) — restart %d/%d in 1s"
                      % (e, self._respawns, self.MAX_RESPAWNS), flush=True)
                self._close_proc()
                time.sleep(1.0)
                if not self._spawn():
                    break

    def write(self, pcm):
        if not self._run or self._q is None:
            return
        try:
            # Block briefly rather than discard, so a full buffer applies
            # backpressure instead of losing the end of the answer.
            self._q.put(self._level(pcm), timeout=10.0)
        except queue.Full:
            self._dropped += 1
            if self._dropped in (1, 100):
                print("[player] buffer full — dropped %d chunks" % self._dropped,
                      flush=True)

    def pending(self):
        return 0 if self._q is None else self._q.qsize()

    def flush(self):
        if self._q is None:
            return
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def stop(self):
        self._run = False
        if self._q is not None:
            try:
                self._q.put_nowait(None)
            except Exception:
                pass
        self._close_proc()
