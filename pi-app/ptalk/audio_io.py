"""Mic capture and speaker playback via ALSA CLI (arecord / aplay).

Chosen over sounddevice/PortAudio because python3-sounddevice is not packaged
for Debian trixie, while alsa-utils is always present. arecord/aplay talk to
ALSA directly (or PipeWire's ALSA bridge), which is robust on the Pi.
"""
import queue
import subprocess
import threading

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

    def start(self):
        if self._run:
            return
        cmd = ["aplay", "-q", "-t", "raw", "-f", "S16_LE",
               "-r", str(self.sr), "-c", str(self.ch), "-D", self.device]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        self._q = queue.Queue(maxsize=4000)
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
            except Exception:
                break

    def write(self, pcm):
        if self._run and self._q is not None:
            try:
                self._q.put_nowait(self._level(pcm))
            except queue.Full:
                pass

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
        p = self._proc
        self._proc = None
        if p:
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
                except Exception:
                    pass
