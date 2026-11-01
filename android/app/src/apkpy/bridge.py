"""原生界面的 Python 入口：每个函数都返回 JSON 字符串。

为什么全返回字符串，而不是让 Kotlin 直接摸 Python 对象：Chaquopy 能把 dict
转成 PyObject，但嵌套结构要在 Kotlin 里一层层 get，字段一改两边都得动。
序列化成 JSON 之后，加字段 Kotlin 完全不用改。

线程：这里不做任何调度。真正的耗时活儿（连设备、体检、守护）由 Kotlin 丢到
后台线程；界面只负责显示。也没有任何交互式提问 —— prompt/input 在 APK 里
没有终端可问，会直接把线程卡死。
"""

import contextlib
import json
import os
import re
import threading
import time
from types import SimpleNamespace

import autotune
import server

_guard = {"thread": None, "started": 0.0, "dry": False}
_RE_TIME = re.compile(r"^\d{1,2}:\d{2}")


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def _err(e):
    return _dumps({"error": "%s: %s" % (type(e).__name__, e)})


def init(files_dir=""):
    """Kotlin 启动时调一次，把应用私有可写目录告诉 Python。

    scanner.py 就是靠这个环境变量去找原生层写的 wifi_scan.json；不设的话
    它以为自己在 Termux 里，扫描失败会一路 fallback 到 nmcli/iw，最后报
    「nmcli / iw 都不可用（需要 root）」—— 而用户就在 app 里，根本没装那些。
    """
    if files_dir:
        os.environ["MIFI_FILES_DIR"] = files_dir
    return _dumps({"files": os.environ.get("MIFI_FILES_DIR", "")})


# ---------------- 状态与结论 ----------------

def status():
    """设备现状：频段、信道、信号、延迟/丢包。

    只测 3 包：界面每 5 秒问一次，而这个数字只是给个参考；手机上没有 ICMP，
    每包都要走一次 TCP 握手，6 包会让一次查询拖到好几秒，把调用线程占死。
    """
    try:
        return _dumps(server.device_status(ping_n=3))
    except Exception as e:  # noqa: BLE001
        return _err(e)


def plan():
    """上次体检留下的结论（plan.json），没有就是 None。"""
    try:
        p = server.HERE / "plan.json"
        if not p.exists():
            return _dumps({"plan": None})
        return _dumps({"plan": json.loads(p.read_text(encoding="utf-8"))})
    except Exception as e:  # noqa: BLE001
        return _dumps({"plan": None, "error": str(e)})


def scan():
    """扫一轮 2.4G 信道并打分（约 20 秒，不会断网）。"""
    try:
        return _dumps(server.channel_scan())
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------- 长任务 ----------------

def _start(args, kind):
    try:
        ok, msg = server.run_job(args, kind)
        return _dumps({"ok": ok, "msg": msg})
    except Exception as e:  # noqa: BLE001
        return _err(e)


def preview():
    """体检并预估（3~6 分钟，中途会断网几次）。"""
    return _start(["--preview"], "preview")


def apply_plan():
    """按上次体检的结论把改动落到设备上。"""
    return _start(["--apply-plan"], "apply")


def apply_channel():
    """按上次「扫描信道」的建议把信道锁上 —— 只动信道，不碰频段。"""
    return _start(["--apply-channel"], "apply")


def restore():
    """恢复原始设置。"""
    return _start(["--restore"], "restore")


def lock_band(band):
    """直接锁到指定频段（只切一次，不测量）。band=0 表示解除锁定交回自动。

    和体检那条路的区别：体检要先测完再裁决，这条路是「用户已经知道答案」。
    """
    return _start(["--lock-band", str(int(band))], "lock-band")


def set_channel(channel):
    """直接设 WiFi 信道（只写一次，不扫描）。channel=0 表示自动。"""
    return _start(["--set-channel", str(int(channel))], "set-channel")


def job():
    """当前任务的进度和日志行，界面每秒拉一次。"""
    with server.JOB_LOCK:
        snap = dict(server.JOB)
        snap["lines"] = list(server.JOB["lines"])
        snap["elapsed"] = (round(time.time() - server.JOB["started"])
                           if server.JOB["started"] and server.JOB["running"] else None)
    return _dumps(snap)


# ---------------- 守护模式 ----------------

def guard_start(dry=False):
    """开守护：在动就解锁、坐满就锁定。整个过程不碰界面线程。"""
    t = _guard["thread"]
    if t is not None and t.is_alive():
        # 关掉之后守护要等当前这一轮采样结束才真的退出（最多 30 秒）。
        # 这段时间里再开一次会撞上它，如实说明，别让人以为开关坏了。
        return _dumps({"ok": False, "msg": "上一次还在退，等几秒再开"})

    cfg = autotune.load_config()
    # 这些字段跟命令行一一对应。host/user/password/driver 给 None 就走
    # detect() 的默认探测，跟 `mifi --guard` 不带宽参数时完全一样。
    args = SimpleNamespace(
        host=None, user=None, password=None, driver=None,
        guard_interval=autotune.GUARD_INTERVAL,
        guard_settle=autotune.GUARD_SETTLE_SEC,
        guard_dry_run=bool(dry),
    )

    def worker():
        # 守护是往 stdout 打的（终端里就直接看）。APK 里没有终端，把这些行
        # 接进任务日志缓冲，界面才能像看进度一样看它。
        sink = server._LineSink()
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                autotune.run_guard(args, cfg)
        except Exception:  # noqa: BLE001
            import traceback
            server.log("[!] 守护异常退出：")
            for line in traceback.format_exc().splitlines():
                server.log(line)
        finally:
            sink.flush()

    t = threading.Thread(target=worker, daemon=True, name="mifi-guard")
    _guard.update(thread=t, started=time.time(), dry=bool(dry))
    t.start()
    return _dumps({"ok": True, "msg": "started"})


def guard_stop():
    t = _guard["thread"]
    if t is None or not t.is_alive():
        _guard["thread"] = None
        return _dumps({"ok": False, "msg": "守护没在跑"})
    autotune.guard_request_stop()
    return _dumps({"ok": True, "msg": "已请求停止（最多等一个采样间隔，约 30 秒）"})


def _last_tip():
    """守护现在在干什么：取它输出里最后一条带时间戳的行。

    守护每 30 秒打一行「16:40  -97dBm  延迟 65ms 丢包 0%  监控中」，
    动作（锁定/解锁/换频段）也带时间戳打在同样的位置。界面只要这一行，
    够回答「它到底有没有在工作」，又不至于糊一屏。
    跳过启动时那段横幅 —— 那些没有时间前缀。
    """
    # 守护线程正在往这个列表里追加，先拿锁抄一份再翻 —— 直接边追加边遍历
    # 会抛 RuntimeError，界面那边就整块刷不动了。
    with server.JOB_LOCK:
        lines = list(server.JOB["lines"])
    for line in reversed(lines):
        s = line.strip()
        if _RE_TIME.match(s):
            # 多个空格压成一个，一行放得下
            return " ".join(s.split())[:80]
    return ""


def guard_state():
    """守护跑没跑，以及它现在在干什么。

    不给完整日志：界面上不摆那一大坨输出（用户明确嫌吵）。要全文走 job()。
    """
    t = _guard["thread"]
    alive = t is not None and t.is_alive()
    return _dumps({
        "running": alive,
        "since": int(_guard["started"]) if alive else None,
        "elapsed": round(time.time() - _guard["started"]) if alive else None,
        "dry": _guard["dry"],
        "tip": _last_tip() if alive else "",
    })
