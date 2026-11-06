# -*- coding: utf-8 -*-
"""没网场景：设备驻留在一条没覆盖的频段上，守护必须自己换一条。

实机验证后的要求：「没信号的频道等很久，最后还是得手动换」。
这条场景就是把那段日志复现成假设备：

   18:56:41  未锁（驻留 41）  PCI —  —  延迟 不通  丢包 100%  静止 1/10 分钟
   ……一直这样，没有任何动作。

跑法（纯本地假设备，不碰真机、不切任何频段）：

   python tests/manual/sim_outage.py

期望：两三拍之后打印「当前 BAND 41 已经连续 N 次不通 —— 它不通就换一条」，
然后真的把设备锁到有依据的那条（BAND 39）。
"""

import json
import sys
import tempfile
import threading
import time
import types
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import autotune as A            # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="sim-outage-"))


class FakeDev:
    """没锁、驻留在 41、PCI 读不到 —— 断网时设备就长这样。"""

    name = "fake-rta"
    description = "Fake RTA (sim)"

    def __init__(self):
        self.locked = 0
        self.calls = []

    def get_state(self):
        band = "41" if not self.locked else str(self.locked)
        return {
            "band": self.locked,
            "channel": 0,
            "bandwidth": "20MHz",
            "mtu": 1500,
            "signal": {"pci": None, "rsrp": None, "sinr": None,
                       "operator": "CMCC", "band": band},
        }

    def set_band(self, b):
        self.locked = int(b or 0)
        self.calls.append(self.locked)
        return True

    def capabilities(self):
        return {"bands": [3, 8, 39, 40, 41]}


DEV = FakeDev()

A.SITES = TMP / "sites.json"
A.DECISIONS = TMP / "decisions.json"
A.OUTAGE = TMP / "guard_outage.json"
A.PLAN = TMP / "plan.json"
A.CONFIG = TMP / "config.json"
A.LOG = TMP / "autotune-log.jsonl"

# 别处见过一条能用的（这就是「借一条」的依据）。位置键 999-CMCC 跟当前无关。
A.save_sites({"999-CMCC": {
    "seen": 3, "best": 39,
    "last": datetime.now().isoformat(timespec="seconds"),
    "bands": {
        "39": {"ok": 2, "best": 12.0, "rsrp": -92,
               "last_ok": datetime.now().isoformat(timespec="seconds")},
        # 第二条：第一条试了不通还得有下一条可试，否则「换一条」变成
        # 「换过一次就没了」，又回到干等（模拟第一版就是这么停住的）。
        "40": {"ok": 3, "best": 9.0, "rsrp": -95,
               "last_ok": datetime.now().isoformat(timespec="seconds")},
    },
}})

A.detect = lambda *a, **k: DEV
A.wait_device_back = lambda dev, timeout=60, **k: True
A.probe.ping_stats = lambda n=10, host=None, **k: {
    "avg": None, "min": None, "max": None, "jitter": None,
    "loss": 100, "sent": 3, "recv": 0, "method": "icmp", "down": True}

# 把节奏压到秒级：两拍不通就算「连续不通」，间隔 0.2 秒。
A.GUARD_BAD_HITS = 2
A.GUARD_OUTAGE_FAST_SEC = 0.2
A.GUARD_OUTAGE_COOLDOWN = 1800

args = types.SimpleNamespace(
    host="192.168.0.1", user="admin", password="admin", driver=None,
    guard_interval=1, guard_settle=600, guard_dry_run=False, guard_fast=False,
)
cfg = {"ping_host": "192.168.0.1", "speed_url": None}


def stop_later():
    time.sleep(6)
    A._GUARD_STOP.set()


threading.Thread(target=stop_later, daemon=True).start()
t0 = time.time()
A.run_guard(args, cfg)
dt = time.time() - t0

print("\n" + "=" * 60)
print(f"跑了 {dt:.1f}s，设备锁定值 = {DEV.locked}，调用过的频段 = {DEV.calls}")
outage = json.loads(A.OUTAGE.read_text(encoding="utf-8")) if A.OUTAGE.exists() else {}
print("断网记录：", outage)
dec = json.loads(A.DECISIONS.read_text(encoding="utf-8")) if A.DECISIONS.exists() else {}
kinds = [d.get("kind") for rows in dec.values() for d in rows]
print("账本 kind：", kinds)

# 假设备里两条候选都不通，所以最后会停在「解锁」—— 这本身是对的（没有
# 依据就不硬锁）。要验的是「它有没有主动一条条试过去」，而不是死等。
tried_bands = {b for b in DEV.calls if b}
ok = {39, 40} <= tried_bands and "outage_try" in kinds
print("\n" + ("[√] 通过：没网时自己试过了有依据的 BAND 39、40，试完才停下等"
              if ok else
              "[×] 没走通：要么一动没动，要么只换了一条就停了（用户报的就是这个）"))
raise SystemExit(0 if ok else 1)
