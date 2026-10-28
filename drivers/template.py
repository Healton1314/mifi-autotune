"""新机型模板：照着填就能接入。

步骤：
  1. 复制本文件为 drivers/你的驱动名.py，改类名和 name
  2. 实现 identify / get_state / capabilities / set_band / set_channel
  3. 在 drivers/__init__.py 的 DRIVERS 列表里加一行
  4. 用 python autotune.py --driver 你的驱动名 验证

判断一个设备能不能适配：抓一下 web 后台的请求，看它有没有
  - 能读到「当前频段 / 信道」的接口
  - 能写入「频段锁定 / 信道」的接口
两者都有就能做，只是字段名不同。

如果设备根本不提供频段锁定（很多低价随身WiFi 没有），
就把 capabilities()["can_lock_band"] 设为 False、set_band 抛 Unsupported，
引擎会自动跳过频段部分，只优化 WiFi 信道。
"""

from .base import DeviceDriver, Unsupported


class MyDeviceDriver(DeviceDriver):
    name = "mydevice"
    description = "示例：某品牌 XX 型号"

    @classmethod
    def identify(cls, host, user, password, timeout=5) -> bool:
        # 用最快的接口探测，例如读一个公开状态页，判断特征字符串
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://{host}/", timeout=timeout) as r:
                return b"YourVendorKeyword" in r.read(4096)
        except Exception:  # noqa: BLE001
            return False

    def capabilities(self):
        return {
            "bands": [1, 3, 8, 34, 38, 39, 40, 41],
            "channels": list(range(1, 14)),
            "can_lock_band": True,
            "can_set_channel": True,
            "wifi_band": "2.4GHz",
            "vendor": "YourVendor",
            "model_hint": "某品牌 XX 系列",
        }

    def get_state(self):
        # TODO: 读设备，按 base.DeviceDriver 里的格式返回
        return {
            "band": 0,
            "channel": 0,
            "bandwidth": "",
            "mtu": "",
            "signal": {"band": "", "rsrp": "", "sinr": "", "rsrq": "", "pci": "", "operator": ""},
        }

    def set_band(self, band: int):
        raise Unsupported("该机型暂不支持锁定频段")

    def set_channel(self, channel: int):
        raise Unsupported("该机型暂不支持设置信道")
