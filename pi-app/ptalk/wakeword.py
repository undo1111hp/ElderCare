"""Local wake-word detection ("Bi ơi") via openWakeWord — offline, CPU-only.

Fed the same 48 kHz mono PCM16 frames as the mic uplink, it downsamples to the
16 kHz openWakeWord requires, buffers to 80 ms hops, and fires on_wake() when the
model score crosses a threshold. A refractory window guarantees one utterance =
at most one wake.

Degrades gracefully: if openwakeword / onnxruntime or the model file are absent,
available() returns False and the app just stays in push-to-talk mode — nothing
else changes.
"""
import os
import time

import numpy as np

_HOP16 = 1280  # openWakeWord processes 80 ms @ 16 kHz per step

# Set PTALK_WAKE_DEBUG=1 to append per-second peak scores + every fire to
# /tmp/ptalk_wake.log — the only way to tell "never heard it" from "heard it but
# scored below threshold" once the app is running headless on the device.
_DEBUG = os.environ.get("PTALK_WAKE_DEBUG") == "1"
_DEBUG_PATH = os.environ.get("PTALK_WAKE_LOG", "/tmp/ptalk_wake.log")


def _dlog(msg):
    if not _DEBUG:
        return
    try:
        with open(_DEBUG_PATH, "a") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


class WakeWord:
    def __init__(self, model_path, on_wake, threshold=0.5, src_rate=48000,
                 refractory_ms=1800, trigger_hits=1):
        self._on_wake = on_wake
        self._thr = float(threshold)
        self._ratio = max(1, int(round(src_rate / 16000)))   # 48000/16000 -> 3
        self._refractory = refractory_ms / 1000.0
        self._need = max(1, int(trigger_hits))
        self._model = None
        self._name = None
        self._ok = False
        self._muted = True                # armed explicitly via mute(False)
        self._last_fire = 0.0
        self._hits = 0
        self._carry = np.zeros(0, dtype=np.float32)   # 48k resample remainder
        self._buf16 = np.zeros(0, dtype=np.int16)     # 16k hop accumulator
        self._fir = None                  # streaming anti-alias filter
        self._zi = None
        self._init_fir()
        self._load(model_path)

    def _init_fir(self):
        """Anti-alias low-pass for the 48k->16k decimation, kept as a streaming
        filter (persistent state) so 20 ms frames splice without edge artefacts.
        Without it, decimation aliases and the audio no longer matches what the
        model was trained on. Falls back to a 3-tap average if scipy is absent."""
        if self._ratio <= 1:
            return
        try:
            from scipy.signal import firwin, lfilter_zi
            self._fir = firwin(63, 1.0 / self._ratio * 0.92)   # ~7.4 kHz @ 48k
            self._zi = lfilter_zi(self._fir, 1.0) * 0.0
        except Exception:
            self._fir = None

    # ---------------- model ----------------
    def _load(self, model_path):
        if not model_path or not os.path.exists(model_path):
            if model_path:
                print("[wakeword] model not found:", model_path)
            return
        try:
            from openwakeword.model import Model
        except Exception as e:
            print("[wakeword] openwakeword not installed:", e)
            return

        # The constructor changed between openWakeWord versions: 0.4.x takes
        # wakeword_model_paths / melspec_onnx_model_path, 0.5+ takes
        # wakeword_models / melspec_model_path / inference_framework. Try the
        # variants rather than pinning a version we don't control on the device.
        d = os.path.dirname(model_path)
        mel = os.path.join(d, "melspectrogram.onnx")
        emb = os.path.join(d, "embedding_model.onnx")
        # Shipping the feature models locally avoids a download on first run.
        feats_new, feats_old = {}, {}
        if os.path.exists(mel) and os.path.exists(emb):
            feats_new = {"melspec_model_path": mel, "embedding_model_path": emb}
            feats_old = {"melspec_onnx_model_path": mel, "embedding_onnx_model_path": emb}

        attempts = [
            dict(wakeword_models=[model_path], inference_framework="onnx", **feats_new),
            dict(wakeword_models=[model_path], **feats_new),
            dict(wakeword_model_paths=[model_path], ncpu=1, **feats_old),
            dict(wakeword_model_paths=[model_path], **feats_old),
            dict(wakeword_model_paths=[model_path]),
            dict(wakeword_models=[model_path]),
        ]
        last = None
        for kw in attempts:
            try:
                self._model = Model(**kw)
                names = list(self._model.models.keys())
                if not names:
                    last = "no models loaded"
                    continue
                self._name = names[0]
                self._ok = True
                print("[wakeword] loaded:", self._name)
                return
            except TypeError as e:
                last = e
                continue
            except Exception as e:
                last = e
                continue
        print("[wakeword] disabled:", last)
        self._ok = False

    def available(self):
        return self._ok

    # ---------------- control ----------------
    def mute(self, m=True):
        self._muted = bool(m)
        if m:
            self._hits = 0

    def reset(self):
        self._hits = 0
        self._buf16 = np.zeros(0, dtype=np.int16)
        try:
            if self._model:
                self._model.reset()
        except Exception:
            pass

    # ---------------- audio ----------------
    def _downsample(self, pcm):
        """48 kHz int16 bytes -> 16 kHz int16 (low-pass then decimate)."""
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        r = self._ratio
        if r <= 1:
            return x.astype(np.int16)
        if self._fir is not None:
            from scipy.signal import lfilter
            x, self._zi = lfilter(self._fir, 1.0, x, zi=self._zi)
        x = np.concatenate((self._carry, x))
        n = (len(x) // r) * r
        self._carry = x[n:]
        if n == 0:
            return None
        if self._fir is not None:
            y = x[:n:r]                       # already band-limited -> plain decimate
        else:
            y = x[:n].reshape(-1, r).mean(axis=1)     # crude fallback
        return np.clip(y, -32768, 32767).astype(np.int16)

    def feed(self, pcm):
        if not self._ok or self._muted:
            return
        y = self._downsample(pcm)
        if y is None:
            return
        self._buf16 = np.concatenate((self._buf16, y))
        while len(self._buf16) >= _HOP16:
            hop = self._buf16[:_HOP16]
            self._buf16 = self._buf16[_HOP16:]
            self._step(hop)

    def _step(self, hop16):
        try:
            scores = self._model.predict(hop16)
        except Exception:
            return
        if not scores:
            return
        score = scores.get(self._name) if self._name in scores else max(scores.values())
        if _DEBUG:
            self._dbg_peak = max(getattr(self, "_dbg_peak", 0.0), float(score))
            self._dbg_lvl = max(getattr(self, "_dbg_lvl", 0.0),
                                float(np.abs(hop16).max()))
            n = getattr(self, "_dbg_n", 0) + 1
            self._dbg_n = n
            if n % 12 == 0:                 # ~ once per second
                _dlog("peak_score=%.3f mic_peak=%d" % (self._dbg_peak, int(self._dbg_lvl)))
                self._dbg_peak = 0.0
                self._dbg_lvl = 0.0
        if score >= self._thr:
            self._hits += 1
            now = time.monotonic()
            _dlog("HIT score=%.3f hits=%d/%d" % (score, self._hits, self._need))
            if self._hits >= self._need and (now - self._last_fire) >= self._refractory:
                self._last_fire = now
                self._hits = 0
                self.reset()
                _dlog("*** WAKE FIRED (score=%.3f) ***" % score)
                try:
                    self._on_wake(float(score))
                except Exception:
                    pass
        elif self._hits:
            self._hits -= 1
