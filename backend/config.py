import os
import socket as _socket


def _detect_server_ip() -> str:
    """Best-effort: find a non-loopback LAN IP for this machine."""
    # 1. Try routing towards a public address (works when internet is available)
    for target in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
                s.connect((target, 80))
                ip = s.getsockname()[0]
                if not ip.startswith("127."):
                    return ip
        except Exception:
            pass

    # 2. Walk all network interfaces and return the first private LAN address
    try:
        import subprocess, re
        out = subprocess.check_output(
            ["ip", "route", "get", "1"], text=True, stderr=subprocess.DEVNULL
        )
        m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out)
        if m and not m.group(1).startswith("127."):
            return m.group(1)
    except Exception:
        pass

    # 3. macOS fallback — read the primary interface via ipconfig
    try:
        import subprocess
        for iface in ("en0", "en1", "en2"):
            out = subprocess.check_output(
                ["ipconfig", "getifaddr", iface],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            if out and not out.startswith("127."):
                return out
    except Exception:
        pass

    # 4. getaddrinfo on the hostname
    try:
        hostname = _socket.gethostname()
        for _, _, _, _, sockaddr in _socket.getaddrinfo(hostname, None):
            ip = sockaddr[0]
            if ":" not in ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


ADB_LAB_PASSWORD        = os.getenv("ADB_LAB_PASSWORD",   "adblab123")
ADB_LAB_PORT            = int(os.getenv("ADB_LAB_PORT",   "8000"))
ADB_PATH                = os.getenv("ADB_PATH",           "adb")
RESERVATION_TIMEOUT_HOURS = float(os.getenv("RESERVATION_TIMEOUT_HOURS", "2"))
DEVICE_POLL_INTERVAL    = int(os.getenv("DEVICE_POLL_INTERVAL",    "5"))

# CI/CD integration
ADB_LAB_CI_API_KEY  = os.getenv("ADB_LAB_CI_API_KEY",  "ci-key-change-me")
ADB_LAB_SERVER_IP   = os.getenv("ADB_LAB_SERVER_IP") or _detect_server_ip()

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "lab.db")
