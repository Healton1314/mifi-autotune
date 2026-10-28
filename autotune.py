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
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from drivers import detect, list_drivers
from drivers.base import Unsupported

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
LOG = HERE / "autotune-log.jsonl"

OWN_SSID = "月亮糖"
SPEED_URL = "https://dldir1.qq.com/weixin/Windows/WeChatSetup.exe"
PING_HOST = "223.5.5.5"

ALL_BANDS = [3, 8, 38, 39, 40, 41]
CANDIDATE_BANDS = [3, 40]          # 默认只测这两个（已验证可用，且覆盖多数场景）
SETTLE_SEC = 26                     # 换频段后等待重新注册
MIN_CHANNEL_GAIN = 0.30             # 信道评分改善阈值，低于此不改
MIN_BAND_GAIN_MBPS = 3              # 频段带宽改善阈值


# ---------------- 基础工具 ----------------

def _env():
    return {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}


def sh(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=_env())
        for enc in ("utf-8", "gbk", "cp936"):
            try:
                return p.stdout.decode(enc)
            except UnicodeDecodeError:
                continue
        return p.stdout.decode("utf-8", "ignore")
    except Exception:
        return ""


CURL = shutil.which("curl") or "curl"


def ping_stats(n=10):
    out = sh(["ping", "-n", str(n), PING_HOST], timeout=60)
    avg = re.search(r"(?:平均|Average)\s*=\s*(\d+)ms", out)
    loss = re.search(r"\((\d+)%", out)
    mx = re.search(r"(?:最长|Maximum)\s*=\s*(\d+)ms", out)
    return {
        "avg": int(avg.group(1)) if avg else None,
        "max": int(mx.group(1)) if mx else None,
        "loss": int(loss.group(1)) if loss else (100 if not avg else None),
    }


def throughput(conn=4, secs=8):
    """多路并发下载，返回 Mbps。"""
    procs = []
    for _ in range(conn):
        procs.append(subprocess.Popen(
            [CURL, "--noproxy", "*", "-L", "-s", "-o", "NUL",
             "--max-time", str(secs), "-A", "Mozilla/5.0",
             "-w", "%{size_download}", SPEED_URL],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_env()))
    total = 0
    for p in procs:
        try:
            out, _ = p.communicate(timeout=secs + 15)
            total += int((out or b"0").decode("utf-8", "ignore").strip() or 0)
        except Exception:
            p.kill()
    return round(total * 8 / secs / 1e6, 1)


# ---------------- 2.4G 信道 ----------------

def scan_2g(rounds=5, delay=3, min_hits=2):
    """多轮扫描合并。单次 netsh 扫描是缓存式的、必然漏 AP，所以：
    - 每轮独立扫描，同一 AP 取多轮信号强度的中位数
    - 只在 >= min_hits 轮里出现的 AP 才计入（滤掉一闪而过的幽灵 AP）
    """
    samples = {}
    for i in range(rounds):
        if i:
            time.sleep(delay)
        out = sh(["netsh", "wlan", "show", "networks", "mode=bssid"], timeout=60)
        ssid = band = sig = None
        for line in out.splitlines():
            s = line.strip()
            m = re.match(r"^SSID\s+\d+\s*:\s*(.*)$", s)
            if m:
                ssid, band, sig = m.group(1).strip(), None, None
                continue
            if re.match(r"^信号\s*:", s):
                sig = int(re.sub(r"[^\d]", "", s.split(":", 1)[1]) or 0)
            elif re.match(r"^波段\s*:", s):
                band = s.split(":", 1)[1].strip()
            elif re.match(r"^(频道|信道)\s*:", s):
                v = re.sub(r"[^\d]", "", s.split(":", 1)[1])
                if ssid and band and v and "2.4" in band and ssid != OWN_SSID:
                    samples.setdefault((ssid, int(v)), []).append(sig or 0)
                sig = band = None
    aps = []
    for (ssid, ch), sigs in samples.items():
        if len(sigs) < min_hits:
            continue
        aps.append({"ssid": ssid, "ch": ch,
                    "sig": int(statistics.median(sigs)), "hits": len(sigs)})
    return aps


def freq(ch):
    return 2412 + (ch - 1) * 5


def score_channels(aps):
    rows = []
    for ch in range(1, 14):
        total, detail = 0.0, []
        for ap in aps:
            gap = abs(freq(ap["ch"]) - freq(ch))
            ov = 20 if gap == 0 else max(0, 20 - gap)
            if ov <= 0:
                continue
            total += (ap["sig"] / 100.0) ** 2 * (ov / 20.0)
            detail.append((ap["ssid"], ap["ch"], ap["sig"], ov))
        rows.append({"ch": ch, "score": round(total, 2), "detail": detail})
    rows.sort(key=lambda r: r["score"])
    return rows


# ---------------- LTE 频段 ----------------

def set_and_settle(dev, band):
    """锁定频段并等待重新注册。"""
    dev.set_band(band)
    time.sleep(SETTLE_SEC)
    return ping_stats(6)


