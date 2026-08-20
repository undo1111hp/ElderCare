"""Break the false-accept rate down by negative type.

A single aggregate FA number hides what matters: firing on ordinary conversation
is fatal (the device would wake constantly), whereas firing on a deliberately
confusable phrase like "bà ơi" is a narrower problem. The saved X.npy is still in
generation order — positives, hard negatives, background — so the groups can be
sliced apart directly.

Also reports the rate per *minute of continuous speech*, since the runtime scores
a window every 80 ms (12.5/s) — that is the number the user actually feels.
"""
import numpy as np
import onnxruntime as ort

X = np.load("out/X.npy").astype(np.float32)
y = np.load("out/y.npy")
s = ort.InferenceSession("out/bi_oi.onnx", providers=["CPUExecutionProvider"])

import os
if os.path.exists("out/group_sizes.npy"):
    gp, gh, gb, gn = (int(v) for v in np.load("out/group_sizes.npy"))
    o = 0
    groups = {}
    for name, n in (("positives", gp), ("hard_negatives", gh),
                    ("background_speech", gb), ("pure_noise", gn)):
        groups[name] = X[o:o + n]; o += n
else:
    npos = int(y.sum())
    groups = {"positives": X[:npos]}
    rest = X[npos:]
    n_hard = int(round(2412 * 3))
    groups["hard_negatives"] = rest[:n_hard]
    groups["background_speech"] = rest[n_hard:]

print("group sizes:", {k: len(v) for k, v in groups.items()})

scores = {}
for k, v in groups.items():
    out = []
    for i in range(0, len(v), 4096):
        out.append(s.run(None, {"input": v[i:i + 4096]})[0].ravel())
    scores[k] = np.concatenate(out)

print("\n thr | recall |  FA hard-neg  |  FA background  | est. false fires")
print("     |        |               |                 | per min of speech")
print("-" * 72)
for thr in (0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99, 0.995, 0.999):
    rec = float((scores["positives"] >= thr).mean())
    fh = float((scores["hard_negatives"] >= thr).mean())
    fb = float((scores["background_speech"] >= thr).mean())
    # 12.5 decision windows per second of audio
    per_min = fb * 12.5 * 60
    print(f" {thr:5.3f} | {rec:.4f} |    {fh:.5f}    |     {fb:.5f}     |  {per_min:8.1f}")

print("\nNOTE: consecutive-window logic in the client (trigger_hits) cuts the")
print("false-fire rate further; these are per-window figures.")

# how much does requiring 2 consecutive above-threshold windows help?
print("\n=== effect of requiring N consecutive windows (background speech) ===")
bgs = scores["background_speech"]
for thr in (0.9, 0.95, 0.98):
    for n in (1, 2, 3):
        hits = bgs >= thr
        if n > 1:
            run = np.ones(len(hits), dtype=bool)
            for k in range(n):
                run[:len(hits) - n + 1] &= hits[k:len(hits) - n + 1 + k]
            run[len(hits) - n + 1:] = False
            rate = float(run.mean())
        else:
            rate = float(hits.mean())
        print(f"  thr={thr} consecutive={n}: rate={rate:.6f}  (~{rate*12.5*60:.2f}/min)")
