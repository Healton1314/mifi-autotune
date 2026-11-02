# -*- coding: utf-8 -*-
"""全自动守护的决策规则。

实测中 定的策略：
  · 在动 → 解锁（锁死一个频段，走到没覆盖的地方就断网）
  · 久坐 → 锁住当前驻留的那个（不重新测，几乎不断网；只为阻止设备自己乱跳）
  · 锁了之后不通 → 先解锁自救；解锁后仍不通 → 才跑体检换频段
    （后者才说明是位置本身没覆盖，不是锁错了）
"""

import unittest
from unittest import mock

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

    def test_collapsed_signal_with_lock_unlocks(self):
        """锁着的这条信号塌了 → 解锁。

        原先只有「人动」和「连续不通」会解锁，「人走开了、信号掉了、链路还没
        垮」正好卡在中间，能锁一整天（2026-11-01 中午）。
        """
        action, why = decide(locked=True, signal_collapsed=True)
        self.assertEqual(action, "unlock")
        self.assertEqual(why, A.UNLOCK_SIGNAL_COLLAPSE)

    def test_collapsed_signal_without_lock_does_nothing(self):
        # 没锁着就不关它的事，设备自己会换
        self.assertIsNone(decide(signal_collapsed=True)[0])

    def test_moving_still_wins_over_collapse(self):
        # 两条都成立时先说「移动」—— 那是更根本的原因
        action, why = decide(locked=True, moving=True, signal_collapsed=True)
        self.assertEqual(action, "unlock")
        self.assertIn("移动", why)

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

    def test_flappy_best_falls_back_to_stable_one(self):
        """最优会掉包、但记忆里还有一条稳的 → 用那条，别跑 5 分钟体检。

        实机 2026-10-31 21:36（住处）：记忆早知道 BAND 40 会周期性掉包、
        BAND 3 不会，守护还是选择「跑一次完整体检」—— 断网几次、耗 5 分钟，
        只为重新发现一个已经写在记忆里的结论。
        """
        self._write({"seen": 6, "last": self.ago(hours=1), "best": 40,
                     "bands": {"40": {"dead": 0, "ok": 4, "best": 43.0,
                                      "samples": [[12, 40, 60, 90], [12, 40, 5, 70],
                                                  [12, 40, 45, 88]]},
                               "3": {"dead": 0, "ok": 3, "best": 23.0}}})
        band, why = A.guard_memory_band(self._st())
        self.assertEqual(band, 3)
        self.assertIn("BAND 40 会周期性掉包", why)
        self.assertIn("BAND 3", why)

    def test_flappy_best_and_no_stable_alt_still_means_measure(self):
        """记忆里只有那一条会掉包的 → 没得选，老老实实重测。"""
        self._write({"seen": 4, "last": self.ago(hours=1), "best": 40,
                     "bands": {"40": {"dead": 0, "ok": 4, "best": 43.0,
                                      "samples": [[12, 40, 60, 90], [12, 40, 5, 70],
                                                  [12, 40, 45, 88]]},
                               "3": {"dead": 5, "ok": 0}}})
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

    def test_provisional_without_data_means_measure(self):
        # 第一次来只锁了一下、没测过 —— 下次来该补测
        self._write({"seen": 0, "provisional": True, "best": 40,
                     "last": self.ago(hours=1), "bands": {}})
        band, why = A.guard_memory_band(self._st())
        self.assertIsNone(band)
        self.assertIn("快速判断", why)

    def test_old_provisional_flag_with_real_data_is_ignored(self):
        # 实机 2026-10-30 19:44：0.6.7 之前的 provisional 只写不清，
        # 236-CMCC 18:14 已实测四条频段，标记却还挂着 → 守护当成「没测过」，
        # 永远走「快测当前、直接锁」，记忆和信号闸门一次都没跑到。
        self._write({"seen": 1, "provisional": True, "best": 40,
                     "last": self.ago(hours=1),
                     "bands": {"40": {"dead": 0, "ok": 1, "best": 17.8},
                               "3": {"dead": 0, "ok": 1, "best": 15.5}}})
        band, why = A.guard_memory_band(self._st())
        self.assertEqual(band, 40)
        self.assertIn("最优 BAND 40", why)


