# -*- coding: utf-8 -*-
"""把「久坐 → 记忆缺信号 → 体检 → 真的锁上」这条链路完整跑一遍。

不是 unittest（要跑 80 秒），手动执行：

    python tests/manual/sim_guard_lock.py

不碰真设备：用一个假驱动模拟一台「频段自动、实际驻留 BAND 3」的机器，
其余全部走 autotune.py 里真实的守护循环（run_guard）。

复刻的是 2026-10-30 22:53 的实机现场：
  记忆里 best=40 但没记 RSRP → 判 blind → 跑体检 →
  体检结论「BAND 3 最优 22 Mbps，BAND 40 丢包 80% 不选」→ 按 auto 保持现状
0.7.0 之前：到这里就结束了，什么都没锁，用户只能手动锁。
0.7.0：必须看到 dev.set_band(3) 被真的调用。

为什么只能模拟：真机上这条路径要「当前链路不够用」才会触发，网络好用的时候
永远走 quick/memo 分支；硬造现场就是反复切频段断网。逻辑走假驱动，选带依据
用的是当晚的真实数字。
"""

import json
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import autotune as A            # noqa: E402

TMP = Path(tempfile.mkdtemp())
SITES = TMP / "sites.json"
PLAN = TMP / "plan.json"


class FakeDev:
    """一台「频段自动、实际驻留 BAND 3」的随身 WiFi。"""

    def __init__(self):
        self.band = 0                 # 0 = 自动（未锁）
        self.sets = []

    def get_state(self):
        band_txt = f"BAND {self.band}" if self.band else "BAND 3"
        return {
            "band": self.band,
            "channel": 0,
            "mtu": 1500,
            "signal": {"pci": "350", "rsrp": -98, "sinr": 4,
                       "operator": "CMCC", "band": band_txt},
        }

    def set_band(self, b):
        self.sets.append(int(b))
        self.band = int(b)
        return True

    def set_channel(self, c):
        return True


DEV = FakeDev()

# ---- 1. 位置记忆：best=40，但没记过 RSRP（老格式）→ 必然判 blind ----
SITES.write_text(json.dumps({
    "350-CMCC": {
        "seen": 3,
        "last": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "best": 40,
        "bands": {"40": {"ok": 3, "best": 40.0, "dead": 0}},
        "cells": [{"pci": "350"}],
    }
}), encoding="utf-8")

PLAN_EXAM = {
    "lte": [
        {"band": 3, "ok": True, "mbps": 22.0, "avg": 66, "loss": 0},
        {"band": 8, "ok": True, "mbps": 10.5, "avg": 65, "loss": 0},
        {"band": 40, "ok": True, "mbps": 17.5, "avg": 79, "loss": 80},
        {"band": 38, "ok": False},
    ],
    "target": {"band": 0},
    "reliable": True,
}

exam_runs = []


def fake_run_cycle(args):
    """体检：不碰设备，只按 22:53 的真实结果落一份 plan.json。"""
    exam_runs.append(time.strftime("%H:%M:%S"))
    PLAN.write_text(json.dumps(PLAN_EXAM, ensure_ascii=False), encoding="utf-8")
    print("      [模拟体检] 已写出结论：BAND 3 最优 22.0 Mbps；"
          "BAND 40 17.5 Mbps 但丢包 80%（应被剔除）", flush=True)
    return 0


def fake_quick(speed_url, host, **kw):
    # 当前这条「不够用」→ 才会从 blind_keep 变成 blind（该跑体检）
    return {"mbps": 3.0, "avg": 210, "loss": 6, "ok": False,
            "samples": [3.0, 3.0], "grade": "poor", "band": 3}


def spy_lock(dev, band):
    ok = A.__dict__["_real_lock"](dev, band)
    raise KeyboardInterrupt      # 锁上了就收工（走 run_guard 自己的收尾路径）


A.SITES = SITES
A.PLAN = PLAN
A.detect = lambda *a, **k: DEV
A.run_cycle = fake_run_cycle
A.guard_quick_probe = fake_quick
A.probe.ping_stats = lambda n, host: {"avg": 65, "loss": 0, "jitter": 5.0}
A.__dict__["_real_lock"] = A._guard_lock_band
A._guard_lock_band = spy_lock

args = types.SimpleNamespace(
    host="192.168.0.1", user="admin", password="admin", driver=None,
    guard_interval=15, guard_settle=60, guard_dry_run=False, guard_fast=False,
)
cfg = {"ping_host": "192.168.0.1", "speed_url": None}

t0 = time.time()
rc = A.run_guard(args, cfg)
dt = time.time() - t0

print("\n" + "=" * 60)
print(f"用时 {dt:.0f}s ｜ 体检跑了几次：{len(exam_runs)} ｜ "
      f"set_band 调用序列：{DEV.sets} ｜ 最终锁在：{DEV.band or '未锁'}")
ok = (len(exam_runs) == 1 and DEV.sets and DEV.sets[-1] == 3
      and DEV.band == 3 and rc == 0)
print("[通过] 体检跑完了，设备真的被锁到 BAND 3" if ok
      else "[失败] 体检后没有落地锁定，或锁错了频段")
sys.exit(0 if ok else 1)
