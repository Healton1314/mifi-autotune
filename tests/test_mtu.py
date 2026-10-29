"""路径 MTU 探测：二分边界与「测不出来就说测不出来」。

起因（2026-10-29 用户提出）：MTU 设得比路径 MTU 大时，症状是「网页能开、
大文件下载卡死、视频一直缓冲」——这种半通不通最容易被误判成信号差，
然后开始反复切频段，而切频段根本治不了。

这项只在电脑上做（手机没有 ICMP 权限），所以重点保证两件事：
  1. 二分结果等于「能整包通过的最大 payload + 28」；
  2. ICMP 被拦时返回 None 并说明，绝不能编一个数字出来。
"""

import unittest
from unittest import mock

import autotune
import probe


def _fake_ok(limit):
    """payload ≤ limit 才通。"""
    return lambda size, host, wait_ms=1500: size <= limit


class DetectPathMtu(unittest.TestCase):
    def test_standard_path_is_1500(self):
        with mock.patch.object(probe, "_ping_df_ok", _fake_ok(1472)):
            mtu, note = probe.detect_path_mtu()
        self.assertEqual(mtu, 1500)
        self.assertIn("标准值", note)

    def test_narrow_path_bisects_to_1428(self):
        # 路径 MTU 1428 → 最大 payload 1400（常见的隧道 / PPPoE 线路）
        with mock.patch.object(probe, "_ping_df_ok", _fake_ok(1400)):
            mtu, _note = probe.detect_path_mtu()
        self.assertEqual(mtu, 1428)

    def test_icmp_blocked_says_unknown(self):
        """连 1200 都不通时不能给数字：公共网络拦 ICMP 很常见。"""
        with mock.patch.object(probe, "_ping_df_ok", lambda *a, **k: False):
            mtu, note = probe.detect_path_mtu()
        self.assertIsNone(mtu)
        self.assertIn("测不出来", note)

    def test_termux_skips_with_reason(self):
        with mock.patch.object(probe, "is_termux", lambda: True), \
                mock.patch.object(probe, "_ping_df_ok", _fake_ok(1472)):
            mtu, note = probe.detect_path_mtu()
        self.assertIsNone(mtu)
        self.assertIn("电脑上测", note)


class MtuConclusion(unittest.TestCase):
    """结论只报告、不自动改（改 MTU 要重新拨号，断网 30~60 秒）。"""

    def _run(self, path_mtu, set_mtu):
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(probe, "is_termux", lambda: False), \
                mock.patch.object(probe, "detect_path_mtu",
                                  lambda host=None: (path_mtu, f"{path_mtu} 字节")), \
                contextlib.redirect_stdout(buf):
            autotune.run_mtu_check({"mtu": str(set_mtu)}, "223.5.5.5")
        return buf.getvalue()

    def test_matching_says_no_change(self):
        text = self._run(1500, 1500)
        self.assertIn("不用动", text)
        self.assertNotIn("大于路径 MTU", text)

    def test_smaller_path_warns_but_does_not_write(self):
        text = self._run(1428, 1500)
        self.assertIn("大于路径 MTU", text)
        self.assertIn("1428", text)
        self.assertIn("没有替你改", text)     # 只建议，不越权改网络层

    def test_unknown_path_stops(self):
        text = self._run(None, 1500)
        self.assertIn("测不出来", text)


if __name__ == "__main__":
    unittest.main()