class QuickProbe(unittest.TestCase):
    """停在半路先「只看不锁」—— 实测中：停下来的地方很可能只是路过，
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


class PickLockBand(unittest.TestCase):
    """久坐锁定前：记忆里的历史最优 vs 刚才快测的当前速度，锁哪一条。

    真实数据（2026-10-30 住处）：记忆说 BAND 3 是 44.6，但那是上午、在另一个
    PCI（350）下测的；当时实测当前 BAND 40 是 32 Mbps、延迟 65ms、丢包 0%。
    差 28% 就切过去，等于拿「别的扇区、别的时段」的数字换掉刚量到的真实值，
    还要断几秒 —— 不值得。差得明显（比如 20 对 44.6）才切。
    """

    def _q(self, band=40, mbps=32.0, ok=True):
        return {"band": band, "ok": ok, "mbps": mbps, "avg": 65, "loss": 0}

    # 这几条都假设「记忆里记过信号」，所以统一给一对差不多的 RSRP，
    # 让判据落在「速度」而不是信号闸门上。信号闸门另有 LockSignalGate 专门测。

    def test_close_enough_keeps_current(self):
        # 2026-10-31 改判：比例从 0.7 提到 1.5。记忆里 44.6、当前 32（差 28%）
        # 已不再算「差不多」—— 熟悉的地方要锁记忆最优，20 秒快测没资格推翻体检。
        band, kind = A.guard_pick_lock(memo=3, memo_mbps=44.6, serving=40,
                                       quick=self._q(40, 32.0),
                                       memo_rsrp=-80, cur_rsrp=-78)
        self.assertEqual((band, kind), (3, "memo"))

    def test_much_faster_current_keeps_current(self):
        # 当前真的快得多（40 对记忆的 20，翻倍）→ 才值得留在当前
        band, kind = A.guard_pick_lock(memo=3, memo_mbps=20.0, serving=40,
                                       quick=self._q(40, 40.0),
                                       memo_rsrp=-80, cur_rsrp=-78)
        self.assertEqual((band, kind), (40, "keep"))

    def test_pinned_beats_signal_gate(self):
        # 2026-10-31 16:13 实机：BAND 3 = -103dBm、BAND 40 = -82dBm，差 21dB，
        # 但 BAND 40 实测丢包 80%、BAND 3 延迟 55ms 丢包 0%。用户用 --lock-band 3
        # 明确指定过 —— 信号闸门不能再把他踢回烂链路。
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=21.4, serving=40, quick=self._q(40, 30.0),
            memo_rsrp=-103, cur_rsrp=-82, pinned_band=3)
        self.assertEqual((band, kind), (3, "pinned"))

    def test_pinned_while_already_there(self):
        # 已经驻留在指定频段上：锁住就行，别切来切去
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=21.4, serving=3, quick=self._q(3, 20.0),
            memo_rsrp=-103, cur_rsrp=-103, pinned_band=3)
        self.assertEqual((band, kind), (3, "pinned"))

    def test_much_slower_current_switches_to_memory(self):
        # 当前只有 20，记忆里 44.6 —— 这个差距值得断几秒换过去
        band, kind = A.guard_pick_lock(memo=3, memo_mbps=44.6, serving=40,
                                       quick=self._q(40, 20.0),
                                       memo_rsrp=-78, cur_rsrp=-80)
        self.assertEqual((band, kind), (3, "memo"))

    def test_memory_without_quick_still_used(self):
        # 快测没做（停的时间不够）→ 只能听记忆的
        band, kind = A.guard_pick_lock(memo=3, memo_mbps=44.6, serving=40,
                                       quick=None, memo_rsrp=-78, cur_rsrp=-80)
        self.assertEqual((band, kind), (3, "memo"))

    def test_quick_measuring_other_band_is_ignored(self):
        # 快测的是 BAND 8，人后来跳到 40 了 —— 那个数字不能拿来给 40 作证
        band, kind = A.guard_pick_lock(memo=3, memo_mbps=44.6, serving=40,
                                       quick=self._q(8, 50.0),
                                       memo_rsrp=-78, cur_rsrp=-80)
        self.assertEqual((band, kind), (3, "memo"))

    def test_no_memory_quick_enough_locks_current(self):
        band, kind = A.guard_pick_lock(memo=None, memo_mbps=0.0, serving=40,
                                       quick=self._q(40, 32.0))
        self.assertEqual((band, kind), (40, "quick"))

    def test_no_memory_quick_not_enough_means_measure(self):
        band, kind = A.guard_pick_lock(memo=None, memo_mbps=0.0, serving=40,
                                       quick=self._q(40, 8.0, ok=False))
        self.assertIsNone(band)
        self.assertIsNone(kind)

    def test_same_band_as_memory_is_not_a_switch(self):
        # 当前就是记忆里那条，不该说「不切换」绕一圈，直接按记忆算
        band, kind = A.guard_pick_lock(memo=40, memo_mbps=44.6, serving=40,
                                       quick=self._q(40, 44.0))
        self.assertEqual((band, kind), (40, "memo"))

    # ---- 当前这条正在大量丢包时，「留在当前」这个选项必须作废 ----
    # 2026-10-31 17:34:21 实机：BAND 40 丢包 100%，快测那个数字是几分钟前量的，
    # 于是判「当前够用」→ 17:45 锁回这条正在掉包的频段。

    def test_losing_current_band_is_not_kept(self):
        # 快测说 40 有 32 Mbps（本来够格「留在当前」），但此刻正在丢 100% 的包
        # → 不能算够用，改走记忆里的最优
        band, kind = A.guard_pick_lock(memo=3, memo_mbps=20.0, serving=40,
                                       quick=self._q(40, 32.0),
                                       memo_rsrp=-80, cur_rsrp=-78, cur_loss=100)
        self.assertEqual((band, kind), (3, "memo"))

    def test_losing_current_without_memory_means_measure(self):
        # 记忆没记信号（blind）+ 当前在丢包 → 该老实体检，不是「先不赌、锁当前」
        band, kind = A.guard_pick_lock(memo=3, memo_mbps=23.0, serving=40,
                                       quick=self._q(40, 32.0),
                                       memo_rsrp=None, cur_rsrp=-78, cur_loss=100)
        self.assertIsNone(band)
        self.assertEqual(kind, "blind")

    def test_clean_current_band_is_still_kept(self):
        # 对照组：同样条件、丢包 0% → 照旧留在当前，别把正常情况也推翻
        band, kind = A.guard_pick_lock(memo=3, memo_mbps=20.0, serving=40,
                                       quick=self._q(40, 32.0),
                                       memo_rsrp=-80, cur_rsrp=-78, cur_loss=0)
        self.assertEqual((band, kind), (40, "keep"))

    def test_pin_beats_lossy_current(self):
        # 人指定过的优先级最高：当前在丢包也不改结论
        band, kind = A.guard_pick_lock(memo=40, memo_mbps=44.6, serving=40,
                                       quick=self._q(40, 32.0),
                                       memo_rsrp=-78, cur_rsrp=-78,
                                       pinned_band=3, cur_loss=100)
        self.assertEqual((band, kind), (3, "pinned"))


class MemoIsTheServingBand(unittest.TestCase):
    """记忆里的「最优」正好就是当前驻留的这条 —— 最容易被拥塞咬到的情况。

    2026-10-31 20:13 实机（住处，解锁后设备自己驻留在 BAND 40）：
      · 信号 -78dBm / SINR 21dB，比 BAND 3 强 20dB，指标漂亮；
      · 快测只有 3 Mbps、延迟 84ms，当场判「不够用」；
      · 记忆里 best 也正好是 40（两小时前测的），
      · 20:14:38 的决策是「原地待了 3 分钟，锁住当前频段防止它自己乱跳」——
        把刚量出 3 Mbps 的那条锁死了，而且整晚都没有再回头看。
    原来所有「够不够用」的判据都挂在 `serving != memo` 上，一旦两者相等就
    只剩一句「记忆说它最优」，眼前刚量到的数字完全不算数。
    """

    def _q(self, band=40, mbps=3.0, grade="poor"):
        return {"band": band, "ok": grade != "poor", "mbps": mbps,
                "avg": 84, "loss": 0, "grade": grade}

    def test_measured_bad_switches_to_alt(self):
        band, kind = A.guard_pick_lock(
            memo=40, memo_mbps=40.7, serving=40, quick=self._q(40, 3.0),
            memo_rsrp=-78, cur_rsrp=-78, alt_band=3)
        self.assertEqual((band, kind), (3, "memo_bad_alt"))

    def test_measured_bad_without_alt_means_exam(self):
        band, kind = A.guard_pick_lock(
            memo=40, memo_mbps=40.7, serving=40, quick=self._q(40, 3.0),
            memo_rsrp=-78, cur_rsrp=-78)
        self.assertEqual((band, kind), (None, "memo_bad"))

    def test_measured_ok_keeps_the_memo_band(self):
        # 当前就是记忆最优、而且刚量出来确实够用 → 照旧锁它，别乱动
        band, kind = A.guard_pick_lock(
            memo=40, memo_mbps=40.7, serving=40,
            quick=self._q(40, 30.0, grade="good"),
            memo_rsrp=-78, cur_rsrp=-78, alt_band=3)
        self.assertEqual((band, kind), (40, "memo"))

    def test_no_quick_yet_keeps_the_memo_band(self):
        band, kind = A.guard_pick_lock(
            memo=40, memo_mbps=40.7, serving=40, quick=None,
            memo_rsrp=-78, cur_rsrp=-78, alt_band=3)
        self.assertEqual((band, kind), (40, "memo"))

    def test_pin_still_wins_over_memory(self):
        """pin 和「记忆说的最优」冲突时，pin 赢 —— 这条没变。

        记忆是程序自己攒的；pin 是人亲手测过、比过之后留下的。
        """
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=44.6, serving=40, quick=self._q(40, 32.0, "good"),
            memo_rsrp=-78, cur_rsrp=-78, alt_band=3, pinned_band=40)
        self.assertEqual((band, kind), (40, "pinned"))

    def test_pin_holds_even_when_that_band_is_slow(self):
        """pin 那条只有 3 Mbps，但链路干净 → 照样听人的。

        这一条是实测中 亲口纠正的。他设 BAND 3 的原因就是这个：
        「住处的 BAND 40 网速和信号有时候检测会比 BAND 3 好，就会导致直接锁
        BAND 40，但 BAND 40 丢包就会用起来一会一卡，就设置在住处优先锁 BAND 3」。

        速度慢是他**明知且接受**的代价。拿「不够快」去推翻这条 pin，等于把他
        做这个决定的理由当没听见。
        """
        band, kind = A.guard_pick_lock(
            memo=40, memo_mbps=40.7, serving=40, quick=self._q(40, 3.0),
            memo_rsrp=-78, cur_rsrp=-78, pinned_band=40)
        self.assertEqual((band, kind), (40, "pinned"))

    def test_pin_yields_when_link_is_dropping_packets(self):
        """但「正在丢一半以上的包」不算 —— 那是它自己也做不到「稳」。

        用户要的是「慢一点但稳」，不是「又慢又掉」。
        """
        band, kind = A.guard_pick_lock(
            memo=40, memo_mbps=40.7, serving=40, quick=self._q(40, 30.0, "good"),
            memo_rsrp=-78, cur_rsrp=-78, alt_band=8, pinned_band=40,
            cur_loss=80)
        self.assertEqual((band, kind), (8, "memo_bad_alt"))

    def test_pin_is_kept_when_we_are_not_on_it(self):
        """pin 的不是当前这条 → 无法当场证伪，照办（换过去再看）。

        此时手里的快测测的是 serving，对 pin 那条什么也没说。
        """
        band, kind = A.guard_pick_lock(
            memo=40, memo_mbps=40.7, serving=40, quick=self._q(40, 3.0),
            memo_rsrp=-78, cur_rsrp=-78, alt_band=8, pinned_band=3)
        self.assertEqual((band, kind), (3, "pinned"))


class PlanLockReadsAltFromMemory(unittest.TestCase):
    """`guard_plan_lock` 要自己去记忆里挑备选，而不是让调用方猜。"""

    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        from datetime import datetime
        self.tmp = Path(tempfile.mkdtemp()) / "sites.json"
        self.tmp.write_text(json.dumps({"236-CMCC": {
            "seen": 3, "last": datetime.now().isoformat(timespec="seconds"),
            "best": 40, "name": "住处",
            "bands": {"40": {"ok": 2, "best": 40.7, "dead": 0, "rsrp": -78},
                      "3": {"ok": 3, "best": 23.0, "dead": 0, "rsrp": -100}},
        }}, ensure_ascii=False), encoding="utf-8")
        self._p = mock.patch.object(A, "SITES", self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_picks_the_second_band_when_the_best_one_is_bad_now(self):
        st = {"band": 40, "signal": {"pci": "236", "operator": "CMCC", "rsrp": "-78"}}
        key, band, kind, memo_best = A.guard_plan_lock(
            st, memo=40, serving=40,
            quick={"band": 40, "ok": False, "mbps": 3.0, "avg": 84,
                   "loss": 0, "grade": "poor"},
            cur_rsrp=-78, cur_loss=0, pinned=None, memo_rsrp=-78)
        self.assertEqual(key, "236-CMCC")
        self.assertEqual((band, kind), (3, "memo_bad_alt"))
        self.assertEqual(memo_best, 40.7)


class MemoSpeed(unittest.TestCase):
    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        self.SITES = Path(tempfile.mkdtemp()) / "sites.json"
        self._p = mock.patch.object(A, "SITES", self.SITES)
        self._p.start()
        self.json = json

    def tearDown(self):
        self._p.stop()

    def test_reads_band_best(self):
        self.SITES.write_text(self.json.dumps(
            {"236-CMCC": {"seen": 2, "last": "2026-10-30T17:00:00",
                          "best": 3, "bands": {"3": {"ok": 2, "best": 44.6}}}}),
            encoding="utf-8")
        self.assertAlmostEqual(A.guard_memo_speed("236-CMCC", 3), 44.6)

    def test_missing_band_is_zero(self):
        self.assertAlmostEqual(A.guard_memo_speed("236-CMCC", 3), 0.0)
        self.assertAlmostEqual(A.guard_memo_speed("", 3), 0.0)
        self.assertAlmostEqual(A.guard_memo_speed("236-CMCC", None), 0.0)


class MovingDetection(unittest.TestCase):
    """PCI 变 ≠ 人在动。实机 2026-10-30 17:43：坐在住处里，锁着 BAND 41，
    PCI 自己从 477 跳到 252（-89 → -105dBm），延迟 57ms、丢包 0%。
    原来这个会被判「检测到移动」并解锁，还顺手把静止计时清零。"""

    def _move(self, **kw):
        base = dict(churn=False, pci_changed=False, bad_now=False)
        base.update(kw)
        return A.guard_is_moving(**base)

    def test_weak_signal_reselection_is_not_moving(self):
        # 静止 + 换小区 + 信号掉了，但链路还通 → 设备自己重选，人没动
        self.assertFalse(self._move(pci_changed=True, bad_now=False))

    def test_reselection_with_outage_is_moving(self):
        # 换了小区、同时链路垮了 → 走出覆盖了
        self.assertTrue(self._move(pci_changed=True, bad_now=True))

    def test_many_changes_alone_is_not_moving(self):
        # 2026-10-31 改判：光「换了几次小区」不再算移动。实机在住处坐了 144 分钟，
        # 设备自己在两个扇区之间重选 2 次就判了「移动中」，解锁白等 10 分钟；
        # 那一刻丢包 0%、延迟 110ms。链路没垮就不是位置变了。
        self.assertFalse(self._move(churn=True))

    def test_many_changes_with_bad_link_is_moving(self):
        # 真的走出覆盖：换小区 + 链路垮，两件事同时发生才算走
        self.assertTrue(self._move(churn=True, pci_changed=True, bad_now=True))

    def test_link_bad_without_pci_change_is_not_moving(self):
        # 基站自己抽风（PCI 没变）不该解锁 —— 那不是位置问题
        self.assertFalse(self._move(bad_now=True))

    def test_still_here(self):
        self.assertFalse(self._move())

    def test_band_switch_is_not_moving(self):
        """实机 2026-10-30 21:46（手机放桌上没动）：BAND 3→40、PCI 350→236、
        丢包 100%。换频段会让射频重启、WiFi 重连，PCI 变和链路垮同时成立，
        旧判据必然判成「移动中」——其实人一直坐着，信号还从 -98 变好到 -76。"""
        self.assertFalse(self._move(pci_changed=True, bad_now=True,
                                    band_changed=True))

    def test_band_switch_is_not_moving_even_with_churn(self):
        """设备自己换频段时 PCI 必变、churn 必累积，人却坐着。

        2026-10-31 实机：BAND 40→3 那一拍 churn 成立，被判「检测到移动」→
        解锁归位回 BAND 40。band_changed 现在优先于 churn。
        """
        self.assertFalse(self._move(churn=True, pci_changed=True, bad_now=True,
                                    band_changed=True))

    def test_three_cells_in_window_is_moving(self):
        """换够 3 个小区就算在动，不再要求链路同时垮。

        两地挨得近时（餐厅离住处两百米）对方就是这么漏的：5 分钟里换了 3 个小区，
        可每个新小区信号都不算差、ping 也没丢，于是每一拍都判「人没动」，
        带着原来那条 BAND 3 的锁在新地方坐了一中午。
        """
        self.assertTrue(self._move(churn=True, pci_changed=True, churn_n=3))

    def test_two_cells_is_still_not_moving(self):
        # 2 次还是放行：住处坐着不动，设备自己在两个扇区之间重选也能凑到 2 次
        self.assertFalse(self._move(churn=True, pci_changed=True, churn_n=2))

    def test_two_cells_when_locked_is_moving(self):
        """锁着频段的时候，2 次就够。

        设备被锁住之后换不了频段，剩下能变的只有「同频段内换小区」—— 这种情况
        要么人挪了位置，要么信号掉到边缘触发重选。

        2026-11-02 上午的漏判就是这样：08:22 锁 BAND 3，一路上换了两个小区、
        链路一直还行（延迟 72~85ms、丢包 0%），三个判据全不成立，人走了 57 分钟
        没被发现，屏幕一直写「监控中」，09:19 才解开。
        """
        self.assertTrue(self._move(churn=True, pci_changed=True, churn_n=2,
                                   locked=True))

    def test_one_cell_when_locked_is_not_moving(self):
        # 只跳了一次不认：单次重选在静止时很常见
        self.assertFalse(self._move(churn=False, pci_changed=True, churn_n=1,
                                    locked=True))

    def test_falling_signal_when_locked_is_moving(self):
        """换着小区、信号还一路往下 —— 走出覆盖的形状，链路没垮也认。

        走出一栋楼时对面小区照样能给你 -100dBm、延迟 80ms，「PCI 变了 + 链路垮」
        永远等不到，于是人明明在走，守护一路「监控中」。
        """
        self.assertTrue(self._move(churn=False, pci_changed=True, churn_n=1,
                                   locked=True, rsrp_falling=True))

    def test_falling_signal_alone_is_not_moving(self):
        # 没有换小区、只是信号在掉 → 可能是基站侧的事，不动
        self.assertFalse(self._move(churn=False, pci_changed=False,
                                    rsrp_falling=True, locked=True))


class FallingSignal(unittest.TestCase):
    """RSRP 是「一路走低」还是「跳一下」，形状不一样。"""

    def test_monotonic_drop_is_falling(self):
        self.assertTrue(A.rsrp_is_falling([-78, -85, -92]))

    def test_big_drop_over_more_samples(self):
        self.assertTrue(A.rsrp_is_falling([-61, -70, -80, -95]))

    def test_reselection_plateau_is_not_falling(self):
        # 设备重选到弱小区：-89 → -105 之后稳在 -105，不是一路走低
        self.assertFalse(A.rsrp_is_falling([-89, -105, -105]))

    def test_small_drop_is_not_falling(self):
        # 累积只掉 4dB，抖动范围内
        self.assertFalse(A.rsrp_is_falling([-80, -82, -84]))

    def test_rising_is_not_falling(self):
        self.assertFalse(A.rsrp_is_falling([-95, -85, -78]))

    def test_not_enough_samples(self):
        self.assertFalse(A.rsrp_is_falling([-78, -92]))
        self.assertFalse(A.rsrp_is_falling([]))

    def test_none_entries_are_skipped(self):
        self.assertTrue(A.rsrp_is_falling([-78, None, -85, -92]))


class StillReset(unittest.TestCase):
    """「坐满 10 分钟才锁」不能被移动判定的尾巴拖长。

    实机 2026-10-30：19:30 人已经坐下，PCI 从那时起一直是 236，但 moving 在
    最后一次换小区之后还要保持 300s，于是每一拍都把计时起点推到当下，
    19:34:55 才显示「静止 0/10」，19:44:29 才锁上 —— 拖了 4 分半。
    """

    def test_no_reset_while_only_the_window_lags(self):
        # moving 为真、但这一拍 PCI 没变 → 不能清零（链路垮了也一样）
        self.assertFalse(A.guard_still_reset(pci_changed=False, moving=True))

    def test_reset_on_real_change(self):
        self.assertTrue(A.guard_still_reset(pci_changed=True, moving=True))

    def test_link_death_does_not_reset(self):
        """链路垮了不清零（连带把「安静的一拍不清零」一起覆盖了）。

        2026-10-31 17:39 实测：锁着 BAND 40，40 丢包 100%，第一拍就把
        已经攒够的静止时间清零回「0/10」，而人还坐在原处。丢包是链路的事，
        该走「换备选 / 解锁自救」，不该把「我在这儿坐够了」这个结论作废。
        """
        self.assertFalse(A.guard_still_reset(pci_changed=False, moving=False))

    def test_band_switch_does_not_reset(self):
        # 设备自己换频段：PCI 变了、链路也断了，但人坐在原处，计时接着数。
        # 实机 21:46 这一下白等了 2 分钟；设备要是爱换，就永远坐不满 10 分钟。
        self.assertFalse(A.guard_still_reset(pci_changed=True, moving=False,
                                            band_changed=True))

    def test_weak_evidence_does_not_reset(self):
        """放宽后的「移动」不能顺带清零。

        实测中 的原话：「不想它识别不出我在移动，但也不想它一直判我在动、
        坐不到十分钟一直不锁」。两件事的代价不对称：多解锁一次只是几十秒，清零
        却要人从头再坐十分钟。所以解锁用宽判据，清零只认严格那套。
        """
        self.assertFalse(A.guard_still_reset(pci_changed=True, moving=True,
                                             strong_moving=False))

    def test_strong_evidence_still_resets(self):
        # 真在走（严格判据也成立）→ 照常清零
        self.assertTrue(A.guard_still_reset(pci_changed=True, moving=True,
                                            strong_moving=True))

    def test_omitting_strong_falls_back_to_moving(self):
        # 不传就是旧行为，别把别处的调用悄悄改掉
        self.assertTrue(A.guard_still_reset(pci_changed=True, moving=True))

    def test_ten_minutes_means_ten_minutes(self):
        # 每秒一拍地模拟：第 0 秒换过小区，之后 moving 还要挂 300 秒。
        # 旧逻辑要 14.5 分钟才判「坐够」，新逻辑应当是 10 分钟。
        t, last_change, stable_since = 0, 0, 0
        window, settle = 300, 600
        lock_at = None
        while t <= 1200:
            moving = (t - last_change) < window          # 尾巴
            pci_changed = False
            if A.guard_still_reset(pci_changed=pci_changed, moving=moving):
                stable_since = t
            if t - stable_since >= settle and lock_at is None:
                lock_at = t
            t += 1
        self.assertEqual(lock_at, settle)
        self.assertGreater(settle + window, 600)         # 旧行为会等到这儿之后


class QuickGrade(unittest.TestCase):
    """快测分档：8 Mbps 以下才算「不够用」，两次差 3 倍算「测不准」。"""

    def _g(self, **kw):
        base = {"mbps": 20, "loss": 0, "avg": 60}
        base.update(kw)
        return A._quick_grade(base)

    def test_grades(self):
        self.assertEqual(self._g(mbps=A.GUARD_QUICK_GOOD_MBPS + 1), "good")
        self.assertEqual(self._g(mbps=A.GUARD_QUICK_MIN_MBPS), "fair")
        self.assertEqual(self._g(mbps=A.GUARD_QUICK_MIN_MBPS - 1), "poor")

    def test_noisy_is_unknown(self):
        # 实机：同一张桌子前后 22 分钟，快测 32、体检 17.8/10.8
        self.assertEqual(self._g(mbps=32, noisy=True), "unknown")
        self.assertTrue(A._quick_ok({"mbps": 32, "loss": 0, "avg": 60,
                                     "noisy": True}))

    def test_unknown_does_not_touch_the_device(self):
        # 测不准时按「够用」处理：宁可放过，不要凭一个不可信的数字断网体检
        self.assertTrue(A._quick_ok({"mbps": 12, "loss": 0, "avg": 60,
                                     "noisy": True}))

    def test_12_mbps_now_counts_as_enough(self):
        # 老阈值 15 会把住处晚高峰的常态判成「不够用」，从而反复触发体检
        self.assertTrue(A._quick_ok({"mbps": 12, "loss": 0, "avg": 70}))


class LockSignalGate(unittest.TestCase):
    """锁之前先看信号：记忆说它快，不等于它现在能用。

    实机 2026-10-30 住处：记忆里 BAND 3 有 46.8 Mbps，可那天 BAND 3 只有
    -105dBm、当前驻留的 BAND 40 是 -77dBm。照记忆锁 = 8 分钟掉线。
    """

    def _q(self, band=40, mbps=30.0, ok=True):
        return {"band": band, "ok": ok, "mbps": mbps, "avg": 65, "loss": 0}

    def test_weak_memory_keeps_current(self):
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=46.8, serving=40, quick=self._q(40, 30.0),
            memo_rsrp=-105, cur_rsrp=-77)
        self.assertEqual((band, kind), (40, "weak_keep"))

    def test_weak_memory_and_weak_current_means_measure(self):
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=46.8, serving=40,
            quick=self._q(40, 5.0, ok=False),
            memo_rsrp=-105, cur_rsrp=-77)
        self.assertEqual((band, kind), (None, "weak"))

    def test_drop_just_under_the_line_is_allowed(self):
        # 差 9dB 还没到 10dB 的线：记忆那条并非不可用，但它也没比当前快 50%
        # （40 对 46.8）→ 2026-10-31 起按记忆锁，不再「差不多就留当前」
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=46.8, serving=40, quick=self._q(40, 40.0),
            memo_rsrp=-86, cur_rsrp=-77)
        self.assertEqual((band, kind), (3, "memo"))

    def test_stronger_memory_still_wins(self):
        # 记忆那条信号更好（-70 对 -95）→ 照旧按记忆切
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=46.8, serving=40, quick=self._q(40, 10.0),
            memo_rsrp=-70, cur_rsrp=-95)
        self.assertEqual((band, kind), (3, "memo"))

    def test_unknown_rsrp_does_not_gamble(self):
        # 老记忆没记过信号：说不准那条现在还能不能用。当前够用就别赌。
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=46.8, serving=40, quick=self._q(40, 20.0),
            memo_rsrp=None, cur_rsrp=-77)
        self.assertEqual((band, kind), (40, "blind_keep"))

    def test_unknown_rsrp_and_weak_current_means_measure(self):
        # 两头都没把握 → 跑体检，顺手把每条频段的 RSRP 记下来
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=46.8, serving=40,
            quick=self._q(40, 3.0, ok=False),
            memo_rsrp=None, cur_rsrp=-77)
        self.assertEqual((band, kind), (None, "blind"))

    def test_same_band_never_gated(self):
        # 当前驻留就是记忆里那条时，比「差多少 dB」没有意义
        band, kind = A.guard_pick_lock(
            memo=40, memo_mbps=44.6, serving=40, quick=self._q(40, 44.0),
            memo_rsrp=-105, cur_rsrp=-77)
        self.assertEqual((band, kind), (40, "memo"))


class MemoRsrp(unittest.TestCase):
    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        self.SITES = Path(tempfile.mkdtemp()) / "sites.json"
        self._p = mock.patch.object(A, "SITES", self.SITES)
        self._p.start()
        self.json = json

    def tearDown(self):
        self._p.stop()

    def test_reads_median(self):
        self.SITES.write_text(self.json.dumps(
            {"236-CMCC": {"seen": 2, "last": "2026-10-30T17:00:00", "best": 3,
                          "bands": {"3": {"ok": 2, "best": 44.6,
                                          "rsrp": -104, "rsrp_med": -101}}}}),
            encoding="utf-8")
        self.assertAlmostEqual(A.guard_memo_rsrp("236-CMCC", 3), -101)

    def test_old_record_without_rsrp_is_none(self):
        # 老数据没记过信号 → None（不参与比较），不能返回 0 当成信号极好
        self.SITES.write_text(self.json.dumps(
            {"236-CMCC": {"seen": 2, "last": "2026-10-30T17:00:00", "best": 3,
                          "bands": {"3": {"ok": 2, "best": 44.6}}}}),
            encoding="utf-8")
        self.assertIsNone(A.guard_memo_rsrp("236-CMCC", 3))
        self.assertIsNone(A.guard_memo_rsrp("", 3))


class GuardPlanBand(unittest.TestCase):
    """体检跑完，守护该锁哪条。

    实机 2026-10-30 22:53：坐满 10 分钟 → 体检 7.5 分钟、断网 6 次 → 结论
    「BAND 3 最优（22 Mbps），按 auto 保持现状」→ 守护什么都没锁就走了。
    用户最后自己手动锁的。体检结论「不必切换」≠「不必锁」。
    """

    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        self.json = json
        self.PLAN = Path(tempfile.mkdtemp()) / "plan.json"
        self._p = mock.patch.object(A, "PLAN", self.PLAN)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _plan(self, lte, target=0):
        self.PLAN.write_text(self.json.dumps(
            {"lte": lte, "target": {"band": target}}), encoding="utf-8")

    def test_picks_fastest_usable(self):
        # 22:53 那次体检的真实数字：BAND 40 带宽不低但丢包 80%，不能选
        self._plan([
            {"band": 3, "ok": True, "mbps": 22.0, "avg": 66, "loss": 0},
            {"band": 8, "ok": True, "mbps": 10.5, "avg": 65, "loss": 0},
            {"band": 40, "ok": True, "mbps": 17.5, "avg": 79, "loss": 80},
            {"band": 38, "ok": False},
        ])
        self.assertEqual(A._guard_plan_band(), 3)

    def test_keep_current_still_returns_a_band(self):
        # 结论是「没必要换」时，也得返回当前这条 —— 锁上它才是守护的活
        self._plan([{"band": 3, "ok": True, "mbps": 22.0, "avg": 66, "loss": 0}], target=3)
        self.assertEqual(A._guard_plan_band(), 3)

    def test_all_dead_falls_back_to_target(self):
        self._plan([{"band": 38, "ok": False}, {"band": 41, "ok": False}], target=8)
        self.assertEqual(A._guard_plan_band(), 8)

    def test_missing_plan_is_none(self):
        # 读不到结论不能瞎猜，交给调用方退化成「锁当前驻留」
        self.assertIsNone(A._guard_plan_band())

    def test_unreliable_data_locks_current(self):
        # 数字不可信时照它跳到「最快的」是 0.6.9 才修过的老毛病，这里同理
        self.PLAN.write_text(self.json.dumps(
            {"lte": [{"band": 40, "ok": True, "mbps": 40.0, "avg": 60, "loss": 0},
                     {"band": 3, "ok": True, "mbps": 12.0, "avg": 70, "loss": 0}],
             "reliable": False, "target": {"band": 40}}), encoding="utf-8")
        self.assertEqual(A._guard_plan_band(prefer=3), 3)


class VerifyMemory(unittest.TestCase):
    """熟悉的地方要「验证记忆」，而不是永远绕过去。

    实机 2026-10-31：记忆里 BAND 3 是 23 Mbps 但没记 RSRP，守护 10:11 那次
    直接判定「不敢赌」锁了 BAND 40，还把记忆的最优改写成 40。用户原话：
    「在一个熟悉的地方要优先锁历史记录里面最优的，最近这几次锁定看起来没有验证记忆」。
    """

    def setUp(self):
        import json
        import tempfile
        from datetime import datetime, timedelta
        from pathlib import Path
        from unittest import mock
        self.json, self.dt = json, datetime
        self.timedelta = timedelta
        self.SITES = Path(tempfile.mkdtemp()) / "sites.json"
        self._p = mock.patch.object(A, "SITES", self.SITES)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _rec(self, **kw):
        rec = {"seen": 2, "last": self.dt.now().isoformat(timespec="seconds"),
               "best": 3, "bands": {"3": {"ok": 2, "best": 23.0},
                                    "40": {"ok": 3, "best": 40.7}}}
        rec.update(kw)
        self.SITES.write_text(self.json.dumps({"350-CMCC": rec}), encoding="utf-8")

    def test_never_verified_is_due(self):
        self._rec()
        self.assertTrue(A.site_verify_due("350-CMCC"))

    def test_verified_recently_is_not_due(self):
        self._rec(verified_at=self.dt.now().isoformat(timespec="seconds"))
        self.assertFalse(A.site_verify_due("350-CMCC"))

    def test_verified_long_ago_is_due_again(self):
        ago = (self.dt.now() - self.timedelta(hours=A.GUARD_VERIFY_HOURS + 1))
        self._rec(verified_at=ago.isoformat(timespec="seconds"))
        self.assertTrue(A.site_verify_due("350-CMCC"))

    def test_mark_verified_closes_the_window(self):
        self._rec()
        A.mark_site_verified("350-CMCC")
        self.assertFalse(A.site_verify_due("350-CMCC"))

    def test_high_loss_samples_turn_into_flap(self):
        # BAND 40 昨晚 80%、今天 12:21 又 40% —— 这些采样必须留痕，
        # 否则 band_flap 永远拿不到证据，记忆里 40 一直是「干净又快」。
        self._rec()
        for loss in (40, 25, 80):
            A.note_band_loss("350-CMCC", 40, loss, avg=80, rsrp=-80)
        data = self.json.loads(self.SITES.read_text(encoding="utf-8"))
        flip = A.band_flap(data["350-CMCC"]["bands"]["40"])
        self.assertTrue(flip[0], f"三条高丢包样本应该判成掉包，实际 {flip}")

    def test_pin_best_is_what_the_user_asked_for(self):
        self._rec()
        A.pin_site_best("350-CMCC", 3)
        data = self.json.loads(self.SITES.read_text(encoding="utf-8"))
        self.assertEqual(data["350-CMCC"]["best"], 3)
        self.assertEqual(data["350-CMCC"]["pinned"], 3)
        self.assertFalse(A.site_verify_due("350-CMCC"))


class DevicePin(unittest.TestCase):
    """「就要 BAND 3」得跟着设备走，不能跟着 PCI 走。

    实机 2026-10-31 16:47 的翻车：锁着 BAND 3 时 PCI=350，`--lock-band 3`
    把 pinned 写进 `350-?` 那条；`--restore` 后设备回 auto、驻留 BAND 40 时
    PCI=236，守护去读 `236-?` 那条 —— 那是今早体检写的 best=40、没有 pinned，
    于是乖乖锁回 40。用户的原话是「WiFi 要锁在 band3」，不是「350 这个小区要 3」。
    """

    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        d = Path(tempfile.mkdtemp())
        self.SITES, self.CONFIG = d / "sites.json", d / "config.json"
        self._ps = [mock.patch.object(A, "SITES", self.SITES),
                    mock.patch.object(A, "CONFIG", self.CONFIG)]
        for p in self._ps:
            p.start()
        self.json = json

    def tearDown(self):
        for p in self._ps:
            p.stop()

    def _sites(self, **rec):
        self.SITES.write_text(self.json.dumps(rec), encoding="utf-8")

    def test_pin_is_device_level_not_site_level(self):
        self._sites(**{"350-CMCC": {"seen": 1, "last": "2026-10-31T16:00:00",
                                    "best": 3, "pinned": 3}})
        A.set_device_pin(3)
        # 换频段后是另一个 PCI、另一条（没有 pinned 的）记录
        self._sites(**{"236-CMCC": {"seen": 1, "last": "2026-10-31T09:00:00",
                                    "best": 40}})
        self.assertIsNone(A.site_pinned({"signal": {"pci": 236,
                                                    "operator": "CMCC"}}))
        self.assertEqual(A.device_pin(), 3)

    def test_clear_on_restore(self):
        A.set_device_pin(3)
        A.clear_device_pin()          # --restore 就是「我要自动」
        self.assertIsNone(A.device_pin())

    def test_restore_must_clear_the_site_level_pin_too(self):
        """只清设备级不够：位置级那份还在，守护一启动又照着它锁回去。

        2026-10-31 20:56 实测：`--restore` 打印了「已取消之前指定的频段」，
        紧接着的守护演练却报「本来会锁 BAND 3 —— 依据 pinned」。设备级清了、
        位置级没清，而守护优先读位置级（pinned_site or device_pin）。
        """
        self._sites(**{"350-CMCC": {"seen": 2, "last": "2026-10-31T20:00:00",
                                    "best": 3, "pinned": 3,
                                    "pinned_at": A.datetime.now().isoformat(
                                        timespec="seconds")},
                       "236-CMCC": {"seen": 2, "last": "2026-10-31T20:10:00",
                                    "best": 40}})
        self.assertEqual(A.site_pinned({"signal": {"pci": 350, "operator": "CMCC"}}), 3)
        hits = A.clear_site_pins()
        self.assertEqual(hits, ["350-CMCC"])
        self.assertIsNone(A.site_pinned({"signal": {"pci": 350, "operator": "CMCC"}}))
        # 别的地方的「来过」记录不许被顺手删掉
        self.assertEqual(sorted(A.load_sites()), ["236-CMCC", "350-CMCC"])

    def test_site_pin_does_not_expire(self):
        """位置级 pin 不设时效 —— 它说的是「这个地方」，那是长期属性。

        实测中 解释了他设 BAND 3 的原因：住处的 BAND 40 信号和速度
        有时候比 3 好，程序就锁 40，可 40 会周期性丢包、用起来一会一卡。
        这种「这个地方这条更稳」的结论，不会因为过了一个星期就变假。
        跨位置的误伤由 device_pin_here() 挡（设备级那条记了来源位置）。
        """
        old = (A.datetime.now() - A.timedelta(days=200)).isoformat(timespec="seconds")
        self._sites(**{"236-CMCC": {"seen": 3, "best": 3, "pinned": 3,
                                    "pinned_at": old}})
        self.assertEqual(A.site_pinned({"signal": {"pci": 236, "operator": "CMCC"}}), 3)

    def test_old_site_pin_without_timestamp_still_works(self):
        # 老记录没有 pinned_at：位置级照样认，别让升级把用户设过的东西弄丢
        self._sites(**{"236-CMCC": {"seen": 3, "best": 3, "pinned": 3}})
        self.assertEqual(A.site_pinned({"signal": {"pci": 236, "operator": "CMCC"}}), 3)

    def test_site_pin_written_with_timestamp(self):
        # 写进去的时候必须带时间戳，否则下一次读就变成「说不清哪天的」直接失效
        self._sites(**{"236-CMCC": {"seen": 1, "best": 40}})
        A.pin_site_best("236-CMCC", 3)
        rec = A.load_sites()["236-CMCC"]
        self.assertIn("pinned_at", rec)
        self.assertEqual(A.site_pinned({"signal": {"pci": 236,
                                                   "operator": "CMCC"}}), 3)

    def test_clear_site_pins_is_a_noop_when_nothing_is_pinned(self):
        self._sites(**{"236-CMCC": {"seen": 1, "last": "2026-10-31T20:10:00",
                                    "best": 40}})
        self.assertEqual(A.clear_site_pins(), [])

    def test_cleared_after_two_failures(self):
        """指定的那条锁上就不通：第一次记一笔，连续两次才放弃。

        不设这条闸门，当地没覆盖时会「锁 → 不通 → 解锁 → 体检 → 照 pin 锁回去」
        无限循环，每次断网 87 秒。
        """
        A.set_device_pin(3)
        A.note_pin_failure(3)
        self.assertEqual(A.device_pin(), 3)      # 一次不算数
        A.note_pin_failure(3)
        self.assertIsNone(A.device_pin())        # 两次就取消

    def test_failure_of_other_band_does_not_touch_pin(self):
        A.set_device_pin(3)
        A.note_pin_failure(40)                   # 自动挑的备选不通，跟指定无关
        self.assertEqual(A.device_pin(), 3)

    def test_reset_counter_when_pinned_again(self):
        A.set_device_pin(3)
        A.note_pin_failure(3)
        A.set_device_pin(3)                      # 人又说了一次 → 重新给机会
        A.note_pin_failure(3)
        self.assertEqual(A.device_pin(), 3)


class PinOverridesLock(unittest.TestCase):
    """设备锁着的不是人要的那条，就该纠正 —— 而不是当成「已锁好」纯监控。

    实机 2026-10-31 17:11：pin=3，设备被外部锁在 BAND 40，守护显示
    「锁定 BAND 40」一路纯监控，pinned 连读都没读到。
    """

    def test_locked_elsewhere_is_not_ok(self):
        self.assertTrue(A.pin_overrides_lock(40, 3))

    def test_locked_as_wanted_is_ok(self):
        self.assertFalse(A.pin_overrides_lock(3, 3))

    def test_auto_is_left_to_normal_flow(self):
        self.assertFalse(A.pin_overrides_lock(0, 3))

    def test_no_pin_means_hands_off(self):
        # 人没指定时，设备锁在哪就是哪 —— 别把用户手动锁的频段改掉
        self.assertFalse(A.pin_overrides_lock(40, None))


class FlapAvoid(unittest.TestCase):
    """当前这条信号更强、速度也还行，但记忆里它会周期性掉包 —— 别锁它。

    2026-10-31 21:14 实测：BAND 40 是 -77dBm、比记忆里的 BAND 3 强 21dB，
    信号闸门（差超过 10dB 就留在当前）当场放行，锁定 BAND 40；21:16:31 它
    丢掉 50% 的包。信号强度和「会不会周期性掉包」是两件独立的事，而后者只有
    历史样本看得出来 —— RSRP 再漂亮也证明不了。
    """

    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        self.tmp = Path(tempfile.mkdtemp()) / "sites.json"
        self._p = mock.patch.object(A, "SITES", self.tmp)
        self._p.start()
        self._json = json

    def tearDown(self):
        self._p.stop()

    def _q(self):
        return {"band": 40, "ok": True, "mbps": 16.0, "avg": 56, "loss": 0}

    def test_flap_beats_signal_strength(self):
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=22.0, serving=40, quick=self._q(),
            memo_rsrp=-98, cur_rsrp=-77, cur_flap=True)
        self.assertEqual((band, kind), (3, "flap_avoid"))

    def test_clean_current_still_wins_on_signal(self):
        # 当前这条没掉包前科：原来那条「信号差 21dB 就留在当前」照旧生效
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=22.0, serving=40, quick=self._q(),
            memo_rsrp=-98, cur_rsrp=-77, cur_flap=False)
        self.assertEqual((band, kind), (40, "weak_keep"))

    def test_flapping_band_is_never_locked_even_if_it_is_the_best(self):
        """当前这条自己会周期性掉包 → 就算它是记忆最优，也不锁死。

        用户在住处的原话：「BAND 40 网速和信号有时候检测会比 BAND 3 好，就会导致
        直接锁 BAND 40，但 BAND 40 丢包就会用起来一会一卡」。所以这不是「要不要
        换过去」的问题，是「这条根本不该被锁」的问题 —— 原来的判断只在
        「要从别处换过来」时才生效，等于放过了它本来就是当前这条的情形。
        """
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=22.0, serving=3, quick={"band": 3, "ok": True,
                                                      "mbps": 20.0, "avg": 60, "loss": 0},
            memo_rsrp=-98, cur_rsrp=-98, cur_flap=True)
        self.assertEqual(kind, "flap_no_alt")
        self.assertIsNone(band)          # 没有备选 → 去体检找一条不掉包的

    def test_flapping_current_switches_to_alt(self):
        # 有备选就先换过去，不必跑体检
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=22.0, serving=3, quick={"band": 3, "ok": True,
                                                      "mbps": 20.0, "avg": 60, "loss": 0},
            memo_rsrp=-98, cur_rsrp=-98, cur_flap=True, alt_band=8)
        self.assertEqual((band, kind), (8, "flap_avoid"))

    def test_clean_current_is_kept(self):
        # 没有掉包前科就照旧：这条不该被新逻辑牵连
        band, kind = A.guard_pick_lock(
            memo=3, memo_mbps=22.0, serving=3, quick={"band": 3, "ok": True,
                                                      "mbps": 20.0, "avg": 60, "loss": 0},
            memo_rsrp=-98, cur_rsrp=-98, cur_flap=False)
        self.assertEqual(band, 3)

    def test_site_band_flap_reads_history(self):
        # 样本格式 [小时, 速度, 丢包%, 延迟, RSRP]
        rows = [[9, 16.0, 0, 60, -77], [10, 16.0, 50, 154, -78],
                [11, 16.0, 0, 65, -77], [12, 16.0, 80, 600, -78]]
        self.tmp.write_text(self._json.dumps({"236-CMCC": {
            "seen": 4, "last": "2026-10-31T21:00:00", "best": 3,
            "bands": {"3": {"ok": 4, "best": 22.0, "dead": 0},
                      "40": {"ok": 4, "best": 40.7, "dead": 0, "samples": rows}}}},
            ensure_ascii=False), encoding="utf-8")
        st = {"band": 40, "signal": {"pci": "236", "operator": "CMCC"}}
        self.assertTrue(A.site_band_flap(st, 40))
        self.assertFalse(A.site_band_flap(st, 3))     # 没样本 → 不定罪
        self.assertFalse(A.site_band_flap(st, None))


class LockSignalCollapse(unittest.TestCase):
    """锁着的那条信号还剩多少 —— 链路指标是结果，信号是原因。

    人走开之后信号先掉，链路过一会儿才垮，甚至根本不垮（餐厅离住处近，
    那条 BAND 3 只是变慢）。所以「信号塌了」要单独判一次。
    """

    def test_drop_ten_db_is_collapse(self):
        self.assertTrue(A.lock_signal_collapsed(-98, -108))

    def test_small_drop_is_not(self):
        # 掉 8dB 还在正常波动范围里，别一惊一乍
        self.assertFalse(A.lock_signal_collapsed(-98, -106))

    def test_signal_getting_better_is_not(self):
        self.assertFalse(A.lock_signal_collapsed(-108, -95))

    def test_unknown_is_never_collapse(self):
        self.assertFalse(A.lock_signal_collapsed(None, -110))
        self.assertFalse(A.lock_signal_collapsed(-98, None))


class HangDetection(unittest.TestCase):
    """「被系统冻结」要不要报出来。

    这条判据写在守护主循环里，没有单测覆盖得到 —— 常量漏定义过一次：
    382 个测试全绿、import 也正常，可守护跑到第二拍就 NameError 退出。
    所以这里至少把「这个数得存在、且是个能用的倍数」钉住。
    """

    def test_factor_exists_and_is_sane(self):
        self.assertGreater(A.GUARD_HANG_FACTOR, 1)

    def test_threshold_is_longer_than_a_normal_beat(self):
        # 正常一拍 = 间隔 + 采样耗时。阈值必须明显长于它，否则每拍都报警
        normal = A.GUARD_INTERVAL + 20
        threshold = A.GUARD_INTERVAL * A.GUARD_HANG_FACTOR + A.GUARD_HANG_SLACK
        self.assertGreater(threshold, normal * 2)


class QuickFreshness(unittest.TestCase):
    """下锁时用的那个速度，是不是还新鲜。

    实机 2026-11-01 19:28：18:21:13 快测出 9 Mbps（两次 2 / 9，当时判「测不准，
    先不动它」），18:28:01 下锁时又拿同一个 9 Mbps 说「快测够用」。一组数字、
    两个相反结论，中间还隔了 7 分钟 —— 晚高峰的链路足够变一个样。
    """

    def test_fresh_result_is_usable(self):
        q = {"mbps": 9, "at": 1000.0}
        self.assertFalse(A.quick_is_stale(q, now=1000.0 + 60))

    def test_result_older_than_window_is_stale(self):
        q = {"mbps": 9, "at": 1000.0}
        self.assertTrue(A.quick_is_stale(q, now=1000.0 + A.GUARD_QUICK_MAX_AGE + 1))

    def test_window_is_shorter_than_the_gap_between_probe_and_lock(self):
        # 快测在「停 3 分钟」，下锁在「坐满 10 分钟」：窗口必须短于这段时间，
        # 否则这条规则永远不会生效（等于没加）。
        self.assertLess(A.GUARD_QUICK_MAX_AGE, A.GUARD_QUICK_SEC * 2 + 120)

    def test_no_timestamp_counts_as_stale(self):
        # 老版本/手工拼出来的 dict 没有 at，别拿它下结论
        self.assertTrue(A.quick_is_stale({"mbps": 9}, now=1e9))

    def test_none_is_not_stale(self):
        # 压根没测过：不该触发「重测」，那是另一条分支的事
        self.assertFalse(A.quick_is_stale(None, now=1e9))


class CollapseCause(unittest.TestCase):
    """「锁着的这条信号塌了」到底是哪一种塌。

    实机 2026-11-01 19:31：锁着 BAND 40，PCI 288 是 -61dBm，两拍内 PCI 变成
    222、-97dBm —— 人正坐在餐厅吃饭。旧文案写死「多半是走开了」，把「设备在
    同一频段里挑了个弱小区」说成「人走了」。处置一样（解锁），但话不能说错。
    """

    def test_same_band_new_cell_is_not_the_user_leaving(self):
        why = A.lock_collapse_cause(lock_ref=-61, cur_rsrp=-97, locked_band=40,
                                    pci_changed=True, band_changed=False)
        self.assertIn("人没动", why)
        self.assertIn("BAND 40", why)
        self.assertNotIn("走开", why)

    def test_no_cell_change_means_the_person_moved(self):
        why = A.lock_collapse_cause(lock_ref=-70, cur_rsrp=-95, locked_band=3,
                                    pci_changed=False, band_changed=False)
        self.assertIn("走开", why)

    def test_band_change_says_so(self):
        why = A.lock_collapse_cause(lock_ref=-70, cur_rsrp=-95, locked_band=3,
                                    pci_changed=True, band_changed=True)
        self.assertIn("别的频段", why)

    def test_missing_reading_does_not_invent_a_reason(self):
        why = A.lock_collapse_cause(lock_ref=None, cur_rsrp=-95, locked_band=3,
                                    pci_changed=False, band_changed=False)
        self.assertIn("拿不到", why)


class QuickLine(unittest.TestCase):
    def test_noisy_result_is_labelled_unmeasurable(self):
        line = A.quick_line({"mbps": 9, "avg": 65, "loss": 0,
                             "samples": [3.0, 9.0], "noisy": True,
                             "grade": "unknown", "ok": True})
        self.assertIn("测不准", line)
        self.assertIn("两次差 3.0 倍", line)

    def test_good_result_says_enough(self):
        line = A.quick_line({"mbps": 26, "avg": 60, "loss": 0,
                             "samples": [25.0, 26.0], "noisy": False,
                             "grade": "good", "ok": True})
        self.assertIn("明确够用", line)
        self.assertNotIn("测不准", line)


if __name__ == "__main__":
    unittest.main()


class DevicePinTTL(unittest.TestCase):
    """「你亲手钉的那条」也要有时效。

    实机 2026-11-02 办公区：程序算出来该锁 BAND 40（那是当场实测的结论），
    被 10-31 在住处设的 `--lock-band 3` 直接改成 BAND 3 —— 而当时 BAND 3
    是 -99dBm、快测 2 Mbps。屏幕上没有任何线索说明为什么。

    一次 --lock-band 说的是「当时、那个地方」。放一周还照样压过所有判断，
    等于让一条旧命令永远赢。
    """

    def _pin(self, cfg):
        with mock.patch.object(A, "load_config", return_value=cfg):
            return A.device_pin()

    def _at(self, days):
        return (A.datetime.now() - A.timedelta(days=days)).isoformat(timespec="seconds")

    def test_recent_pin_is_honoured(self):
        self.assertEqual(self._pin({"pin_band": 3, "pin_at": self._at(1)}), 3)

    def test_expired_pin_is_ignored(self):
        self.assertIsNone(self._pin({"pin_band": 3, "pin_at": self._at(30)}))

    def test_pin_without_timestamp_is_ignored(self):
        # 老版本写的数据说不清是什么时候钉的，而那恰恰是最可能过时的一批
        self.assertIsNone(self._pin({"pin_band": 3}))

    def test_no_pin(self):
        self.assertIsNone(self._pin({}))

    def test_pin_note_explains_expiry(self):
        with mock.patch.object(A, "load_config",
                               return_value={"pin_band": 3, "pin_at": self._at(30)}):
            note = A.pin_note()
        self.assertIn("BAND 3", note)
        self.assertIn("超过", note)


class QuickLineFormat(unittest.TestCase):
    """快测那行字得能看懂。

    实机日志里出现过「两次 3 / 0，两次差 10.3 倍」—— 0 其实是 0.29 Mbps，
    被四舍五入成了 0。0 和 3 之间「差 10.3 倍」，看的人只会以为程序算错了。
    """

    def test_sub_one_is_not_shown_as_zero(self):
        q = {"band": 40, "ok": False, "grade": "poor", "mbps": 1.6,
             "samples": [3.0, 0.29], "avg": 87, "loss": 0}
        line = A.quick_line(q)
        self.assertIn("<1", line)
        self.assertNotIn("两次 3 / 0", line)

    def test_normal_values_stay_integers(self):
        q = {"band": 40, "ok": True, "grade": "good", "mbps": 32.4,
             "samples": [31.0, 33.0], "avg": 60, "loss": 0}
        self.assertIn("32 Mbps", A.quick_line(q))

    def test_missing_speed(self):
        q = {"band": 40, "ok": False, "grade": "poor", "samples": [],
             "avg": None, "loss": 100}
        line = A.quick_line(q)
        self.assertIn("延迟 不通", line)


class DevicePinScope(unittest.TestCase):
    """设备级 pin 只在「当初设它的那个地方」算数。

    实测中 办公区里遇到的就是这个：住处里设的「优先 BAND 3」，到了办公区
    被当成「这台设备永远要 3」，程序本来算好该锁 BAND 40，被它直接改掉。
    pin 说的是「这个地方 3 比 40 稳」，不是「这台设备永远要 3」。
    """

    def setUp(self):
        self.cfg = {
            "pin_band": 3,
            "pin_at": A.datetime.now().isoformat(timespec="seconds"),
            "pin_site": "236-CMCC",
            "pin_cells": [236, 350],
        }

    def _here(self, st):
        with mock.patch.object(A, "load_config", return_value=self.cfg):
            return A.device_pin_here(st)

    def test_same_site_still_applies(self):
        self.assertEqual(
            self._here({"signal": {"pci": "236", "operator": "CMCC"}}), 3)

    def test_other_cell_of_the_same_place_still_applies(self):
        # 同一间屋子，设备自己换了个小区（PCI 236 → 350），还是「这个地方」
        self.assertEqual(
            self._here({"signal": {"pci": "350", "operator": "CMCC"}}), 3)

    def test_another_place_does_not_apply(self):
        # 办公区：PCI 441 不在这组里 → 住处说的那条不跟过来
        self.assertIsNone(
            self._here({"signal": {"pci": "441", "operator": "CMCC"}}))

    def test_without_recorded_site_falls_back(self):
        # 老数据没记来源 → 按原样生效（时效由 device_pin 把关）
        self.cfg.pop("pin_site")
        self.assertEqual(
            self._here({"signal": {"pci": "441", "operator": "CMCC"}}), 3)

    def test_no_state_returns_pin(self):
        with mock.patch.object(A, "load_config", return_value=self.cfg):
            self.assertEqual(A.device_pin_here(None), 3)
