"""设备驱动抽象层。

核心思路：把「怎么跟设备说话」和「怎么测量、怎么决策」彻底分开。
autotune.py 只依赖本模块定义的接口，不关心背后是飞猫、中兴、还是别的牌子。

新增一个机型 = 写一个 DeviceDriver 子类 + 在 drivers/__init__.py 里登记一行。
参考 drivers/template.py。
"""

from abc import ABC, abstractmethod


class Unsupported(Exception):
    """设备不支持该操作（例如某机型无法锁定频段）。"""


class DeviceBusy(ConnectionError):
    """设备可达、但当前拒绝服务。

    典型场景：管理员会话被别的客户端（浏览器后台）占用、登录失败次数过多被
    临时锁定、或设备刚重启还没准备好。这类问题**换驱动没用**，应该告诉用户
    去重启设备，而不是报「没有驱动认得这台设备」。
    """


class DeviceDriver(ABC):
    #: 驱动标识，命令行用 --driver 指定
    name = "base"
    #: 人类可读的适配范围说明
    description = "抽象基类"

    def __init__(self, host="192.168.0.1", user="admin", password="admin", timeout=25):
        self.host = host
        self.user = user
        self.password = password
        self.timeout = timeout

    # ---------- 必须实现 ----------

    @classmethod
    @abstractmethod
    def identify(cls, host, user, password, timeout=5) -> bool:
        """快速探测：这个设备是否归本驱动管。要快（<5 秒），失败就返回 False。"""

    @abstractmethod
    def get_state(self) -> dict:
        """读取当前状态，统一格式见下方约定。"""

    @abstractmethod
    def capabilities(self) -> dict:
        """能力声明，引擎据此决定能不能改、能改哪些值。"""

    @abstractmethod
    def set_band(self, band: int):
        """锁定 LTE 频段；band=0 表示不锁定（自动）。不支持时抛 Unsupported。"""

    @abstractmethod
    def set_channel(self, channel: int):
        """设置 WiFi 信道；0 表示自动。不支持时抛 Unsupported。"""

    # ---------- 统一格式约定 ----------
    #
    # get_state() 返回：
    # {
    #   "band":    3,            # int，0 = 不锁定
    #   "channel": 13,           # int，0 = 自动
    #   "bandwidth": "20MHz",    # str，可空
    #   "mtu": "1500",           # str，可空
    #   "signal": {              # 可空；字段缺失填空字符串
    #       "band": "3", "rsrp": "-104dBm", "sinr": "9dB",
    #       "rsrq": "-13.0dB", "pci": "441", "operator": "CMCC"
    #   },
    #   "raw": {...}             # 可选，驱动自己的原始数据
    # }
    #
    # capabilities() 返回：
    # {
    #   "bands": [3, 8, 38, 39, 40, 41],   # 可选频段；空列表表示不支持锁定
    #   "channels": [1..13],               # 可选信道
    #   "wifi_band": "2.4GHz",             # 说明性文字
    #   "can_lock_band": True,
    #   "can_set_channel": True,
    #   "vendor": "ZTE-RTA",
    #   "model_hint": "飞猫 FM_M1 等",
    # }

    # ---------- 可选钩子 ----------

    def verify_online(self) -> bool:
        """改动后自检：设备是否还能提供数据。默认 True，驱动可覆盖。"""
        return True

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
