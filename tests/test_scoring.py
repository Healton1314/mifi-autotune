"""纯逻辑单元测试：不连设备、不扫 WiFi，任何平台都能跑。

CI 会在 Linux / Windows / macOS 上执行这些测试，所以这里不能依赖
netsh、curl 或真实网络。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scanner  # noqa: E402


class TestScoring(unittest.TestCase):
    def test_freq_mapping(self):
        self.assertEqual(scanner.freq(1), 2412)
        self.assertEqual(scanner.freq(6), 2437)
        self.assertEqual(scanner.freq(13), 2472)

    def test_empty_environment_ties(self):
        """没有任何邻居时，所有信道同分（都为 0），不应该出现"最差信道"。"""
        rows = scanner.score_channels([])
        self.assertEqual(len(rows), 13)
        self.assertTrue(all(r["score"] == 0 for r in rows))

    def test_avoids_occupied_channel(self):
        """邻居挤在 6 号时，1 号或 11 号这类不重叠信道应当胜出。"""
        aps = [{"ssid": "n1", "ch": 6, "sig": 90},
               {"ssid": "n2", "ch": 6, "sig": 80}]
        rows = scanner.score_channels(aps)
        best = rows[0]["ch"]
        self.assertNotIn(best, range(4, 9), f"不该选按 6 号重叠的信道 {best}")
        self.assertIn(best, (1, 11, 12, 13))

    def test_signal_weight_is_quadratic(self):
        """强信号的影响必须显著大于弱信号（平方加权）。"""
        strong = scanner.score_channels([{"ssid": "s", "ch": 6, "sig": 100}])
        weak = scanner.score_channels([{"ssid": "w", "ch": 6, "sig": 50}])
        s_strong = next(r["score"] for r in strong if r["ch"] == 6)
        s_weak = next(r["score"] for r in weak if r["ch"] == 6)
        self.assertAlmostEqual(s_strong / s_weak, 4.0, places=2)

    def test_overlap_width(self):
        """相距 10MHz（如 ch1 与 ch3）时重叠 10MHz；相距 25MHz 时不重叠。"""
        rows = scanner.score_channels([{"ssid": "a", "ch": 1, "sig": 100}])
        ch3 = next(r for r in rows if r["ch"] == 3)
        ch6 = next(r for r in rows if r["ch"] == 6)
        self.assertEqual(ch3["detail"][0][3], 10)
        self.assertEqual(ch6["score"], 0)

    def test_channel_subset(self):
        """只传部分可用信道时，结果也只覆盖这些信道。"""
        rows = scanner.score_channels([], channels=[1, 6, 11])
        self.assertEqual(sorted(r["ch"] for r in rows), [1, 6, 11])


class TestParsing(unittest.TestCase):
    def test_classify_multilingual(self):
        cases = {
            "信号": "signal", "Signal": "signal", "Signalstärke": "signal",
            "電波強度": "signal", "신호": "signal",
            "波段": "band", "Band": "band", "バンド": "band",
            "信道": "channel", "频道": "channel", "Channel": "channel",
            "Kanal": "channel", "チャンネル": "channel", "채널": "channel",
            "网络类型": None, "Network type": None, "认证": None,
        }
        for key, want in cases.items():
            self.assertEqual(scanner._classify(key), want, f"{key} 分类错误")

    def test_num(self):
        self.assertEqual(scanner._num(" 82%"), 82)
        self.assertEqual(scanner._num("2437 MHz"), 2437)
        self.assertIsNone(scanner._num(""))


if __name__ == "__main__":
    unittest.main()
