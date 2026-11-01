"""驱动注册表与自动探测。

新增机型时，只需要在 DRIVERS 里加一行，autotune / server 都不用改。
"""

import threading
import time

from .base import DeviceBusy, DeviceDriver, Unsupported
from .zte_rta import ZteRtaDriver

# 探测顺序：越通用的放越后面也没关系，identify 要准
DRIVERS = [
    ZteRtaDriver,
    # MyDeviceDriver,   <- 在这里登记
]

REGISTRY = {d.name: d for d in DRIVERS}

# 探测结果的短期记忆：host/账号 → 驱动类。
#
# 代价：60 秒内换了设备会被误判成旧驱动，此时请求会失败，重试一次即恢复。
_DETECT_MEMO = {}
DETECT_MEMO_SEC = 60

# 每个线程一份驱动实例，连着 Digest 会话一起复用。
#
# 为什么必须按线程分：客户端带着 nonce 计数（_nonce / _nc），两个线程同时推进
# 同一个计数，设备立刻判会话失效。所以「复用」和「线程安全」在这里是同一件事，
# 不能只做一半。
#
# 为什么非复用不可（2026-11-01 实测，PC 直连设备）：
#   get_state() 本身只要 3 个请求、约 2.7 秒；
#   可每建一次会话，还要再花 4 个请求、约 1 秒去「撞 401 → login.cgi → 验一次」。
#   而界面每 5 秒查一次状态，等于每秒都在为同一个会话反复付钱 —— 用户感受到的
#   就是「点一下先卡几秒」。会话留着，这一整块就没了。
#
# 会话失效（设备重启射频、被浏览器后台顶掉）时不用特殊处理：客户端自己的
# get() 会丢 nonce、重新挑战、必要时重新登录。
_LOCAL = threading.local()


def _thread_cache():
    d = getattr(_LOCAL, "by_key", None)
    if d is None:
        d = _LOCAL.by_key = {}
    return d


def forget_thread_driver(host=None, user=None, password=None):
    """丢掉本线程缓存的实例。换设备、或怀疑会话已经不可用时调用。"""
    cache = _thread_cache()
    if host is None:
        cache.clear()
    else:
        cache.pop((host, user, password), None)


def _memo_driver(host, user, password):
    key = (host, user, password)
    hit = _DETECT_MEMO.get(key)
    if not hit:
        return None
    if time.time() - hit[0] > DETECT_MEMO_SEC:
        _DETECT_MEMO.pop(key, None)
        return None
    return hit[1]


def list_drivers():
    return [{"name": d.name, "description": d.description} for d in DRIVERS]


def get_driver_class(name=None):
    if name:
        if name not in REGISTRY:
            raise KeyError(f"未知驱动 {name}，可用：{', '.join(REGISTRY)}")
        return REGISTRY[name]
    return None


def detect(host=None, user=None, password=None, name=None, timeout=5):
    """返回一个已实例化的驱动；指定 name 时不探测，直接用。

    None 一律当「用默认」处理。命令行那边有 argparse 的 default 兜着，但 APK
    的原生界面是手工构造参数对象的，漏填一项就会拼出 "None:80" 这样的地址去
    连，报错还特别像网络问题（真机上就踩过：守护日志满屏「连不上 None:80」）。
    注意只兜 None，不管空字符串 —— 空密码是合法输入，不能替用户改成 admin。
    """
    if host is None:
        host = "192.168.0.1"
    if user is None:
        user = "admin"
    if password is None:
        password = "admin"
    if name:
        return get_driver_class(name)(host, user, password, timeout=timeout)

    key = (host, user, password)
    cache = _thread_cache()
    hit = cache.get(key)
    if hit is not None:
        # 与驱动记忆同一个节拍失效：万一换了一台设备（同 IP 不同固件），
        # 最多 60 秒就能自愈，不会在这一条线程上永久卡着旧会话。
        if time.time() - hit[0] <= DETECT_MEMO_SEC:
            return hit[1]
        cache.pop(key, None)

    memo = _memo_driver(host, user, password)
    if memo is not None:
        dev = memo(host, user, password, timeout=timeout)
        cache[key] = (time.time(), dev)
        return dev

    for cls in DRIVERS:
        try:
            # 驱动若能在探测时把「已认证的会话」交回来，就用它构造 —— 否则这里
            # 等于让用户为同一个会话付两次钱（见 zte_rta.identify_client）。
            prober = getattr(cls, "identify_client", None)
            ok, client = prober(host, user, password, timeout=timeout) if prober else (
                cls.identify(host, user, password, timeout=timeout), None)
            if ok:
                _DETECT_MEMO[(host, user, password)] = (time.time(), cls)
                if client is not None:
                    dev = cls(host, user, password, timeout=timeout, client=client)
                else:
                    dev = cls(host, user, password, timeout=timeout)
                cache[key] = (time.time(), dev)
                return dev
        except DeviceBusy:
            raise                       # 设备可达但拒答，原因比"没驱动"重要得多
        except Exception:  # noqa: BLE001
            continue
    # 走到这里有两种完全不同的原因，必须分开说：设备就在那儿但驱动不认识，
    # 还是**根本没连上它**。实机 2026-10-30 21:43（手机）：换完频段 WiFi 还没
    # 重连，守护连打四行「没有驱动认得这台设备」，看起来像工具坏了，其实只是
    # 等 40 秒就好 —— 把人引到「去写驱动」这条死胡同。
    if not _port_open(host, 80, timeout=timeout):
        raise LookupError(
            f"连不上 {host}:80 —— WiFi 还没连上它，或设备刚换频段正在重启"
            "（通常 30~60 秒）。等它自己回来即可。"
        )
    raise LookupError(
        "没有驱动认得这台设备。\n"
        f"  已尝试：{', '.join(REGISTRY) or '（无）'}\n"
        "  如果是新机型，可参考 drivers/template.py 写一个驱动，"
        "或用 --driver 指定已知驱动。"
    )


def _port_open(host, port, timeout=3):
    """TCP 连得上吗（只用来区分「没连上设备」和「连上了但不认识」）。"""
    import socket

    try:
        with socket.create_connection((host, port), timeout=min(timeout, 3)):
            return True
    except OSError:
        return False
