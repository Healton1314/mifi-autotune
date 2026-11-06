# -*- coding: utf-8 -*-
"""体检和手动锁定不能同时改设备。

实测：守护起了一轮 8 分钟体检，等不到它跑完就连点三次
`--lock-band 3`。日志里三次都写着「已锁定 BAND 3」，读回来却是 BAND 38 / 39 ——
体检子流程紧接着又 set_band 回去了。人的操作必须优先，体检必须会让路，
而且**让路之后不许再把设备还原成原频段**（那等于把刚设的整个抹掉）。
"""

import threading
import types
import unittest
from unittest import mock

import autotune as A


class CancelStopsExam(unittest.TestCase):
    def tearDown(self):
        A._CYCLE_CANCEL.clear()

    def test_measure_band_bails_out_before_touching_device(self):
        A._CYCLE_CANCEL.set()
        dev = mock.Mock()
        with self.assertRaises(A.CycleCancelled):
            A.measure_band(dev, 3)
        dev.set_band.assert_not_called()

    def test_exam_loop_bails_out_on_cancel(self):
        A._CYCLE_CANCEL.set()
        with mock.patch.object(A, "measure_band") as mb, \
                mock.patch.object(A, "restore_band") as restore:
            A.measure_all_bands(mock.Mock(), [3, 8], 1, 41, "BAND 41")
        mb.assert_not_called()
        restore.assert_not_called()     # 人接管了，别还原


class ExamRestoreBehaviour(unittest.TestCase):
    def tearDown(self):
        A._CYCLE_CANCEL.clear()

    def test_normal_exam_still_restores(self):
        row = {"band": 3, "ok": True, "mbps": 10, "avg": 50, "jitter": 1,
               "loss": 0, "mbps_all": [10], "mbps_min": 10, "mbps_max": 10,
               "rounds": 1, "rsrp": -95, "pci": "350"}
        with mock.patch.object(A, "measure_band", return_value=row), \
                mock.patch.object(A, "restore_band", return_value=(True, {})) as restore:
            A.measure_all_bands(mock.Mock(), [3], 1, 41, "BAND 41")
        restore.assert_called_once()

    def test_cancel_mid_way_skips_restore(self):
        row = {"band": 3, "ok": True, "mbps": 10, "avg": 50, "jitter": 1,
               "loss": 0, "mbps_all": [10], "mbps_min": 10, "mbps_max": 10,
               "rounds": 1, "rsrp": -95, "pci": "350"}
        state = {"n": 0}

        def fake_measure(*a, **k):
            state["n"] += 1
            if state["n"] == 2:
                A._CYCLE_CANCEL.set()
                raise A.CycleCancelled()
            return row

        with mock.patch.object(A, "measure_band", side_effect=fake_measure), \
                mock.patch.object(A, "restore_band", return_value=(True, {})) as restore:
            A.measure_all_bands(mock.Mock(), [3, 8, 39], 1, 41, "BAND 41")
        restore.assert_not_called()


class OnlyOneExamAtATime(unittest.TestCase):
    def tearDown(self):
        A._CYCLE_CANCEL.clear()
        if A._CYCLE_LOCK.locked():
            A._CYCLE_LOCK.release()

    def test_second_exam_is_refused(self):
        A._CYCLE_LOCK.acquire(blocking=False)      # 假装第一轮在跑
        with mock.patch.object(A, "run_cycle") as run:
            self.assertIsNone(A._guard_measure(mock.Mock(), prefer=3))
        run.assert_not_called()

    def test_measure_skips_when_link_is_down(self):
        with mock.patch.object(A, "run_cycle") as run:
            self.assertIsNone(A._guard_measure(mock.Mock(), prefer=3, net_down=True))
        run.assert_not_called()


class ManualLockWaitsForExam(unittest.TestCase):
    def tearDown(self):
        A._CYCLE_CANCEL.clear()
        if A._CYCLE_LOCK.locked():
            A._CYCLE_LOCK.release()

    def _dev(self):
        dev = mock.Mock()
        dev.get_state.return_value = {"band": 3,
                                      "signal": {"rsrp": -95, "pci": "350",
                                                 "operator": "CMCC"}}
        return dev

    def test_lock_applies_after_exam_lets_go(self):
        A._CYCLE_LOCK.acquire()                    # 体检占着
        threading.Timer(0.2, A._CYCLE_LOCK.release).start()
        dev = self._dev()
        args = types.SimpleNamespace(lock_band=3)
        with mock.patch.object(A, "wait_device_back", return_value=True), \
                mock.patch.object(A, "set_device_pin"), \
                mock.patch.object(A, "pin_site_best"):
            rc = A.run_lock_band(dev, args)
        self.assertEqual(rc, 0)
        dev.set_band.assert_called_with(3)
        self.assertFalse(A._CYCLE_LOCK.locked())   # 用完要放手
        self.assertFalse(A._CYCLE_CANCEL.is_set())

    def test_lock_gives_up_instead_of_fighting(self):
        """拿不到锁就别硬切 —— 宁可让用户再点一次，也别给他「假成功」。"""
        A._CYCLE_LOCK.acquire()
        dev = self._dev()
        args = types.SimpleNamespace(lock_band=3)
        with mock.patch.object(A, "_CYCLE_LOCK", type("L", (), {
                "acquire": lambda self, timeout=None, blocking=True: False,
                "release": lambda self: None,
                "locked": lambda self: True})()), \
                mock.patch.object(A, "_run_lock_band_locked") as inner:
            rc = A.run_lock_band(dev, args)
        self.assertEqual(rc, 1)
        inner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
