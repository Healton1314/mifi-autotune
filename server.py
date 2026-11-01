"""mifi-autotune 的本地 Web 面板（手机可用）。

纯标准库，无依赖：
    python server.py              # 监听 127.0.0.1:8787（本机调试）
    python server.py --lan        # 监听 0.0.0.0:8787（手机同一个 Wi-Fi 可访问）

然后手机浏览器打开终端打印的 http://<电脑局域网IP>:8787/?k=<令牌>
iOS/Android 都可以「添加到主屏幕」，用起来跟 App 一样。

安全设计：所有 /api/* 都要求访问令牌，令牌来源优先级为
  --token 参数 > 环境变量 MIFI_PANEL_TOKEN > 工作目录下的 .panel-token（首次自动生成）。
浏览器首次用 ?k=<令牌> 打开时会把令牌写进 Cookie，之后正常使用即可。

为什么必须校验：面板能改设备频段、能让整个网络断线，而 --lan 是暴露在
整个局域网里的。不校验的话，同一 WiFi 下的任何人都能操作你的路由器。
"""

import argparse
import contextlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".panel-token"
COOKIE_NAME = "mifi_pt"

sys.path.insert(0, str(HERE))
from drivers import detect  # noqa: E402
import autotune  # noqa: E402
import probe  # noqa: E402
import scanner  # noqa: E402

JOB = {"running": False, "lines": [], "kind": None, "started": None, "exit": None}
JOB_LOCK = threading.Lock()

# 持续检测（--watch）是常驻进程，跟一次性任务分开管理：
# 它不会自己结束，必须让用户看得见、并且能一键停掉。
WATCH = {"proc": None, "lines": [], "since": None, "dry": False}
WATCH_LOCK = threading.Lock()

TOKEN = ""


def web_dir():
    """面板静态文件位置：源码运行时在 ./web，pip 安装后在 share/mifi-autotune/web。"""
    for cand in (HERE / "web", Path(sys.prefix) / "share" / "mifi-autotune" / "web"):
        if (cand / "index.html").exists():
            return cand
    raise SystemExit("找不到面板静态文件 web/，请从源码目录运行，或重新安装本包。")


# ---------- 手机独立运行的安装包 ----------
#
# 一次性把代码送到手机上，之后手机自己跑（Termux），不再依赖这台电脑。
# 为什么不走 git clone：实测这条链路上 HTTPS 到 GitHub 会被 reset，
# 而同一局域网内的 HTTP 下载稳定可用。
PHONE_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "env", "dist", "build",
                   ".idea", ".vscode", "tests"}
PHONE_SKIP_FILES = {"state.json", "config.json", "plan.json", "autotune-log.jsonl",
                    ".panel-token"}

# 打进包时必须强制 LF 的文本类型。
#
# 踩过的坑：Windows 上工作区的 .sh 是 CRLF（.gitattributes 只管 git 读写，
# 管不到已经落盘的文件），直接 zip 给手机后 bash 会把行尾的 \r 当成命令的一部分——
# 报错是 `line 11: PREFIX: command not found`、`case "$PKG" in\r` 语法错误这种
# 完全看不出和换行符有关的怪样子，排查一次要半天。这里在打包时抹掉，谁的工作区
# 是什么样都不影响手机端能不能跑。
# .bat 不在此列：Windows 自己要用，CRLF 才是它该有的样子。
PHONE_LF_SUFFIXES = {".sh", ".py", ".txt", ".md", ".toml", ".yml", ".yaml", ".json",
                     ".html", ".css", ".js", ".manifest", ".webmanifest"}


