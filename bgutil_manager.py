import os
import subprocess
import time
import urllib.request
from shutil import which

PING_URL = "http://127.0.0.1:4416/ping"

def _ping(timeout=0.35) -> bool:
    try:
        with urllib.request.urlopen(PING_URL, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False

def _find_node() -> str:
    node = which("node") or r"C:\Program Files\nodejs\node.exe"
    if not node or not os.path.exists(node):
        raise RuntimeError("Node.js topilmadi (PATH yoki C:\\Program Files\\nodejs\\node.exe).")
    return node

def start_bgutil_if_needed(server_js_path: str) -> subprocess.Popen | None:
    if _ping():
        return None  # oldindan ishlayapti

    if not os.path.exists(server_js_path):
        raise RuntimeError(f"bgutil build topilmadi: {server_js_path}")

    node = _find_node()

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        [node, server_js_path],
        cwd=os.path.dirname(server_js_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    deadline = time.time() + 6.0
    while time.time() < deadline:
        if _ping():
            return proc
        time.sleep(0.15)

    try:
        proc.terminate()
    except Exception:
        pass
    raise RuntimeError("bgutil server start bo‘lmadi (ping timeout).")

def stop_bgutil(proc):
    if not proc:
        return

    # already dead
    if proc.poll() is not None:
        return

    pid = proc.pid

    # 1) polite terminate
    try:
        proc.terminate()
    except Exception:
        pass

    # 2) wait a bit
    try:
        proc.wait(timeout=2)
        return
    except Exception:
        pass

    # 3) force kill process tree (Windows)
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception:
        pass

    # 4) last resort
    try:
        proc.kill()
    except Exception:
        pass