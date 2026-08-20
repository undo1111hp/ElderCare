"""Train the "Bi ơi" openWakeWord classifier and export bi_oi.onnx.

Pipeline (mirrors openWakeWord exactly so the model drops into the runtime):
    16 kHz audio -> melspectrogram.onnx -> (T,32) mel, transformed x/10+2
                 -> embedding_model.onnx over 76-frame windows, step 8 -> (N,96)
                 -> classifier over the last 16 embeddings -> P(wake)

A decision window is 196 mel frames = 31840 samples ~= 1.99 s. Positives are
built so the phrase *ends* near the window end (that is what the runtime sees
the instant the wake word completes); negatives are hard phrases and ordinary
speech placed the same way.

Augmentation models the real device: synthetic room reverb, background noise at
varied SNR, random gain, and the same ~7.4 kHz low-pass the Pi client applies
when it decimates 48 kHz mic audio to 16 kHz.

Run:  ./venv/bin/python train_ww.py
"""
import glob
import os
import sys
import wave

import numpy as np
import onnxruntime as ort

rng = np.random.default_rng(7)
WW = os.path.dirname(os.path.abspath(__file__))
SR = 16000
MEL_HOP = 160
WIN_MEL = 196          # 76 + 15*8  -> exactly 16 embeddings
WIN_SAMPLES = (WIN_MEL + 3) * MEL_HOP     # 31840
N_EMB = 16

os.chdir(WW)

# ORT sessions are created lazily *inside* each process: a session inherited
# across fork() is not safe to run, and one thread each avoids oversubscribing
# the box when 24 workers run at once.
_SESS = {}


def _sessions():
    if not _SESS:
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        m = ort.InferenceSession("models/melspectrogram.onnx", so,
                                 providers=["CPUExecutionProvider"])
        e = ort.InferenceSession("models/embedding_model.onnx", so,
                                 providers=["CPUExecutionProvider"])
        _SESS.update(mel=m, emb=e, mel_in=m.get_inputs()[0].name,
                     emb_in=e.get_inputs()[0].name)
    return _SESS


# ----------------------------------------------------------------- audio io
def read_wav(p):
    with wave.open(p) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return a.astype(np.float32)


def load_dir(d, limit=None):
    fs = sorted(glob.glob(os.path.join(d, "*.wav")))
    if limit:
        fs = fs[:limit]
    return [read_wav(f) for f in fs]


# -------------------------------------------------------------- augmentation
def synth_rir(rt60):
    n = int(SR * rt60)
    t = np.arange(n) / SR
    h = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    h[0] += 3.0                                   # direct path
    for _ in range(rng.integers(2, 6)):           # early reflections
        i = rng.integers(int(0.003 * SR), max(int(0.003 * SR) + 1, int(0.05 * SR)))
        if i < n:
            h[i] += rng.uniform(-1.0, 1.0)
    return h / (np.abs(h).max() + 1e-9)


def reverb(a, p=0.5):
    if rng.random() > p:
        return a
    from scipy.signal import fftconvolve
    h = synth_rir(rng.uniform(0.15, 0.6))
    return fftconvolve(a, h)[:len(a)]


def lowpass_like_device(a):
    """The Pi decimates 48k->16k behind a ~7.4 kHz FIR; match that colouration."""
    from scipy.signal import firwin, lfilter
    b = firwin(63, 7400 / (SR / 2))
    return lfilter(b, 1.0, a)


def pink(n):
    w = rng.standard_normal(n)
    f = np.fft.rfft(w)
    k = np.arange(len(f)); k[0] = 1
    return np.fft.irfft(f / np.sqrt(k), n)


def add_noise(a, bg_pool, snr_db):
    if bg_pool is not None and len(bg_pool) and rng.random() < 0.6:
        b = bg_pool[rng.integers(len(bg_pool))]
        if len(b) < len(a):
            b = np.tile(b, int(np.ceil(len(a) / len(b))))
        off = rng.integers(0, max(1, len(b) - len(a)))
        noise = b[off:off + len(a)].astype(np.float32)
    else:
        noise = pink(len(a)) * 3000.0 if rng.random() < 0.5 else rng.standard_normal(len(a)) * 1200.0
    ap = np.sqrt(np.mean(a ** 2)) + 1e-9
    npow = np.sqrt(np.mean(noise ** 2)) + 1e-9
    target = ap / (10 ** (snr_db / 20.0))
    return a + noise * (target / npow)


