# -*- coding: utf-8 -*-
"""驱动探测的短期记忆。

为什么要有它：界面每 5 秒查一次状态，每次都要 detect()。而 identify() 是
「发请求 → 撞 401 → 带 Digest 再来一次」，中途可能还要走 login.cgi ——
这是整条状态查询里最贵的一段，比读状态本身贵得多。

这里锁住三件事：
  ① 同一个设备 60 秒内只探测一次；
  ② 换个设备（host 不同）照常重新探测，不能把它当成同一台；
  ③ 记忆会过期 —— 换设备后最多 60 秒就自愈，不会永久用错驱动。
"""

import threading
import time
import unittest
from unittest import mock

import drivers


def _probe_ok(_host=None, _user=None, _password=None, timeout=5):
    """替身探测：认得这台设备，但没有可复用的会话。"""
    return True, None


class DetectMemo(unittest.TestCase):
    def setUp(self):
        drivers._DETECT_MEMO.clear()
        drivers.forget_thread_driver()
        self._ttl = drivers.DETECT_MEMO_SEC

    def tearDown(self):
        drivers._DETECT_MEMO.clear()
        drivers.forget_thread_driver()
        drivers.DETECT_MEMO_SEC = self._ttl

    def _detect(self, host="192.168.0.1"):
        return drivers.detect(host=host, user="admin", password="admin")

    def test_identify_runs_once_within_ttl(self):
        with mock.patch.object(drivers.ZteRtaDriver, "identify_client",
                               side_effect=_probe_ok) as m:
            self._detect()
            self._detect()
            self._detect()
        self.assertEqual(m.call_count, 1, "60 秒内不该反复探测")

    def test_other_host_is_probed_separately(self):
        with mock.patch.object(drivers.ZteRtaDriver, "identify_client",
                               side_effect=_probe_ok) as m:
            self._detect("192.168.0.1")
            self._detect("192.168.8.1")
        self.assertEqual(m.call_count, 2)

    def test_memo_expires(self):
        drivers.DETECT_MEMO_SEC = 0
        with mock.patch.object(drivers.ZteRtaDriver, "identify_client",
                               side_effect=_probe_ok) as m:
            self._detect()
            time.sleep(0.01)
            self._detect()
        self.assertEqual(m.call_count, 2, "过期后要重新探测，不能永久用错驱动")

    def test_same_thread_reuses_one_session(self):
        """同一条线程要复用同一个实例 —— 会话（nonce/_nc）就在里面。

        每建一次会话，设备那边要多花四个请求、约一秒；界面每 5 秒查一次状态，
        不复用等于每秒都在为同一个会话反复付钱。
        """
        with mock.patch.object(drivers.ZteRtaDriver, "identify_client",
                               side_effect=_probe_ok):
            dev = self._detect()
            again = self._detect()
        self.assertEqual(dev.name, "zte_rta")
        self.assertIs(dev, again)

    def test_other_thread_shares_the_same_instance(self):
        """跨线程共用一份会话，靠 base 的方法级锁串行 —— 不是各持一份。

        上一版是「每线程一份」，理由是「两个线程同时推进 nonce 计数会判会话失效」。
        担心是对的，解法错了：各持一份等于两个管理员会话互相顶，双方都在替对方付
        重登的钱。实测（PC 直连设备，守护线程 3s / 界面线程 5s 的节奏）：

            各持一份：avg 4.49s / 5.21s，max 7.88s / 9.23s
            共用一份：avg 3.66s / 3.70s，max 4.48s / 6.05s   （单线程 2.66s）

        串行由 base.DeviceDriver.__init_subclass__ 自动套在会碰设备的方法上，
        所以「共用」和「不并发推进 nonce」同时成立。
        """
        with mock.patch.object(drivers.ZteRtaDriver, "identify_client",
                               side_effect=_probe_ok):
            mine = self._detect()
            other = []
            t = threading.Thread(target=lambda: other.append(self._detect()))
            t.start()
            t.join()
        self.assertIs(mine, other[0])

    def test_device_methods_are_serialized(self):
        """两个线程同时读状态，不能同时进驱动内部。

        这条防的是「只加了缓存、忘了加锁」：那样两个线程会共用同一个实例却仍
        并发推进 nonce，症状是偶发变慢，不报错，极难查。
        """
        import drivers.base as base

        inside, overlapped = [], []

        class Fake(base.DeviceDriver):
            name = "fake"

            def identify(self, *a, **kw):
                return True

            def get_state(self):
                if inside:
                    overlapped.append(1)
                inside.append(1)
                time.sleep(0.05)
                inside.pop()
                return {}

            def capabilities(self):
                return {}

            def set_band(self, band):
                pass

            def set_channel(self, ch):
                pass

        d = Fake()
        ts = [threading.Thread(target=d.get_state) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(overlapped, [])

    def test_shared_cache_expires_with_memo(self):
        """缓存过期后重建实例：换了一台设备（同 IP 不同固件）也能自愈。"""
        drivers.DETECT_MEMO_SEC = 0
        with mock.patch.object(drivers.ZteRtaDriver, "identify_client",
                               side_effect=_probe_ok):
            first = self._detect()
            time.sleep(0.01)
            second = self._detect()
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
