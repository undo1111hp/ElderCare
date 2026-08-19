# ElderCare — Trợ lý giọng nói + đọc thơ cho người cao tuổi

Sản phẩm riêng trong hệ thống CloudPTalk. Một thiết bị Raspberry Pi 5 chạy app
giọng nói, trò chuyện với "Ngân" (persona người cháu), **đọc thơ của chính bà**
(RAG trên kho thơ Phan Ngọc Lan), nhắc lịch/thuốc, và quét thuốc bằng camera.

Toàn bộ phần server **cô lập tuyệt đối** với các dịch vụ đang chạy: dùng service
mới `ptalk_signature` ở cổng **8005**, route nginx **/device/**, collection Qdrant
riêng **eldercare_poems** — KHÔNG sửa `llm_worker`/`tts_worker`/`stt_worker`, KHÔNG
đụng `/v2` `/eldercare` `/v1`.

```
┌──────────────────────────┐        ws://YOUR_L40S_HOST:8000/device/ws
│  Raspberry Pi 5 (app)    │  ───────────────────────────────────────►┐
│  pi-app/  PyQt6 + ALSA   │  Opus uplink / PCM16 downlink + chat JSON │
│  ReSpeaker Lite + camera │  ◄───────────────────────────────────────┘
└──────────────────────────┘                                          │
                                   ┌──────────────────────────────────▼───────────────┐
                                   │ nginx :8000  ──/device/──►  host:8005             │
                                   │ ptalk_signature (server/)  [clone của ptalk_v2]   │
                                   │  • STT ZipFormer (CPU)                            │
                                   │  • RAG bge-m3(CPU) → Qdrant eldercare_poems       │
                                   │  • Elder prompt + giờ thực + recite line-by-line  │
                                   │        │ push STREAM_LLM / STREAM_TTS (Redis)     │
                                   │        ▼ tái dùng llm_worker(Gemma)+tts_worker    │
                                   └───────────────────────────────────────────────────┘
```

## Cấu trúc thư mục

| Thư mục | Nội dung |
|---|---|
| `pi-app/` | App native trên Pi 5 (Python/PyQt6). Build thành `.deb`. |
| `server/` | Service `/device` chạy trên L40S (clone cô lập của `ptalk_v2`). |
| `poem-rag/` | Script nạp + truy vấn kho thơ vào Qdrant, kèm dữ liệu thơ đã xử lý. |
| `deploy/` | Tiện ích triển khai (chèn block nginx, test loa A/B). |

### `pi-app/` (client)
- `ptalk/` — mã nguồn: `config.py` (cấu hình), `voice_client.py` (VoiceEngine WS +
  parse khung chat JSON), `audio_io.py` (arecord/aplay + **auto-level/limiter loa**),
  `ui.py` (đa màn hình, khung chat "Bà vừa nói / Ngân"), `opus_codec.py`, `protocol.py`,
  `reminders.py`, `medicine.py`, `tts.py`, `__main__.py` (`--check`, `--screenshot`).
- `pkg/` — control, postinst, launcher `/usr/bin/ptalk-signature`, `config.toml` mặc
  định (đã trỏ `/device/ws`, `output_gain=0.6`), service kiosk.
- `assets_src/` — ảnh nhân vật + logo.
- `build_deb.sh` — build gói (VER hiện tại 0.4.0).

### `server/` (dịch vụ /device)
- `ptalk_signature/settings.py` — persona "Ngân" (gọi bà/xưng cháu), luật đọc thơ,
  ngắt nghỉ, tham số LLM. Dùng CHUNG Gemma qua `.env` của CloudPTalk.
- `ptalk_signature/device_pipeline.py` — lõi: STT → tìm thơ (bge-m3+Qdrant) →
  **RECITE bypass** (đọc nguyên văn từng dòng qua STREAM_TTS) hoặc đường LLM (chat/
  liệt kê/hỏi đáp) có bơm **giờ thực Việt Nam**.
- `ptalk_signature/main.py` — WS server (giao thức audio y hệt v2, thêm khung chat
  JSON, đã vá tiếng nhiễu lúc bắt đầu cho chế độ PCM_OUT). HTTP `/device/ask` để test.
- `run_ptalk_signature.sh` — chạy uvicorn cổng 8005 (CPU-only).

### `poem-rag/`
- `poems_structured.json` — 90 bài thơ đã tách (title/date/dedication/body).
- `poem_themes.json` — metadata làm giàu (themes/mood/tóm tắt/đối tượng/dịp).
- `ingest_poems.py` — nạp vào Qdrant `eldercare_poems` (90 bài) + `eldercare_poem_lines`
  (590 đoạn), embed bge-m3 **trên CPU** (không đụng GPU). Tự merge `poem_themes.json`.
- `query_poems.py` — test truy vấn ngữ nghĩa. `list_by_theme.py` — liệt kê theo chủ đề.

## Triển khai nhanh

**Hạ tầng:** Pi 5 = `eldercare@YOUR_PI_IP` (IP động, hay đổi — nên đặt IP tĩnh);
L40S = `USER@YOUR_L40S_HOST`, repo `~/Ptalk_project/CloudPTalk`, venv `CloudPTalk/venv`.
Mật khẩu lưu riêng (không ghi trong repo này).

### 1) Nạp kho thơ (chạy 1 lần, trên L40S)
```bash
# đặt các file poem-rag/ vào /home/USER/eldercare/ rồi:
cd /home/USER/eldercare
CUDA_VISIBLE_DEVICES="" /home/USER/Ptalk_project/CloudPTalk/venv/bin/python ingest_poems.py
# kiểm tra: python query_poems.py
```

### 2) Dịch vụ /device (trên L40S)
```bash
# copy server/ptalk_signature/ -> ~/Ptalk_project/CloudPTalk/ptalk_signature/
# copy server/run_ptalk_signature.sh -> ~/Ptalk_project/CloudPTalk/
cd ~/Ptalk_project/CloudPTalk && ./run_ptalk_signature.sh        # uvicorn :8005
python deploy/insert_device_nginx.py                              # chèn location /device/ (tự backup)
sudo docker exec nginx-gateway nginx -t && sudo docker exec nginx-gateway nginx -s reload
sudo ufw allow from 172.27.0.0/16 to any port 8005 proto tcp      # cho phép docker→8005
# test: curl -X POST http://localhost:8000/device/ask -d '{"question":"..."}'
```

### 3) App trên Pi
```bash
# scp pi-app/ -> Pi:~/ptalk-native-src, đổi CRLF nếu build từ Windows:
find . -type f \( -name '*.py' -o -name '*.sh' -o -name '*.toml' \) -exec sed -i 's/\r$//' {} +
bash build_deb.sh && sudo dpkg -i ~/ptalk-build/ptalk-signature-native_0.4.0.deb
# tự chạy trong desktop labwc (không dùng cage vì desktop đã chiếm màn DSI):
mkdir -p ~/.config/autostart && cp pkg autostart entry...   # đã cài; app tự bật khi boot
ptalk-signature --check                                     # tự kiểm tra round-trip
```

## Ghi chú kỹ thuật quan trọng
- **Loa ReSpeaker Lite chỉ 16 kHz, không có núm chỉnh** → hạ mức bằng phần mềm
  (`audio.output_gain=0.6` + limiter mềm trong `audio_io.Player`).
- **Đọc thơ = RECITE bypass** (bỏ qua LLM) để 100% nguyên văn + ngắt nghỉ từng dòng.
- **Giờ**: L40S chạy UTC → `device_pipeline` bơm giờ Asia/Ho_Chi_Minh vào prompt.
- **Không tự bịa thơ**: nếu kho không có, Ngân nói chưa có rồi hỏi lại (thác nước
  poems-first; web fallback qua SearXNG là bước sau).
- **Rollback**: tắt :8005, gỡ block nginx (khôi phục `nginx.conf.bak.*`), `ufw delete`,
  trỏ Pi về `/v2/ws`.

## Việc còn lại (roadmap)
- Nhắc lịch/thuốc bằng lời qua `/device` (server sinh `reminder_action`, Pi giữ lịch offline).
- Web search fallback (SearXNG tự host) khi kho thơ không có.
- Camera Pi hiện báo "no camera detected" — kiểm tra lại cáp.
- Đặt IP tĩnh cho Pi để khỏi đổi liên tục.