def make_window(clip, bg_pool, end_pad_range=(0.05, 0.55), place_end=True):
    """Drop `clip` into a 1.99 s window; when place_end, the phrase finishes
    shortly before the window end — exactly what the runtime sees on detection."""
    w = np.zeros(WIN_SAMPLES, dtype=np.float32)
    # fill the window with quiet room tone / other speech first
    if bg_pool is not None and len(bg_pool) and rng.random() < 0.7:
        b = bg_pool[rng.integers(len(bg_pool))]
        if len(b) < WIN_SAMPLES:
            b = np.tile(b, int(np.ceil(WIN_SAMPLES / len(b))))
        off = rng.integers(0, max(1, len(b) - WIN_SAMPLES))
        w += b[off:off + WIN_SAMPLES].astype(np.float32) * rng.uniform(0.05, 0.35)

    c = clip[:WIN_SAMPLES]
    if place_end:
        pad = int(rng.uniform(*end_pad_range) * SR)
        start = WIN_SAMPLES - len(c) - pad
    else:
        start = rng.integers(0, max(1, WIN_SAMPLES - len(c)))
    start = int(max(0, min(start, WIN_SAMPLES - len(c))))
    w[start:start + len(c)] += c * rng.uniform(0.4, 1.0)
    return w


def augment(w, bg_pool):
    w = reverb(w)
    w = add_noise(w, bg_pool, rng.uniform(5.0, 30.0))
    w = lowpass_like_device(w)
    w = w * rng.uniform(0.25, 1.6)
    peak = np.abs(w).max()
    if peak > 32000:                     # occasional clipping, like a loud voice
        w = w * (32000.0 / peak) if rng.random() < 0.7 else np.clip(w, -32768, 32767)
    return np.clip(w, -32768, 32767)


# ------------------------------------------------------------------ features
def embed_window(w):
    """1.99 s of 16k audio -> (16, 96) embeddings."""
    S = _sessions()
    m = S["mel"].run(None, {S["mel_in"]: w[None, :].astype(np.float32)})[0]
    m = m.squeeze() / 10.0 + 2.0                       # (T,32)
    if m.shape[0] < WIN_MEL:
        m = np.pad(m, ((0, WIN_MEL - m.shape[0]), (0, 0)))
    m = m[:WIN_MEL]
    wins = np.stack([m[i * 8:i * 8 + 76] for i in range(N_EMB)])    # (16,76,32)
    e = S["emb"].run(None, {S["emb_in"]: wins[..., None].astype(np.float32)})[0]
    return e.reshape(N_EMB, 96)


def noise_clip():
    """Non-speech sounds an always-on mic actually hears: fan hiss, mains hum,
    knocks, clatter, near-silence. Without these the model has never seen pure
    noise and can fire on it."""
    n = WIN_SAMPLES
    kind = rng.integers(0, 6)
    if kind == 0:
        a = rng.standard_normal(n) * rng.uniform(200, 6000)          # white/hiss
    elif kind == 1:
        a = pink(n) * rng.uniform(500, 9000)                          # pink/fan
    elif kind == 2:
        a = np.cumsum(rng.standard_normal(n)); a = a / (np.abs(a).max() + 1e-9)
        a = a * rng.uniform(2000, 12000)                              # brown/rumble
    elif kind == 3:
        t = np.arange(n) / SR                                         # mains hum
        a = np.zeros(n)
        for f in (50, 100, 150):
            a += np.sin(2 * np.pi * f * t) * rng.uniform(200, 2500)
        a += rng.standard_normal(n) * 200
    elif kind == 4:
        a = rng.standard_normal(n) * 60                               # room tone
        for _ in range(rng.integers(1, 5)):                           # + knocks
            i = rng.integers(0, n - 800)
            L = rng.integers(100, 800)
            a[i:i + L] += (rng.standard_normal(L) *
                           np.exp(-np.arange(L) / (L / 4)) * rng.uniform(3000, 20000))
    else:
        a = rng.standard_normal(n) * rng.uniform(1, 80)               # near-silence
    return np.clip(a, -32768, 32767).astype(np.float32)


def _noise_chunk(args):
    count, seed = args
    global rng
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(count):
        w = noise_clip()
        if rng.random() < 0.4:
            w = reverb(w, p=1.0)
        out.append(embed_window(np.clip(w, -32768, 32767)))
    return np.array(out, dtype=np.float32)


