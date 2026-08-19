#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B test the ReSpeaker 'rè': play the same TTS clip 3 ways so we can hear the
cause. 1=16k native full  2=16k native LEVELED(fix)  3=48k upsampled (app path)."""
import wave, subprocess, sys
import numpy as np

SRC = "/tmp/tts_sample.wav"
w = wave.open(SRC, "rb"); sr = w.getframerate(); ch = w.getnchannels(); n = w.getnframes()
pcm = w.readframes(n); w.close()
x = np.frombuffer(pcm, dtype=np.int16)
if ch > 1:
    x = x.reshape(-1, ch)[:, 0].copy()
print(f"loaded {SRC}: {sr}Hz {ch}ch {len(x)} samples")

def level(a, gain=0.6):
    f = a.astype(np.float32) / 32768.0 * gain
    thr = 0.8; m = np.abs(f) > thr
    f[m] = np.sign(f[m]) * (thr + (1 - thr) * np.tanh((np.abs(f[m]) - thr) / (1 - thr)))
    np.clip(f, -0.997, 0.997, out=f)
    return (f * 32768.0).astype(np.int16)

def up48(a):
    try:
        from scipy.signal import resample_poly
        return resample_poly(a.astype(np.float32), 3, 1).astype(np.int16)
    except Exception:
        idx = np.arange(0, len(a), 1/3.0)
        return np.interp(idx, np.arange(len(a)), a.astype(np.float32)).astype(np.int16)

def say(t):
    subprocess.run(["espeak-ng", "-v", "vi", t])

def play(y, rate):
    p = subprocess.Popen(["aplay", "-q", "-t", "raw", "-f", "S16_LE",
                          "-r", str(rate), "-c", "1", "-D", "default"],
                         stdin=subprocess.PIPE)
    p.stdin.write(y.tobytes()); p.stdin.close(); p.wait()

say("Âm thanh số một. Mức gốc, mười sáu ki lô héc.")
play(x, 16000)
say("Âm thanh số hai. Đã cân bằng tự động.")
play(level(x), 16000)
say("Âm thanh số ba. Kiểu app hiện tại, bốn tám ki lô héc.")
play(up48(x), 48000)
print("done")
