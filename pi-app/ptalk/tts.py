"""Tiny local text-to-speech for reminders, via espeak-ng (Vietnamese).

Best-effort: if espeak-ng/espeak is not installed, speak() is a silent no-op.
Reminders still show the fullscreen visual alert regardless.
"""
import math
import os
import shutil
import struct
import subprocess
import threading
import wave

_BIN = shutil.which("espeak-ng") or shutil.which("espeak")


def available():
    return bool(_BIN)


def _chime_file():
    path = "/tmp/ptalk_chime.wav"
    if os.path.exists(path):
        return path
    sr, dur, freq = 48000, 0.6, 880
    try:
        with wave.open(path, "w") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
            data = bytearray()
            n = int(sr * dur)
            for i in range(n):
                env = math.exp(-3.0 * i / n)          # decay
                v = int(6000 * env * math.sin(2 * math.pi * freq * i / sr))
                data += struct.pack("<h", v)
            w.writeframes(bytes(data))
    except Exception:
        return None
    return path


def chime():
    f = _chime_file()
    if not f:
        return
    try:
        subprocess.Popen(["aplay", "-q", f],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _ack_file():
    """Short, gentle rising two-tone — the 'I'm listening' cue after 'Bi ơi'."""
    path = "/tmp/ptalk_ack.wav"
    if os.path.exists(path):
        return path
    sr = 48000
    try:
        with wave.open(path, "w") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
            data = bytearray()
            for freq, dur in ((660, 0.09), (990, 0.12)):
                n = int(sr * dur)
                for i in range(n):
                    env = math.sin(math.pi * i / n)        # smooth in/out
                    v = int(4200 * env * math.sin(2 * math.pi * freq * i / sr))
                    data += struct.pack("<h", v)
            w.writeframes(bytes(data))
    except Exception:
        return None
    return path


def ack():
    f = _ack_file()
    if not f:
        return
    try:
        subprocess.Popen(["aplay", "-q", f],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def speak(text, speed=150):
    if not _BIN or not text:
        return
    def _run():
        try:
            subprocess.run([_BIN, "-v", "vi", "-s", str(speed), text],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()
