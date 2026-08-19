"""Entry point.

  ptalk-signature                 launch fullscreen Elder Care GUI
  ptalk-signature --check         headless diagnostics (opus/audio/camera/WS)
  ptalk-signature --screenshot out.png [--state playing] [--size 720x1280]
                                  render the UI offscreen to a PNG (no display)
"""
import argparse
import os
import subprocess
import sys
import time

from . import config as cfgmod


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    except Exception as e:
        return f"(error: {e})"


def _ws_selftest(cfg):
    import asyncio
    import json
    from .opus_codec import OpusEncoder
    from .protocol import pack_frame

    enc = OpusEncoder(cfg["audio"]["sample_rate"], cfg["audio"]["channels"])
    packed = pack_frame(enc.encode(b"\x00" * cfg.pcm_frame_bytes))
    stats = {"frames": 0, "bytes": 0}

    async def run():
        import websockets
        s = cfg["server"]
        async with websockets.connect(s["ws_url"], open_timeout=10,
                                      max_size=None, ping_interval=None) as ws:
            await ws.send(json.dumps({"device_id": s["device_id"],
                                      "firmware_version": s["firmware_version"]}))
            await ws.send("START_PCM_OUT")

            async def snd():
                for _ in range(60):
                    await ws.send(packed)
                    await asyncio.sleep(0.02)
                await ws.send("END")

            async def rcv():
                while True:
                    m = await ws.recv()
                    if isinstance(m, (bytes, bytearray)):
                        stats["frames"] += 1
                        stats["bytes"] += len(m)
                    else:
                        print("   event:", m.strip())
                        if m.strip().upper() == "IDLE":
                            return

            t = asyncio.create_task(snd())
            try:
                await asyncio.wait_for(rcv(), timeout=25)
            except asyncio.TimeoutError:
                print("   (timeout waiting for IDLE)")
            t.cancel()
        print(f"   downlink audio: {stats['frames']} frames, {stats['bytes']} bytes")

    asyncio.run(run())


def cmd_check(cfg):
    print("== PTalk Signature diagnostics ==\n")
    from .opus_codec import OpusEncoder
    enc = OpusEncoder(cfg["audio"]["sample_rate"], cfg["audio"]["channels"])
    n = len(enc.encode(b"\x00" * cfg.pcm_frame_bytes))
    print(f"[opus]   OK — encoded {cfg.pcm_frame_bytes}-byte PCM frame -> {n} bytes\n")
    print("[mic]  arecord -l:\n" + (_run(["arecord", "-l"]) or "(no capture device)"))
    print("\n[speaker]  aplay -l:\n" + (_run(["aplay", "-l"]) or "(no playback device)"))
    print("\n[alsa names]  arecord -L (first lines):")
    print("\n".join(_run(["arecord", "-L"]).splitlines()[:20]))
    print("\n[camera]  picamera2:")
    try:
        from picamera2 import Picamera2
        info = Picamera2.global_camera_info()
        print("   " + (str(info) if info else "(no camera detected)"))
    except Exception as e:
        print("   error:", e)
    print("\n[voice server]  WS round-trip to " + cfg["server"]["ws_url"])
    try:
        _ws_selftest(cfg)
    except Exception as e:
        print("   FAILED:", e)
    print("\nDone.")


_SAMPLE_MED = (
    "Tên thuốc: Paracetamol 500mg\n\n"
    "Công dụng chính: Hạ sốt, giảm đau nhẹ đến vừa (đau đầu, đau răng, đau cơ).\n\n"
    "Liều dùng tham khảo: Người lớn 1–2 viên mỗi lần, cách nhau 4–6 giờ, "
    "không quá 8 viên trong 24 giờ.\n\n"
    "Lưu ý quan trọng: Không dùng chung rượu bia. Hỏi bác sĩ nếu có bệnh gan.\n")


def cmd_screenshot(cfg, out, state, size, screen):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6 import QtWidgets
    from .ui import MainWindow
    from . import reminders as rem
    app = QtWidgets.QApplication(sys.argv)
    try:
        w, h = (int(x) for x in size.lower().split("x"))
    except Exception:
        w, h = 720, 1280
    win = MainWindow(cfg, start_engine=False, preview_state=state)
    win.resize(w, h)
    win.show()
    if screen in ("reminders", "alarm") and not win.store.items:
        win.store.items = [
            rem.Reminder("a", "07:00", "Thuốc huyết áp", "med"),
            rem.Reminder("b", "12:30", "Thuốc tiểu đường", "med"),
            rem.Reminder("c", "19:00", "Vitamin", "med"),
            rem.Reminder("d", "09:00", "Tái khám tim mạch", "appt"),
        ]
    if screen == "medicine":
        win.med.set_result(_SAMPLE_MED); win.navigate("medicine")
    elif screen == "alarm":
        win.alarm.show_for(win.store.items[0])
    elif screen and screen != "home":
        win.navigate(screen)
    for _ in range(45):                 # let timer tick / amp settle
        app.processEvents()
        time.sleep(0.03)
    win.grab().save(out)
    print(f"screenshot ({screen or state}) saved -> {out}")


def main():
    ap = argparse.ArgumentParser(prog="ptalk-signature")
    ap.add_argument("--check", action="store_true", help="headless diagnostics")
    ap.add_argument("--screenshot", default=None, metavar="PATH",
                    help="render UI to PNG offscreen and exit")
    ap.add_argument("--state", default="idle",
                    choices=["idle", "recording", "uploading", "playing", "error"],
                    help="forced character state (for --screenshot)")
    ap.add_argument("--screen", default="home",
                    choices=["home", "medicine", "reminders", "add", "settings", "alarm", "wifi"],
                    help="which screen to render (for --screenshot)")
    ap.add_argument("--size", default="720x1280", help="WxH (for --screenshot)")
    ap.add_argument("--config", default=None, help="path to config.toml")
    args = ap.parse_args()
    cfg = cfgmod.load(args.config)

    if args.check:
        cmd_check(cfg); return
    if args.screenshot:
        cmd_screenshot(cfg, args.screenshot, args.state, args.size, args.screen); return

    from PyQt6 import QtWidgets
    from .ui import MainWindow
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow(cfg)
    if cfg["ui"]["fullscreen"]:
        win.showFullScreen()
    else:
        win.resize(720, 1280)
        win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
