"""End-of-utterance detection for hands-free turns.

After the wake word fires there is no button to release, so we decide when the
speaker has stopped: ignore a short lead-in (so the "Dạ?" chime isn't counted),
wait for speech to begin, then end after a run of silence — with a no-speech
timeout for false wakes and a hard cap for run-ons. Pure numpy energy VAD; good
enough for a quiet room and adds no dependency.
"""
import numpy as np


class Endpointer:
    def __init__(self, frame_ms=20, silence_ms=1300, lead_ms=6000,
                 max_ms=13000, ignore_ms=350, rms_floor=520.0, rms_factor=2.8):
        f = max(1, int(frame_ms))
        self.silence_frames = max(1, int(silence_ms / f))
        self.lead_frames = max(1, int(lead_ms / f))
        self.max_frames = max(1, int(max_ms / f))
        self.ignore_frames = max(0, int(ignore_ms / f))
        self.rms_floor = float(rms_floor)
        self.rms_factor = float(rms_factor)
        self.reset()

    def reset(self):
        self.i = 0
        self.started = False
        self.sil = 0
        self.noise = None
        self._noise_acc = []

    def feed(self, pcm):
        """Feed one frame. Returns None to keep listening, else a reason string:
        'silence' (speech then quiet), 'no_speech' (false wake), 'max' (run-on)."""
        self.i += 1
        if self.i <= self.ignore_frames:
            return None
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0

        if self.noise is None:
            self._noise_acc.append(rms)
            if len(self._noise_acc) >= 10:
                self.noise = float(np.median(self._noise_acc))
            thr = self.rms_floor
        else:
            thr = max(self.rms_floor, self.noise * self.rms_factor)

        if rms > thr:
            self.started = True
            self.sil = 0
        elif self.started:
            self.sil += 1
            if self.sil >= self.silence_frames:
                return "silence"

        if not self.started and self.i >= self.lead_frames:
            return "no_speech"
        if self.i >= self.max_frames:
            return "max"
        return None
