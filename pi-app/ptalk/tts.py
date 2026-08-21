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

_ACK_PCM = None


def ack_pcm(sample_rate=48000):
    """The 'I'm listening' blip as raw PCM16, to be pushed through the player
    that is already open.

    Deliberately NOT played with its own aplay: on the ReSpeaker (USB audio) a
    second playback client renegotiates the interface and kills the live capture
    stream mid-turn (kernel: usb_set_interface failed (-71)). Reusing the one
    open output never touches the device configuration.
    """
    global _ACK_PCM
    if _ACK_PCM is not None:
        return _ACK_PCM
    sr = int(sample_rate)
    data = bytearray()
    for freq, dur in ((660, 0.09), (990, 0.12)):     # gentle rising two-tone
        n = int(sr * dur)
        for i in range(n):
            env = math.sin(math.pi * i / n)          # smooth in/out, no click
            v = int(4200 * env * math.sin(2 * math.pi * freq * i / sr))
            data += struct.pack("<h", v)
    _ACK_PCM = bytes(data)
    return _ACK_PCM


def speak_pcm(text, sample_rate=48000, speed=150):
    """Render espeak-ng to PCM16 at `sample_rate` instead of letting espeak open
    the audio device itself — same reason as ack_pcm(): one output client only.

    Returns None if espeak or the resample is unavailable; callers fall back to
    speak().
    """
    if not _BIN or not text:
        return None
    try:
        out = subprocess.run([_BIN, "-v", "vi", "-s", str(speed), "--stdout", text],
                             capture_output=True, timeout=30).stdout
        if not out:
            return None
        import io as _io
        with wave.open(_io.BytesIO(out)) as w:
            sr = w.getframerate()
            ch = w.getnchannels()
            raw = w.readframes(w.getnframes())
        import numpy as np
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        if sr != sample_rate:
            from math import gcd
            g = gcd(int(sr), int(sample_rate))
            up, down = sample_rate // g, sr // g
            try:
                from scipy.signal import resample_poly
                a = resample_poly(a, up, down)
            except Exception:
                # scipy is not installed on the Pi; linear interpolation is
                # perfectly adequate for a short spoken prompt.
                n_out = int(len(a) * sample_rate / sr)
                a = np.interp(np.linspace(0, len(a) - 1, n_out),
                              np.arange(len(a)), a)
        return np.clip(a, -32768, 32767).astype(np.int16).tobytes()
    except Exception:
        return None