def measure_band(dev, band, rounds=3):
    """单频段多轮实测，取中位数，同时保留离散度用于判断可信程度。

    单轮容易撞上瞬时限速或基站调度抖动，所以至少 3 轮；
    中位数抗异常值，min/max 用来看波动大不大。
    """
    st = set_and_settle(dev, band)
    if st["avg"] is None or st["loss"] == 100:
        return {"band": band, "ok": False, "mbps": 0, "mbps_all": [],
                "mbps_min": 0, "mbps_max": 0, "avg": None, "max": None,
                "loss": 100, "rounds": 0}

    samples = []
    for i in range(rounds):
        mbps = throughput(4, 8)
        p = ping_stats(10)
        samples.append((mbps, p["avg"], p["max"], p["loss"]))
        if i < rounds - 1:
            time.sleep(1.5)

    mbps_list = [s[0] for s in samples]
    avg_list = [s[1] for s in samples if s[1] is not None]
    max_list = [s[2] for s in samples if s[2] is not None]
    loss_list = [s[3] for s in samples if s[3] is not None]
    return {
        "band": band, "ok": True,
        "mbps": round(statistics.median(mbps_list), 1),
        "mbps_all": mbps_list,
        "mbps_min": min(mbps_list), "mbps_max": max(mbps_list),
        "avg": int(statistics.median(avg_list)) if avg_list else None,
        "max": max(max_list) if max_list else None,
        "loss": max(loss_list) if loss_list else 0,
        "rounds": len(samples),
    }


# ---------------- 状态保存 ----------------

def save_state(payload):
    STATE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return None


def append_log(entry):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="随身WiFi 自动调优")
    ap.add_argument("--apply", action="store_true", help="实际应用最优设置（默认只检测）")
    ap.add_argument("--wifi-only", action="store_true", help="只优化 WiFi 信道")
    ap.add_argument("--lte-only", action="store_true", help="只优化 LTE 频段")
    ap.add_argument("--bands", default="default", help="default / all / 逗号分隔，如 3,40,41")
    ap.add_argument("--restore", action="store_true", help="还原为不锁频段 + 自动信道")
    ap.add_argument("--host", default="192.168.0.1", help="设备管理地址")
    ap.add_argument("--rounds", type=int, default=3, help="每个频段的测量轮数（默认 3，取中位数）")
    ap.add_argument("--scan-rounds", type=int, default=5, help="信道扫描轮数（默认 5，合并并滤除幽灵 AP）")
    ap.add_argument("--user", default="admin", help="后台账号")
    ap.add_argument("--password", default="admin", help="后台密码")
    ap.add_argument("--driver", default=None, help="指定驱动名（默认自动探测）")
    ap.add_argument("--list-drivers", action="store_true", help="列出已登记的驱动")
    args = ap.parse_args()

    if args.list_drivers:
        print("可用驱动：")
        for d in list_drivers():
            print(f"  {d['name']:<12} {d['description']}")
        print("\n新增机型：参考 drivers/template.py，在 drivers/__init__.py 里登记一行。")
        return 0

    try:
        dev = detect(args.host, args.user, args.password, name=args.driver)
    except LookupError as e:
        print(f"[×] {e}")
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
        print(f"\n[1/2] 扫描周边 2.4G 信道（{args.scan_rounds} 轮合并，滤除出现不足 2 轮的幽灵 AP）…")
        aps = scan_2g(rounds=args.scan_rounds)
        print(f"      发现 {len(aps)} 个邻居 AP")
        rows = score_channels(aps)
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
            st = ping_stats(8)
            print(f"\n[2/2] LTE 当前频段：{'未锁定' if cur_band == 0 else 'BAND ' + str(cur_band)}"
                  f"  延迟 {st['avg']}ms  丢包 {st['loss']}%")
            print("      （频段实测需要逐个切换，会断网，因此只在 --apply 时执行）")
            report["lte"] = {"current": cur_band, "probed": False}
        else:
            if not caps.get("can_lock_band"):
                print("\n[2/2] 该机型不支持锁定频段，跳过（只优化 WiFi）。")
                report["lte"] = {"supported": False}
            else:
                bands = (caps["bands"] if args.bands == "all"
                         else [int(x) for x in args.bands.split(",")] if args.bands != "default"
                         else [b for b in CANDIDATE_BANDS if b in caps["bands"]] or caps["bands"][:2])
                per = SETTLE_SEC + args.rounds * 18 + 5
                print(f"\n[2/2] 实测 LTE 频段 {bands}（每频段 {args.rounds} 轮取中位数，"
                      f"约 {per} 秒/个，期间会短暂断网）…")
                results = []
                for b in bands:
                    r = measure_band(dev, b, rounds=args.rounds)
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
    do_ch = (caps.get("can_set_channel", True) and best_ch is not None and best_ch != cur_ch and
             report["wifi"]["cur_score"] - report["wifi"]["best_score"] >= MIN_CHANNEL_GAIN)
    do_band = (best_band is not None and best_band != cur_band)
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
        chk = ping_stats(6)
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
