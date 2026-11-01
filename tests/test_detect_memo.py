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

import time
import unittest
from unittest import mock

import drivers


class DetectMemo(unittest.TestCase):
    def setUp(self):
        drivers._DETECT_MEMO.clear()
        self._ttl = drivers.DETECT_MEMO_SEC

    def tearDown(self):
        drivers._DETECT_MEMO.clear()
        drivers.DETECT_MEMO_SEC = self._ttl

    def _detect(self, host="192.168.0.1"):
        return drivers.detect(host=host, user="admin", password="admin")

    def test_identify_runs_once_within_ttl(self):
        with mock.patch.object(drivers.ZteRtaDriver, "identify",
                               return_value=True) as m:
            self._detect()
            self._detect()
            self._detect()
        self.assertEqual(m.call_count, 1, "60 秒内不该反复探测")

    def test_other_host_is_probed_separately(self):
        with mock.patch.object(drivers.ZteRtaDriver, "identify",
                               return_value=True) as m:
            self._detect("192.168.0.1")
            self._detect("192.168.8.1")
        self.assertEqual(m.call_count, 2)

    def test_memo_expires(self):
        drivers.DETECT_MEMO_SEC = 0
        with mock.patch.object(drivers.ZteRtaDriver, "identify",
                               return_value=True) as m:
            self._detect()
            time.sleep(0.01)
            self._detect()
        self.assertEqual(m.call_count, 2, "过期后要重新探测，不能永久用错驱动")

    def test_returns_real_driver_instance(self):
        with mock.patch.object(drivers.ZteRtaDriver, "identify", return_value=True):
            dev = self._detect()
        self.assertEqual(dev.name, "zte_rta")
        # 必须是真的新实例：客户端带 Digest 的 nonce 计数，跨线程共用会乱
        self.assertIsNot(dev, self._detect())


if __name__ == "__main__":
    unittest.main()
