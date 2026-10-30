# -*- coding: utf-8 -*-
"""信道表的三个事实：全都算、隐藏网络算、没邻居就是 0。

2026-10-30 用户提问：「没检测到的信道不应该才是最干净的吗？」
查下来评分一直是给全部 1~13 打分（没邻居的得 0），问题出在两处观感：
① `--scan` 只印前 5 条，用户以为没印出来的等于没检测；
② 隐藏网络（不广播 SSID）在解析阶段就被丢弃，等于把它的干扰算成 0 ——
   那是真的会把最脏的信道说成最干净。
"""

import json
import time
import unittest
from unittest import mock

import scanner


class AllChannelsScored(unittest.TestCase):
    def test_returns_all_13_channels(self):
        rows = scanner.score_channels([{"ssid": "邻居", "ch": 6, "sig": 80}])
        self.assertEqual(sorted(r["ch"] for r in rows), list(range(1, 14)))

    def test_channel_far_from_any_neighbor_is_zero(self):
        # 只有 1 号有邻居：13 号既没重叠也没别的邻居，必须是 0 分
        rows = scanner.score_channels([{"ssid": "邻居", "ch": 1, "sig": 80}])
        r13 = next(r for r in rows if r["ch"] == 13)
        self.assertEqual(r13["score"], 0.0)
        self.assertEqual(r13["detail"], [])
        self.assertEqual(r13["max_overlap"], 0)
        # 0 分的一定排在前面（最优候选）
        self.assertEqual(rows[0]["score"], 0.0)

    def test_max_overlap_recorded_for_gap_channel(self):
        # 邻居在 11 号，13 号只重叠 10MHz —— 这正是「夹缝信道分低」的由来
        rows = scanner.score_channels([{"ssid": "邻居", "ch": 11, "sig": 80}])
        r13 = next(r for r in rows if r["ch"] == 13)
        self.assertEqual(r13["max_overlap"], 10)
        r11 = next(r for r in rows if r["ch"] == 11)
        self.assertEqual(r11["max_overlap"], 20)     # 标准信道上完全重叠


class HiddenNetworkCounted(unittest.TestCase):
    """不广播名字的 AP（ssid 为空）照样占信道，丢掉就等于少算干扰。"""

    def _termux(self, payload):
        with mock.patch.object(scanner, "run_detail",
                               lambda *a, **k: (json.dumps(payload), "", 0)), \
                mock.patch.object(scanner.shutil, "which", lambda n: "/usr/bin/" + n):
            return scanner._scan_termux()

    def test_hidden_ap_kept_with_placeholder(self):
        now = time.time() * 1000
        aps = self._termux([
            {"bssid": "aa:bb", "ssid": "", "frequency_mhz": 2437,
             "rssi": -60, "timestamp": now},
        ])
        self.assertEqual(len(aps), 1)
        self.assertEqual(aps[0]["ch"], 6)
        self.assertEqual(aps[0]["ssid"], scanner._HIDDEN_SSID)

    def test_hidden_ap_scores_its_channel(self):
        now = time.time() * 1000
        aps = self._termux([
            {"bssid": "aa:bb", "ssid": "", "frequency_mhz": 2437,
             "rssi": -55, "timestamp": now},
        ])
        rows = scanner.score_channels(aps)
        r6 = next(r for r in rows if r["ch"] == 6)
        self.assertGreater(r6["score"], 0)          # 以前这里是 0（被丢了）


class PickBestWithZeroChannels(unittest.TestCase):
    """有 0 分信道时，标准信道优先规则不该把它挤掉。"""

    def test_zero_channels_are_not_displaced_by_standard_ones(self):
        # 只有 1 号有邻居：6/11/13 都是 0 分
        rows = scanner.score_channels([{"ssid": "a", "ch": 1, "sig": 90}])
        zeros = [r for r in rows if r["score"] == 0.0]
        self.assertGreaterEqual(len(zeros), 2)       # 确实存在多条空信道
        best = scanner.pick_best_channel(rows)
        # 必须在 0 分里面选；并列的 0 分里优先标准信道（6 或 11）
        self.assertEqual(best["score"], 0.0)
        self.assertIn(best["ch"], scanner.STANDARD_2G_CHANNELS)


if __name__ == "__main__":
    unittest.main()
