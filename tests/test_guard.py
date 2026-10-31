# -*- coding: utf-8 -*-
"""全自动守护的决策规则。

用户 2026-10-30 定的策略：
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


class PickLockBand(unittest.TestCase):
    """久坐锁定前：记忆里的历史最优 vs 刚才快测的当前速度，锁哪一条。

    真实数据（2026-10-30 宿舍）：记忆说 BAND 3 是 44.6，但那是上午、在另一个
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

    2026-10-31 20:13 实机（宿舍，解锁后设备自己驻留在 BAND 40）：
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

    def test_pin_still_wins(self):
        band, kind = A.guard_pick_lock(
            memo=40, memo_mbps=40.7, serving=40, quick=self._q(40, 3.0),
            memo_rsrp=-78, cur_rsrp=-78, alt_band=3, pinned_band=40)
        self.assertEqual((band, kind), (40, "pinned"))


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
            "best": 40, "name": "宿舍",
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
    """PCI 变 ≠ 人在动。实机 2026-10-30 17:43：坐在宿舍里，锁着 BAND 41，
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
        # 2026-10-31 改判：光「换了几次小区」不再算移动。实机在宿舍坐了 144 分钟，
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

        2026-10-31 17:39 用户实机：锁着 BAND 40，40 丢包 100%，第一拍就把
        已经攒够的静止时间清零回「0/10」，而人还坐在原处。丢包是链路的事，
        该走「换备选 / 解锁自救」，不该把「我在这儿坐够了」这个结论作废。
        """
        self.assertFalse(A.guard_still_reset(pci_changed=False, moving=False))

    def test_band_switch_does_not_reset(self):
        # 设备自己换频段：PCI 变了、链路也断了，但人坐在原处，计时接着数。
        # 实机 21:46 这一下白等了 2 分钟；设备要是爱换，就永远坐不满 10 分钟。
        self.assertFalse(A.guard_still_reset(pci_changed=True, moving=False,
                                            band_changed=True))

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
        # 老阈值 15 会把宿舍晚高峰的常态判成「不够用」，从而反复触发体检
        self.assertTrue(A._quick_ok({"mbps": 12, "loss": 0, "avg": 70}))


class LockSignalGate(unittest.TestCase):
    """锁之前先看信号：记忆说它快，不等于它现在能用。

    实机 2026-10-30 宿舍：记忆里 BAND 3 有 46.8 Mbps，可那天 BAND 3 只有
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
                                    "best": 3, "pinned": 3},
                       "236-CMCC": {"seen": 2, "last": "2026-10-31T20:10:00",
                                    "best": 40}})
        self.assertEqual(A.site_pinned({"signal": {"pci": 350, "operator": "CMCC"}}), 3)
        hits = A.clear_site_pins()
        self.assertEqual(hits, ["350-CMCC"])
        self.assertIsNone(A.site_pinned({"signal": {"pci": 350, "operator": "CMCC"}}))
        # 别的地方的「来过」记录不许被顺手删掉
        self.assertEqual(sorted(A.load_sites()), ["236-CMCC", "350-CMCC"])

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


if __name__ == "__main__":
    unittest.main()
