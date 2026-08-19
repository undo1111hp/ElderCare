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
