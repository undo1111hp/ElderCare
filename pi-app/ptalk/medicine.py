"""Medicine scanner — port of HoloboxApi. Capture with picamera2, send to the
holobox medicine endpoint, return the Vietnamese analysis text."""
import json
import time

import requests

MEDICINE_PROMPT = (
    "Hãy phân tích hình ảnh này và nhận diện chính xác viên/vỉ thuốc trong ảnh. "
    "1) Loại trừ người dùng, chỉ tập trung vào thuốc. "
    "2) Cho biết: Tên thuốc, Công dụng chính, Liều dùng tham khảo, và Lưu ý quan trọng. "
    "Trả lời bằng tiếng Việt. TUYỆT ĐỐI KHÔNG dùng bảng hay Markdown phức tạp. "
    "Viết dưới dạng các đoạn văn ngắn hoặc liệt kê dòng đơn giản."
)


def capture_jpeg(path, rotation=180):
    from picamera2 import Picamera2
    from libcamera import Transform

    picam = Picamera2()
    try:
        transform = Transform(hflip=1, vflip=1) if rotation == 180 else Transform()
        cfg = picam.create_still_configuration(transform=transform)
        picam.configure(cfg)
        picam.start()
        time.sleep(1.0)          # let auto-exposure / focus settle
        picam.capture_file(path)
    finally:
        try:
            picam.stop()
        except Exception:
            pass
        picam.close()
    return path


def analyze_medicine(image_path, aitools_url):
    url = aitools_url.rstrip("/") + "/holobox/medician"
    with open(image_path, "rb") as f:
        data = f.read()
    files = [
        ("image", ("medicine.jpg", data, "image/jpeg")),
        ("image_file", ("medicine.jpg", data, "image/jpeg")),
    ]
    resp = requests.post(url, data={"prompt": MEDICINE_PROMPT}, files=files, timeout=60)
    resp.raise_for_status()
    return _extract_gemini_text(resp.text)


def _extract_gemini_text(body):
    try:
        j = json.loads(body)
        text = j["candidates"][0]["content"]["parts"][0]["text"]
        return (text or "").strip()
    except Exception:
        return body.strip()[:2000]


def camera_available():
    """True if a sensor is actually attached. Picamera2 only raises on
    construction, which is too late to draw a helpful screen, so probe first."""
    try:
        from picamera2 import Picamera2
        return bool(Picamera2.global_camera_info())
    except Exception:
        return False


class CameraPreview:
    """Live preview + still capture on ONE open sensor.

    The scanner used to capture blind: open, sleep 1s, shoot. Elderly users had
    no way to see whether the blister was in frame, so a miss only surfaced as a
    failed analysis. This keeps a single stream running so the preview the user
    lines up IS the frame that gets sent — the rotation transform is applied in
    the configuration, not per-capture, so what they see is what is analysed.

    One stream (no lores): the lores path forces YUV420 on several pipelines and
    the format juggling is not worth it at this resolution.
    """

    SIZE = (1024, 768)

    def __init__(self, rotation=180):
        self._rotation = rotation
        self._picam = None

    def start(self):
        from picamera2 import Picamera2
        from libcamera import Transform

        if self._picam is not None:
            return
        picam = Picamera2()
        transform = Transform(hflip=1, vflip=1) if self._rotation == 180 else Transform()
        cfg = picam.create_preview_configuration(
            main={"size": self.SIZE, "format": "RGB888"}, transform=transform)
        picam.configure(cfg)
        picam.start()
        self._picam = picam

    def frame(self):
        """Latest frame as a numpy array, or None if the camera is not running.

        picamera2 labels this format "RGB888" but lays the bytes out B,G,R —
        callers must treat it as BGR (Qt: Format_BGR888)."""
        if self._picam is None:
            return None
        return self._picam.capture_array("main")

    def capture(self, path):
        if self._picam is None:
            raise RuntimeError("camera not started")
        self._picam.capture_file(path)
        return path

    def stop(self):
        picam, self._picam = self._picam, None
        if picam is None:
            return
        try:
            picam.stop()
        except Exception:
            pass
        try:
            picam.close()
        except Exception:
            pass
