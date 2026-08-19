"""Wi-Fi + IP helpers via NetworkManager (nmcli).

Used by the Settings / WiFi screen so the device (kiosk) can be connected to
Wi-Fi and show its IP without leaving the app. The eldercare user is in the
`netdev`/`sudo` groups and the session is local+active, so nmcli connect works
without a password prompt (see the polkit rule installed with the package).
"""
import subprocess

WIFI_DEV = "wlan0"


class _R:
    returncode = 1
    stdout = ""
    stderr = ""


def _run(args, timeout=25):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        r = _R(); r.stderr = str(e); return r


def _split(line):
    """Split an `nmcli -t` line on unescaped ':' (nmcli escapes literal ':' as '\\:')."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]; i += 2; continue
        if c == ":":
            out.append(cur); cur = ""; i += 1; continue
        cur += c; i += 1
    out.append(cur)
    return out


def current():
    """Return {'ip': ..., 'ssid': ...} for display."""
    ip, ssid = "", ""
    r = _run(["hostname", "-I"], timeout=5)
    toks = [t for t in (r.stdout or "").split() if ":" not in t]     # drop IPv6
    for t in toks:
        if t.startswith(("10.0.3.", "172.", "169.254.")):            # drop lxc bridge / link-local
            continue
        ip = t; break
    if not ip and toks:
        ip = toks[0]
    r = _run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"], timeout=8)
    for line in (r.stdout or "").splitlines():
        p = _split(line)
        if len(p) >= 2 and p[0] == "yes":
            ssid = p[1]; break
    return {"ip": ip or "(chưa có)", "ssid": ssid}


def scan(rescan=True):
    """Return list of {'ssid','signal','security'} (unique SSID, strongest first)."""
    if rescan:
        _run(["nmcli", "dev", "wifi", "rescan"], timeout=12)          # ignore errors (rate-limited)
    r = _run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"], timeout=15)
    seen, out = {}, []
    for line in (r.stdout or "").splitlines():
        p = _split(line)
        if len(p) < 2:
            continue
        ssid = p[0].strip()
        if not ssid:
            continue
        try:
            sig = int(p[1])
        except ValueError:
            sig = 0
        sec = (p[2] if len(p) > 2 else "").strip()
        d = seen.get(ssid)
        if d:
            if sig > d["signal"]:
                d["signal"], d["security"] = sig, sec
            continue
        d = {"ssid": ssid, "signal": sig, "security": sec}
        seen[ssid] = d; out.append(d)
    out.sort(key=lambda d: d["signal"], reverse=True)
    return out


def is_secured(security):
    s = (security or "").strip()
    return bool(s) and s != "--"


def connect(ssid, password=""):
    """Connect to ssid. Returns (ok: bool, message: str)."""
    args = ["nmcli", "dev", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    r = _run(args, timeout=35)
    if r.returncode == 0:
        return True, "Đã kết nối"
    # fallback via sudo (NOPASSWD/cached) if polkit denies from this session
    r2 = _run(["sudo", "-n"] + args, timeout=35)
    if r2.returncode == 0:
        return True, "Đã kết nối"
    msg = ((r.stderr or "") + " " + (r2.stderr or "")).strip() or "Không kết nối được"
    # keep it short/friendly
    low = msg.lower()
    if "password" in low or "secrets" in low or "802-11" in low:
        return False, "Sai mật khẩu hoặc thiếu mật khẩu"
    if "no network" in low or "not found" in low:
        return False, "Không tìm thấy mạng này"
    return False, msg.splitlines()[-1][:120]
