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


class GuardMemoryFirst(unittest.TestCase):
    """久坐时先认「这个位置的历史最优」，别每次都跑 5 分钟体检。

    用户体感（2026-10-30）：手动测一次锁上比设备自动好，但守护每次久坐都
    重测、断网几次，用起来反而像厂商的自动 —— 因为它重新决策，而不用已知答案。
    """

    def setUp(self):
        import json
        import tempfile
        from datetime import datetime, timedelta
        from pathlib import Path
        from unittest import mock
        self.json, self.tmpdir, self.dt = json, tempfile, datetime
        self.SITES = Path(tempfile.mkdtemp()) / "sites.json"
        self._p = mock.patch.object(A, "SITES", self.SITES)
        self._p.start()
        self.ago = lambda **kw: (datetime.now() - timedelta(**kw)).isoformat()

    def tearDown(self):
        self._p.stop()

    def _write(self, rec):
        self.SITES.write_text(self.json.dumps({"236-CMCC": rec}), encoding="utf-8")

    def _st(self, pci="236", op="CMCC"):
        return {"signal": {"pci": pci, "operator": op}}

    def test_uses_recent_memory(self):
        self._write({"seen": 4, "last": self.ago(hours=2), "best": 40,
                     "bands": {"40": {"dead": 0, "ok": 4, "best": 43.0}}})
        band, why = A.guard_memory_band(self._st())
        self.assertEqual(band, 40)
        self.assertIn("最优 BAND 40", why)

    def test_no_record_means_measure(self):
        band, why = A.guard_memory_band(self._st())
        self.assertIsNone(band)
        self.assertIn("没测过", why)

    def test_stale_memory_means_measure(self):
        self._write({"seen": 9, "last": self.ago(days=9), "best": 40,
                     "bands": {"40": {"dead": 0, "ok": 9, "best": 43.0}}})
        band, why = A.guard_memory_band(self._st())
        self.assertIsNone(band)
        self.assertIn("过期", why)

    def test_flappy_best_means_measure(self):
        # 历史最优会周期性掉包 → 不能直接锁它，宁可重测
        self._write({"seen": 4, "last": self.ago(hours=1), "best": 40,
                     "bands": {"40": {"dead": 0, "ok": 4, "best": 43.0,
                                      "samples": [[12, 40, 60, 90], [12, 40, 5, 70],
                                                  [12, 40, 45, 88]]}}})
        band, why = A.guard_memory_band(self._st())
        self.assertIsNone(band)
        self.assertIn("掉包", why)

    def test_dead_best_means_measure(self):
        self._write({"seen": 4, "last": self.ago(hours=1), "best": 38,
                     "bands": {"38": {"dead": 6, "ok": 0}}})
        band, why = A.guard_memory_band(self._st())
        self.assertIsNone(band)
        self.assertIn("没覆盖", why)

    def test_no_pci_means_measure(self):
        band, why = A.guard_memory_band({"signal": {}})
        self.assertIsNone(band)
        self.assertIn("PCI", why)


class QuickProbe(unittest.TestCase):
    """停在半路先「只看不锁」—— 用户 2026-10-30：停下来的地方很可能只是路过，
    这时候锁死频段，后面走到没覆盖的地方就是断网。"""

    def test_good_enough(self):
        self.assertTrue(A._quick_ok({"mbps": 20, "loss": 0, "avg": 60}))

    def test_too_slow(self):
        self.assertFalse(A._quick_ok({"mbps": A.GUARD_QUICK_MIN_MBPS - 1,
                                      "loss": 0, "avg": 60}))

    def test_lossy(self):
        self.assertFalse(A._quick_ok({"mbps": 40, "loss": A.GUARD_QUICK_MAX_LOSS,
                                      "avg": 60}))

    def test_slow_ping(self):
        self.assertFalse(A._quick_ok({"mbps": 40, "loss": 0,
                                      "avg": A.GUARD_QUICK_MAX_MS + 1}))

    def test_missing_numbers_are_not_ok(self):
        self.assertFalse(A._quick_ok({"mbps": 0, "loss": None, "avg": None}))
        self.assertFalse(A._quick_ok(None))


class AltBand(unittest.TestCase):
    """锁定的频段不行时，先换记忆里的第二条，别把决定权交还给设备。"""

    def setUp(self):
        import json
        import tempfile
        from datetime import datetime, timedelta
        from pathlib import Path
        from unittest import mock
        self.json = json
        self.SITES = Path(tempfile.mkdtemp()) / "sites.json"
        self._p = mock.patch.object(A, "SITES", self.SITES)
        self._p.start()
        self._ago = lambda **kw: (datetime.now() - timedelta(**kw)).isoformat()

    def tearDown(self):
        self._p.stop()

    def _write(self, bands, **extra):
        rec = {"seen": 5, "last": self._ago(hours=1), "best": 3, "bands": bands}
        rec.update(extra)
        self.SITES.write_text(self.json.dumps({"236-CMCC": rec}), encoding="utf-8")

    def _st(self):
        return {"signal": {"pci": "236", "operator": "CMCC"}}

    def test_picks_fastest_remaining(self):
        self._write({"3": {"dead": 0, "ok": 5, "best": 40.0},
                     "40": {"dead": 0, "ok": 5, "best": 42.0},
                     "8": {"dead": 0, "ok": 5, "best": 9.0}})
        self.assertEqual(A.guard_alt_band(self._st(), skip=3), 40)

    def test_skips_dead_and_flappy(self):
        self._write({"3": {"dead": 0, "ok": 5, "best": 40.0},
                     "40": {"dead": 6, "ok": 0},                       # 没覆盖
                     "8": {"dead": 0, "ok": 5, "best": 30.0,
                           "samples": [[12, 30, 60, 90], [12, 30, 55, 88]]},   # 会掉包
                     "39": {"dead": 0, "ok": 5, "best": 12.6}})
        self.assertEqual(A.guard_alt_band(self._st(), skip=3), 39)

    def test_no_memory_no_alt(self):
        self.assertIsNone(A.guard_alt_band(self._st(), skip=3))

    def test_provisional_record_is_not_trusted(self):
        # 只做过快速判断的位置，不该拿它的 best 去换
        self._write({"40": {"dead": 0, "ok": 5, "best": 42.0}}, provisional=True)
        self.assertIsNone(A.guard_alt_band(self._st(), skip=3))


if __name__ == "__main__":
    unittest.main()
