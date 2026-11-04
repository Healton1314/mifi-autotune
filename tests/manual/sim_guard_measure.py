# -*- coding: utf-8 -*-
"""守护体检的三条链路，用假设备完整跑一遍（秒级，不碰任何设备）。

    python tests/manual/sim_guard_measure.py all        # 四条全跑
    python tests/manual/sim_guard_measure.py lock       # 体检结论要落到设备上
    python tests/manual/sim_guard_measure.py stale      # 旧结论不许拿来锁
    python tests/manual/sim_guard_measure.py preempt    # 手动锁要能打断体检
    python tests/manual/sim_guard_measure.py stop       # 关守护要掐掉体检

背景（实机 2026-11-04 早上，用户守着一轮体检）：
  09:58 体检跑了 9 分钟，结论「BAND 40 32.6 Mbps 最优」，
  10:07 守护却锁了 BAND 3 —— 因为它读的是前一天 17:20 留在 plan.json 里的旧结论。
  同期用户想手动锁，界面先说「先关守护」，关掉之后体检还在跑，人只能干等。

这里的替身只有 run_cycle（真跑体检要 5~9 分钟且会切频段），其余
_guard_measure / _guard_plan_band / run_lock_band / guard_request_stop 全是真代码。
"""

import json
import sys
import tempfile
import threading
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import autotune as A            # noqa: E402

TMP = Path(tempfile.mkdtemp())
A.PLAN = TMP / "plan.json"
A.SITES = TMP / "sites.json"
A.CONFIG = TMP / "config.json"
A.DECISIONS = TMP / "decisions.json"
A.SITES.write_text("{}", encoding="utf-8")
A.CONFIG.write_text("{}", encoding="utf-8")
A._measure_eta = lambda: "几秒"

ARGS = types.SimpleNamespace(wifi_only=False, lte_only=False,
                             bands="default", ssid=None)


def write_plan(best=40, ts=None, target=3, **extra):
    body = {
        "ts": (ts or datetime.now()).isoformat(timespec="seconds"),
        "lte": [
            {"band": 3, "ok": True, "mbps": 24.5, "avg": 169, "loss": 0},
            {"band": 40, "ok": True, "mbps": 32.6, "avg": 112, "loss": 0},
        ],
        "target": {"band": target},
    }
    body.update(extra)
    A.PLAN.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


RESULTS = []


