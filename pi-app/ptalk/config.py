"""Configuration: DEFAULTS <- /etc config <- user override (writable)."""
import copy
import json
import os
import tomllib

DEFAULTS = {
    "server": {
        "ws_url": "ws://YOUR_L40S_HOST:8000/device/ws",   # Elder Care /device endpoint
        "aitools_url": "https://aitools.ptit.edu.vn",
        "device_id": "pi_eldercare",
        "firmware_version": "3.0.0",
    },
    "audio": {
        "sample_rate": 48000,
        "channels": 1,
        "frame_ms": 20,
        "input_device": "default",
        "output_device": "default",
        "opus_bitrate": 24000,
        # Loa ReSpeaker Lite khuếch đại cố định (không có núm chỉnh) → tín hiệu to
        # dễ vỡ tiếng ("rè"). output_gain hạ mức + limiter mềm chống méo (0..1).
        "output_gain": 0.6,
    },
    "camera": {"rotation": 180},
    "emergency": {"number": "115"},
    # Gọi rảnh-tay bằng từ khoá "Bi ơi" (openWakeWord, chạy tại chỗ trên Pi).
    # enabled=False cho tới khi có model + openwakeword; khi đó bật trong Cài đặt.
    "wakeword": {
        "enabled": True,
        "model_path": "/opt/ptalk-signature/models/bi_oi.onnx",
        # 0.9 + 2 lần liên tiếp: đo trên tập kiểm thử gần như không báo nhầm khi
        # trong phòng có người nói chuyện bình thường, mà vẫn nghe được ~90%.
        "threshold": 0.9,            # cao hơn = ít báo nhầm, khó gọi hơn
        "trigger_hits": 2,           # cần mấy lần liên tiếp mới coi là gọi
        "refractory_ms": 1800,       # khoảng chờ giữa 2 lần đánh thức
        "silence_ms": 1300,          # im lặng bao lâu thì coi là nói xong
        "lead_ms": 6000,             # gọi xong mà không nói gì -> huỷ lượt
        "max_ms": 13000,             # trần một lượt nói
    },
    "display": {"output": "DSI-2"},          # wlr-randr output name for rotate
    "tts": {"enabled": True},
    "ui": {
        "fullscreen": True,
        "font_scale": 1.15,                  # người già: chữ to hơn mặc định
        "rotation": 0,                       # 0/90/180/270 — nút Xoay cộng dồn 90°
    },
}


def user_data_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "ptalk-signature")


def _user_override_path():
    return os.path.join(user_data_dir(), "settings.json")


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data):
        self._d = data

    def __getitem__(self, key):
        return self._d[key]

    def get(self, key, default=None):
        return self._d.get(key, default)

    @property
    def samples_per_frame(self):
        a = self._d["audio"]
        return a["sample_rate"] // 1000 * a["frame_ms"]

    @property
    def pcm_frame_bytes(self):
        return self.samples_per_frame * 2 * self._d["audio"]["channels"]

    def save_user(self, section, values):
        """Persist a small user override (e.g. ui font_scale / orientation)."""
        path = _user_override_path()
        cur = {}
        try:
            with open(path, encoding="utf-8") as f:
                cur = json.load(f)
        except Exception:
            cur = {}
        cur.setdefault(section, {})
        cur[section].update(values)
        self._d[section].update(values)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)


def load(path=None):
    path = path or os.environ.get("PTALK_CONFIG", "/etc/ptalk-signature/config.toml")
    data = {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[config] warning: could not parse {path}: {e}")
    merged = _merge(DEFAULTS, data)
    # user override (writable, from Settings screen)
    try:
        with open(_user_override_path(), encoding="utf-8") as f:
            merged = _merge(merged, json.load(f))
    except Exception:
        pass
    return Config(merged)
