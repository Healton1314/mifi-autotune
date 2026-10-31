# -*- coding: utf-8 -*-
"""把守护的两条关键链路完整跑一遍（假驱动 + 真实数字，不碰任何设备）。

    python tests/manual/sim_guard_lock.py blind     # 场景一：体检完必须真的锁上
    python tests/manual/sim_guard_lock.py verify    # 场景二：熟悉的地方要验证记忆

不是 unittest —— 每个场景要跑 80 秒左右（守护的 settle 有下限），手动执行。

场景一（0.7.0 修的病）：记忆里的最优没记信号 → 判 blind → 跑体检 →
  0.6.9 之前体检完什么都不锁；0.7.0 必须看到 set_band(3)。

场景二（0.7.1 修的病，2026-10-31 一整早的实机）：记忆里最优是 BAND 3（23 Mbps，
  没记 RSRP），当前驻留 BAND 40 且快测够用 → 0.7.0 会判 blind_keep「先不赌」，
  锁当前 BAND 40，还把记忆的最优改写成 40。现在的行为：切到 BAND 3 实测一次，
  够用就留在它上面，并且**不动**记忆里的最优。
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

MODE = (sys.argv[1] if len(sys.argv) > 1 else "blind").lower()
TMP = Path(tempfile.mkdtemp())
SITES = TMP / "sites.json"
PLAN = TMP / "plan.json"


class FakeDev:
    """一台随身 WiFi。band=0 表示没锁，此时实际驻留 serving_auto。"""

    def __init__(self, serving):
        self.band = 0
        self.serving_auto = serving
        self.sets = []

    def serving(self):
        return self.band or self.serving_auto

    def get_state(self):
        b = self.serving()
        return {
            "band": self.band,
            "channel": 0,
            "mtu": 1500,
            "signal": {"pci": "350" if b == 3 else "236", "rsrp": -98, "sinr": 4,
                       "operator": "CMCC", "band": f"BAND {b}"},
        }

    def set_band(self, b):
        self.sets.append(int(b))
        self.band = int(b)
        return True

    def set_channel(self, c):
        return True


DEV = FakeDev(serving=40 if MODE == "verify" else 3)

CREATED = time.strftime("%Y-%m-%dT%H:%M:%S")
if MODE == "verify":
    # 今早实机：最优写的是 BAND 3，但只有速度、没有信号
    SITES.write_text(json.dumps({
        "350-CMCC": {
            "seen": 2, "last": CREATED, "best": 3,
            "bands": {"3": {"ok": 2, "best": 23.0, "dead": 0},
                      "40": {"ok": 3, "best": 40.7, "dead": 0}},
            "cells": [{"pci": "350"}, {"pci": "236"}],
        }
    }), encoding="utf-8")
else:
    SITES.write_text(json.dumps({
        "350-CMCC": {
            "seen": 3, "last": CREATED, "best": 40,
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
    exam_runs.append(time.strftime("%H:%M:%S"))
    PLAN.write_text(json.dumps(PLAN_EXAM, ensure_ascii=False), encoding="utf-8")
    print("      [模拟体检] 结论：BAND 3 最优 22.0 Mbps；"
          "BAND 40 17.5 Mbps 但丢包 80%（应被剔除）", flush=True)
    return 0


def fake_quick(speed_url, host, **kw):
    b = DEV.serving()
    if MODE == "verify":
        # 两条都够用：重点不是谁快，而是「有没有真的去验记忆里的那条」
        return {"mbps": 22.0 if b == 3 else 25.0, "avg": 70, "loss": 0,
                "ok": True, "samples": [22.0, 22.0], "grade": "good", "band": b}
    return {"mbps": 3.0, "avg": 210, "loss": 6, "ok": False,
            "samples": [3.0, 3.0], "grade": "poor", "band": b}


def spy_lock(dev, band):
    ok = A.__dict__["_real_lock"](dev, band)
    if MODE == "verify" and int(band) == int(DEV.serving()):
        raise KeyboardInterrupt      # 场景二：切到目标频段、测完就收工
    return ok


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

sites = json.loads(SITES.read_text(encoding="utf-8"))
best = (sites.get("350-CMCC") or {}).get("best")
print("\n" + "=" * 60)
print(f"用时 {dt:.0f}s ｜ 体检次数 {len(exam_runs)} ｜ set_band {DEV.sets} ｜ "
      f"最终 BAND {DEV.band or '未锁'} ｜ 记忆 best={best}")

if MODE == "verify":
    ok = (bool(DEV.sets) and DEV.sets[0] == 3 and DEV.band == 3
          and best == 3 and rc == 0)
    print("[通过] 切到记忆里的 BAND 3 实测过，留在它上面，记忆的最优没被改写"
          if ok else "[失败] 没有去验证记忆里的最优，或把 best 改掉了")
else:
    ok = (len(exam_runs) == 1 and bool(DEV.sets) and DEV.sets[-1] == 3
          and DEV.band == 3 and rc == 0)
    print("[通过] 体检跑完了，设备真的被锁到 BAND 3" if ok
          else "[失败] 体检后没有落地锁定，或锁错了频段")
sys.exit(0 if ok else 1)
