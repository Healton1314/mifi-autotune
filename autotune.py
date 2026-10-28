"""自动调优：探测周边 2.4G 信道占用 + 实测 LTE 各频段表现，自动选择最优。

安全原则：
  1. 默认只检测、给建议，不加 --apply 绝不改设备
  2. 只动「频段」和「信道」，永不碰 MTU / 加密 / SSID / APN
  3. 每次改动前保存原始状态到 state.json，--restore 可一键还原
  4. 频段改完必须验证能重新上网，否则自动回滚

用法：
  python autotune.py                     # 只体检，不改
  python autotune.py --apply             # 体检并应用最优设置
  python autotune.py --apply --bands all # 全频段扫描（慢，约 5 分钟）
  python autotune.py --restore           # 还原为「不锁频段 + 自动信道」
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import probe
import scanner
from drivers import detect, list_drivers
from drivers.base import DeviceBusy, Unsupported

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
LOG = HERE / "autotune-log.jsonl"
CONFIG = HERE / "config.json"

SETTLE_SEC = 26                     # 换频段后等待重新注册
MIN_CHANNEL_GAIN = 0.30             # 信道评分改善阈值，低于此不改
MIN_BAND_GAIN_MBPS = 3              # 频段带宽改善阈值
PLAN = HERE / "plan.json"           # 体检结论，供「一键应用」复用

# 守护模式（--watch）的劣化判定阈值。
# 三段式：min(上限, max(基线×倍数, 下限))
#   下限   —— 正常波动不许触发，否则一有抖动就断网重连
#   倍数   —— 本来就慢的链路（基线 120ms）按比例放宽，不误判
#   上限   —— 防止基线太高时阈值一起飘走，那样永远触发不了
WATCH_LOSS_LIMIT = 3                # 丢包 ≥3% 视为劣化
WATCH_FACTOR = 1.8                  # 相对基线的倍数

WATCH_MIN_MS = 80                   # 延迟下限
WATCH_CAP_MS = 260                  # 延迟上限
WATCH_COLD_MS = 180                 # 冷启动（还没有基线）时的默认阈值

WATCH_JITTER_MIN_MS = 25            # 抖动下限
WATCH_JITTER_CAP_MS = 80            # 抖动上限
WATCH_JITTER_COLD_MS = 40           # 冷启动抖动阈值

DEFAULT_CONFIG = {
    "own_ssid": "",                 # 自己设备的 WiFi 名，用于从干扰源里排除
    "speed_url": probe.DEFAULT_SPEED_URL,
    "ping_host": probe.DEFAULT_PING_HOST,
    "candidate_bands": [3, 40],     # 仅 --bands preferred 时用来缩小范围
}


# ---------------- 配置 ----------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    return cfg


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def save_state(payload):
    STATE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_log(entry):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------- LTE 频段 ----------------

def _signal_usable(st):
    """设备是否报了可用的服务小区。

    RSRP 常见的"没服务"取值：空、0、-0、0dBm、--。不同固件写法不一样，
    所以只做保守判断：拿得到一个像样的负数 dBm 才算有信号。
    """
    sig = st.get("signal") or {}
    raw = str(sig.get("rsrp") or "").strip()
    m = re.search(r"-?\d+", raw)
    if not m:
        return False
    return int(m.group()) < 0          # RSRP 一定是负值，0/正数都代表无效


def wait_for_service(dev, band, timeout=None, step=2.5, dead_confirm=3):
    """等设备在目标频段重新注册，并尽快识别"这个频段根本没信号"。

    为什么不能只 sleep 固定秒数：
      - 有信号的频段可能 12 秒就注册上了，傻等 26 秒纯属浪费；
      - 没信号的频段（运营商在当地没部署）等再久也没用，越早跳过早省事。
    所以改成轮询设备状态，两个退出条件：
      1) 报出可用的 RSRP 且当前频段=目标 → 成功
      2) 连续 dead_confirm 次仍无可用信号 → 判定无服务，立即返回
    返回 (status, 秒数)，status ∈ {"ok", "no_signal", "timeout"}。
    """
    timeout = SETTLE_SEC + 10 if timeout is None else timeout
    t0 = time.time()
    miss = 0
    seen_band = False
    while time.time() - t0 < timeout:
        time.sleep(step)
        try:
            st = dev.get_state()
        except Exception:  # noqa: BLE001
            continue
        now_band = st.get("band")
        if band and str(now_band) == str(band):
            seen_band = True
        if _signal_usable(st):
            if not band or str(now_band) == str(band):
                return "ok", round(time.time() - t0, 1)
            miss += 1
        else:
            miss += 1
        if miss >= dead_confirm and not _signal_usable(st):
            # 已经过了几轮仍没有任何服务小区 → 当地大概率没这个频段，别等了
            return "no_signal", round(time.time() - t0, 1)
        if not seen_band and time.time() - t0 > timeout - step:
            break
    # 最后再看一眼：可能刚注册上
    try:
        st = dev.get_state()
        if _signal_usable(st) and (not band or str(st.get("band")) == str(band)):
            return "ok", round(time.time() - t0, 1)
    except Exception:  # noqa: BLE001
        pass
    return "timeout", round(time.time() - t0, 1)


def measure_band(dev, band, rounds=3, speed_url=None, ping_host=None):
    """单频段多轮实测，取中位数，同时保留离散度用于判断可信程度。

    无服务的频段会在 wait_for_service 阶段就被判定并跳过，
    不再白等固定的 26 秒，也不会去跑根本跑不动的测速。
    """
    dev.set_band(band)
    status, waited = wait_for_service(dev, band)
    if status != "ok":
        return {"band": band, "ok": False, "mbps": 0, "mbps_all": [],
                "mbps_min": 0, "mbps_max": 0, "avg": None, "max": None,
                "jitter": None, "loss": 100, "rounds": 0,
                "reason": status, "waited": waited}

    st = probe.ping_stats(6, ping_host)
    if st["avg"] is None or st["loss"] == 100:
        return {"band": band, "ok": False, "mbps": 0, "mbps_all": [],
                "mbps_min": 0, "mbps_max": 0, "avg": None, "max": None,
                "jitter": None, "loss": 100, "rounds": 0,
                "reason": "no_data", "waited": waited}

    samples = []
    for i in range(rounds):
        mbps = probe.throughput(4, 8, speed_url)
        p = probe.ping_stats(10, ping_host)
        samples.append((mbps, p["avg"], p["max"], p["jitter"], p["loss"]))
        if i < rounds - 1:
            time.sleep(1.5)

    mbps_list = [s[0] for s in samples]
    avg_list = [s[1] for s in samples if s[1] is not None]
    max_list = [s[2] for s in samples if s[2] is not None]
    jit_list = [s[3] for s in samples if s[3] is not None]
    loss_list = [s[4] for s in samples if s[4] is not None]
    return {
        "band": band, "ok": True,
        "mbps": round(statistics.median(mbps_list), 1),
        "mbps_all": mbps_list,
        "mbps_min": min(mbps_list), "mbps_max": max(mbps_list),
        "avg": int(statistics.median(avg_list)) if avg_list else None,
        "max": max(max_list) if max_list else None,
        "jitter": statistics.median(jit_list) if jit_list else None,
        "loss": max(loss_list) if loss_list else 0,
        "rounds": len(samples),
    }


# ---------------- 体检预估 → 一键应用 ----------------
#
# 这是主推的使用方式：
#   1) --preview   实测一遍，输出「现在多少 → 能到多少」的对比表，最后恢复原状
#   2) --apply-plan 直接按结论改一次，不再重测（只切一次频段，最快）
#
# 为什么预览阶段也要切频段：不实测就没法给出可信的提升数字。设备只能上报
# 当前服务小区的信号，看不到邻区，所以「其它频段能跑多快」只有切过去测才知道。
# 但预览结束时一定会切回原频段，中间断几次网，最终状态不变。

def _band_label(band):
    return f"BAND {band}" if band else "自动"


def resolve_bands(caps, args, cfg):
    """决定要实测哪些频段。

    默认走「设备自己声明的全部频段」——先问设备支持什么，再逐个测，
    而不是拿一份写死的清单去猜。config 里的 candidate_bands 只在
    --bands preferred 时作为缩小范围用。
    """
    supported = list(caps.get("bands") or [])
    if args.bands in ("default", "all"):
        return supported or list(cfg.get("candidate_bands") or [])
    if args.bands == "preferred":
        pref = [b for b in (cfg.get("candidate_bands") or []) if b in supported]
        return pref or supported
    return [int(x) for x in args.bands.split(",") if x.strip()]


def run_preview(dev, caps, st0, args, cfg, speed_url, ping_host):
    cur_band, cur_ch = st0["band"], st0["channel"]
    rounds = args.rounds or 1          # 体检默认 1 轮粗筛，精度靠后面的「决赛」补
    plan = {"ts": datetime.now().isoformat(),
            "device": getattr(dev, "description", dev.name),
            "current": {"band": cur_band, "channel": cur_ch}}

    # ---- 一、WiFi 信道 ----
    wifi = None
    if not args.lte_only:
        own = args.ssid or cfg.get("own_ssid") or scanner.connected_ssid() or st0.get("ssid")
        print(f"\n[1/2] 扫描周边 2.4G 信道（{args.scan_rounds} 轮合并）…")
        aps = scanner.scan_2g(rounds=args.scan_rounds, own_ssid=own)
        if aps:
            rows = scanner.score_channels(aps, caps.get("channels") or range(1, 14))
            cur_score = next((r["score"] for r in rows if r["ch"] == cur_ch), None)
            best = rows[0]
            wifi = {"current": cur_ch, "cur_score": cur_score,
                    "best": best["ch"], "best_score": best["score"],
                    "neighbors": len(aps)}
            print(f"      {len(aps)} 个邻居 AP")
            print(f"      当前信道 {cur_ch} 干扰分 {cur_score} ｜ 最优 {best['ch']} 干扰分 {best['score']}")
        else:
            print("      [!] 没扫到邻居 AP，跳过信道部分（系统语言不支持或无线网卡未启用）")

    # ---- 二、LTE 频段 ----
    lte = []
    best_band = None
    if not args.wifi_only and caps.get("can_lock_band"):
        bands = resolve_bands(caps, args, cfg)
        if not bands:
            print("\n[2/2] 驱动没有声明可用频段，跳过频段实测。")
        else:
            per = SETTLE_SEC + rounds * 20 + 6
            print(f"\n[2/2] 设备声明支持 {len(bands)} 个频段：{bands}")
            print(f"      逐个实测（每段 {rounds} 轮，快的约 {per} 秒；"
                  f"没信号的频段会提前跳过，最多约 {per * len(bands) // 60} 分钟）")
            print("      期间会短暂断网，全部测完自动切回原频段…")
        for b in bands or []:
            r = measure_band(dev, b, rounds=rounds, speed_url=speed_url, ping_host=ping_host)
            lte.append(r)
            if r["ok"]:
                print(f"      {_band_label(b):<8} {r['mbps']:>5} Mbps  "
                      f"延迟 {r['avg']}ms  抖动 {r['jitter']}ms  丢包 {r['loss']}%")
            else:
                why = {"no_signal": "无信号（当地没这个频段）",
                       "timeout": "注册超时",
                       "no_data": "有信号但数据不通"}.get(r.get("reason"), "不可用")
                print(f"      {_band_label(b):<8} {why}  已跳过（{r.get('waited', '?')}s）")

        usable = [r for r in lte if r["ok"]]
        # 决赛：全部粗测完后，对「最优」和「当前」各补一轮。
        # 单轮结果容易被瞬时调度带偏，而这两者的差值正是决策依据，值得多花 90 秒确认。
        if usable and len(bands) > 2:
            usable.sort(key=lambda r: (-r["mbps"], r["avg"] or 999))
            contest = [b for b in dict.fromkeys([usable[0]["band"], cur_band]) if b]
            print(f"\n[决赛] 对 {[f'BAND {b}' for b in contest]} 各补 1 轮确认…")
            by_band = {r["band"]: r for r in lte}
            for b in contest:
                prev = by_band.get(b)
                if not prev or not prev["ok"]:
                    continue
                extra = measure_band(dev, b, rounds=1,
                                     speed_url=speed_url, ping_host=ping_host)
                if extra.get("ok"):
                    merged = sorted(prev["mbps_all"] + extra["mbps_all"])
                    prev["mbps"] = round(statistics.median(merged), 1)
                    prev["mbps_all"] = merged
                    prev["mbps_min"] = min(merged)
                    prev["mbps_max"] = max(merged)
                    prev["rounds"] = len(merged)
                    print(f"      {_band_label(b):<8} 复测后 {prev['mbps']} Mbps"
                          f"（{merged}）")

        # 无论结论如何，先把设备恢复原状，用户不动手也不会被留在别的频段
        now_band = dev.get_state()["band"]
        if now_band != cur_band:
            print(f"\n[还原] 切回 {_band_label(cur_band)} …")
            dev.set_band(cur_band)
            time.sleep(SETTLE_SEC)

        usable = [r for r in lte if r["ok"]]
        if usable:
            usable.sort(key=lambda r: (-r["mbps"], r["avg"] or 999))
            best_band = usable[0]["band"]

    # ---- 三、结论 ----
    cur_res = next((r for r in lte if r["band"] == cur_band), None)
    best_res = next((r for r in lte if r["band"] == best_band), None)
    cur_mbps = cur_res["mbps"] if cur_res and cur_res["ok"] else None
    best_mbps = best_res["mbps"] if best_res else None
    band_gain = (best_mbps - cur_mbps) if (cur_mbps is not None and best_mbps is not None) else None

    do_band = bool(best_res and best_band != cur_band
                   and band_gain is not None and band_gain >= MIN_BAND_GAIN_MBPS)
    do_ch = bool(wifi and caps.get("can_set_channel", True) and wifi["best"] != cur_ch
                 and wifi["cur_score"] is not None
                 and wifi["cur_score"] - wifi["best_score"] >= MIN_CHANNEL_GAIN)

    target = {"band": best_band if do_band else cur_band,
              "channel": wifi["best"] if do_ch else cur_ch}
    plan.update({
        "target": target,
        "lte": lte,
        "wifi": wifi,
        "expected": {
            "band_from": cur_band, "band_to": target["band"],
            "mbps_now": cur_mbps, "mbps_best": best_mbps,
            "mbps_gain": round(band_gain, 1) if band_gain is not None else None,
            "mbps_gain_pct": (round(band_gain / cur_mbps * 100) if cur_mbps else None),
            "ping_now": cur_res["avg"] if cur_res and cur_res["ok"] else None,
            "ping_best": best_res["avg"] if best_res else None,
            "channel_from": cur_ch, "channel_to": target["channel"],
            "channel_score_from": wifi["cur_score"] if wifi else None,
            "channel_score_to": wifi["best_score"] if wifi and do_ch else None,
        },
        "changes": ([f"频段 {_band_label(cur_band)} → {_band_label(target['band'])}"] if do_band else [])
                   + ([f"信道 {cur_ch} → {target['channel']}"] if do_ch else []),
    })
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 56)
    print("体检结论")
    print("=" * 56)
    if cur_mbps is not None or best_mbps is not None:
        if cur_mbps is not None:
            now_s = f"{cur_mbps} Mbps"
        elif cur_res is None:
            now_s = "未测（不在本次范围）"
        else:
            now_s = "测不出（该频段无服务）"
        best_s = f"{best_mbps} Mbps" if best_mbps is not None else "—"
        print(f"  频段：{_band_label(cur_band)} {now_s}"
              f"  →  最优 {_band_label(best_band) if best_band else '—'} {best_s}")
        if do_band and band_gain is not None:
            pct = plan["expected"]["mbps_gain_pct"]
            print(f"        切换后预计 {band_gain:+.1f} Mbps"
                  f"{f'（{pct:+d}%）' if pct is not None else ''}")
        elif best_band == cur_band:
            print("        当前频段已是最优，无需改动")
        elif band_gain is not None:
            print(f"        最优只高 {band_gain:.1f} Mbps，不到 {MIN_BAND_GAIN_MBPS} Mbps，不值得切换")

    if wifi:
        print(f"  信道：{cur_ch}（干扰分 {wifi['cur_score']}）"
              f"  →  最优 {wifi['best']}（干扰分 {wifi['best_score']}）")
        if do_ch:
            drop = 0
            if wifi["cur_score"]:
                drop = round((wifi["cur_score"] - wifi["best_score"]) / wifi["cur_score"] * 100)
            print(f"        切换后干扰下降约 {drop}%，主要改善重传和抖动（不承诺带宽提升）")
        elif wifi["best"] == cur_ch:
            print("        当前信道已是最优，无需改动")
        else:
            print(f"        改善不足阈值 {MIN_CHANNEL_GAIN}，不值得动")

    print("-" * 56)
    if plan["changes"]:
        print("可以优化的项：")
        for c in plan["changes"]:
            print(f"  · {c}")
        print(f"\n应用方式：python autotune.py --apply-plan"
              f"   （只在手机上点一下也行）")
    else:
        print("当前配置已经是这个环境下最优，不需要改动。")
        print("（结论已存 plan.json；环境变了可以重新体检一次）")
    return 0


def run_apply_plan(dev, caps, st0, args, cfg, ping_host):
    if not PLAN.exists():
        print("[×] 还没有体检结论。先跑一次：python autotune.py --preview")
        return 1
    try:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        print("[×] plan.json 损坏，请重新体检")
        return 1

    cur_band, cur_ch = st0["band"], st0["channel"]
    target = plan.get("target") or {}
    t_band, t_ch = target.get("band", cur_band), target.get("channel", cur_ch)

    todo = []
    if t_band != cur_band and caps.get("can_lock_band"):
        todo.append(("band", t_band))
    if t_ch != cur_ch and caps.get("can_set_channel", True):
        todo.append(("channel", t_ch))

    if not todo:
        print("[√] 当前配置已经和体检结论一致，无需改动。")
        return 0

    print("按体检结论应用：")
    for kind, val in todo:
        if kind == "band":
            print(f"  · 频段 {_band_label(cur_band)} → {_band_label(val)}")
        else:
            print(f"  · 信道 {cur_ch} → {val}")

    save_state({"saved_at": datetime.now().isoformat(),
                "band": cur_band, "channel": cur_ch})

    failed = False
    for kind, val in todo:
        if kind == "band":
            print(f"\n[应用] 锁定 {_band_label(val)} …")
            try:
                dev.set_band(val)
            except Unsupported as e:
                print(f"[跳过] {e}")
                continue
            time.sleep(SETTLE_SEC)
            chk = probe.ping_stats(6, ping_host)
            if chk["avg"] is None:
                print("[!] 切换后无法上网，自动回滚")
                dev.set_band(cur_band)
                time.sleep(SETTLE_SEC)
                failed = True
                break
            print(f"[√] 已生效，延迟 {chk['avg']}ms")
        else:
            print(f"\n[应用] 切换 WiFi 信道到 {val} …")
            try:
                dev.set_channel(val)
            except Unsupported as e:
                print(f"[跳过] {e}")
                continue
            time.sleep(5)
            got = dev.get_state()["channel"]
            if got != val:
                print(f"[!] 信道未生效（当前 {got}），恢复原值")
                dev.set_channel(cur_ch)
                failed = True
            else:
                print("[√] 已生效")

    end = dev.get_state()
    append_log({"ts": datetime.now().isoformat(), "mode": "apply-plan",
                "plan": plan.get("expected"), "before": {"band": cur_band, "channel": cur_ch},
                "after": {"band": end["band"], "channel": end["channel"]}})
    print(f"\n完成：频段 {_band_label(end['band'])} ｜ 信道 {end['channel'] or '自动'}")
    if failed:
        print("（有一项没成功，已回滚到原值，详见上面的 [!] 行）")
        return 1
    try:
        PLAN.unlink()
    except OSError:
        pass
    print(f"原始设置已存 {STATE}，可用 --restore 还原")
    return 0


# ---------------- 守护模式 ----------------

def _threshold(base, floor_ms, cold_ms, cap_ms):
    """算出触发阈值：min(上限, max(基线×倍数, 下限))；没有基线时用冷启动默认值。"""
    if base is None:
        return cold_ms
    return min(cap_ms, max(base * WATCH_FACTOR, floor_ms))


def judge_degradation(stats, base_avg=None, base_jitter=None):
    """判断一次探测是否算「网络劣化」，返回命中的原因列表（空列表 = 健康）。

    阈值同时看绝对值和相对基线：只比绝对值会在本来就慢的链路上频繁误报；
    只比相对基线，又会让已经很差的链路永远算「正常」。所以两边都设边界。
    """
    reasons = []
    if stats.get("avg") is None:
        return ["无法上网"]
    if (stats.get("loss") or 0) >= WATCH_LOSS_LIMIT:
        reasons.append(f"丢包 {stats['loss']}%")

    avg_floor = _threshold(base_avg, WATCH_MIN_MS, WATCH_COLD_MS, WATCH_CAP_MS)
    if stats["avg"] > avg_floor:
        reasons.append(f"延迟 {stats['avg']}ms 超过 {int(avg_floor)}ms")

    jit = stats.get("jitter") or 0
    jit_floor = _threshold(base_jitter, WATCH_JITTER_MIN_MS,
                           WATCH_JITTER_COLD_MS, WATCH_JITTER_CAP_MS)
    if jit > jit_floor:
        reasons.append(f"抖动 {jit}ms 超过 {int(jit_floor)}ms")
    return reasons


def run_cycle(args):
    """跑一次完整的体检 + 应用，返回子进程退出码。"""
    cmd = [sys.executable, "-u", str(Path(__file__).resolve())]
    if args.wifi_only:
        cmd.append("--wifi-only")
    if args.lte_only:
        cmd.append("--lte-only")
    if args.bands != "default":
        cmd += ["--bands", args.bands]
    if args.ssid:
        cmd += ["--ssid", args.ssid]
    cmd.append("--apply")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("utf-8", "ignore").rstrip()
        if line:
            print("      " + line, flush=True)
    p.wait()
    return p.returncode


def watch(args, cfg):
    """守护模式：低成本探测，只在明显劣化时才触发完整重测。

    为什么不是「每隔几分钟测一次速」：测速要跑几十兆流量，本身就干扰你在用的
    网络，长期跑不现实。所以平时只打 10 个 ping（几百字节），只有延迟/抖动/丢包
    明显偏离基线时，才付出「断网几十秒」的代价。
    """
    interval = max(10, args.watch_interval)
    cooldown = max(interval, args.watch_cooldown)
    host = cfg["ping_host"]
    hist, jit_hist = deque(maxlen=12), deque(maxlen=12)
    next_allowed, dry = 0.0, args.watch_dry_run

    print("=" * 62)
    print(f"守护模式：每 {interval}s 探测 {host}，明显劣化才触发重测")
    print(f"  触发：丢包≥{WATCH_LOSS_LIMIT}% ｜ 延迟>{WATCH_MIN_MS}~{WATCH_CAP_MS}ms"
          f"（基线×{WATCH_FACTOR}）｜ 抖动>{WATCH_JITTER_MIN_MS}~{WATCH_JITTER_CAP_MS}ms")
    print(f"  冷却：{cooldown}s（避免反复断网）"
          f"{'  ｜ 演练模式：只报告，不改设备' if dry else ''}")
    print("=" * 62)
    try:
        while True:
            st = probe.ping_stats(10, host)
            base_avg = statistics.median(hist) if len(hist) >= 3 else None
            base_jit = statistics.median(jit_hist) if len(jit_hist) >= 3 else None
            reasons = judge_degradation(st, base_avg, base_jit)

            ts = datetime.now().strftime("%H:%M:%S")
            line = (f"{ts}  延迟 {st['avg'] if st['avg'] is not None else '—'}ms"
                    f"  抖动 {st['jitter'] if st['jitter'] is not None else '—'}ms"
                    f"  丢包 {st['loss']}%"
                    f"  基线 {str(int(base_avg)) + 'ms' if base_avg else '采样中'}")

            if not reasons:
                print(line + "  ✓", flush=True)
                if st["avg"] is not None:
                    hist.append(st["avg"])
                if st["jitter"] is not None:
                    jit_hist.append(st["jitter"])
            elif time.time() < next_allowed:
                print(line + f"  ⚠ {'/'.join(reasons)}（冷却中，"
                             f"{int(next_allowed - time.time())}s 后再评估）", flush=True)
            else:
                print(line + f"  ✗ 判定劣化：{'/'.join(reasons)}", flush=True)
                if dry:
                    print("      （演练模式，未改动设备）", flush=True)
                else:
                    print("      → 开始完整重测…", flush=True)
                    code = run_cycle(args)
                    print(f"      → 重测结束（exit={code}）", flush=True)
                    hist.clear()
                    jit_hist.clear()
                next_allowed = time.time() + cooldown
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[守护模式] 已停止")
        return 0


# ---------------- 主流程 ----------------

def main():
    cfg = load_config()

    ap = argparse.ArgumentParser(description="随身WiFi 自动调优（跨平台）")
    ap.add_argument("--apply", action="store_true", help="实际应用最优设置（默认只检测）")
    ap.add_argument("--wifi-only", action="store_true", help="只优化 WiFi 信道")
    ap.add_argument("--lte-only", action="store_true", help="只优化 LTE 频段")
    ap.add_argument("--bands", default="default",
                    help="default=设备声明的全部频段 / preferred=配置里的子集 / 逗号分隔如 3,40")
    ap.add_argument("--restore", action="store_true", help="还原为不锁频段 + 自动信道")
    ap.add_argument("--host", default="192.168.0.1", help="设备管理地址")
    ap.add_argument("--user", default="admin", help="后台账号")
    ap.add_argument("--password", default="admin", help="后台密码")
    ap.add_argument("--driver", default=None, help="指定驱动名（默认自动探测）")
    ap.add_argument("--list-drivers", action="store_true", help="列出已登记的驱动")
    ap.add_argument("--ssid", default=None,
                    help="自己设备的 WiFi 名称，扫描时排除（默认自动识别，识别不到则不过滤）")
    ap.add_argument("--speed-url", default=None, help="测速下载地址（默认 Cloudflare，国内可换国内镜像）")
    ap.add_argument("--ping-host", default=None, help="延迟探测目标（默认 223.5.5.5）")
    ap.add_argument("--rounds", type=int, default=None,
                    help="每个频段的测量轮数（体检默认 1，应用默认 3，均取中位数）")
    ap.add_argument("--scan-rounds", type=int, default=5, help="信道扫描轮数（默认 5，合并并滤除幽灵 AP）")
    ap.add_argument("--preview", action="store_true",
                    help="体检并给出「预期提升」，结束时恢复原配置（推荐的第一步）")
    ap.add_argument("--apply-plan", action="store_true",
                    help="按上一次 --preview 的结论应用，不再重测（只切一次，最快）")
    ap.add_argument("--watch", action="store_true",
                    help="进阶：长期挂在后台，明显劣化时才自动重测并切换")
    ap.add_argument("--watch-interval", type=int, default=300,
                    help="守护模式探测间隔秒数（默认 300）")
    ap.add_argument("--watch-cooldown", type=int, default=900,
                    help="两次自动重测之间的最小间隔秒数（默认 900）")
    ap.add_argument("--watch-dry-run", action="store_true",
                    help="守护模式演练：只报告会触发什么，不改设备")
    args = ap.parse_args()

    if args.list_drivers:
        print("可用驱动：")
        for d in list_drivers():
            print(f"  {d['name']:<12} {d['description']}")
        print("\n新增机型：参考 drivers/template.py，在 drivers/__init__.py 里登记一行。")
        return 0

    speed_url = args.speed_url or cfg["speed_url"]
    ping_host = args.ping_host or cfg["ping_host"]
    if args.speed_url:
        cfg["speed_url"] = args.speed_url
        save_config(cfg)
    if args.ping_host:
        cfg["ping_host"] = args.ping_host
        save_config(cfg)

    if args.watch:
        return watch(args, cfg)

    try:
        dev = detect(args.host, args.user, args.password, name=args.driver)
    except LookupError as e:
        print(f"[×] {e}")
        return 1
    except DeviceBusy as e:
        print("[×] 设备在线，但当前拒绝提供服务：")
        print(f"    {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[×] 连不上设备：{type(e).__name__}: {e}")
        print("    请确认已连上这台随身WiFi（USB 或 WiFi 均可）。")
        return 1

    caps = dev.capabilities()
    try:
        st0 = dev.get_state()
    except Exception as e:  # noqa: BLE001
        print(f"[×] 读取状态失败：{type(e).__name__}: {e}")
        return 1

    cur_band = st0["band"]
    cur_ch = st0["channel"]
    print("=" * 56)
    print(f"设备：{getattr(dev, 'description', dev.name)}")
    print(f"当前状态：频段 {('未锁定' if cur_band == 0 else 'BAND ' + str(cur_band))} | "
          f"信道 {cur_ch or '自动'} | 带宽 {st0.get('bandwidth') or '—'} | MTU {st0.get('mtu') or '—'}")
    sig = st0.get("signal") or {}
    if sig.get("rsrp"):
        print(f"信号：RSRP {sig.get('rsrp')} | SINR {sig.get('sinr')} | PCI {sig.get('pci')}")
    print("=" * 56)

    if args.preview:
        return run_preview(dev, caps, st0, args, cfg, speed_url, ping_host)

    if args.apply_plan:
        return run_apply_plan(dev, caps, st0, args, cfg, ping_host)

    if args.restore:
        try:
            print("\n[还原] 频段 -> 未锁定")
            dev.set_band(0)
            time.sleep(SETTLE_SEC)
        except Unsupported as e:
            print(f"[跳过] {e}")
        try:
            print("[还原] 信道 -> 自动")
            dev.set_channel(0)
            time.sleep(4)
        except Unsupported as e:
            print(f"[跳过] {e}")
        print("[√] 已还原。建议等待 30 秒重新注册。")
        save_state({"restored_at": datetime.now().isoformat()})
        return 0

    report = {"ts": datetime.now().isoformat(), "before": {"band": cur_band, "channel": cur_ch}}

    # --- WiFi ---
    best_ch = None
    if not args.lte_only:
        own = args.ssid or cfg.get("own_ssid") or scanner.connected_ssid() or st0.get("ssid")
        if own and own != cfg.get("own_ssid"):
            cfg["own_ssid"] = own
            save_config(cfg)
        print(f"\n[1/2] 扫描周边 2.4G 信道（{args.scan_rounds} 轮合并，"
              f"出现不足 2 轮的 AP 视为幽灵丢弃）…")
        aps = scanner.scan_2g(rounds=args.scan_rounds, own_ssid=own)
        if own:
            print(f"      已排除自己的 AP「{own}」")
        elif aps:
            # 没连自己的 WiFi（例如走 USB）时无法直接识别，退化为「信号最强的那台大概率是自己」
            guess = aps.pop(0)
            print(f"      未指定自己的 SSID，按信号最强猜为「{guess['ssid']}」并已排除")
            print(f"      猜错会明显影响结论，建议固定：--ssid {guess['ssid']}")

        if not aps:
            print("      [!] 没有扫到任何邻居 AP。")
            print("          可能是系统语言不在支持列表里，或无线网卡被占用/未启用。")
            print("          本次跳过信道优化（宁可不改，也不能拿空数据乱改）。")
            report["wifi"] = {"supported": False, "reason": "no_ap_found"}
        else:
            print(f"      发现 {len(aps)} 个邻居 AP")
            chs = caps.get("channels") or list(range(1, 14))
            rows = scanner.score_channels(aps, chs)
            cur_score = next((r["score"] for r in rows if r["ch"] == cur_ch), None)
            best = rows[0]
            best_ch = best["ch"]
            print(f"      当前信道 {cur_ch} 评分 {cur_score} ｜ 最优信道 {best_ch} 评分 {best['score']}")
            if best["detail"]:
                print("      最优信道的重叠源：" + "；".join(
                    f"{n}(ch{c},{s}%,重叠{o}MHz)" for n, c, s, o in best["detail"]))
            else:
                print("      最优信道无重叠")
            report["wifi"] = {"current": cur_ch, "cur_score": cur_score,
                              "best": best_ch, "best_score": best["score"],
                              "neighbors": aps}

    # --- LTE ---
    best_band = None
    if not args.wifi_only:
        if not args.apply:
            st = probe.ping_stats(8, ping_host)
            print(f"\n[2/2] LTE 当前频段：{'未锁定' if cur_band == 0 else 'BAND ' + str(cur_band)}"
                  f"  延迟 {st['avg']}ms  抖动 {st['jitter']}ms  丢包 {st['loss']}%")
            print("      （频段实测需要逐个切换，会断网，因此只在 --apply 时执行）")
            report["lte"] = {"current": cur_band, "probed": False}
        elif not caps.get("can_lock_band"):
            print("\n[2/2] 该机型不支持锁定频段，跳过（只优化 WiFi）。")
            report["lte"] = {"supported": False}
        else:
            bands = resolve_bands(caps, args, cfg)
            rounds_n = args.rounds or 3
            per = SETTLE_SEC + rounds_n * 18 + 5
            print(f"\n[2/2] 实测 LTE 频段 {bands}（每频段 {rounds_n} 轮取中位数，"
                  f"约 {per} 秒/个；没信号的会提前跳过，期间会短暂断网）…")
            results = []
            for b in bands:
                r = measure_band(dev, b, rounds=rounds_n,
                                 speed_url=speed_url, ping_host=ping_host)
                results.append(r)
                state = "可用" if r["ok"] else {
                    "no_signal": "无信号", "timeout": "注册超时",
                    "no_data": "数据不通"}.get(r.get("reason"), "无服务")
                spread = (f"[{r['mbps_min']}~{r['mbps_max']}]"
                          if r["ok"] and r.get("rounds") else "")
                print(f"      BAND {b:<2} {state:<5} {r['mbps']:>5} Mbps {spread:<12} "
                      f"延迟 {r['avg']}ms  峰值 {r['max']}ms  丢包 {r['loss']}%")
            usable = [r for r in results if r["ok"]]
            report["lte"] = results
            if usable:
                usable.sort(key=lambda r: (-r["mbps"], r["avg"] or 999))
                best_res = usable[0]
                best_band = best_res["band"]
                print(f"\n      最优频段：BAND {best_band}"
                      f"（中位 {best_res['mbps']} Mbps，单轮 {best_res['mbps_all']}）")
                cur_res = next((r for r in results if r["band"] == cur_band), None)
                if (cur_res and cur_res["ok"] and best_band != cur_band
                        and best_res["mbps"] - cur_res["mbps"] < MIN_BAND_GAIN_MBPS):
                    print(f"      但相比当前 BAND {cur_band}（{cur_res['mbps']} Mbps）"
                          f"提升不足 {MIN_BAND_GAIN_MBPS} Mbps，不值得切换")
                    best_band = cur_band
            else:
                print("\n      [!] 所有频段都不通，回滚到未锁定")
                best_band = 0

    # --- 决策 ---
    print("\n" + "-" * 56)
    do_ch = bool(
        caps.get("can_set_channel", True) and best_ch is not None and best_ch != cur_ch
        and report.get("wifi", {}).get("cur_score") is not None
        and report["wifi"]["cur_score"] - report["wifi"]["best_score"] >= MIN_CHANNEL_GAIN
    )
    do_band = best_band is not None and best_band != cur_band
    print(f"建议：信道 {cur_ch} -> {best_ch if do_ch else '保持'} ｜ "
          f"频段 {'未锁定' if cur_band == 0 else 'BAND ' + str(cur_band)} -> "
          f"{('BAND ' + str(best_band)) if do_band else '保持'}")

    if not args.apply:
        print("\n（只检测模式，未改动设备。加 --apply 才会应用）")
        append_log(report)
        return 0

    # --- 应用 ---
    save_state({"saved_at": datetime.now().isoformat(),
                "band": cur_band, "channel": cur_ch})

    if do_band:
        print(f"\n[应用] 锁定 BAND {best_band} …")
        dev.set_band(best_band)
        time.sleep(SETTLE_SEC)
        chk = probe.ping_stats(6, ping_host)
        if chk["avg"] is None:
            print("[!] 切换后无法上网，回滚到原频段")
            dev.set_band(cur_band)
            time.sleep(SETTLE_SEC)
            best_band = cur_band
        else:
            print(f"[√] 已生效，延迟 {chk['avg']}ms")

    if do_ch:
        print(f"\n[应用] 切换 WiFi 信道到 {best_ch} …")
        dev.set_channel(best_ch)
        time.sleep(5)
        ch_now = dev.get_state()["channel"]
        if ch_now != best_ch:
            print(f"[!] 信道未生效（当前 {ch_now}），可能被设备自动接管，保持原样")
            dev.set_channel(cur_ch)
        else:
            print("[√] 已生效")

    st_end = dev.get_state()
    after = {"band": st_end["band"], "channel": st_end["channel"]}
    report["after"] = after
    append_log(report)
    print(f"\n完成。最终：频段 {'未锁定' if after['band'] == 0 else 'BAND ' + str(after['band'])}"
          f" | 信道 {after['channel']}")
    print(f"原始设置已存 {STATE}，可用 --restore 还原")
    return 0


if __name__ == "__main__":
    sys.exit(main())
