# -*- coding: utf-8 -*-
"""全自动守护的决策规则。

用户 2026-10-30 定的策略：
  · 在动 → 解锁（锁死一个频段，走到没覆盖的地方就断网）
  · 久坐 → 锁住当前驻留的那个（不重新测，几乎不断网；只为阻止设备自己乱跳）
  · 锁了之后不通 → 先解锁自救；解锁后仍不通 → 才跑体检换频段
    （后者才说明是位置本身没覆盖，不是锁错了）
"""

import unittest

import autotune as A


def decide(**kw):
    base = dict(moving=False, still_sec=0, settle_sec=600, locked=False,
                bad_streak=0, cooldown_left=0.0, outage_after_unlock=False)
    base.update(kw)
    return A.guard_decide(**base)


class GuardDecide(unittest.TestCase):
    def test_moving_with_lock_unlocks(self):
        action, why = decide(moving=True, locked=True)
        self.assertEqual(action, "unlock")
        self.assertIn("移动", why)

    def test_moving_without_lock_does_nothing(self):
        # 本来就没锁，设备自己会换，不用管
        self.assertIsNone(decide(moving=True, locked=False)[0])

    def test_settled_unlocked_locks(self):
        action, why = decide(still_sec=700, settle_sec=600, locked=False)
        self.assertEqual(action, "lock")
        self.assertIn("11 分钟", why)

    def test_not_settled_yet_does_nothing(self):
        self.assertIsNone(decide(still_sec=300, settle_sec=600)[0])

    def test_settled_but_already_locked_does_nothing(self):
        self.assertIsNone(decide(still_sec=999, settle_sec=600, locked=True)[0])

    def test_locked_and_dead_unlocks_first(self):
        # 锁定后连续不通：先解锁（最快自救），而不是直接跑去体检
        action, why = decide(locked=True, bad_streak=A.GUARD_BAD_HITS)
        self.assertEqual(action, "unlock")
        self.assertIn("解锁自救", why)

    def test_unlocked_and_still_dead_measures(self):
        # 已经解锁了还不通 → 不是频段问题，跑体检换一个
        action, why = decide(locked=False, bad_streak=A.GUARD_BAD_HITS,
                             outage_after_unlock=True)
        self.assertEqual(action, "measure")
        self.assertIn("不是频段问题", why)

    def test_unlocked_but_just_resumed_does_nothing(self):
        # 解锁很久之后才不通：可能只是偶发，不该立刻又去断网体检
        self.assertIsNone(decide(locked=False, bad_streak=A.GUARD_BAD_HITS,
                                 outage_after_unlock=False)[0])

    def test_cooldown_blocks_proactive_lock_only(self):
        # 冷却只该管「别反复切频段」这类主动动作，不该挡住自救。
        action, why = decide(still_sec=700, locked=False, cooldown_left=120)
        self.assertIsNone(action)
        self.assertIn("冷却", why)

    def test_cooldown_does_not_block_unlock(self):
        # 实机踩到：解锁后冷却 300s，「解锁后仍不通→体检」的窗口也正好 300s，
        # 两者互斥，自救链整条走不到。所以自救必须排在冷却前面。
        self.assertEqual(decide(locked=True, bad_streak=A.GUARD_BAD_HITS,
                                cooldown_left=200)[0], "unlock")
        self.assertEqual(decide(moving=True, locked=True, cooldown_left=200)[0], "unlock")
        self.assertEqual(decide(locked=False, bad_streak=A.GUARD_BAD_HITS,
                                outage_after_unlock=True, cooldown_left=200)[0],
                         "measure")

    def test_bad_streak_below_threshold_ignored(self):
        self.assertIsNone(decide(locked=True, bad_streak=A.GUARD_BAD_HITS - 1)[0])


class PciParsing(unittest.TestCase):
    def test_string_pci(self):
        self.assertEqual(A._pci_num("236"), 236)

    def test_missing_pci_is_none_not_zero(self):
        # 0 是合法 PCI（会撞上「PCI 变化」判定），必须用 None 表示「读不到」
        self.assertIsNone(A._pci_num(None))
        self.assertIsNone(A._pci_num(""))
        self.assertIsNone(A._pci_num("--"))

    def test_zero_pci_is_kept(self):
        self.assertEqual(A._pci_num("0"), 0)


if __name__ == "__main__":
    unittest.main()
