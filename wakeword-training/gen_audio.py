"""Synthesize the "Bi ơi" training corpus with Vietnamese Piper voices.

Positives   : the wake phrase itself, across 65 VIVOS speakers x prosody sweeps.
Hard negs   : phrases that sound close ("bà ơi", "bơi", "bi", "trời ơi", ...) —
              these matter most, a 2-syllable wake word false-fires easily.
Background  : real sentences (the poem corpus) so ordinary conversation in the
              room does not trigger it.

Writes 16 kHz mono WAVs to positives/ negatives/ background/.
Run:  ./venv/bin/python gen_audio.py
"""
import json
import os
import random
import sys
import wave

import numpy as np

random.seed(1234)
WW = os.path.dirname(os.path.abspath(__file__))
SR = 16000

VOICES = [
    ("vivos", "models/vi_vivos.onnx", 65),
    ("vais",  "models/vi_vais.onnx",   1),
    ("h25",   "models/vi_25h.onnx",    1),
]

# ---- the wake phrase, spelled a few ways so prosody/phonemisation varies ----
POSITIVE_TEXTS = [
    "Bi ơi", "Bi ơi!", "Bi ơi.", "bi ơi", "Bi ơi?",
    "Bi ơi Bi ơi", "Bi ơiii", "Bi ời", "Này Bi ơi", "Bi ơi ơi",
]

# ---- hard negatives: minimal pairs and common "... ơi" address forms --------
HARD_NEG_TEXTS = [
    "bà ơi", "ông ơi", "bố ơi", "mẹ ơi", "bé ơi", "bác ơi", "cô ơi", "chú ơi",
    "dì ơi", "chị ơi", "anh ơi", "em ơi", "con ơi", "cháu ơi", "mình ơi",
    "này ơi", "ai ơi", "trời ơi", "giời ơi", "ối giời ơi", "Ngân ơi", "Lan ơi",
    "bà nội ơi", "bà ngoại ơi", "y tá ơi", "bác sĩ ơi",
    "ơi", "ời", "ơi ời", "bi", "bí", "bị", "bìa", "bia", "bơi", "bời", "bởi",
    "bối", "bôi", "bay", "vi", "vơi", "với", "vội", "mi", "mì", "mơi", "đi",
    "đi ơi", "ti vi", "bi ve", "bi da", "bi bô", "bi sắt", "hòn bi", "vi ơi",
    "mi ơi", "ti ơi", "di ơi", "phi ơi", "li ơi", "ly ơi", "nghi ơi",
    "cứ đi ơi", "bi thương", "bi kịch", "bi quan", "tỉ ơi", "tí ơi",
]


def load_voice(path):
    from piper import PiperVoice
    return PiperVoice.load(os.path.join(WW, path),
                           config_path=os.path.join(WW, path + ".json"))


def synth(voice, text, speaker_id, length_scale, noise_scale, noise_w):
    from piper import SynthesisConfig
    cfg = SynthesisConfig(speaker_id=speaker_id, length_scale=length_scale,
                          noise_scale=noise_scale, noise_w_scale=noise_w,
                          normalize_audio=False)
    chunks = list(voice.synthesize(text, syn_config=cfg))
    if not chunks:
        return None, SR
    sr = chunks[0].sample_rate
    a = np.concatenate([c.audio_int16_array for c in chunks]).astype(np.float32)
    return a, sr


def to16k(a, sr):
    if sr == SR:
        return a
    from scipy.signal import resample_poly
    from math import gcd
    g = gcd(int(sr), SR)
    return resample_poly(a, SR // g, sr // g)


def trim(a, thresh=0.01):
    """Strip leading/trailing near-silence so we know where the phrase ends."""
    if a.size == 0:
        return a
    e = np.abs(a) / max(1.0, np.abs(a).max())
    idx = np.where(e > thresh)[0]
    if idx.size == 0:
        return a
    return a[max(0, idx[0] - 320): idx[-1] + 320]


def write_wav(path, a):
    a = np.clip(a, -32768, 32767).astype(np.int16)
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(a.tobytes())


def gen_set(texts, outdir, per_text_variants, tag):
    os.makedirs(outdir, exist_ok=True)
    n = 0
    for vname, vpath, nspk in VOICES:
        voice = load_voice(vpath)
        # VIVOS carries 65 speakers — draw more from it, it is the diversity source
        variants = per_text_variants * (3 if nspk > 1 else 1)
        # spread the prosody sweep over speakers so the set stays diverse
        for text in texts:
            for k in range(variants):
                spk = random.randrange(nspk) if nspk > 1 else None
                ls = random.uniform(0.75, 1.45)      # speaking rate
                ns = random.uniform(0.5, 0.85)       # timbre variation
                nw = random.uniform(0.6, 1.0)        # phoneme-duration variation
                try:
                    a, sr = synth(voice, text, spk, ls, ns, nw)
                    if a is None or a.size < 800:
                        continue
                    a = trim(to16k(a, sr))
                    if a.size < 800:
                        continue
                    write_wav(os.path.join(outdir, f"{tag}_{vname}_{n:06d}.wav"), a)
                    n += 1
                except Exception as e:
                    print("  synth err:", text, e, file=sys.stderr)
        print(f"  [{tag}] {vname}: total so far {n}", flush=True)
    return n


def poem_sentences(limit=900):
    """Real Vietnamese sentences for background negatives — from her own poems."""
    out = []
    p = os.path.expanduser("~/eldercare/poems_structured.json")
    try:
        data = json.load(open(p, encoding="utf-8"))
    except Exception as e:
        print("poem corpus unavailable:", e, file=sys.stderr)
        return out
    items = data if isinstance(data, list) else data.get("poems", [])
    for poem in items:
        body = poem.get("body") or ""
        for line in str(body).splitlines():
            line = line.strip()
            if 12 <= len(line) <= 90:
                out.append(line)
    random.shuffle(out)
    return out[:limit]


def main():
    os.chdir(WW)
    print("=== positives ===", flush=True)
    np_ = gen_set(POSITIVE_TEXTS, "positives", per_text_variants=42, tag="pos")
    print("positives:", np_, flush=True)

    print("=== hard negatives ===", flush=True)
    nn = gen_set(HARD_NEG_TEXTS, "negatives", per_text_variants=7, tag="neg")
    print("hard negatives:", nn, flush=True)

    print("=== background speech (poem lines) ===", flush=True)
    sents = poem_sentences()
    print("sentences:", len(sents), flush=True)
    nb = gen_set(sents, "background", per_text_variants=1, tag="bg")
    print("background:", nb, flush=True)

    print(f"DONE pos={np_} hardneg={nn} bg={nb}")


if __name__ == "__main__":
    main()
