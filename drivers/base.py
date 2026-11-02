"""设备驱动抽象层。

核心思路：把「怎么跟设备说话」和「怎么测量、怎么决策」彻底分开。
autotune.py 只依赖本模块定义的接口，不关心背后是飞猫、中兴、还是别的牌子。

新增一个机型 = 写一个 DeviceDriver 子类 + 在 drivers/__init__.py 里登记一行。
参考 drivers/template.py。
"""

from abc import ABC, abstractmethod
import functools
import threading


# 这些方法会真的跟设备说话，必须串行。
#
# 为什么要串到「方法级」而不是只在探测时加锁：会话（Digest 的 nonce 计数）是
# 一份共享状态，两个线程交替推进它，设备就判会话失效。2026-11-02 实测（PC 直连
# 192.168.0.1，两个线程分别按 3 秒 / 5 秒的节奏查状态，就是 App 里守护线程和
# 界面线程的样子）：
#
#     各持一份会话：avg 4.49s / 5.21s，max 7.88s / 9.23s
#     共用一份 + 串行：avg 3.66s / 3.70s，max 4.48s / 6.05s
#
# 单线程稳态是 2.66s —— 也就是说「各持一份」让两个线程都在替对方付重登的钱。
_SERIALIZED_METHODS = ("get_state", "capabilities", "set_band",
                       "set_channel", "verify_online")


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        lock = getattr(self, "_io_lock", None)
        if lock is None:            # 极端情况：还没走完 __init__
            return fn(self, *a, **kw)
        with lock:
            return fn(self, *a, **kw)
    wrapper.__mifi_serialized__ = True
    return wrapper


class Unsupported(Exception):
    """设备不支持该操作（例如某机型无法锁定频段）。"""


class DeviceBusy(ConnectionError):
    """设备可达、但当前拒绝服务。

    典型场景：管理员会话被别的客户端（浏览器后台）占用、登录失败次数过多被
    临时锁定、或设备刚重启还没准备好。这类问题**换驱动没用**，应该告诉用户
    去重启设备，而不是报「没有驱动认得这台设备」。
    """


class DeviceUnreachable(ConnectionError):
    """设备 HTTP 接口暂时没响应（连接超时 / 被重置）。

    典型场景：正在切频段，设备射频重启，web 服务短暂不响应（5~20 秒）。
    这类错误是**暂时的**，调用方应该重试或跳过当前步骤，绝不能让它
    冒到最外层把整个任务中止 —— 否则设备会被留在半途的频段上。
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
        # 实例被多个线程共用，所有跟设备说话的方法都靠它串行（见 _serialized）。
        # RLock 而不是 Lock：get_state() 内部可能再调一次被包装过的方法。
        self._io_lock = threading.RLock()

    def __init_subclass__(cls, **kw):
        """子类一被定义，就自动把「会碰设备的方法」包上互斥锁。

        放在这儿而不是让每个驱动自己写：漏包一个方法，症状是「偶尔莫名慢一下」，
        既不会报错也不会崩，极难查。注册在基类，新驱动自动继承这条保护。
        """
        super().__init_subclass__(**kw)
        for name in _SERIALIZED_METHODS:
            fn = cls.__dict__.get(name)
            if fn is not None and not getattr(fn, "__mifi_serialized__", False):
                setattr(cls, name, _serialized(fn))

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

    def drop_session(self):
        """丢掉缓存的认证会话。

        设备重启射频（换频段、改信道）之后，手里的 nonce 一定已经作废。
        留着一个已知死掉的会话，下一次读要先吃一次「还没就绪」再重发 ——
        干脆提前扔掉，让第一趟就从干净的挑战开始。默认无事可做。
        """
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