def case(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(("  [√] " if ok else "  [×] ") + name + (f"：{detail}" if detail else ""))


# ---------------- 场景一：体检结论要落到设备上 ----------------
def scene_lock():
    print("\n[1] 守护体检跑完 → 必须按刚测出来的那条锁")
    write_plan(best=40, target=3)          # target 是「保持现状 3」，最优是 40
    written = {}

    def fake_cycle(args, lte_only=False, preview=False):
        written["preview"] = preview
        return 0

    A._CYCLE_CANCEL.clear()
    with_bak = A.run_cycle
    A.run_cycle = fake_cycle
    try:
        band = A._guard_measure(ARGS, prefer=41)
    finally:
        A.run_cycle = with_bak
    case("守护体检用 --preview（才写 plan.json）", written.get("preview") is True,
         f"preview={written.get('preview')}")
    case("锁的是体检结论里的最优（40），不是旧 target（3）", band == 40, f"拿到 {band}")


# ---------------- 场景二：旧结论不许拿来锁 ----------------
def scene_stale():
    print("\n[2] plan.json 是前一天留下的 → 不许拿来锁")
    old = datetime.now() - timedelta(hours=16)
    write_plan(best=40, ts=old, target=3)
    band = A._guard_plan_band(prefer=41)
    case("16 小时前的结论被拒，退回当前驻留 41", band == 41, f"拿到 {band}")
    case("绝不会是旧结论里的 3", band != 3, f"拿到 {band}")

    print("\n[2b] 体检被中途打断 → 半截结论更不许用")
    write_plan(best=40, interrupted=True, target=3)
    band = A._guard_plan_band(prefer=41)
    case("标记 interrupted 的结论被拒", band == 41, f"拿到 {band}")


# ---------------- 场景三：手动锁能打断体检 ----------------
def scene_preempt():
    print("\n[3] 体检跑着，人手动锁 → 立刻让路，不覆盖人的设置")
    A._CYCLE_CANCEL.clear()
    seen = {"cancel": False, "locked": None}
    holder = {}

    def cycle():                       # 假装一轮体检：持锁、每 20ms 看一次取消旗
        A._CYCLE_LOCK.acquire()
        holder["on"] = True
        try:
            for _ in range(200):
                if A._CYCLE_CANCEL.is_set():
                    seen["cancel"] = True
                    return
                time.sleep(0.02)
        finally:
            A._CYCLE_LOCK.release()

    t = threading.Thread(target=cycle, daemon=True)
    t.start()
    time.sleep(0.15)                   # 让它先跑起来、拿到锁

    dev = types.SimpleNamespace(set_band=lambda b: True, get_state=lambda: {
        "band": 40, "channel": 0,
        "signal": {"pci": "441", "rsrp": -98, "sinr": 5, "operator": "CMCC"}})

    def fake_locked(d, band, st0=None):
        seen["locked"] = band
        return 0

    args = types.SimpleNamespace(lock_band=3)
    real = A._run_lock_band_locked
    A._run_lock_band_locked = fake_locked
    t0 = time.time()
    try:
        code = A.run_lock_band(dev, args, {"band": 40})
    finally:
        A._run_lock_band_locked = real
    dt = time.time() - t0
    t.join(timeout=3)

    case("体检在下一个检查点就退出", seen["cancel"], f"用时 {dt:.1f}s")
    case("手动锁真的执行了", code == 0 and seen["locked"] == 3,
         f"exit={code} band={seen['locked']}")
    case("3 秒内拿到设备（不是干等 45 秒后放弃）", dt < 3, f"{dt:.1f}s")


# ---------------- 场景四：关守护要掐掉体检 ----------------
def scene_stop():
    print("\n[4] 关掉守护 → 正在跑的体检要停，不能继续断网")
    A._GUARD_STOP.clear()
    A._CYCLE_CANCEL.clear()
    observed = {}

    def slow_cycle(args, lte_only=False, preview=False):
        # 真体检会在检查点看 _CYCLE_CANCEL；这里模拟它正在跑，等人来掐
        for _ in range(300):
            if A._CYCLE_CANCEL.is_set():
                observed["cancelled"] = True
                raise A.CycleCancelled()
            time.sleep(0.02)
        return 0

    real = A.run_cycle
    A.run_cycle = slow_cycle
    box = {}
    try:
        th = threading.Thread(
            target=lambda: box.update(band=A._guard_measure(ARGS, prefer=40)))
        th.start()
        time.sleep(0.2)
        A.guard_request_stop()
        th.join(timeout=5)
    finally:
        A.run_cycle = real
        A._GUARD_STOP.clear()
        A._CYCLE_CANCEL.clear()

    case("停守护同时举取消旗", observed.get("cancelled") is True)
    case("这轮体检不作数（不锁任何频段）", box.get("band") is None,
         f"返回 {box.get('band')}")


def main():
    which = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    scenes = {"lock": [scene_lock], "stale": [scene_stale],
              "preempt": [scene_preempt], "stop": [scene_stop]}
    todo = sum(scenes.values(), []) if which == "all" else scenes.get(which, [])
    if not todo:
        print("用法：sim_guard_measure.py [all|lock|stale|preempt|stop]")
        return 2
    for fn in todo:
        fn()
    bad = [n for n, ok in RESULTS if not ok]
    print("\n" + "=" * 58)
    print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过"
          + ("" if not bad else f"｜失败：{bad}"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
