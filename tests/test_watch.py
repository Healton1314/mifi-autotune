"""守护模式（--watch）的触发逻辑测试。

judge_degradation 是纯函数，不碰网络、不碰设备，所以可以放心在 CI 里跑。
这部分逻辑的价值在于：判错了会导致两种坏结果——
  太敏感 → 频繁切频段，网络反复断；
  太迟钝 → 该切的时候不切，等于没守护。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import autotune  # noqa: E402


def st(avg=50, jitter=4, loss=0):
    return {"avg": avg, "jitter": jitter, "loss": loss}


class TestJudge(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(autotune.judge_degradation(st(), 50, 4), [])

    def test_no_network(self):
        """完全不通时直接判劣化，不能因为"没有基线"就放过。"""
        self.assertEqual(autotune.judge_degradation({"avg": None, "loss": 100}), ["无法上网"])

    def test_packet_loss(self):
        self.assertTrue(autotune.judge_degradation(st(loss=3), 50, 4))
        self.assertEqual(autotune.judge_degradation(st(loss=2), 50, 4), [])

    def test_cold_start_uses_absolute(self):
        """冷启动没有基线时用默认阈值 180ms：200ms 要触发，150ms 不触发。"""
        self.assertTrue(autotune.judge_degradation(st(avg=200), None, None))
        self.assertEqual(autotune.judge_degradation(st(avg=150), None, None), [])

    def test_relative_spike(self):
        """基线 50ms 时阈值是 max(50×1.8, 80)=90ms，所以 100ms 就该触发。"""
        self.assertTrue(autotune.judge_degradation(st(avg=100), 50, 4))

    def test_small_baseline_noise_ignored(self):
        """基线 50ms 时 58ms 属正常波动，不能触发（否则频繁断网）。"""
        self.assertEqual(autotune.judge_degradation(st(avg=58), 50, 4), [])

    def test_scale_with_slow_baseline(self):
        """基线 100ms 的链路，50% 的延时波动（150ms）不该判故障。"""
        self.assertEqual(autotune.judge_degradation(st(avg=150), 100, 4), [])
        self.assertTrue(autotune.judge_degradation(st(avg=200), 100, 4))

    def test_cap_prevents_never_firing(self):
        """上限作用：即便基线很高，阈值也不会无限放大（最多 260ms）。"""
        self.assertTrue(autotune.judge_degradation(st(avg=300), 200, 20))

    def test_jitter_spike(self):
        """抖动基线 4ms 时阈值 max(7.2, 25)=25ms，60ms 要触发。"""
        self.assertTrue(autotune.judge_degradation(st(jitter=60), 50, 4))

    def test_jitter_floor_with_bad_baseline(self):
        """基线抖动 30ms 时阈值 max(54, 25)=54ms：50ms 不触发，60ms 触发。"""
        self.assertEqual(autotune.judge_degradation(st(jitter=50), 50, 30), [])
        self.assertTrue(autotune.judge_degradation(st(jitter=60), 50, 30))

    def test_reason_text(self):
        reasons = autotune.judge_degradation(st(avg=300, loss=10), 50, 4)
        joined = " / ".join(reasons)
        self.assertIn("丢包", joined)
        self.assertIn("延迟", joined)


if __name__ == "__main__":
    unittest.main()
