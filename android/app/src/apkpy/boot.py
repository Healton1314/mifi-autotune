"""APK 启动器：把面板起在 127.0.0.1，交给 WebView 加载。

为什么不直接用 server.py 的 main()：那是给终端用的，要解析 --lan/--port、
往 stdout 打一堆提示，而且会阻塞在 serve_forever。APK 里要的是「调一个函数、
拿回访问令牌」，界面由 Kotlin 那边负责。

端口失败会往后试几个：8787 万一被别的进程占了，整个 app 不该因此打不开。
"""

import os
import secrets
import threading
import traceback

import server

_state = {"srv": None, "token": "", "port": 0, "error": ""}


def set_files_dir(path):
    """告诉 Python 侧「应用私有可写目录」在哪。

    原生扫描桥把结果写成 wifi_scan.json 放这儿，scanner.py 通过这个环境变量
    找它。也让配置文件、位置记忆落在可写的地方，而不是 APK 里那个解压目录。
    """
    os.environ["MIFI_FILES_DIR"] = path
    return path


def start(port=8787, tries=6):
    """起面板，返回访问令牌；起不来抛异常（Kotlin 侧接住并显示）。"""
    if _state["srv"] is not None:
        return _state["token"]

    token = secrets.token_urlsafe(16)
    last = None
    for p in range(port, port + tries):
        try:
            srv = server.serve(host="127.0.0.1", port=p, token=token)
        except OSError as e:          # 端口被占：换下一个，别直接放弃
            last = e
            continue
        threading.Thread(target=srv.serve_forever, daemon=True,
                         name="mifi-panel").start()
        _state.update(srv=srv, token=token, port=p, error="")
        return token

    _state["error"] = str(last or "没有可用端口")
    raise last or OSError("没有可用端口")


def url():
    """WebView 要打开的地址（带令牌，首次访问就会写进 Cookie）。

    app=1 让面板收起「装 Termux」「持续检测」那几块 —— 那些只对电脑上的
    浏览器有意义，在 app 里出现只会让人以为这东西还是个终端工具的壳。
    """
    if not _state["port"]:
        return ""
    return "http://127.0.0.1:%d/?k=%s&app=1" % (_state["port"], _state["token"])


def status():
    """一行状态，给界面底部或排障用。"""
    if _state["srv"] is None:
        return "面板没起来：" + (_state["error"] or "未初始化")
    return "面板 %d ｜ can_spawn=%s" % (_state["port"], server.can_spawn())


def probe(files_dir=""):
    """自检报告。启动失败时 main 会把这段打在屏幕上，省得连 logcat 猜。"""
    try:
        import android_probe
        return android_probe.report(files_dir)
    except Exception:  # noqa: BLE001
        return traceback.format_exc()
