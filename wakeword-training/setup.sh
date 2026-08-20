#!/bin/bash
# ==========================================================================
#  Wake-word "Bi ơi" — training environment setup on the L40S.
#  ISOLATED: everything lives under ~/eldercare/wakeword. Touches no
#  production service, no shared venv, CPU-only (GPU is busy with prod).
# ==========================================================================
set -u
WW=$HOME/eldercare/wakeword
exec > $WW/setup.log 2>&1 || { mkdir -p $WW; exec > $WW/setup.log 2>&1; }
set -x
mkdir -p $WW/{models,data,positives,negatives,out}
cd $WW

echo "=== 1. isolated venv (CPU torch, keeps GPU free for production) ==="
if [ ! -x venv/bin/python ]; then
  python3 -m venv venv
fi
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q piper-tts onnxruntime numpy scipy tqdm
./venv/bin/pip install -q torch --index-url https://download.pytorch.org/whl/cpu

echo "=== 2. openWakeWord feature models (melspec + embedding) ==="
BASE=https://github.com/dscripka/openWakeWord/releases/download/v0.5.1
for f in melspectrogram.onnx embedding_model.onnx; do
  [ -s models/$f ] || curl -sSL --retry 3 -o models/$f $BASE/$f
done
ls -l models/

echo "=== 3. Vietnamese Piper voices (positives + hard negatives) ==="
PV=https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN
dl() {  # dl <url> <dest>
  [ -s "$2" ] || curl -sSL --retry 3 -o "$2" "$1"
}
dl $PV/vivos/x_low/vi_VN-vivos-x_low.onnx            models/vi_vivos.onnx
dl $PV/vivos/x_low/vi_VN-vivos-x_low.onnx.json       models/vi_vivos.onnx.json
dl $PV/vais1000/medium/vi_VN-vais1000-medium.onnx      models/vi_vais.onnx
dl $PV/vais1000/medium/vi_VN-vais1000-medium.onnx.json models/vi_vais.onnx.json
dl $PV/25hours_single/low/vi_VN-25hours_single-low.onnx      models/vi_25h.onnx
dl $PV/25hours_single/low/vi_VN-25hours_single-low.onnx.json models/vi_25h.onnx.json
ls -l models/

echo "=== 4. sanity: piper speaks Vietnamese? ==="
echo "Bi ơi" | ./venv/bin/python -m piper --model models/vi_vivos.onnx \
     --output_file /tmp/ww_test.wav 2>&1 | tail -5
ls -l /tmp/ww_test.wav 2>/dev/null || echo "PIPER-FAIL"

echo "=== 5. speaker count per voice ==="
./venv/bin/python - <<'PY'
import json, glob
for f in sorted(glob.glob('models/*.onnx.json')):
    try:
        d = json.load(open(f))
        print(f, "num_speakers=", d.get("num_speakers"), "sr=", d.get("audio", {}).get("sample_rate"))
    except Exception as e:
        print(f, "ERR", e)
PY

echo "=== SETUP DONE ==="