def featurize_noise(total, workers=24):
    from concurrent.futures import ProcessPoolExecutor
    per = max(1, total // (workers * 2))
    args = [(per, 5000 + i) for i in range(workers * 2)]
    parts = []
    with ProcessPoolExecutor(workers) as ex:
        for r in ex.map(_noise_chunk, args):
            parts.append(r)
    return np.concatenate(parts)


def _featurize_chunk(args):
    """Worker: build and embed n_aug windows for each clip in this shard."""
    clips, bg_pool, n_aug, place_end, seed = args
    global rng
    rng = np.random.default_rng(seed)
    out = []
    for c in clips:
        for _ in range(n_aug):
            out.append(embed_window(augment(make_window(c, bg_pool,
                                                        place_end=place_end), bg_pool)))
    return np.array(out, dtype=np.float32) if out else np.zeros((0, N_EMB, 96), np.float32)


def featurize(clips, bg_pool, n_aug, place_end=True, label="", workers=24):
    """Embedding extraction dominates runtime (~0.26 s/example on one core), so
    shard it across processes — each worker owns its own ORT session and RNG."""
    from concurrent.futures import ProcessPoolExecutor
    if not clips:
        return np.zeros((0, N_EMB, 96), np.float32)
    nsh = max(1, min(workers * 3, len(clips)))
    shards = [clips[i::nsh] for i in range(nsh)]
    # every worker needs background audio to mix, but shipping the whole pool to
    # each process is wasteful — send a bounded random slice
    bgs = bg_pool[:400] if bg_pool else []
    args = [(s, bgs, n_aug, place_end, 1000 + i) for i, s in enumerate(shards) if s]
    parts, done = [], 0
    with ProcessPoolExecutor(workers) as ex:
        for r in ex.map(_featurize_chunk, args):
            parts.append(r); done += 1
            if done % 12 == 0:
                print(f"   {label} shard {done}/{len(args)}", flush=True)
    return np.concatenate(parts) if parts else np.zeros((0, N_EMB, 96), np.float32)


# ------------------------------------------------------------------ training
def main():
    # Featurising is the slow part (~25k ORT passes); reuse it across training
    # runs unless FORCE_FEATS=1 is set.
    if os.path.exists("out/X.npy") and os.environ.get("FORCE_FEATS") != "1":
        print("=== reusing cached features (FORCE_FEATS=1 to rebuild) ===", flush=True)
        X = np.load("out/X.npy"); y = np.load("out/y.npy")
        print("dataset:", X.shape, "pos", int(y.sum()), "neg", int((1 - y).sum()), flush=True)
        return train(X, y)

    print("=== loading audio ===", flush=True)
    # *_clean are the STT-verified sets: positives confirmed to say the phrase,
    # negatives with any accidental wake word removed.
    pos = load_dir("positives_clean")
    hard = load_dir("negatives_clean") or load_dir("negatives")
    bg = load_dir("background")
    print(f"positives={len(pos)} hard_negs={len(hard)} background={len(bg)}", flush=True)
    if not pos or not hard:
        print("missing data; run gen_pos_v2.py + filter_stt.py first"); sys.exit(1)

    bg_pool = bg
    print("=== featurising positives ===", flush=True)
    Xp = featurize(pos, bg_pool, n_aug=4, place_end=True, label="pos")
    print("=== featurising hard negatives ===", flush=True)
    Xh = featurize(hard, bg_pool, n_aug=3, place_end=True, label="hard")
    print("=== featurising background speech ===", flush=True)
    Xb = featurize(bg, bg_pool, n_aug=2, place_end=False, label="bg")
    print("=== featurising pure noise / room tone ===", flush=True)
    Xn = featurize_noise(4000)
    print("noise negatives:", Xn.shape, flush=True)

    X = np.concatenate([Xp, Xh, Xb, Xn])
    y = np.concatenate([np.ones(len(Xp)),
                        np.zeros(len(Xh) + len(Xb) + len(Xn))]).astype(np.float32)
    np.save("out/group_sizes.npy", np.array([len(Xp), len(Xh), len(Xb), len(Xn)]))
    print("dataset:", X.shape, "pos", int(y.sum()), "neg", int((1 - y).sum()), flush=True)
    np.save("out/X.npy", X); np.save("out/y.npy", y)
    return train(X, y)


def train(X, y):
    import torch
    import torch.nn as nn
    torch.manual_seed(0)
    torch.set_num_threads(16)

    idx = rng.permutation(len(X))
    X, y = X[idx], y[idx]
    ntr = int(len(X) * 0.9)
    Xtr, ytr, Xva, yva = X[:ntr], y[:ntr], X[ntr:], y[ntr:]

    model = nn.Sequential(
        nn.Flatten(),
        nn.Linear(N_EMB * 96, 96), nn.ReLU(), nn.LayerNorm(96), nn.Dropout(0.4),
        nn.Linear(96, 48), nn.ReLU(), nn.LayerNorm(48), nn.Dropout(0.3),
        nn.Linear(48, 1), nn.Sigmoid(),
    )
    opt = torch.optim.AdamW(model.parameters(), lr=8e-4, weight_decay=1e-3)
    lossf = nn.BCELoss()

    Xtr_t = torch.tensor(Xtr); ytr_t = torch.tensor(ytr)[:, None]
    Xva_t = torch.tensor(Xva); yva_t = torch.tensor(yva)[:, None]

    def fa_at_recall(pv, target=0.95):
        """False-accept rate at the threshold that still catches `target` of
        real wake words — the number that actually matters on the device."""
        pos_s = np.sort(pv[yva == 1])
        if len(pos_s) == 0:
            return 1.0, 0.5
        thr = float(pos_s[max(0, int((1 - target) * len(pos_s)) - 1)])
        fa = float((pv[yva == 0] >= thr).mean())
        return fa, thr

    best, best_state, best_ep = 1e9, None, -1
    bs = 256
    for ep in range(60):
        model.train()
        perm = torch.randperm(len(Xtr_t))
        tot = 0.0
        for i in range(0, len(perm), bs):
            b = perm[i:i + bs]
            opt.zero_grad()
            p = model(Xtr_t[b])
            loss = lossf(p, ytr_t[b])
            loss.backward(); opt.step()
            tot += float(loss.detach()) * len(b)
        model.eval()
        with torch.no_grad():
            pv = model(Xva_t).numpy().ravel()
            vl = float(lossf(torch.tensor(pv)[:, None], yva_t))
        fa, _ = fa_at_recall(pv)
        # select on false-accepts at fixed recall, not raw loss
        score = fa
        if score < best:
            best, best_state, best_ep = score, {k: v.clone() for k, v in model.state_dict().items()}, ep
        if ep % 5 == 0 or ep == 59:
            acc = float(((pv > 0.5) == (yva == 1)).mean())
            print(f"ep{ep:02d} train={tot/len(perm):.4f} val={vl:.4f} acc={acc:.4f} FA@95recall={fa:.4f}", flush=True)
    model.load_state_dict(best_state)
    model.eval()
    print(f"\nbest epoch {best_ep}: FA@95%recall = {best:.4f}", flush=True)

    with torch.no_grad():
        pv = model(Xva_t).numpy().ravel()
    print("\n=== threshold sweep (validation) ===", flush=True)
    print("  thr    recall   false-accept")
    for thr in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98):
        rec = float(((pv >= thr) & (yva == 1)).sum() / max(1, (yva == 1).sum()))
        fa = float(((pv >= thr) & (yva == 0)).sum() / max(1, (yva == 0).sum()))
        print(f"  {thr:.2f}   {rec:.4f}   {fa:.5f}")

    # ---- export ONNX in the shape openWakeWord expects: (batch,16,96)->(batch,1)
    dummy = torch.zeros(1, N_EMB, 96)
    try:
        torch.onnx.export(
            model, dummy, "out/bi_oi.onnx",
            input_names=["input"], output_names=["output"],
            dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
            opset_version=13, dynamo=False,
        )
    except TypeError:      # older torch has no dynamo kwarg
        torch.onnx.export(
            model, dummy, "out/bi_oi.onnx",
            input_names=["input"], output_names=["output"],
            dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
            opset_version=13,
        )
    print("\nexported out/bi_oi.onnx", flush=True)

    s = ort.InferenceSession("out/bi_oi.onnx", providers=["CPUExecutionProvider"])
    print("onnx in :", [(i.name, i.shape) for i in s.get_inputs()])
    print("onnx out:", [(o.name, o.shape) for o in s.get_outputs()])
    r = s.run(None, {"input": Xva[:8].astype(np.float32)})[0]
    print("onnx vs torch max-diff:", float(np.abs(r.ravel() - pv[:8]).max()))
    print("DONE")


if __name__ == "__main__":
    main()