def build_phone_package():
    """把当前代码打成 zip（排除运行期产物与本机配置）。"""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(HERE.rglob("*")):
            rel = p.relative_to(HERE)
            if set(rel.parts) & PHONE_SKIP_DIRS:
                continue
            if any(part.endswith(".egg-info") for part in rel.parts):
                continue
            if p.is_dir() or p.name in PHONE_SKIP_FILES:
                continue
            if p.suffix in (".pyc", ".zip"):
                continue
            arcname = str(Path("mifi-autotune") / rel)
            if p.suffix.lower() in PHONE_LF_SUFFIXES:
                # 用 writestr 才能改写内容；from_file 保留时间戳与权限位
                info = zipfile.ZipInfo.from_file(p, arcname)
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, p.read_bytes().replace(b"\r\n", b"\n"))
            else:
                z.write(p, arcname)
    return buf.getvalue()


def load_token(explicit=None):
    """决定本次使用的令牌，并把它**持久化**到 .panel-token。

    持久化很重要：如果某次用 --token 指定了别的值却没写回文件，下次不带参数启动
    就会换一个令牌，浏览器里已经登录的页面全部变成 401——用户只会看到"点了没反应"。
    """
    if explicit:
        t = explicit
    else:
        env = os.environ.get("MIFI_PANEL_TOKEN")
        if env:
            t = env
        elif TOKEN_FILE.exists():
            t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        else:
            t = ""
        if not t:
            t = secrets.token_urlsafe(12)
    try:
        if not TOKEN_FILE.exists() or TOKEN_FILE.read_text(encoding="utf-8").strip() != t:
            TOKEN_FILE.write_text(t, encoding="utf-8")
            os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return t


def _env():
    e = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def log(line):
    with JOB_LOCK:
        JOB["lines"].append(line)
        if len(JOB["lines"]) > 400:
            JOB["lines"] = JOB["lines"][-400:]


# ---------------- 能不能起子进程 ----------------
# 面板本来是「再起一个 python 进程跑 autotune」：这样任务崩了不会带倒面板，
# 输出也能逐行流式读到。但 Chaquopy 打出来的 APK 里**没有独立的 python 二进制**
# （sys.executable 指向 /system/bin/app_process64），Popen 出去的东西跑不了
# python 代码。所以先探一次并缓存结果，探不通就退到「同一个解释器里跑」。
_SPAWN_OK = None


def can_spawn():
    """当前解释器能不能真的起一个 python 子进程（结果只探一次）。"""
    global _SPAWN_OK
    if _SPAWN_OK is not None:
        return _SPAWN_OK
    try:
        p = subprocess.run([sys.executable, "-c", "print(1)"],
                           capture_output=True, timeout=8)
        _SPAWN_OK = p.returncode == 0 and b"1" in p.stdout
    except Exception:  # noqa: BLE001
        _SPAWN_OK = False
    return _SPAWN_OK


class _LineSink:
    """把 print 出来的东西按行喂给 log()，替代读子进程 stdout 那条路。"""

    encoding = "utf-8"

    def __init__(self, buf=""):
        self._buf = buf

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                log(line.rstrip())
        return len(s)

    def flush(self):
        if self._buf.strip():
            log(self._buf.strip())
        self._buf = ""


def run_inprocess(args):
    """在同一个解释器里跑一次 autotune，返回退出码。

    只用于起不了子进程的环境。autotune.main() 是纯命令行入口（读 sys.argv），
    所以这里临时换掉 argv —— 比把参数解析复制一份出来稳，改参数定义时不会漏。
    """
    sink = _LineSink()
    old_argv = sys.argv
    sys.argv = [str(HERE / "autotune.py")] + list(args)
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            autotune.main()
        return 0
    except SystemExit as e:          # argparse 出错或 --help
        return int(e.code or 0)
    except Exception:  # noqa: BLE001
        for line in traceback.format_exc().splitlines():
            log(line)
        return 1
    finally:
        sink.flush()
        sys.argv = old_argv


