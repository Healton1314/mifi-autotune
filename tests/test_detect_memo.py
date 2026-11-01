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

    def test_other_thread_gets_its_own_instance(self):
        """跨线程绝不能共用：两个线程同时推进 nonce 计数，设备直接判会话失效。"""
        with mock.patch.object(drivers.ZteRtaDriver, "identify_client",
                               side_effect=_probe_ok):
            mine = self._detect()
            other = []
            t = threading.Thread(target=lambda: other.append(self._detect()))
            t.start()
            t.join()
        self.assertIsNot(mine, other[0])

    def test_thread_cache_expires_with_memo(self):
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
