"""驱动注册表与自动探测。

新增机型时，只需要在 DRIVERS 里加一行，autotune / server 都不用改。
"""

from .base import DeviceBusy, DeviceDriver, Unsupported
from .zte_rta import ZteRtaDriver

# 探测顺序：越通用的放越后面也没关系，identify 要准
DRIVERS = [
    ZteRtaDriver,
    # MyDeviceDriver,   <- 在这里登记
]

REGISTRY = {d.name: d for d in DRIVERS}


def list_drivers():
    return [{"name": d.name, "description": d.description} for d in DRIVERS]


def get_driver_class(name=None):
    if name:
        if name not in REGISTRY:
            raise KeyError(f"未知驱动 {name}，可用：{', '.join(REGISTRY)}")
        return REGISTRY[name]
    return None


def detect(host="192.168.0.1", user="admin", password="admin", name=None, timeout=5):
    """返回一个已实例化的驱动；指定 name 时不探测，直接用。"""
    if name:
        return get_driver_class(name)(host, user, password, timeout=timeout)

    for cls in DRIVERS:
        try:
            if cls.identify(host, user, password, timeout=timeout):
                return cls(host, user, password, timeout=timeout)
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