def run_job(args, kind):
    with JOB_LOCK:
        if JOB["running"]:
            return False, "已有任务在跑"
        JOB.update(running=True, lines=[], kind=kind, started=time.time(), exit=None)

    def worker():
        log(f"$ autotune.py {' '.join(args)}")
        if can_spawn():
            cmd = [sys.executable, "-u", str(HERE / "autotune.py")] + args
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
        else:
            code = run_inprocess(args)
        log(f"—— 任务结束（exit={code}）")
        with JOB_LOCK:
            JOB["running"] = False
            JOB["exit"] = code

    threading.Thread(target=worker, daemon=True).start()
    return True, "started"


# ---------------- 持续检测（可随时关闭的常驻进程） ----------------

def _kill_tree(pid):
    """杀掉整个进程树：watch 会再起子进程跑重测，只杀父进程会留下孤儿。"""
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, env=_env())
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass


def watch_status():
    with WATCH_LOCK:
        proc = WATCH["proc"]
        running = proc is not None and proc.poll() is None
        if proc is not None and not running:
            WATCH["proc"] = None
        return {
            "running": running,
            "pid": proc.pid if running else None,
            "since": WATCH["since"],
            "elapsed": (round(time.time() - WATCH["since"]) if running and WATCH["since"] else None),
            "dry": WATCH["dry"],
            "lines": list(WATCH["lines"])[-200:],
        }


def watch_start(dry=False):
    # APK 里没有独立解释器，也就没有「随时能被 Ctrl+C 或信号停掉」的常驻进程。
    # 硬塞进线程的话，watch 的循环永远等不到退出信号 —— 面板上点了「停止」也
    # 停不下来。与其假装能跑，不如直说，让用户改用一次性的体检。
    if not can_spawn():
        return False, "APK 里没有独立解释器，持续检测用不了；改用面板的「体检」"
    with WATCH_LOCK:
        proc = WATCH["proc"]
        if proc is not None and proc.poll() is None:
            return False, "已经在运行中"
        args = [sys.executable, "-u", str(HERE / "autotune.py"), "--watch"]
        if dry:
            args.append("--watch-dry-run")
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=_env(), bufsize=1,
                             creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                                            if sys.platform.startswith("win") else 0))
        WATCH.update(proc=p, lines=["$ autotune.py --watch" + (" --watch-dry-run" if dry else "")],
                     since=time.time(), dry=dry)

    def reader():
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", "ignore").rstrip()
            if line:
                with WATCH_LOCK:
                    WATCH["lines"].append(line)
                    if len(WATCH["lines"]) > 400:
                        WATCH["lines"] = WATCH["lines"][-400:]
        with WATCH_LOCK:
            WATCH["lines"].append("—— 持续检测已停止")

    threading.Thread(target=reader, daemon=True).start()
    return True, "started"


