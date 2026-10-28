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
    "candidate_bands": [3, 40],     # 默认只测这两个，--bands all 才全测
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

def set_and_settle(dev, band):
    """锁定频段并等待重新注册。"""
    dev.set_band(band)
    time.sleep(SETTLE_SEC)
    return probe.ping_stats(6)


def measure_band(dev, band, rounds=3, speed_url=None, ping_host=None):
    """单频段多轮实测，取中位数，同时保留离散度用于判断可信程度。

    单轮容易撞上瞬时限速或基站调度抖动，所以至少 3 轮；
    中位数抗异常值，min/max 用来看波动大不大。
    """
    st = set_and_settle(dev, band)
    if st["avg"] is None or st["loss"] == 100:
        return {"band": band, "ok": False, "mbps": 0, "mbps_all": [],
                "mbps_min": 0, "mbps_max": 0, "avg": None, "max": None,
                "jitter": None, "loss": 100, "rounds": 0}

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
    ap.add_argument("--bands", default="default", help="default / all / 逗号分隔，如 3,40,41")
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
    ap.add_argument("--rounds", type=int, default=3, help="每个频段的测量轮数（默认 3，取中位数）")
    ap.add_argument("--scan-rounds", type=int, default=5, help="信道扫描轮数（默认 5，合并并滤除幽灵 AP）")
    ap.add_argument("--watch", action="store_true",
                    help="守护模式：持续监测，明显劣化时才自动重测并切换")
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
            cand = cfg.get("candidate_bands") or [3, 40]
            if args.bands == "all":
                bands = list(caps["bands"])
            elif args.bands != "default":
                bands = [int(x) for x in args.bands.split(",")]
            else:
                bands = [b for b in cand if b in caps["bands"]] or list(caps["bands"])[:2]
            per = SETTLE_SEC + args.rounds * 18 + 5
            print(f"\n[2/2] 实测 LTE 频段 {bands}（每频段 {args.rounds} 轮取中位数，"
                  f"约 {per} 秒/个，期间会短暂断网）…")
            results = []
            for b in bands:
                r = measure_band(dev, b, rounds=args.rounds,
                                 speed_url=speed_url, ping_host=ping_host)
                results.append(r)
                state = "可用" if r["ok"] else "无服务"
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
