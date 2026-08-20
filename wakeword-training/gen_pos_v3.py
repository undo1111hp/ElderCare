"""Regenerate the "Bi ơi" positives with conservative prosody.

The first pass swept length/noise so widely that these low-quality Vietnamese
voices produced garbled audio — an STT spot-check showed only ~5% actually said
the phrase. Training on that would teach the model the wrong sound. Here the
ranges stay near each voice's natural operating point; filter_stt.py then keeps
only clips a Vietnamese recogniser confirms.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_audio as G   # reuse load_voice/synth/to16k/trim/write_wav

import random
random.seed(99)

TEXTS = ["Bi ơi", "Bi ơi!", "Bi ơi.", "bi ơi", "Bi ơi?",
         "Bi ơi ơi", "Này Bi ơi", "Bi ơi Bi ơi", "Bi ơi, ", "Ơ Bi ơi"]

OUT = "pos_pool2"


def main():
    os.chdir(G.WW)
    os.makedirs(OUT, exist_ok=True)
    n = 0
    for vname, vpath, nspk in G.VOICES:
        voice = G.load_voice(vpath)
        reps = 120 if nspk > 1 else 40      # VIVOS: sweep its 65 speakers hard
        for text in TEXTS:
            for _ in range(reps * (3 if nspk > 1 else 1)):
                spk = random.randrange(nspk) if nspk > 1 else None
                ls = random.uniform(0.88, 1.22)     # near-natural rate only
                ns = random.uniform(0.50, 0.68)
                nw = random.uniform(0.65, 0.90)
                try:
                    from piper import SynthesisConfig
                    cfg = SynthesisConfig(speaker_id=spk, length_scale=ls,
                                          noise_scale=ns, noise_w_scale=nw,
                                          normalize_audio=True)
                    chunks = list(voice.synthesize(text, syn_config=cfg))
                    if not chunks:
                        continue
                    sr = chunks[0].sample_rate
                    a = np.concatenate([c.audio_int16_array for c in chunks]).astype(np.float32)
                    a = G.trim(G.to16k(a, sr))
                    if a.size < 1600:            # < 0.1 s cannot be the phrase
                        continue
                    G.write_wav(os.path.join(OUT, f"p_{vname}_{n:06d}.wav"), a)
                    n += 1
                except Exception as e:
                    print("  err:", text, e, file=sys.stderr)
        print(f"  [{vname}] pool now {n}", flush=True)
    print("POOL", n)


if __name__ == "__main__":
    main()
