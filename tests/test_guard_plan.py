# -*- coding: utf-8 -*-
"""守护锁频段的「结论来源」与「人的优先级」。

实机 2026-11-04 早上，用户守着跑了一次体检，三件事同时出问题：
  1. 体检结论写着「BAND 40 32.6 Mbps 最优」，守护却锁了 BAND 3；
  2. 关掉守护后，那轮体检还在继续跑；
  3. 手动设频段在守护开着时直接被界面挡住，关掉之后又要等体检跑完。

根因是三个独立的断点，这里各钉一条：
  · 守护的体检走的是 --apply，从来不写 plan.json；守护读到的永远是上一轮
    留在文件里的旧结论（那天读到的是前一天 17:20 的，target 恰好是 3）。
  · guard_request_stop 只置 _GUARD_STOP，不碰体检的取消旗。
  · 手动锁等 45 秒拿不到锁就放弃，而体检的检查点比 45 秒稀。
"""

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import autotune as A


def _plan(ts=None, **extra):
    body = {
        "ts": ts or datetime.now().isoformat(timespec="seconds"),
        "lte": [
            {"band": 3, "ok": True, "mbps": 24.5, "avg": 169, "loss": 0},
            {"band": 40, "ok": True, "mbps": 32.6, "avg": 112, "loss": 0},
        ],
        "target": {"band": 3},
    }
    body.update(extra)
    return body


class PlanMustBeFresh(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.plan = self.tmp / "plan.json"
        self._p = mock.patch.object(A, "PLAN", self.plan)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _write(self, body):
        self.plan.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    def test_fresh_plan_wins(self):
        self._write(_plan())
        # 40 更快，就该锁 40：这才是「体检说 40 就锁 40」
        self.assertEqual(A._guard_plan_band(prefer=3), 40)

    def test_stale_plan_is_ignored(self):
        """前一天 17:20 的结论，第二天早上不能拿来锁。"""
        old = (datetime.now() - timedelta(hours=16)).isoformat(timespec="seconds")
        self._write(_plan(ts=old))
        self.assertEqual(A._guard_plan_band(prefer=41), 41)

    def test_plan_without_ts_is_untrusted(self):
        body = _plan()
        body.pop("ts")
        self._write(body)
        self.assertEqual(A._guard_plan_band(prefer=8), 8)

    def test_interrupted_plan_is_ignored(self):
        """被打断的体检只测了一半，里面的「最优」不能当真。"""
        self._write(_plan(interrupted=True))
        self.assertEqual(A._guard_plan_band(prefer=8), 8)

    def test_explicit_max_age_overrides_default(self):
        old = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        self._write(_plan(ts=old))
        self.assertEqual(A._guard_plan_band(prefer=8, max_age=7200), 40)


class GuardCheckupWritesPlan(unittest.TestCase):
    """守护的体检必须走 --preview，否则它锁的时候读不到自己刚测的东西。"""

    def test_preview_flag_is_used(self):
        args = mock.Mock(wifi_only=False, lte_only=False,
                         bands="default", ssid=None)
        seen = {}

        def fake_main():
            import sys
            seen["argv"] = list(sys.argv)

        with mock.patch.object(A, "main", side_effect=fake_main):
            A._run_cycle_inprocess(args, lte_only=True, preview=True)
        self.assertIn("--preview", seen["argv"])
        self.assertNotIn("--apply", seen["argv"])

    def test_apply_is_still_the_default(self):
        args = mock.Mock(wifi_only=False, lte_only=False,
                         bands="default", ssid=None)
        seen = {}

        def fake_main():
            import sys
            seen["argv"] = list(sys.argv)

        with mock.patch.object(A, "main", side_effect=fake_main):
            A._run_cycle_inprocess(args)
        self.assertIn("--apply", seen["argv"])
        self.assertNotIn("--preview", seen["argv"])


class StoppingGuardStopsCheckup(unittest.TestCase):
    def tearDown(self):
        A._GUARD_STOP.clear()
        A._CYCLE_CANCEL.clear()

    def test_request_stop_cancels_the_running_checkup(self):
        A._CYCLE_CANCEL.clear()
        A.guard_request_stop()
        self.assertTrue(A._GUARD_STOP.is_set())
        self.assertTrue(A._CYCLE_CANCEL.is_set())

    def test_cancelled_checkup_does_not_lock_anything(self):
        args = mock.Mock(wifi_only=False, lte_only=False,
                         bands="default", ssid=None)

        def fake_cycle(*_a, **_kw):
            A._CYCLE_CANCEL.set()
            return 0

        A._CYCLE_CANCEL.clear()
        with mock.patch.object(A, "_measure_eta", return_value="几秒"), \
                mock.patch.object(A, "run_cycle", side_effect=fake_cycle), \
                mock.patch.object(A, "_guard_plan_band") as plan_band:
            self.assertIsNone(A._guard_measure(args, prefer=40))
        plan_band.assert_not_called()          # 半截数据连读都不读


class ManualLockPreempts(unittest.TestCase):
    """手动锁在体检跑到一半时也要能进去（人的操作优先）。"""

    def tearDown(self):
        A._CYCLE_CANCEL.clear()
        while A._CYCLE_LOCK.locked():
            try:
                A._CYCLE_LOCK.release()
            except RuntimeError:
                break

    def test_waits_for_the_cycle_to_let_go(self):
        dev = mock.Mock()
        released = threading.Event()

        def hold():
            A._CYCLE_LOCK.acquire()
            time.sleep(0.6)
            A._CYCLE_LOCK.release()
            released.set()

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        time.sleep(0.05)                       # 让它先拿到锁
        args = mock.Mock(lock_band=3)
        with mock.patch.object(A, "_run_lock_band_locked", return_value=0) as run:
            code = A.run_lock_band(dev, args, {"band": 40})
        t.join(timeout=3)
        self.assertTrue(released.is_set())
        self.assertEqual(code, 0)
        run.assert_called_once()

    def test_gives_up_loudly_if_never_released(self):
        A._CYCLE_LOCK.acquire()                # 假装体检一直不放手
        args = mock.Mock(lock_band=3)
        with mock.patch.object(A, "MANUAL_PREEMPT_SEC", 0.3):
            code = A.run_lock_band(mock.Mock(), args, {"band": 40})
        self.assertEqual(code, 1)
        A._CYCLE_LOCK.release()


if __name__ == "__main__":
    unittest.main()