def watch_stop():
    with WATCH_LOCK:
        proc = WATCH["proc"]
        if proc is None or proc.poll() is not None:
            WATCH["proc"] = None
            return False, "没有在运行"
        _kill_tree(proc.pid)
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            pass
        WATCH["proc"] = None
        WATCH["lines"].append("—— 已手动停止")
        return True, "stopped"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(web_dir()), **kw)

    def log_message(self, fmt, *args):  # 静音
        pass

    # ---------- 鉴权 ----------

    def _presented_token(self):
        q = parse_qs(urlparse(self.path).query)
        if q.get("k"):
            return q["k"][0]
        h = self.headers.get("X-Panel-Token")
        if h:
            return h
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == COOKIE_NAME:
                    return v.strip()
        return None

    def _authorized(self):
        got = self._presented_token()
        return bool(got) and secrets.compare_digest(got, TOKEN)

    def _deny(self):
        body = json.dumps({"error": "需要访问令牌：请用终端打印的带 ?k=... 的地址打开"},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        # 带 ?k= 打开首页：写入 Cookie 并重定向，让后续 fetch 自动带上
        if path in ("/", "/index.html") and q.get("k"):
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={q['k'][0]}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            self.end_headers()
            return

        # 手机安装包：不打令牌校验，因为拿到它的目的就是「在手机上装一个独立副本」，
        # 而 Termux 里没有浏览器的 Cookie。里面只有源码（和 GitHub 上那份一样），
        # 不含 config.json / plan.json / .panel-token 等本机数据。
        if path == "/api/phone-package":
            data = build_phone_package()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             'attachment; filename="mifi-autotune-phone.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        if path.startswith("/api/"):
            if not self._authorized():
                return self._deny()
            try:
                if path == "/api/status":
                    return self._json(self.api_status())
                if path == "/api/scan":
                    return self._json(self.api_scan())
                if path == "/api/plan":
                    plan = HERE / "plan.json"
                    if plan.exists():
                        try:
                            return self._json({"plan": json.loads(plan.read_text(encoding="utf-8"))})
                        except Exception:  # noqa: BLE001
                            return self._json({"plan": None})
                    return self._json({"plan": None})
                if path == "/api/watch":
                    return self._json(watch_status())
                if path == "/api/job":
                    with JOB_LOCK:
                        snap = dict(JOB)
                        snap["lines"] = list(JOB["lines"])
                        snap["elapsed"] = (round(time.time() - JOB["started"])
                                           if JOB["started"] and JOB["running"] else None)
                    return self._json(snap)
                if path == "/api/phone-package":
                    data = build_phone_package()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Disposition",
                                     'attachment; filename="mifi-autotune-phone.zip"')
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
            except (LookupError, OSError) as e:
                return self._json({"error": str(e)}, 503)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return self._deny()
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        try:
            if path == "/api/watch/start":
                ok, msg = watch_start(bool(body.get("dry")))
                return self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
            if path == "/api/watch/stop":
                ok, msg = watch_stop()
                return self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
            if path == "/api/preview":
                ok, msg = run_job(["--preview"], "preview")
                return self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
            if path == "/api/apply-plan":
                ok, msg = run_job(["--apply-plan"], "apply-plan")
                return self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
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
        return device_status()

    def api_scan(self):
        return channel_scan()

_PING_CACHE = {"ts": 0.0, "key": None, "val": None}
PING_CACHE_SEC = 10


def _cached_ping(n, host, ttl=PING_CACHE_SEC):
    """给状态轮询用的 ping，带 10 秒缓存。

    为什么必须缓存：`ping -c 6` 在电脑上是 6 秒（每秒一包），在安卓上没有
    ICMP 权限、退回「6 次 TCP 握手」，也要好几秒。而界面每 5 秒就来问一次
    —— 于是那条调用线程**永远在处理上一个请求**，用户点任何东西都得排队，
    表现就是「点了没反应、任务结束了按钮还挂着『正在…』」。

    延迟数字晚 10 秒不影响任何判断（本来就是个参考值），但「点了不动」
    很影响。真要最新值，去看设备页或跑一次 --status。
    """
    key = (host, n)
    now = time.time()
    c = _PING_CACHE
    if c["val"] is not None and c["key"] == key and now - c["ts"] < ttl:
        return c["val"]
    val = probe.ping_stats(n, host)
    c.update(ts=now, key=key, val=val)
    return val


