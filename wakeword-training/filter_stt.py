"""Quality-gate the synthetic corpus with a Vietnamese recogniser.

Keeps only positives that demonstrably say the wake phrase, and removes any
"negative" that accidentally does. Tone variants (bi/bì/bí/bị) are ACCEPTED on
purpose: a speaker will not hit one exact tone every time, and the acoustics are
what the wake model learns.

Run:  ./venv/bin/python filter_stt.py
"""
import glob
import os
import re
import shutil
import sys
import unicodedata
import wave
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import sherpa_onnx

WW = os.path.dirname(os.path.abspath(__file__))
STT = os.path.expanduser("~/Ptalk_project/CloudPTalk/models/ZipFormer")
os.chdir(WW)


def build():
    f = {os.path.basename(x): x for x in glob.glob(STT + "/*")}
    pick = lambda k: next(v for n, v in f.items() if k in n and n.endswith(".onnx")
                          and "int8" not in n)
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=pick("encoder"), decoder=pick("decoder"), joiner=pick("joiner"),
        tokens=f["tokens.txt"], num_threads=2, sample_rate=16000, feature_dim=80)


REC = build()


def read(p):
    with wave.open(p) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(p):
    s = REC.create_stream()
    s.accept_waveform(16000, read(p))
    REC.decode_stream(s)
    return p, s.result.text.strip().lower()


def strip_tone(s):
    """'bị ơi' -> 'bi oi' so tone/diacritic variants compare equal."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("đ", "d")
    return re.sub(r"\s+", " ", s).strip()


# 'bi oi' possibly repeated / with a short lead-in ("nay bi oi", "o bi oi")
WAKE = re.compile(r"\bbi\s*(oi|oii|ui)\b")


def is_wake(text):
    t = strip_tone(text)
    if not t:
        return False
    return bool(WAKE.search(t))


def run(paths, workers=16):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(transcribe, paths))


def main():
    pool = sorted(glob.glob("pos_pool/*.wav") + glob.glob("pos_pool2/*.wav"))
    print(f"transcribing {len(pool)} candidate positives...", flush=True)
    res = run(pool)
    keep = [(p, t) for p, t in res if is_wake(t)]
    print(f"positives kept: {len(keep)}/{len(pool)}  ({100*len(keep)/max(1,len(pool)):.1f}%)")

    import collections
    print("\ntop accepted transcripts:")
    for t, n in collections.Counter(t for _, t in keep).most_common(10):
        print(f"  {n:4d}  {t!r}")
    print("\ntop REJECTED transcripts:")
    for t, n in collections.Counter(t for p, t in res if not is_wake(t)).most_common(10):
        print(f"  {n:4d}  {t!r}")

    out = "positives_clean"
    shutil.rmtree(out, ignore_errors=True); os.makedirs(out)
    for i, (p, _) in enumerate(keep):
        shutil.copy(p, os.path.join(out, f"pos_{i:06d}.wav"))
    print(f"\nwrote {len(keep)} -> {out}/")

    # scrub the negatives: anything that really says the wake word must not
    # be trained as a negative
    negs = sorted(glob.glob("negatives/*.wav"))
    print(f"\nscreening {len(negs)} hard negatives...", flush=True)
    nres = run(negs)
    bad = [p for p, t in nres if is_wake(t)]
    outn = "negatives_clean"
    shutil.rmtree(outn, ignore_errors=True); os.makedirs(outn)
    badset = set(bad)
    i = 0
    for p, t in nres:
        if p in badset:
            continue
        shutil.copy(p, os.path.join(outn, f"neg_{i:06d}.wav")); i += 1
    print(f"dropped {len(bad)} wake-like negatives; kept {i} -> {outn}/")


if __name__ == "__main__":
    main()
