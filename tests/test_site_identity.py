"""位置身份：怎么认定「现在这个地方」是以前记过的那个地方。

实机 2026-11-04 的现场（用户原话）：
「办公区这个地方我应该进行的第一次测试，然后刚刚告诉我我已经进行过 4 次了，
  且上一次是 18 小时之前，那他应该是把我办公区的位置定位到住处了，
  且直接忽略了 ban41 和 ban38」

日志原文：
    [2/2] 这个位置（PCI 441）之前测过 4 次，上次来是 18 小时前
          同一处还见过 PCI 147、PCI 236、PCI 6、PCI 141、PCI 252、PCI 193
          跳过 [38, 41]：这里从来没信号

根因是 cells 的雪球：每次体检都把「每条频段各自的服务小区」记进同一条记录，
而判定「同一个地方」只看 PCI 数字在不在名单里。名单越长越容易命中，命中越多
名单越长 —— 最后一条记录覆盖了所有去过的地方，且带着这些地方攒下的 dead。
"""

import inspect
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import autotune as A


def _ago(hours):
    return (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")


class RememberCell(unittest.TestCase):
    """cells 的语义：这张桌子在每条频段上驻留哪个小区。"""

    def test_same_band_keeps_one_cell(self):
        rec = {}
        A._remember_cell(rec, "350-CMCC", 3)
        A._remember_cell(rec, "141-CMCC", 3)
        A._remember_cell(rec, "441-CMCC", 3)
        cells = rec["cells"]
        self.assertEqual(len(cells), 1, "同一条频段只能留一个服务小区")
        self.assertEqual(str(cells[0]["pci"]), "441", "留最近见到的那个")
        self.assertEqual(str(cells[0]["band"]), "3")

    def test_different_bands_all_kept(self):
        """跨频段必须都留着 —— 这是「同一张桌子切频段换小区」的唯一依据。"""
        rec = {}
        A._remember_cell(rec, "350-CMCC", 3)
        A._remember_cell(rec, "236-CMCC", 40)
        A._remember_cell(rec, "477-CMCC", 41)
        bands = sorted(str(c["band"]) for c in rec["cells"])
        self.assertEqual(bands, ["3", "40", "41"])

    def test_same_pci_twice_just_counts(self):
        rec = {}
        A._remember_cell(rec, "350-CMCC", 3)
        A._remember_cell(rec, "350-CMCC", 3)
        self.assertEqual(len(rec["cells"]), 1)
        self.assertEqual(rec["cells"][0]["n"], 2)

    def test_band_unknown_does_not_evict_known(self):
        """读不到频段时不许把已知的那条挤掉。"""
        rec = {}
        A._remember_cell(rec, "350-CMCC", 3)
        A._remember_cell(rec, "999-CMCC", None)
        pc = {str(c["pci"]) for c in rec["cells"]}
        self.assertIn("350", pc)
        self.assertIn("999", pc)


class FindSiteAcrossBands(unittest.TestCase):
    """同一个 PCI 数字在不同频段上不是同一个地方。"""

    def _data(self):
        return {
            "350-CMCC": {
                "seen": 5, "last": _ago(1),
                "bands": {"3": {"ok": 5, "dead": 0},
                          "38": {"ok": 0, "dead": 3},
                          "41": {"ok": 0, "dead": 3}},
                "cells": [
                    {"pci": "350", "band": 3, "n": 3, "last": _ago(1)},
                    {"pci": "32", "band": 40, "n": 2, "last": _ago(1)},
                ],
            }
        }

    def test_same_band_cell_hits(self):
        """住处切成 BAND 40（PCI 32）—— 还是这张桌子。"""
        d = self._data()
        self.assertEqual(A.find_site(d, "32-CMCC", band=40), "350-CMCC")

    def test_other_band_does_not_hit(self):
        """BAND 3 上见过 350，不代表 BAND 40 上的 350 是同一处。

        PCI 只有 0~503，每个频段各一套编号。原来的判定只看数字在不在名单里，
        所以在别的频段撞到一个数字就被认成老地方。
        """
        d = {
            "777-CMCC": {"seen": 5, "last": _ago(1),
                         "cells": [{"pci": "350", "band": 3, "n": 1,
                                    "last": _ago(1)}]},
        }
        self.assertEqual(A.find_site(d, "350-CMCC", band=40), "350-CMCC")
        # 不传 band（老调用点）时退回宽松匹配，别把已有行为改坏
        self.assertEqual(A.find_site(d, "350-CMCC"), "777-CMCC")

    def test_classroom_first_visit_is_a_new_site(self):
        """办公区：PCI 441 / BAND 3，住处那条只有 350 / BAND 3 → 不许命中。"""
        d = self._data()
        self.assertEqual(A.find_site(d, "441-CMCC", band=3), "441-CMCC")

    def test_named_same_key_still_wins(self):
        """同名键永远最强 —— 那是当前这个小区本身。"""
        d = self._data()
        self.assertEqual(A.find_site(d, "350-CMCC", band=3), "350-CMCC")

    def test_merge_window_is_short(self):
        """10 分钟前测过的别处，不许因为「刚测过」被并进来。

        原来窗口是 20 分钟 —— 从住处走到办公区刚好落进去，办公区就成了住处。
        """
        d = {"350-CMCC": {"seen": 2, "last": _ago(10 / 60.0),
                          "cells": [{"pci": "350", "band": 3, "n": 1,
                                     "last": _ago(10 / 60.0)}]}}
        self.assertEqual(A.find_site(d, "441-CMCC", band=3), "441-CMCC")


class HealPollutedMemory(unittest.TestCase):
    """历史已经被并烂的记录，要能修回来。"""

    def _polluted(self):
        return {
            "350-CMCC": {
                "seen": 5, "last": _ago(1),
                "bands": {"3": {"ok": 5, "dead": 0},
                          "38": {"ok": 0, "dead": 3},
                          "41": {"ok": 0, "dead": 3}},
                "cells": [
                    {"pci": "141", "band": 3, "n": 1, "last": _ago(30)},
                    {"pci": "304", "band": 3, "n": 1, "last": _ago(20)},
                    {"pci": "441", "band": 3, "n": 1, "last": _ago(1)},
                    {"pci": "6", "band": 8, "n": 1, "last": _ago(30)},
                    {"pci": "306", "band": 8, "n": 1, "last": _ago(20)},
                    {"pci": "193", "band": 39, "n": 1, "last": _ago(20)},
                    {"pci": "214", "band": 39, "n": 1, "last": _ago(1)},
                    {"pci": "252", "band": 41, "n": 1, "last": _ago(1)},
                    {"pci": "32", "band": 40, "n": 1, "last": _ago(1)},
                ],
            }
        }

    def test_same_band_cells_collapse(self):
        d = self._polluted()
        self.assertEqual(A._heal_site_memory(d), 1)
        bands = [str(c["band"]) for c in d["350-CMCC"]["cells"]]
        self.assertEqual(len(bands), len(set(bands)), "每个频段只剩一个小区")
        kept = {str(c["pci"]) for c in d["350-CMCC"]["cells"]}
        self.assertIn("441", kept)          # band 3 留最近的那个
        self.assertNotIn("141", kept)
        self.assertIn("214", kept)          # band 39 同理
        self.assertNotIn("193", kept)

    def test_dead_is_cleared_because_it_cannot_be_attributed(self):
        """办公区被跳过 38/41 的直接原因就是这条 dead 被继承了。"""
        d = self._polluted()
        A._heal_site_memory(d)
        for b in ("38", "41"):
            self.assertEqual(d["350-CMCC"]["bands"][b]["dead"], 0)

    def test_idempotent(self):
        d = self._polluted()
        A._heal_site_memory(d)
        snapshot = json.dumps(d, sort_keys=True, ensure_ascii=False)
        self.assertEqual(A._heal_site_memory(d), 0)
        self.assertEqual(json.dumps(d, sort_keys=True, ensure_ascii=False), snapshot)

    def test_clean_record_is_untouched(self):
        d = {"350-CMCC": {"seen": 3, "last": _ago(1),
                          "bands": {"3": {"ok": 3, "dead": 0}},
                          "cells": [{"pci": "350", "band": 3, "n": 2, "last": _ago(1)}]}}
        self.assertEqual(A._heal_site_memory(d), 0)
        self.assertEqual(len(d["350-CMCC"]["cells"]), 1)


class RecordSiteSlotsByServingBand(unittest.TestCase):
    """record_site 要把「当前驻留的那条频段」记进 cells，不是「跑得最快的那条」。"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self._p = [mock.patch.object(A, "SITES", d / "sites.json")]
        for p in self._p:
            p.start()
        A.save_sites({})

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _lte(self):
        return [
            {"band": 3, "ok": True, "mbps": 20.0, "avg": 60, "loss": 0,
             "rsrp": -90, "pci": "350"},
            {"band": 40, "ok": True, "mbps": 30.0, "avg": 70, "loss": 0,
             "rsrp": -92, "pci": "32"},
        ]

    def test_key_cell_uses_current_band_not_best(self):
        # 当前驻留 BAND 3（PCI 350），但本次最快是 BAND 40 —— 别把 350 标成 40
        A.record_site("350-CMCC", self._lte(), best_band=40, cur_band=3)
        rec = A.load_sites()["350-CMCC"]
        cell = next(c for c in rec["cells"] if str(c["pci"]) == "350")
        self.assertEqual(str(cell["band"]), "3")

    def test_both_bands_recorded(self):
        A.record_site("350-CMCC", self._lte(), best_band=40, cur_band=3)
        rec = A.load_sites()["350-CMCC"]
        by_band = {str(c["band"]): str(c["pci"]) for c in rec["cells"]}
        self.assertEqual(by_band.get("3"), "350")
        self.assertEqual(by_band.get("40"), "32")

    def test_classroom_does_not_inherit_dorm_dead(self):
        """端到端：住处攒了 dead=3，第一次到办公区不该被跳过。"""
        A.save_sites({"350-CMCC": {
            "seen": 4, "last": _ago(18),
            "bands": {"3": {"ok": 4, "dead": 0, "best": 20.0},
                      "38": {"ok": 0, "dead": 3},
                      "41": {"ok": 0, "dead": 3}},
            "cells": [{"pci": "350", "band": 3, "n": 4, "last": _ago(18)}],
        }})
        # 办公区：PCI 441 / BAND 3
        data = A.load_sites()
        real = A.find_site(data, "441-CMCC", band=3)
        self.assertEqual(real, "441-CMCC")
        self.assertIsNone(data.get(real), "办公区里不该看到住处那条记录")
        # 被跳过的频段也只能来自办公区自己（一条都没有）
        bands, dead, _slow, _best = A.band_plan({}, [3, 8, 38, 39, 40, 41], serving=3)
        self.assertEqual(dead, [])
        self.assertIn(38, bands)
        self.assertIn(41, bands)


class GuardPlanUsesBand(unittest.TestCase):
    """守护侧认位置也要带频段，否则又会退回「只看 PCI 数字」。"""

    def test_call_passes_band(self):
        src = inspect.getsource(A.run_preview)
        self.assertIn("find_site(data, site, band=", src)

    def test_record_site_signature_has_cur_band(self):
        sig = inspect.signature(A.record_site)
        self.assertIn("cur_band", sig.parameters)


if __name__ == "__main__":
    unittest.main()
