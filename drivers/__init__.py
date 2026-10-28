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
    raise LookupError(
        "没有驱动认得这台设备。\n"
        f"  已尝试：{', '.join(REGISTRY) or '（无）'}\n"
        "  如果是新机型，可参考 drivers/template.py 写一个驱动，"
        "或用 --driver 指定已知驱动。"
    )