def device_status(ping_n=6):
    """读一次设备现状（频段/信号/延迟）。面板和 APK 原生界面共用这一份。

    抽成模块级而不是留在 Handler 里：APK 那边不再走 HTTP，直接调这个函数，
    两边看到的数据才不会各算各的。
    """
    cfg = autotune.load_config()
    dev = detect()
    st = dev.get_state()
    caps = dev.capabilities()
    ping = _cached_ping(ping_n, cfg["ping_host"])
    # 「频段」有两个含义，别混：band 是锁定值（0 = 自动），serving_band 是
    # 设备此刻实际驻留的。恢复默认之后锁定被解除，band 就是 0 —— 界面上如果
    # 只认 band，就会显示成一个「—」，看着像设备坏了，其实它只是变回自动了。
    serving = autotune.serving_band(st)
    _text, band_warn = autotune.describe_band_state(st["band"], serving)
    return {
        "driver": dev.name,
        "vendor": caps.get("vendor"),
        "band": st["band"],
        "serving_band": serving,
        "band_warn": band_warn,
        "channel": st["channel"],
        "bandwidth": st.get("bandwidth"),
        "mtu": st.get("mtu"),
        "max_clients": (st.get("raw", {}).get("wlan") or {}).get("max_clients"),
        "can_lock_band": caps.get("can_lock_band", False),
        # 手动设频段/信道要给用户一份「这台设备认的合法值」，不能让他手填。
        # 填了个不支持的值，设备要么静默忽略、要么花 26 秒重新注册再失败，
        # 用户只会以为工具坏了。
        "can_set_channel": caps.get("can_set_channel", False),
        "bands": caps.get("bands") or [],
        "channels": caps.get("channels") or [],
        "lte": st.get("signal") or {},
        "ping": ping,
        "ts": int(time.time()),
    }


def channel_scan():
    """扫一轮 2.4G 并打分，返回当前/最优信道与全部周边 AP。"""
    cfg = autotune.load_config()
    dev = detect()
    st = dev.get_state()
    own = cfg.get("own_ssid") or scanner.connected_ssid() or st.get("ssid")
    aps = scanner.scan_2g(rounds=5, delay=3, own_ssid=own)
    if not aps:
        # 面板这里以前写死成「系统语言不支持或无线网卡未启用」—— 手机上那句话
        # 是错的（真正原因通常是没装 Termux:API 或没给定位权限）。统一走
        # 扫描层那套病因判断，面板和命令行不能给两套互相矛盾的说法。
        return {"current": st["channel"], "best": None, "aps": [],
                "scores": [], "error": scanner.scan_failure_hint()}
    rows = scanner.score_channels(aps, dev.capabilities().get("channels") or range(1, 14))
    best = scanner.pick_best_channel(rows)
    # 顺手把建议存下来：界面上那句「建议固定到 3」下面就跟着「应用建议」，
    # 用户点了之后引擎得知道该锁哪条 —— 两个进程只能靠文件传。
    if best:
        autotune.save_channel_advice(st["channel"], best["ch"])
    return {"current": st["channel"], "best": best["ch"] if best else None,
            "aps": aps, "scores": rows}

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


def serve(host="127.0.0.1", port=8787, token=None):
    """起一个面板 server 并返回它（不阻塞、不打印）。

    抽出来是给 APK 用的：那边由 Kotlin 起线程调它，再把 WebView 指到
    127.0.0.1。终端用法完全不变，main() 还是原来那套。
    返回的 server 需要调用方自己 serve_forever()。
    """
    global TOKEN
    TOKEN = load_token(token)
    return ThreadingHTTPServer((host, port), Handler)


def main():
    ap = argparse.ArgumentParser(description="mifi-autotune 本地面板")
    ap.add_argument("--lan", action="store_true", help="允许局域网（手机）访问")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--token", default=None,
                    help="指定访问令牌（默认自动生成并存到 .panel-token）")
    a = ap.parse_args()

    host = "0.0.0.0" if a.lan else "127.0.0.1"
    srv = serve(host=host, port=a.port, token=a.token)

    print("面板已启动")
    print(f"  本机访问： http://127.0.0.1:{a.port}/?k={TOKEN}")
    if a.lan:
        print(f"  手机访问： http://{lan_ip()}:{a.port}/?k={TOKEN}   （需与电脑同一 Wi-Fi）")
    print(f"  令牌文件： {TOKEN_FILE}")
    print("  所有 /api/* 都要校验令牌；浏览器用带 ?k= 的地址打开一次即可记住。")
    print("Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        # 面板退出时不要把常驻的持续检测留在后台
        try:
            watch_stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
