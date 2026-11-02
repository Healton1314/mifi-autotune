# -*- coding: utf-8 -*-
"""决策留痕 + 锁后回评。

实测中 要的两件事：

  「锁完回评效果」—— 现在只记了「锁住了没垮」，没记「锁对了没」。
  「决策留痕」—— 每次选哪条、凭什么，存成一条可回看的记录。

账本单独一个文件（decisions.json），不塞进 sites.json —— 2026-11-02 真机上
就是因为两个线程读改写同一个文件，刚记下的账目被整段覆盖，界面里一直是空的。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import autotune as A


class _Sites(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.SITES, self.CONFIG = d / "sites.json", d / "config.json"
        self.DEC = d / "decisions.json"
        self._ps = [mock.patch.object(A, "SITES", self.SITES),
                    mock.patch.object(A, "CONFIG", self.CONFIG),
                    mock.patch.object(A, "DECISIONS", self.DEC)]
        for p in self._ps:
            p.start()

    def tearDown(self):
        for p in self._ps:
            p.stop()

    def write_sites(self, data):
        self.SITES.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def read_sites(self):
        return json.loads(self.SITES.read_text(encoding="utf-8"))

    def write_decisions(self, data):
        self.DEC.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def read_decisions(self):
        return json.loads(self.DEC.read_text(encoding="utf-8"))

    @staticmethod
    def st(pci=236, op="CMCC"):
        return {"band": 40, "signal": {"pci": str(pci), "operator": op}}

    def site(self, bands=None, **extra):
        rec = {"seen": 3, "last": "2026-11-02T10:00:00", "best": 3,
               "bands": bands or {}}
        rec.update(extra)
        return {"236-CMCC": rec}


class LockVerdict(unittest.TestCase):
    """锁前 vs 锁后怎么算。丢包优先，其次延迟，都不明显就是 same。"""

    def test_loss_recovers(self):
        # 锁之前丢 20%，锁之后 0% —— 这就是「锁对了」
        self.assertEqual(A.lock_verdict({"loss": 20, "avg": 90},
                                        {"loss": 0, "avg": 90}), "better")

    def test_loss_gets_worse(self):
        self.assertEqual(A.lock_verdict({"loss": 0, "avg": 70},
                                        {"loss": 30, "avg": 70}), "worse")

    def test_small_loss_change_is_same(self):
        # 差 3 个百分点在噪声里，不该吹成「变好了」
        self.assertEqual(A.lock_verdict({"loss": 5, "avg": 70},
                                        {"loss": 2, "avg": 70}), "same")

    def test_latency_decides_when_loss_is_flat(self):
        self.assertEqual(A.lock_verdict({"loss": 0, "avg": 120},
                                        {"loss": 0, "avg": 70}), "better")
        self.assertEqual(A.lock_verdict({"loss": 0, "avg": 70},
                                        {"loss": 0, "avg": 140}), "worse")

    def test_latency_noise_is_same(self):
        self.assertEqual(A.lock_verdict({"loss": 0, "avg": 70},
                                        {"loss": 0, "avg": 78}), "same")

    def test_missing_numbers_do_not_crash(self):
        self.assertEqual(A.lock_verdict({}, {}), "same")
        self.assertEqual(A.lock_verdict(None, None), "same")


class ReviewLastLock(_Sites):
    def _decision(self, band=3, pre=None, kind="quick", out=None, serving=40):
        return {"t": "2026-11-02T11:00:00", "band": band, "kind": kind,
                "serving": serving, "out": out,
                "pre": pre if pre is not None else {"loss": 25, "avg": 120}}

    def test_fills_outcome_and_counts_a_win(self):
        self.write_sites(self.site())
        self.write_decisions({"236-CMCC": [self._decision()]})
        verdict, pre, post, switched = A.review_last_lock(
            "236-CMCC", 3, loss=0, avg=72, rsrp=-96)
        self.assertEqual(verdict, "better")
        self.assertTrue(switched)
        self.assertEqual(pre["loss"], 25)
        self.assertEqual(post["loss"], 0)
        self.assertEqual(self.read_decisions()["236-CMCC"][0]["out"]["verdict"],
                         "better")
        rec = self.read_sites()["236-CMCC"]
        self.assertEqual(rec["bands"]["3"]["review"]["good"], 1)
        self.assertEqual(rec["bands"]["3"]["review"]["bad"], 0)

    def test_counts_a_regret(self):
        # 锁之前丢 0%，锁之后丢 40% —— 这次锁上去就是错的
        self.write_sites(self.site())
        self.write_decisions({"236-CMCC": [
            self._decision(pre={"loss": 0, "avg": 70})]})
        verdict, _, _, _ = A.review_last_lock("236-CMCC", 3, loss=40, avg=70)
        self.assertEqual(verdict, "worse")
        self.assertEqual(
            self.read_sites()["236-CMCC"]["bands"]["3"]["review"]["bad"], 1)

    def test_does_not_review_twice(self):
        self.write_decisions({"236-CMCC": [
            self._decision(out={"verdict": "better", "loss": 0})]})
        verdict, _, _, _ = A.review_last_lock("236-CMCC", 3, loss=40, avg=70)
        self.assertEqual(verdict, "unknown")

    def test_ignores_unlock_entries(self):
        """解锁也记账，但「解开」不是「锁到这条上」，不能拿来评锁得好不好。"""
        self.write_decisions({"236-CMCC": [
            self._decision(kind="unlock_bad", pre={"loss": 0, "avg": 70})]})
        verdict, _, _, _ = A.review_last_lock("236-CMCC", 3, loss=40, avg=70)
        self.assertEqual(verdict, "unknown")

    def test_picks_the_matching_band_only(self):
        self.write_sites(self.site())
        self.write_decisions({"236-CMCC": [self._decision(band=40),
                                           self._decision(band=3)]})
        verdict, _, _, _ = A.review_last_lock("236-CMCC", 3, loss=0, avg=72)
        self.assertEqual(verdict, "better")
        self.assertIsNone(self.read_decisions()["236-CMCC"][0].get("out"))

    def test_no_band_switch_is_not_counted(self):
        """锁的就是本来驻留的那条 → 前后带宽没变，这十分钟的差别跟锁无关。

        给它记「锁对了」或「锁错了」都是栽赃：这类锁定占的还不少
        （keep / quick 两条路都是锁当前这条）。结论照记，但不进评分。
        """
        self.write_sites(self.site())
        self.write_decisions({"236-CMCC": [
            self._decision(band=40, pre={"loss": 0, "avg": 70}, serving=40)]})
        verdict, _, _, switched = A.review_last_lock("236-CMCC", 40, loss=60, avg=70)
        self.assertEqual(verdict, "worse")
        self.assertFalse(switched)
        rec = self.read_sites()["236-CMCC"]
        self.assertNotIn("review", rec["bands"].get("40") or {})

    def test_baseline_less_entry_does_not_shadow_a_real_one(self):
        """没有锁前基线的条目（接手记录 / 老账本）不能挡住它后面那条能评的。

        实机 2026-11-02：守护启动时设备本来就锁着 BAND 3，会记一条「接手」。
        要是它一直排在队首、每次扫描都先撞到它，后面真正能评的那条永远评不到。
        """
        adopt = self._decision(kind="adopt")
        adopt["pre"] = None                     # 接手记录没有锁前基线
        self.write_sites(self.site())
        self.write_decisions({"236-CMCC": [
            adopt, self._decision(band=3, pre={"loss": 0, "avg": 70})]})
        verdict, pre, _, _ = A.review_last_lock("236-CMCC", 3, loss=50, avg=70)
        self.assertEqual(verdict, "worse")
        self.assertEqual(pre["loss"], 0)

    def test_without_baseline_says_unknown(self):
        # 老版本留下的账本没有 pre：不猜，也不写一个假结论
        self.write_decisions({"236-CMCC": [{"t": "x", "band": 3, "kind": "memo"}]})
        verdict, _, _, _ = A.review_last_lock("236-CMCC", 3, loss=0, avg=70)
        self.assertEqual(verdict, "unknown")

    def test_empty_ledger(self):
        verdict, _, _, _ = A.review_last_lock("236-CMCC", 3, loss=0, avg=70)
        self.assertEqual(verdict, "unknown")


class RegretGate(_Sites):
    """「锁过两次都没好处」= 这个位置以后别再优先锁它。"""

    def _v(self, good=0, bad=0):
        return {"ok": 1, "review": {"good": good, "bad": bad}}

    def test_needs_two_bad(self):
        self.assertEqual(A.band_regret(self._v(bad=1)), (1, 0))

    def test_one_good_clears_it(self):
        # 有一次确实变好了 → 说明这条不是不能用，只是不稳定，别给判死刑
        self.write_sites(self.site(bands={"3": self._v(good=1, bad=5)}))
        self.assertFalse(A.site_band_regret(self.st(), 3))

    def test_two_bad_and_no_good_blocks_it(self):
        self.write_sites(self.site(bands={"3": self._v(bad=2)}))
        self.assertTrue(A.site_band_regret(self.st(), 3))

    def test_missing_review_is_not_a_regret(self):
        self.assertFalse(A.site_band_regret(self.st(), 40))


class PickLockUsesRegret(unittest.TestCase):
    def test_regret_switches_to_alt(self):
        band, kind = A.guard_pick_lock(
            memo=None, memo_mbps=None, serving=40,
            quick={"band": 40, "ok": True, "mbps": 30.0, "avg": 60, "loss": 0},
            cur_rsrp=-78, alt_band=3, cur_regret=True)
        self.assertEqual((band, kind), (3, "regret_avoid"))

    def test_regret_without_alt_measures(self):
        band, kind = A.guard_pick_lock(
            memo=None, memo_mbps=None, serving=40,
            quick={"band": 40, "ok": True, "mbps": 30.0, "avg": 60, "loss": 0},
            cur_rsrp=-78, cur_regret=True)
        self.assertEqual((band, kind), (None, "regret_no_alt"))

    def test_no_regret_keeps_normal_path(self):
        band, kind = A.guard_pick_lock(
            memo=None, memo_mbps=None, serving=40,
            quick={"band": 40, "ok": True, "mbps": 30.0, "avg": 60, "loss": 0},
            cur_rsrp=-78)
        self.assertEqual((band, kind), (40, "quick"))


class Ledger(_Sites):
    def _e(self, t, band=3, kind="quick"):
        return {"t": t, "band": band, "kind": kind,
                "serving": 40, "pre": {"loss": 20, "avg": 100}}

    def test_appends_and_reads_back(self):
        A.note_decision("236-CMCC", self._e("2026-11-02T11:00:00", kind="flap_avoid"))
        self.assertEqual(self.read_decisions()["236-CMCC"][0]["kind"], "flap_avoid")

    def test_keeps_a_bounded_window(self):
        """账本不是档案：留最近 DECISIONS_KEEP 条，文件不能一直涨。"""
        for i in range(A.DECISIONS_KEEP + 5):
            A.note_decision("236-CMCC", self._e(f"2026-11-02T11:{i:02d}:00"))
        rows = self.read_decisions()["236-CMCC"]
        self.assertEqual(len(rows), A.DECISIONS_KEEP)
        self.assertEqual(rows[-1]["t"],
                         f"2026-11-02T11:{A.DECISIONS_KEEP + 4:02d}:00")

    def test_does_not_need_a_site_record(self):
        """账本是「当时做了什么」，不依赖这处有没有位置记忆 —— 也不为它造假记录。"""
        r = A.note_decision("999-CMCC", self._e("2026-11-02T11:00:00"))
        self.assertNotIsInstance(r, str)
        self.assertIn("999-CMCC", self.read_decisions())
        self.assertFalse(self.SITES.exists())

    def test_only_if_empty(self):
        """「启动时本来锁着」那条只在账本为空时记一次，别每次重启都塞。"""
        A.note_decision("236-CMCC", self._e("2026-11-02T11:00:00"), only_if_empty=True)
        A.note_decision("236-CMCC", self._e("2026-11-02T11:05:00"), only_if_empty=True)
        self.assertEqual(len(self.read_decisions()["236-CMCC"]), 1)
        A.note_decision("236-CMCC", self._e("2026-11-02T11:10:00"), only_if_empty=True)
        self.assertEqual(len(self.read_decisions()["236-CMCC"]), 1)

    def test_reports_why_it_did_not_write(self):
        # 写不进去要能说出来（界面上那句「启动时设备已经锁在…」直接用它）
        A.note_decision("236-CMCC", self._e("2026-11-02T11:00:00"), only_if_empty=True)
        r = A.note_decision("236-CMCC", self._e("2026-11-02T11:05:00"),
                            only_if_empty=True)
        self.assertIsInstance(r, str)
        self.assertIn("已有 1 条", r)

    def test_report_is_readable(self):
        self.write_sites(self.site(
            bands={"3": {"ok": 1, "review": {"good": 2, "bad": 0}}}))
        A.note_decision("236-CMCC", self._e("2026-11-02T11:00:00", kind="flap_avoid"))
        text = "\n".join(A.decisions_report(5))
        self.assertIn("236-CMCC", text)
        self.assertIn("BAND 3", text)
        self.assertIn("会周期性掉包", text)      # 依据翻成了人话
        self.assertIn("好2/差0", text)

    def test_report_when_empty(self):
        self.assertTrue(any("还没有账目" in x for x in A.decisions_report(5)))


class LockBandWritesLedger(_Sites):
    """只有真锁上了才记账：决定了但没锁成，不该进账本。"""

    def _dev(self, boom=False):
        dev = mock.Mock()
        dev.set_band = mock.Mock(side_effect=RuntimeError("x") if boom else None)
        return dev

    def test_writes_on_success(self):
        with mock.patch.object(A, "wait_device_back", return_value=True):
            ok = A._guard_lock_band(self._dev(), 3, trace={
                "_key": "236-CMCC", "t": "2026-11-02T11:00:00",
                "kind": "quick", "pre": {"loss": 20, "avg": 100}})
        self.assertTrue(ok)
        row = self.read_decisions()["236-CMCC"][0]
        self.assertEqual(row["band"], 3)
        self.assertNotIn("_key", row)           # 内部字段不落盘

    def test_skips_when_lock_fails(self):
        with mock.patch.object(A, "wait_device_back", return_value=False):
            ok = A._guard_lock_band(self._dev(), 3, trace={
                "_key": "236-CMCC", "t": "2026-11-02T11:00:00", "kind": "quick"})
        self.assertFalse(ok)
        self.assertFalse(self.DEC.exists())

    def test_no_trace_no_ledger(self):
        with mock.patch.object(A, "wait_device_back", return_value=True):
            A._guard_lock_band(self._dev(), 3)
        self.assertFalse(self.DEC.exists())


if __name__ == "__main__":
    unittest.main()
