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


class SharedMic:
    """One persistent arecord stream whose frames go to a *swappable* consumer.

    The wake-word detector and the Opus uplink can't each open their own capture
    stream (the ReSpeaker Lite exposes a single capture device), so they share
    this one: when idle the consumer is the wake detector, during a turn it's the
    uplink encoder. set_consumer(None) suspends delivery without closing the mic.
    """

    MAX_BACKOFF = 8.0          # seconds between capture-restart attempts

    def __init__(self, device, sample_rate, channels, frame_bytes):
        self.device = device
        self.sr = sample_rate
        self.ch = channels
        self.frame_bytes = frame_bytes
        self._proc = None
        self._thread = None
        self._run = False
        self._consumer = None
        self._lock = threading.Lock()
        self._err_count = 0
        self._restarts = 0

    def alive(self):
        """True only if the reader thread is genuinely still pumping frames."""
        return self._run and self._thread is not None and self._thread.is_alive()

    def set_consumer(self, fn):
        with self._lock:
            self._consumer = fn

    def running(self):
        return self._run

    def _spawn(self):
        cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE",
               "-r", str(self.sr), "-c", str(self.ch), "-D", self.device]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL)
            return True
        except Exception as e:
            print("[sharedmic] cannot start arecord:", repr(e), flush=True)
            self._proc = None
            return False

    def _kill_proc(self):
        p = self._proc
        self._proc = None
        if not p:
            return
        try:
            p.terminate()
            p.wait(timeout=1)
        except Exception:
            try:
                p.kill()
                p.wait(timeout=1)
            except Exception:
                pass
        # Close the pipe explicitly. Leaving it to the garbage collector leaks a
        # file descriptor per respawn, which matters once respawns repeat.
        try:
            if p.stdout:
                p.stdout.close()
        except Exception:
            pass

    def start(self):
        if self._run:
            return
        if not self._spawn():
            return
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        """Keep one capture stream alive for the lifetime of the app.

        arecord can hand us EOF mid-session (the audio server reconfigures the
        device — playing the wake chime is enough to do it). Previously that
        ended this thread while `running()` still said True, so nothing restarted
        it: the mic looked healthy but delivered no frames, and a hands-free turn
        sat in 'listening' forever. Respawn instead.
        """
        need = self.frame_bytes
        backoff = 0.5
        while self._run:
            p = self._proc
            if p is None:
                if not self._spawn():
                    time.sleep(min(backoff, self.MAX_BACKOFF))
                    backoff = min(backoff * 2, self.MAX_BACKOFF)
                    continue
                p = self._proc
            try:
                buf = p.stdout.read(need)
            except Exception:
                buf = b""
            if not buf or len(buf) < need:
                if not self._run:
                    break
                self._restarts += 1
                if self._restarts <= 10 or self._restarts % 50 == 0:
                    print("[sharedmic] capture ended (restart #%d) — respawning in %.1fs"
                          % (self._restarts, backoff), flush=True)
                self._kill_proc()
                # Back off exponentially. This is a USB audio device: reopening
                # it in a tight loop makes the kernel fail the interface outright
                # ("usb_set_interface failed (-71)") and wedges capture for good.
                time.sleep(min(backoff, self.MAX_BACKOFF))
                backoff = min(backoff * 2, self.MAX_BACKOFF)
                continue
            backoff = 0.5                 # healthy again
            with self._lock:
                c = self._consumer
            if c is not None:
                try:
                    c(buf)
                except Exception as e:
                    # Never swallow silently: a consumer that throws on every
                    # frame stalls the whole turn (no uplink, no END) and looks
                    # exactly like a hang.
                    if self._err_count < 5:
                        self._err_count += 1
                        print("[sharedmic] consumer error:", repr(e), flush=True)

    def stop(self):
        self._run = False
        with self._lock:
            self._consumer = None
        self._kill_proc()


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
