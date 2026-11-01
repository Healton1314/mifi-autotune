"""APK 自检：确认 Python 运行时、源码、静态文件在包里都到位了。

只被 Kotlin 侧调用（MainActivity 启动时打一屏报告），终端和面板用法完全不受影响。

之所以单独做这么一层：Python 打进 APK 之后，「文件在不在、能不能 import、
哪个目录可写」这三件事一旦出问题，表现是黑屏或者一闪就退，看不出原因。
先把它们摊成文字打在屏幕上，比连 logcat 省事得多。
"""

import os
import platform
import sys
import traceback


def _try_import():
    """逐个 import 主模块，返回 (成功列表, 失败明细)。"""
    ok, bad = [], []
    for name in ("autotune", "scanner", "probe", "server", "drivers"):
        try:
            __import__(name)
            ok.append(name)
        except Exception:  # noqa: BLE001
            bad.append(f"{name}:\n{traceback.format_exc(limit=3)}")
    return ok, bad


def _web_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "web"), os.path.join(os.path.dirname(here), "web")):
        if os.path.isfile(os.path.join(cand, "index.html")):
            return cand
    return None


def report(work_dir=""):
    out = []
    out.append("=== mifi-autotune APK 自检 ===")
    out.append(f"Python   {sys.version.split()[0]}  ({platform.machine()})")
    out.append(f"前缀     {sys.prefix}")
    out.append(f"源码目录 {os.path.dirname(os.path.abspath(__file__))}")
    out.append(f"可写目录 {work_dir or '(未传入)'}")

    ok, bad = _try_import()
    out.append("")
    out.append("[import] 成功：" + (", ".join(ok) if ok else "（无）"))
    for b in bad:
        out.append("[import] 失败：" + b)

    web = _web_dir()
    out.append("")
    out.append("[静态文件] " + (web if web else "找不到 web/index.html —— 面板会打不开"))

    exe = getattr(sys, "executable", "") or ""
    out.append(f"[解释器] sys.executable = {exe or '(空)'}")
    out.append("          APK 里没有独立 python 二进制，子进程方式起不来是正常的，"
               "需走线程内调用。")

    try:
        if work_dir:
            probe_file = os.path.join(work_dir, ".writable")
            with open(probe_file, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe_file)
            out.append("[可写] 是")
    except Exception:  # noqa: BLE001
        out.append("[可写] 否 —— " + traceback.format_exc(limit=2))

    try:
        import autotune
        ver = getattr(autotune, "VERSION", None) or getattr(autotune, "__version__", "?")
        out.append(f"[版本] {ver}")
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(out)
