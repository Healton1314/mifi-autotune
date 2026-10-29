"""信号判读：区分「覆盖不足」和「干扰主导」。

为什么要有这个：用户看到「本次不改动」时无法判断是工具测不准还是环境真没救。
实测 2026-10-29 同一位置隔一小时，RSRP 从 -118dBm 回到 -94dBm —— 前者换
任何频段都救不了，后者（SINR 0dB）恰恰是换频段最可能有效的场景。
两者的建议必须不一样，否则用户会一直在「换位置」和「换频段」之间乱试。
"""

import unittest

import autotune


class DiagnoseSignal(unittest.TestCase):
    def diag(self, rsrp, sinr=None):
        return autotune.diagnose_signal({"rsrp": rsrp, "sinr": sinr})

    def test_weak_coverage_says_move_the_device(self):
        d = self.diag("-118dBm", "2dB")          # 用户截图那一轮
        self.assertEqual(d["level"], "weak")
        self.assertIn("覆盖不足", d["text"])
        self.assertIn("挪到窗边", d["advice"])
        self.assertNotIn("换频段成功率高", d["advice"])   # 这种时候换频段没用

    def test_interference_dominated_encourages_switching(self):
        d = self.diag("-94dBm", "0dB")           # 同位置稍后，覆盖够但 SINR 0
        self.assertEqual(d["level"], "noisy")
        self.assertIn("干扰", d["text"])
        # 必须是有条件的说法：实跑那轮诊断说「换频段成功率高」，
        # 结论却写「本次不改动」，两句并排读起来就是自相矛盾。
        self.assertIn("如果下面给出了切换建议", d["advice"])
        self.assertNotIn("换频段救不了", d["advice"])

    def test_weak_rsrp_with_bad_sinr_says_weak_not_enough(self):
        """-103dBm 说成「覆盖够」与数值不符（实跑输出过）。"""
        d = self.diag("-103dBm", "-1dB")
        self.assertIn("覆盖偏弱", d["text"])
        self.assertNotIn("覆盖够", d["text"])

    def test_good_signal_manages_expectations(self):
        d = self.diag("-85dBm", "15dB")
        self.assertEqual(d["level"], "good")
        self.assertIn("收益有限", d["advice"])

    def test_middle_ground_is_neutral(self):
        d = self.diag("-105dBm", "6dB")
        self.assertEqual(d["level"], "ok")

    def test_no_service_values_return_none(self):
        """设备报 0 / 空 / -- 时代表没有服务，不能猜一个等级出来。"""
        for rsrp in ("0", "", None, "--", "0dBm"):
            self.assertIsNone(self.diag(rsrp, "5dB"), f"{rsrp!r} 应当判为不可解析")

    def test_rsrp_without_sinr_still_works(self):
        d = self.diag("-94dBm")
        self.assertEqual(d["level"], "good")


if __name__ == "__main__":
    unittest.main()
