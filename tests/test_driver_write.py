"""写操作（锁频段 / 改信道）的单元测试。

这组测试是补出来的——真实故障是：`ZteRtaClient.post()` 返回三元组
`(code, body, headers)`，而 `set_band()` 按两个值解包，直接 ValueError。

为什么之前一直没暴露：早期换频段都是通过浏览器界面做的，Python 驱动只发过 GET，
写路径从没被执行过。这类「写在代码里但没人走」的分支，只能靠测试兜住。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drivers.zte_rta import ZteRtaDriver  # noqa: E402

WAN_XML = """<RGW><wan><cellular><Prefer_Band>3</Prefer_Band>
<NW_mode>2</NW_mode><prefer_mode>0</prefer_mode><prefer_lte_type>0</prefer_lte_type>
</cellular><mtu>1500</mtu></wan></RGW>"""

WLAN_XML = """<RGW><wlan_settings><channel>13</channel><bandwidth>1</bandwidth>
<wlan_enable>1</wlan_enable><net_mode>9</net_mode><max_clients>8</max_clients>
<first_channel>1</first_channel><last_channel>13</last_channel>
<dual_band_support>0</dual_band_support></wlan_settings></RGW>"""


class FakeClient:
    """只实现驱动用到的两个方法，post 故意返回三元组（与真实实现一致）。"""

    def __init__(self, code=200):
        self.code = code
        self.posted = []

    def get(self, file, retries=2):
        return WLAN_XML if "wlan" in file else WAN_XML

    def post(self, file, inner_xml):
        self.posted.append((file, inner_xml))
        return self.code, "", {}


def make_driver():
    d = ZteRtaDriver("192.168.0.1", "admin", "admin", timeout=1)
    fake = FakeClient()
    d.c = fake
    return d, fake


class TestWritePath(unittest.TestCase):
    def test_set_band_does_not_crash(self):
        """回归：post 返回三元组时不能崩。"""
        d, fake = make_driver()
        d.set_band(40)
        self.assertEqual(len(fake.posted), 1)

    def test_set_band_payload(self):
        d, fake = make_driver()
        d.set_band(40)
        _, xml = fake.posted[0]
        self.assertIn("<Prefer_Band>40</Prefer_Band>", xml)
        self.assertIn("<Prefer_Band_Action>1</Prefer_Band_Action>", xml)

    def test_set_band_unlock_uses_zero(self):
        """0 表示不锁定，必须能写进去（出门前 --restore 依赖它）。"""
        d, fake = make_driver()
        d.set_band(0)
        _, xml = fake.posted[0]
        self.assertIn("<Prefer_Band>0</Prefer_Band>", xml)

    def test_set_channel_does_not_crash(self):
        d, fake = make_driver()
        d.set_channel(7)
        self.assertEqual(len(fake.posted), 1)

    def test_set_channel_keeps_other_fields(self):
        """改信道时不能把带宽/加密/最大客户端数等字段冲掉。"""
        d, fake = make_driver()
        d.set_channel(7)
        _, xml = fake.posted[0]
        self.assertIn("<channel>7</channel>", xml)
        for keep in ("<bandwidth>1</bandwidth>", "<wlan_enable>1</wlan_enable>",
                     "<net_mode>9</net_mode>", "<max_clients>8</max_clients>"):
            self.assertIn(keep, xml)

    def test_capabilities_reads_channel_range(self):
        d, _ = make_driver()
        caps = d.capabilities()
        self.assertEqual(caps["channels"], list(range(1, 14)))
        self.assertEqual(caps["bands"], [3, 8, 38, 39, 40, 41])

    def test_get_state_parses_fields(self):
        d, _ = make_driver()
        st = d.get_state()
        self.assertEqual(st["band"], 3)
        self.assertEqual(st["channel"], 13)
        self.assertEqual(st["bandwidth"], "20MHz")


if __name__ == "__main__":
    unittest.main()
