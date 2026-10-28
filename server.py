"""fm-autotune 的本地 Web 面板（手机可用）。

纯标准库，无依赖：
    python server.py            # 监听 127.0.0.1:8787（本机调试）
    python server.py --lan      # 监听 0.0.0.0:8787（手机同一个 Wi-Fi 可访问）

然后手机浏览器打开 http://<电脑局域网IP>:8787
iOS/Android 都可以「添加到主屏幕」，用起来跟 App 一样。
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"

sys.path.insert(0, str(HERE))
from drivers import detect  # noqa: E402
import autotune  # noqa: E402
import probe  # noqa: E402
import scanner  # noqa: E402

JOB = {"running": False, "lines": [], "kind": None, "started": None, "exit": None}
JOB_LOCK = threading.Lock()


def _env():
    e = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def log(line):
    with JOB_LOCK:
        JOB["lines"].append(line)
        if len(JOB["lines"]) > 400:
            JOB["lines"] = JOB["lines"][-400:]


def run_job(args, kind):
    with JOB_LOCK:
        if JOB["running"]:
            return False, "已有任务在跑"
        JOB.update(running=True, lines=[], kind=kind, started=time.time(), exit=None)

    def worker():
        cmd = [sys.executable, "-u", str(HERE / "autotune.py")] + args
        log(f"$ autotune.py {' '.join(args)}")
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env=_env(), bufsize=1)
            for raw in iter(p.stdout.readline, b""):
                line = raw.decode("utf-8", "ignore").rstrip()
                if line:
                    log(line)
            p.wait()
            code = p.returncode
        except Exception as e:  # noqa: BLE001
            log(f"[!] 启动失败：{e}")
            code = -1
        log(f"—— 任务结束（exit={code}）")
        with JOB_LOCK:
            JOB["running"] = False
            JOB["exit"] = code

    threading.Thread(target=worker, daemon=True).start()
    return True, "started"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, fmt, *args):  # 静音
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/status":
                return self._json(self.api_status())
            if path == "/api/scan":
                return self._json(self.api_scan())
            if path == "/api/job":
                with JOB_LOCK:
                    snap = dict(JOB)
                    snap["lines"] = list(JOB["lines"])
                    snap["elapsed"] = round(time.time() - JOB["started"]) if JOB["started"] and JOB["running"] else None
                return self._json(snap)
        except (LookupError, OSError) as e:
            return self._json({"error": str(e)}, 503)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        try:
            if path == "/api/optimize":
                mode = body.get("mode", "all")
                args = {"check": [],
                        "wifi": ["--apply", "--wifi-only"],
                        "lte": ["--apply", "--lte-only"],
                        "all": ["--apply"]}.get(mode, [])
                if body.get("bands"):
                    args += ["--bands", str(body["bands"])]
                ok, msg = run_job(args, f"optimize-{mode}")
                return self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
            if path == "/api/restore":
                ok, msg = run_job(["--restore"], "restore")
                return self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)

    # ---------- API 实现 ----------

    def api_status(self):
        cfg = autotune.load_config()
        dev = detect()
        st = dev.get_state()
        caps = dev.capabilities()
        ping = probe.ping_stats(6, cfg["ping_host"])
        return {
            "driver": dev.name,
            "vendor": caps.get("vendor"),
            "band": st["band"],
            "channel": st["channel"],
            "bandwidth": st.get("bandwidth"),
            "mtu": st.get("mtu"),
            "max_clients": (st.get("raw", {}).get("wlan") or {}).get("max_clients"),
            "can_lock_band": caps.get("can_lock_band", False),
            "lte": st.get("signal") or {},
            "ping": ping,
            "ts": int(time.time()),
        }

    def api_scan(self):
        cfg = autotune.load_config()
        dev = detect()
        st = dev.get_state()
        own = cfg.get("own_ssid") or scanner.connected_ssid() or st.get("ssid")
        aps = scanner.scan_2g(rounds=5, delay=3, own_ssid=own)
        if not aps:
            return {"current": st["channel"], "best": None, "aps": [],
                    "scores": [], "error": "没有扫到邻居 AP（系统语言不支持或无线网卡未启用）"}
        rows = scanner.score_channels(aps, dev.capabilities().get("channels") or range(1, 14))
        return {"current": st["channel"], "best": rows[0]["ch"], "aps": aps, "scores": rows}


def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lan", action="store_true", help="允许局域网（手机）访问")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    srv = ThreadingHTTPServer((host, a.port), Handler)
    print(f"面板已启动： http://127.0.0.1:{a.port}")
    if a.lan:
        print(f"手机访问：   http://{lan_ip()}:{a.port}   （需与电脑同一 Wi-Fi）")
    print("Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
