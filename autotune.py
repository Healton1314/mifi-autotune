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

# 版本号：手机上排查「我装的到底是哪一版」全靠它（mifi --version）。
# 改功能就往上加，别让两个不同的行为共用一个号。
__version__ = "0.5.1"

SETTLE_SEC = 26                     # 换频段后等待重新注册
MIN_CHANNEL_GAIN = 0.30             # 信道评分改善阈值，低于此不改
MIN_BAND_GAIN_MBPS = 3              # 频段带宽改善阈值
STABILITY_BEST_RATIO = 1.6          # 候选频段「最大/最小」≤1.6 倍才算稳

# 「自动裁决」的取向（2026-10-29 用户定的规则）：
#   速度强太多 → 选快的；速度差不多 → 选稳的；稳定性差不多 → 选快的
SPEED_DECISIVE_RATIO = 1.5          # 用「最差一轮」比，快 50% 以上算强太多
SPEED_SIMILAR_RATIO = 0.9           # 中位数 ≥ 当前的 90% 视为「速度差不多」
STABILITY_DECISIVE_RATIO = 1.4      # 波动倍数好 40% 以上算「明显更稳」
STABILITY_SIMILAR_RATIO = 1.25      # 波动倍数差 25% 以内算「稳定性差不多」
SPEED_FLOOR_MBPS = 3                # 最优频段都低于这个值，说明网络太差、结论不可信
MAX_USABLE_LOSS = 20                # 丢包 ≥20%：带宽数字再好看也不能选
MAX_USABLE_PING_MS = 800            # 延迟 ≥800ms：同上
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

LOG_KEEP = 1                        # 运行记录默认只留最近一次（0 = 写完就删）

DEFAULT_CONFIG = {
    "own_ssid": "",                 # 自己设备的 WiFi 名，用于从干扰源里排除
    "speed_url": probe.DEFAULT_SPEED_URL,
    "ping_host": probe.DEFAULT_PING_HOST,
    "candidate_bands": [3, 40],     # 仅 --bands preferred 时用来缩小范围
    "log_keep": LOG_KEEP,           # 想留更多历史就改成 5、10；0 = 不保存
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


def _log_keep():
    """运行记录保留几条：环境变量 > config.json > 默认 1。"""
    raw = os.environ.get("MIFI_LOG_KEEP")
    if raw is None or str(raw).strip() == "":
        raw = load_config().get("log_keep", LOG_KEEP)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return LOG_KEEP


def append_log(entry):
    """写一条运行记录，并只保留最近 log_keep 条（默认 1 条 = 上一次）。

    为什么不是纯追加：手机上每跑一次体检就多一份完整报告，跑十次就是十份
    堆在那儿；用户要的是「保留最近一次」，不是一本流水账。
    裁剪用「写临时文件 + 原子替换」，避免写到一半断电把仅有的记录也弄坏。
    """
    keep = _log_keep()
    if keep <= 0:
        try:
            LOG.unlink()
        except OSError:
            pass
        return

    lines = []
    if LOG.exists():
        try:
            lines = [ln for ln in LOG.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            lines = []
    lines.append(json.dumps(entry, ensure_ascii=False))
    if len(lines) > keep:
        lines = lines[-keep:]

    tmp = LOG.with_name(LOG.name + ".tmp")
    try:
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, LOG)
    except OSError:
        # 裁剪失败不能让体检白跑：退回普通追加
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def clean_run_files():
    """清掉运行产物（保留 config.json 和面板令牌）。

    手机上只想留「上一次」，历史残留（旧报告、缓存）留着只会占地方、
    还会让 `ls` 里一堆 .pyc 看着很乱。
    """
    removed = []
    for p in (PLAN, STATE, LOG):
        try:
            if p.exists():
                p.unlink()
                removed.append(p.name)
        except OSError:
            pass
    for cache in HERE.rglob("__pycache__"):
        try:
            shutil.rmtree(cache)
            removed.append(str(cache.relative_to(HERE)))
        except OSError:
            pass
    return removed


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


SIG_RSRP_WEAK = -110     # 低于这个：基本没法用（覆盖不足）
SIG_RSRP_OK = -100       # 高于这个：覆盖够用
SIG_SINR_BAD = 3         # 低于等于这个：干扰很重
SIG_SINR_OK = 10         # 高于等于这个：信号干净


def _num(v):
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or ""))
    return float(m.group()) if m else None


def diagnose_signal(sig):
    """按 RSRP / SINR 判断「换频段到底有没有用」。

    为什么要单独诊断：用户看到「本次不改动」时无从判断是「工具测不准」
    还是「环境真没救」。实测（2026-10-29）同一位置隔一小时，RSRP 从 -118dBm
    回到 -94dBm，两种情形下换频段的收益完全不同，必须区分：

      · 覆盖不足（RSRP ≤ -110）  → 所有频段都不会好，换频段救不了，挪位置才行
      · 干扰主导（SINR ≤ 3）     → 覆盖够但信号脏，换频段成功率高
      · 两者都好                 → 环境本来不差，切换收益有限

    返回 None 表示设备没报出可解析的信号值（不能猜）。
    """
    rsrp = _num((sig or {}).get("rsrp"))
    if rsrp is None or rsrp >= 0:       # 0 / 正数都是"无服务"的写法
        return None
    sinr = _num((sig or {}).get("sinr"))
    head = f"RSRP {rsrp:g}dBm" + (f"、SINR {sinr:g}dB" if sinr is not None else "")

    if rsrp <= SIG_RSRP_WEAK:
        return {"level": "weak", "text": f"{head} —— 覆盖不足",
                "advice": "这个强度换频段救不了（哪个频段都会很差）。"
                          "把设备挪到窗边、抬高、避开金属和承重墙，比任何软件调优都有效。"}
    if sinr is not None and sinr <= SIG_SINR_BAD:
        # RSRP -103dBm 这种「偏弱」不能说成「覆盖够」—— 实跑出现过
        # 「RSRP -103dBm、SINR -1dB —— 覆盖够但干扰重」，与数值不符。
        weak_cover = rsrp < SIG_RSRP_OK
        return {"level": "noisy",
                "text": f"{head} —— {'覆盖偏弱' if weak_cover else '覆盖够'}但干扰重",
                "advice": ("属于「干扰主导」：这类环境下换频段收益通常明显，"
                           "如果下面给出了切换建议，值得照做。"
                           + ("另外覆盖本身也偏弱，位置能改善就一起改善。" if weak_cover else ""))}
    if rsrp >= SIG_RSRP_OK and (sinr is None or sinr >= SIG_SINR_OK):
        return {"level": "good", "text": f"{head} —— 信号良好",
                "advice": "环境本来就不差，切换频段的收益有限，别指望大幅提速。"}
    return {"level": "ok", "text": head,
            "advice": "信号中等，切换效果取决于目标频段当时的负载。"}


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


def restore_band(dev, band, tries=3, poll=45, step=5):
    """把设备切回原频段，然后**耐心等它重新注册**到能读状态为止。

    为什么不能「发一次 POST，睡 26 秒，读一次」：
    实测（2026-10-29）在无覆盖频段上，设备是**在线**的、也照常发认证挑战，
    但响应体是空的 —— 也就是切回好频段之后，它需要一段时间重新注册才会重新
    吐 XML。这期间读状态会失败，但设备本身没问题。固定等一次就判定失败，
    会把「还在恢复」误判成「还原失败」，也让最后一行结论不可信。
    """
    last = None
    for i in range(tries):
        try:
            st = dev.get_state()
            if str(st.get("band") or "") == str(band or ""):
                return True, st
        except Exception as e:  # noqa: BLE001
            last = e            # 读不到就继续往下发指令，不要卡在这里

        try:
            dev.set_band(band)   # 实测：状态读不出时，这条 POST 依然能改频段
        except Exception as e:   # noqa: BLE001
            last = e
            if i < tries - 1:
                print(f"      [!] 第 {i + 1} 次还原没成功（{type(e).__name__}），稍后重试…")
                time.sleep(min(step * (i + 1), 15))
                continue

        # 发出去了，等它重新注册（这段时间读不到状态是正常的）
        deadline = time.time() + poll
        while time.time() < deadline:
            time.sleep(step)
            try:
                st = dev.get_state()
            except Exception as e:  # noqa: BLE001
                last = e
                continue
            if str(st.get("band") or "") == str(band or ""):
                return True, st
        if i < tries - 1:
            print(f"      [!] 第 {i + 1} 次还原后仍读不到状态，继续等…")
    if last is not None:
        print(f"      （最后一次错误：{type(last).__name__}: {last}）")
    return False, None


def measure_band(dev, band, rounds=3, speed_url=None, ping_host=None):
    """单频段多轮实测，取中位数，同时保留离散度用于判断可信程度。

    无服务的频段会在 wait_for_service 阶段就被判定并跳过，
    不再白等固定的 26 秒，也不会去跑根本跑不动的测速。

    任何一步失败（设备切频段瞬间失联、写频段被拒、测速异常）都只让**这一个
    频段**判为不可用，绝不允许异常冒出去把整轮体检打断 —— 否则设备会被留在
    半途的频段上（实测踩过：停在 BAND 8，丢包 60%）。
    """
    empty = {"band": band, "ok": False, "mbps": 0, "mbps_all": [],
             "mbps_min": 0, "mbps_max": 0, "avg": None, "max": None,
             "jitter": None, "loss": 100, "rounds": 0}

    try:
        dev.set_band(band)
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        return dict(empty, reason="device_error", error=f"{type(e).__name__}: {e}")

    try:
        status, waited = wait_for_service(dev, band)
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        return dict(empty, reason="device_error", error=f"{type(e).__name__}: {e}")

    if status != "ok":
        return dict(empty, reason=status, waited=waited)

    # 注册成功 ≠ 数据通道就绪。实测（2026-10-29 手机端）：BAND 40 在 4.6 秒就被判
    # 「有信号但数据不通」，而它实际能跑 27~31 Mbps —— 刚注册完那一两秒 ping 全丢，
    # 拿一次结果就下结论会漏掉真正更快的频段。这里多确认几次再判死。
    st = {"avg": None, "loss": 100}
    for attempt in range(4):
        try:
            st = probe.ping_stats(6, ping_host)
        except Exception:  # noqa: BLE001
            st = {"avg": None, "loss": 100}
        if st.get("avg") is not None and st.get("loss") != 100:
            break
        if attempt < 3:
            time.sleep(5)
    if st.get("avg") is None or st.get("loss") == 100:
        return dict(empty, reason="no_data", waited=waited)

    samples = []
    # 注册完成不等于数据通道就绪：实测 ping 已经通了、下载还拿不到字节，
    # 这时候测出来就是 0 Mbps，会被误判成「该频段没有数据能力」。
    # 给 3 秒缓冲，再开始正式测量。
    time.sleep(3)
    candidates = [speed_url] + [u for u in probe.SPEED_CANDIDATES if u != speed_url]
    for i in range(rounds):
        try:
            mbps, _used = probe.throughput_any(candidates, 4, 8)
        except Exception:  # noqa: BLE001
            mbps = 0.0
        p = probe.ping_stats(10, ping_host)
        samples.append((mbps, p["avg"], p["max"], p["jitter"], p["loss"]))
        if i < rounds - 1:
            time.sleep(1.5)

    # 全部轮次都测不出速度，但 ping 是通的 → 是测速源/出口的问题，不是频段差。
    # 这种情况必须单独标出来：拿 0 Mbps 去和别的频段比，会得出完全错误的结论。
    if samples and all(s[0] <= 0 for s in samples):
        return dict(empty, reason="speed_fail", waited=waited)

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

def _band_label(band, serving=None):
    """频段的人类可读标签。

    band=0 表示「没锁频段（自动）」，但用户真正关心的是**它此刻驻留在哪个频段**，
    所以带上 serving（来自设备 status1 的 lte_band）。不带上就会出现「频段 自动
    → 最优 BAND 40」这种看不出提升多少的句子。
    """
    if band:
        return f"BAND {band}"
    if serving:
        return f"自动（当前 BAND {serving}）"
    return "自动"


def _serving_band(st):
    """设备当前**实际驻留**的频段。

    get_state()["band"] 是「锁定值」，0 表示自动；一台没锁过频段的设备（出厂态、
    或刚 --restore 完）拿到的永远是 0。这时如果直接拿 0 去和实测结果比，就永远
    比不出「现在多少 → 能到多少」，用户跑了 5 分钟只得到一句「未测」。
    真实驻留频段在 status1 的 lte_band 里，这里兜住它。
    """
    if st.get("band"):
        return st["band"]
    sig = st.get("signal") or {}
    m = re.search(r"\d+", str(sig.get("band") or ""))
    band = int(m.group()) if m else 0
    return band or None


def quality_issue(res):
    """这个频段算不算「能用的」？返回问题描述（空串 = 干净可用）。

    为什么不能只看带宽：2026-10-29 手机实测里，BAND 39 丢包 90%、延迟 1068ms，
    却照样测出 13.4 Mbps。那天它带宽不最高所以没被选上；但只要它带宽高一点点，
    就会被推荐成「最优」—— 而它连网页都打不开。带宽好看 ≠ 能用。
    """
    if not res.get("ok"):
        return ""
    issues = []
    loss = res.get("loss") or 0
    if loss >= MAX_USABLE_LOSS:
        issues.append(f"丢包 {loss}%")
    ping = res.get("avg")
    if ping is not None and ping >= MAX_USABLE_PING_MS:
        issues.append(f"延迟 {ping}ms")
    return "、".join(issues)


def bands_worth_comparing(lte):
    """能参与「最优」评选的频段：测得出来 + 质量可接受。

    丢包 90% 的频段即使带宽数字漂亮也不能选，否则会把人推荐到打不开网页的网络里。
    但如果所有能用的都质量差，就原样返回 —— 不能因此得出「没有可用频段」的假结论，
    那种情况该由 assess_reliability 说「这次结论不可信」。
    """
    ok = [r for r in lte if r.get("ok")]
    clean = [r for r in ok if not quality_issue(r)]
    return clean or ok


def _sample_ratio(res):
    """多轮「最大/最小」= 波动倍数。样本不足 2 条时返回 None（无法判断稳不稳）。"""
    vs = [x for x in (res.get("mbps_all") or []) if x > 0]
    return (max(vs) / min(vs)) if len(vs) >= 2 else None


def _cons_mbps(res):
    """保守值：多轮取最小（最差那一轮），只有一轮就用它本身。

    判断「谁更强」不能拿峰值比 —— 峰值是运气，最差那一轮才是你真正会遇到的速度。
    """
    vs = [x for x in (res.get("mbps_all") or []) if x > 0]
    if len(vs) >= 2:
        return min(vs)
    return res.get("mbps")


def _gain_enough(cur_res, cand):
    """提升够不够大：绝对差 ≥3 Mbps，或者保底（最差一轮）快 1.5 倍以上。

    为什么不能只看绝对差：实测（2026-10-29 手机，信号很差那一轮）BAND 3 只有
    2.6 Mbps，BAND 8 是 5.1 —— 慢了近一倍，可绝对差只有 2.5 Mbps，卡在 3 Mbps
    阈值下面。于是「按速度」说「不到阈值，不值得切」，而「自动裁决」同一份数据
    说「强太多，切」—— 两个结论并排印出来，用户只会觉得这工具在胡说。
    现在三条线共用这一个判定。相对口径一律用最差一轮比，不拿峰值吹牛。
    """
    gain = (cand.get("mbps") or 0) - (cur_res.get("mbps") or 0)
    if gain >= MIN_BAND_GAIN_MBPS:
        return True, "abs"
    cur_cons, cand_cons = _cons_mbps(cur_res), _cons_mbps(cand)
    if cur_cons and cand_cons and cand_cons / cur_cons >= SPEED_DECISIVE_RATIO:
        return True, "ratio"
    return False, "none"


def decide_band(cur_res, candidates, cmp_band):
    """自动裁决换哪个频段。返回 (option 或 None, 不换的原因)。

    规则（用户 2026-10-29 定的）：
      1) 一方速度强太多   → 选快的（哪怕它稍微不稳）
      2) 速度差不多       → 选更稳的
      3) 稳定性差不多     → 选更快的
    第 1 条刻意用「最差一轮」比较：拿峰值去说「强太多」是在骗人。
    """
    if not cur_res or not cur_res.get("ok"):
        return None, "当前频段没有可用数据"
    cands = [r for r in (candidates or [])
             if r and r.get("ok") and str(r.get("band")) != str(cmp_band)]
    if not cands:
        return None, "没有可比的其他频段"

    cur_med = cur_res.get("mbps") or 0
    cur_cons = _cons_mbps(cur_res)
    cur_ratio = _sample_ratio(cur_res)

    # 1) 速度强太多：比最差一轮
    strong = [r for r in cands
              if cur_cons and _cons_mbps(r) and _cons_mbps(r) / cur_cons >= SPEED_DECISIVE_RATIO]
    if strong:
        w = max(strong, key=lambda r: r.get("mbps") or 0)
        return {"band": w["band"], "kind": "speed",
                "reason": f"速度强太多 —— 它最差一轮 {_cons_mbps(w)} Mbps，"
                          f"现在最差一轮 {cur_cons} Mbps",
                "gain": round((w.get("mbps") or 0) - cur_med, 1)}, ""

    # 2) 速度差不多 → 选明显更稳的（与「按稳定」那条线共用同一份判定）
    st = stability_option(cur_res, cands, cmp_band)
    if st:
        return st, ""

    # 3) 稳定性差不多（双方都不乱跳）→ 选更快的，仍要过阈值
    w = max(cands, key=lambda r: r.get("mbps") or 0)
    w_ratio = _sample_ratio(w)
    both_steady = ((cur_ratio is None or cur_ratio <= STABILITY_BEST_RATIO)
                   and (w_ratio is None or w_ratio <= STABILITY_BEST_RATIO))
    gain = (w.get("mbps") or 0) - cur_med
    enough, _basis = _gain_enough(cur_res, w)
    if both_steady and enough:
        return {"band": w["band"], "kind": "speed",
                "reason": f"稳定性差不多（波动 {cur_ratio or 1:.1f} 倍 vs {w_ratio or 1:.1f} 倍），"
                          f"但它快 {gain:.1f} Mbps",
                "gain": round(gain, 1)}, ""
    return None, "速度和稳定性都差不多，不值得为它断一次网"


def speed_option(cur_res, best_res, cmp_band, caveats):
    """「按速度换」的建议：只有数据可信、且提升够大，才给。

    这里和「自动裁决」共用 _gain_enough —— 两条线对「够不够大」必须是同一个
    标准，否则会出现「A 说不到阈值、自动却切了」的自相矛盾输出。
    """
    if caveats or not best_res or best_res.get("band") is None:
        return None
    if str(best_res["band"]) == str(cmp_band):
        return None
    if not cur_res or not cur_res.get("ok"):
        return None
    enough, basis = _gain_enough(cur_res, best_res)
    if not enough:
        return None
    gain = best_res["mbps"] - cur_res["mbps"]
    return {"band": best_res["band"], "gain": round(gain, 1), "basis": basis,
            "cons_now": _cons_mbps(cur_res), "cons_best": _cons_mbps(best_res),
            "gain_pct": (round(gain / cur_res["mbps"] * 100) if cur_res.get("mbps") else None)}


def stability_option(cur_res, candidates, cmp_band):
    """「按稳定换」的建议：速度差不多、但另一个频段明显更稳时给。

    为什么需要这条：实测（2026-10-29 本机）BAND 3 是 28.0~49.7（1.78 倍），
    BAND 40 是 33.7~41.1（1.22 倍），中位数只差 4%。按速度那条线会说「现在最快」、
    什么都不做；但对实际体验来说，一个会在 28~50 之间来回的链路，比稳在 37 的差得多。
    所以：只承诺稳定，不承诺更快，也不给提速百分比。

    candidates 传**所有**可用频段（不是只有最快的那个）：最快的不一定最稳。
    """
    if not cur_res or not cur_res.get("ok"):
        return None
    cur_ratio = _sample_ratio(cur_res)
    if cur_ratio is None:
        return None                     # 当前只测了一轮，不谈稳不稳
    cur_med = cur_res.get("mbps") or 0

    best = None
    for r in candidates or []:
        if not r or not r.get("ok") or str(r.get("band")) == str(cmp_band):
            continue
        if (r.get("mbps") or 0) < cur_med * SPEED_SIMILAR_RATIO:
            continue                    # 明显更慢，换过去是降速
        rr = _sample_ratio(r)
        if rr is None or rr > STABILITY_BEST_RATIO:
            continue                    # 样本不够 / 候选自己也乱跳
        if cur_ratio / rr < STABILITY_DECISIVE_RATIO:
            continue                    # 稳定性差得不够明显，不值得为它断网
        if best is None or (r.get("mbps") or 0) > (best.get("mbps") or 0):
            best = r
    if best is None:
        return None
    bs = [x for x in best["mbps_all"] if x > 0]
    cs = [x for x in cur_res["mbps_all"] if x > 0]
    return {"band": best["band"], "kind": "stability",
            "reason": f"速度差不多（{best.get('mbps')} vs {cur_med} Mbps），"
                      f"但它稳得多（{bs} vs 现在 {cs}）",
            "cur_range": cs, "best_range": bs,
            "cur_ratio": round(cur_ratio, 1),
            "best_ratio": round(max(bs) / min(bs), 1),
            "delta": round((best.get("mbps") or 0) - cur_med, 1)}


def describe_unmeasured(lte):
    """「有信号但这次没测出结果」的频段，用一句话说清楚。

    这类频段**不等于比当前慢**，只是没测成。不点出来，用户会以为所有频段
    都比过了，从而完全相信「当前已是最优」。实测（2026-10-29 手机端）：
    BAND 40 被 4.6 秒判成「数据不通」直接跳过，而它实际能跑 27~31 Mbps。
    """
    reason_cn = {"no_data": "数据不通", "speed_fail": "测速失败",
                 "timeout": "注册超时", "device_error": "通信中断"}
    skipped = [r for r in lte
               if not r.get("ok") and r.get("reason") not in (None, "no_signal")]
    if not skipped:
        return ""
    names = "、".join(f"BAND {r.get('band')}（{reason_cn.get(r.get('reason'), '不可用')}）"
                      for r in skipped)
    return f"另有 {names}没测出结果，所以「最优」只在这些能测出来的频段里成立。"


def assess_reliability(lte, cur_res, best_res):
    """这次测量的结论能不能信？返回不可信的原因列表（空 = 可信）。

    为什么要这一层：网络很差或测速源抽风时，单轮波动的幅度会盖过频段之间的
    真实差异。这时候给出「切过去能快 20Mbps」是假的，用户白断一次网还更差。
    宁可明确说「测不准」。
    """
    caveats = []
    if cur_res and not cur_res.get("ok") and cur_res.get("reason") == "speed_fail":
        caveats.append("当前频段测不出速度（测速源不可用，或该频段几乎没有数据能力）")
    if best_res and best_res.get("mbps") is not None and best_res["mbps"] < SPEED_FLOOR_MBPS:
        caveats.append(f"最优频段也只有 {best_res['mbps']} Mbps，低于 {SPEED_FLOOR_MBPS} Mbps 参考线")
    if best_res and quality_issue(best_res):
        caveats.append(f"被选为最优的 BAND {best_res['band']} 本身质量不过关"
                       f"（{quality_issue(best_res)}），说明当前网络整体状态不好")
    for r in lte:
        samples = r.get("mbps_all") or []
        if r.get("ok") and len(samples) >= 2 and min(samples) > 0 \
                and max(samples) / min(samples) >= 4:
            caveats.append(f"BAND {r['band']} 多轮结果波动过大"
                           f"（{samples}，最大是最小的 {max(samples) / min(samples):.1f} 倍）")
            break
    return caveats


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


def measure_all_bands(dev, bands, rounds, cur_band, cur_label,
                      speed_url=None, ping_host=None, contest_band=None):
    """逐个实测频段，并**保证**无论怎么退出都把设备切回 cur_band。

    为什么抽成一个函数：--preview 和 --apply（--watch 触发的就是它）都要这份
    「测量 + 还原」逻辑，以前各写一遍，结果 --apply 那份漏了 try/finally ——
    中途崩溃就把设备留在没人覆盖的频段上（真实事故：停在 BAND 8、丢包 60%）。
    只有一份实现，才不会再次漂移。

    contest_band 给定时，粗测完再对它和最优各补 1 轮（「决赛」），
    因为两者的差值正是决策依据，值得多花 90 秒把单轮波动压掉。
    """
    results = []
    t0 = time.time()
    try:
        for idx, b in enumerate(bands, 1):
            print(f"\n      [{idx}/{len(bands)}] BAND {b} 测试中…")
            r = measure_band(dev, b, rounds=rounds, speed_url=speed_url, ping_host=ping_host)
            results.append(r)
            if r["ok"]:
                jit = f"{r['jitter']}ms" if r["jitter"] is not None else "—"
                print(f"      {_band_label(b):<8} {r['mbps']:>5} Mbps  "
                      f"延迟 {r['avg']}ms  抖动 {jit}  丢包 {r['loss']}%")
                bad = quality_issue(r)
                if bad:
                    print(f"       [!] {bad} —— 质量不可接受，不会被选为最优"
                          f"（决赛时只当对照，用来算「提升多少」）")
            else:
                why = {"no_signal": "无信号（当地没这个频段）",
                       "timeout": "注册超时",
                       "no_data": "有信号但数据不通",
                       "speed_fail": "有信号但测不出速度（测速源不可用，或该频段几无数据能力）",
                       "device_error": "设备通信中断"}.get(r.get("reason"), "不可用")
                print(f"      {_band_label(b):<8} {why}  已跳过（{r.get('waited', '?')}s）")
                if r.get("error"):
                    print(f"      原因：{r['error']}")
            left = len(bands) - idx

            # 这个频段测失败（没覆盖 / 读不到状态）时，先退回原频段再测下一个。
            # 2026-10-29 实测：切到 BAND 38（当地无覆盖）后设备仍在线、仍发认证挑战，
            # 但响应体是空的 —— 状态读不出来，于是后面每个频段都被判「通信中断」，
            # 连自动还原都失败，用户被留在没信号的频段上。
            # 从健康状态出发测下一个频段，就不会被上一个频段的坏状态连累。
            if not r["ok"] and str(b) != str(cur_band) and left:
                print("      退回原频段，避免在坏状态上继续切…")
                ok_r, _ = restore_band(dev, cur_band, tries=2, poll=45)
                if not ok_r:
                    print("      [!] 退不回去，剩下的频段先不测了")
                    break
            if left:
                used = (time.time() - t0) / idx
                print(f"      （还剩 {left} 个，预计 {used * left / 60:.1f} 分钟）")

        usable = bands_worth_comparing(results)
        if contest_band and usable and len(bands) > 2:
            # 决赛前先把设备从「上一个频段」拉回健康状态。
            # 实测（连续两轮，2026-10-29）：BAND 41（当地无覆盖）刚跳过，紧接着决赛
            # BAND 3，两轮复测都只有 8.7 / 9.9 Mbps，而第一轮是 40.1 / 46.6 Mbps ——
            # 同一个频段、同样 20 秒，差 4~5 倍。原因不是频段本身，是设备刚从死频段
            # 切回来、数据通路还没恢复；measure_band 内部 3 秒预热不够。
            # 决赛是决策依据，必须在设备健康时测，否则整轮结论会被判「波动过大」而作废。
            print("\n[决赛] 先把设备切回原频段并等它恢复…")
            ok_r, _ = restore_band(dev, cur_band)
            if ok_r:
                time.sleep(5)
                print("      已恢复，开始复测")
            else:
                print("      [!] 没切回去，复测结果可能偏低")

            usable.sort(key=lambda r: (-r["mbps"], r["avg"] or 999))
            # 候选和「当前」要分开说。之前把两者塞进一个列表打印「各补 1 轮确认」，
            # 而当前频段刚被上面判过「质量不可接受、不参与最优评选」——
            # 用户当场就问：说好不参评，怎么又进决赛了？（2026-10-29 实机截图）
            # 真相是：它确实还会被复测一次，但它不参评，只是给「提升多少」当基准。
            pairs = [(usable[0]["band"], "候选")]
            if contest_band is not None and str(contest_band) != str(usable[0]["band"]):
                pairs.append((contest_band, "当前·只作对照，不参评"))
            print("\n[决赛] 补 1 轮确认：" +
                  "、".join(f"BAND {b}（{role}）" for b, role in pairs))
            by_band = {r["band"]: r for r in results}
            for b, role in pairs:
                prev = by_band.get(b)
                if not prev or not prev["ok"]:
                    continue
                extra = measure_band(dev, b, rounds=1, speed_url=speed_url, ping_host=ping_host)
                if not extra.get("ok"):
                    # 复测失败必须说出来。原来这里没有 else，名单里列了它、
                    # 后面一行结果都没有 —— 用户会以为「测了但没变化」，实际是没测成。
                    why = {"no_data": "数据不通", "speed_fail": "测不出速度",
                           "timeout": "注册超时", "no_signal": "无信号",
                           "device_error": "设备通信中断"}.get(extra.get("reason"), "不可用")
                    print(f"      {_band_label(b):<8} 复测没测成（{why}），"
                          f"仍用粗测的 {prev['mbps']} Mbps")
                    continue
                merged = sorted(prev["mbps_all"] + extra["mbps_all"])
                prev["mbps"] = round(statistics.median(merged), 1)
                prev["mbps_all"] = merged
                prev["mbps_min"] = min(merged)
                prev["mbps_max"] = max(merged)
                prev["rounds"] = len(merged)
                print(f"      {_band_label(b):<8} 复测后 {prev['mbps']} Mbps"
                      f"（{merged}）")
    except KeyboardInterrupt:
        print("\n[!] 已中断，马上切回原频段…")
    finally:
        # 无论正常结束、抛异常还是 Ctrl+C，都必须还原。
        # 这条是血的教训：之前在测 BAND 8 时驱动超时崩溃，整个进程退出，
        # 设备被留在 BAND 8（丢包 60%），用户当场上不了网。
        print(f"\n[还原] 切回 {cur_label} …")
        ok, _st = restore_band(dev, cur_band)
        if ok:
            print("      已切回")
        else:
            print(f"      [!] 自动还原失败。请手动在设备后台把频段改回 {cur_label}")
            print("          设备可能刚重启射频，等 1 分钟再试 `mifi --restore` 也行。")
    return results


def run_preview(dev, caps, st0, args, cfg, speed_url, ping_host):
    cur_band, cur_ch = st0["band"], st0["channel"]
    serving = _serving_band(st0)
    # 用于「现在多少」的对比基准：锁了频段就是锁定值；没锁（0=自动）就用设备
    # 实际驻留的频段，否则整轮测完也只能说「未测」，用户拿不到提升数字。
    cmp_band = cur_band or serving
    cur_label = _band_label(cur_band, serving)
    rounds = args.rounds or 1          # 体检默认 1 轮粗筛，精度靠后面的「决赛」补
    plan = {"ts": datetime.now().isoformat(),
            "device": getattr(dev, "description", dev.name),
            "current": {"band": cur_band, "channel": cur_ch, "serving_band": serving}}

    # ---- 一、WiFi 信道 ----
    wifi = None
    if not args.lte_only:
        own = args.ssid or cfg.get("own_ssid") or scanner.connected_ssid() or st0.get("ssid")
        print(f"\n[1/2] 扫描周边 2.4G 信道（{args.scan_rounds} 轮合并）…")
        aps = scanner.scan_2g(rounds=args.scan_rounds, own_ssid=own)
        if not aps:
            # 一次空结果不等于「扫不了」：netsh/nmcli 的结果是缓存的，刚开机、
            # 网卡刚切过信道、或系统正在后台扫描时都可能一轮全空（本机实测遇到过）。
            # 直接跳过整个信道部分，用户看到的就是「这次没有信道结果」——
            # 所以先重试，还不行就把原始诊断摊开，而不是猜一句「语言不支持」。
            print("      [!] 这一轮一个都没扫到，等 3 秒重试一次…")
            time.sleep(3)
            aps = scanner.scan_2g(rounds=max(3, args.scan_rounds - 2), own_ssid=own)
        if aps:
            rows = scanner.score_channels(aps, caps.get("channels") or range(1, 14))
            ch_auto = not cur_ch            # 0/None = 设备处于「自动信道」
            cur_score = (None if ch_auto
                         else next((r["score"] for r in rows if r["ch"] == cur_ch), None))
            best = scanner.pick_best_channel(rows)
            wifi = {"current": cur_ch, "auto": ch_auto, "cur_score": cur_score,
                    "best": best["ch"], "best_score": best["score"],
                    "neighbors": len(aps)}
            print(f"      {len(aps)} 个邻居 AP")
            if ch_auto:
                print("      当前信道：自动（设备自己选，无法比较「现在有多差」）")
                print(f"      扫描最干净的是 {best['ch']} 号（干扰分 {best['score']}），仅供参考")
            else:
                print(f"      当前信道 {cur_ch} 干扰分 {cur_score} ｜ "
                      f"最优 {best['ch']} 干扰分 {best['score']}")
        else:
            print(f"      [!] 重试也扫不到，本次跳过信道部分：{scanner.scan_failure_hint()}")
            print("          原始信息（排查用，复制这一段就能定位问题）：")
            print(scanner.scan_diagnose())

    # ---- 二、LTE 频段 ----
    lte = []
    best_band = None
    if not args.wifi_only and caps.get("can_lock_band"):
        bands = resolve_bands(caps, args, cfg)
        if not bands:
            print("\n[2/2] 驱动没有声明可用频段，跳过频段实测。")
        else:
            per = SETTLE_SEC + rounds * 20 + 8
            print(f"\n[2/2] 设备声明支持 {len(bands)} 个频段：{bands}")
            print(f"      逐个实测（每段 {rounds} 轮，有信号的约 {per} 秒，"
                  f"没信号的会提前跳过；预计 {per * len(bands) / 60:.0f} 分钟以内）")
            print("      期间会短暂断网，全部测完自动切回原频段…")

        lte = measure_all_bands(dev, bands, rounds, cur_band, cur_label,
                                speed_url, ping_host, contest_band=cmp_band)

        usable = bands_worth_comparing(lte)
        if usable:
            usable.sort(key=lambda r: (-r["mbps"], r["avg"] or 999))
            best_band = usable[0]["band"]

    # ---- 三、结论 ----
    cur_res = next((r for r in lte if r["band"] == cmp_band), None)
    best_res = next((r for r in lte if r["band"] == best_band), None)
    cur_mbps = cur_res["mbps"] if cur_res and cur_res["ok"] else None
    best_mbps = best_res["mbps"] if best_res else None
    band_gain = (best_mbps - cur_mbps) if (cur_mbps is not None and best_mbps is not None) else None

    # 可信度检查：网络很差、或测速源抽风时，单轮波动会盖过频段之间的真实差异。
    # 这种时候给出「建议切换」是害人 —— 宁可说「测不准」，也不要让人白折腾一次断网。
    caveats = assess_reliability(lte, cur_res, best_res)
    reliable = not caveats

    # 两条独立的建议线，用户自己选走哪条：
    #   speed     —— 只认「更快」，数据不可信时不出建议（保守）
    #   stability —— 只认「更稳」，当前频段自己在乱跳、另有一个稳定且不慢的频段时给建议
    # 两者通常互斥（乱跳本身就会让速度结论不可信），所以不是二选一之后再二选一。
    # 三条建议：自动裁决（默认）+ 两条手动强制的单线
    cands = bands_worth_comparing(lte)
    auto, auto_why = decide_band(cur_res, cands, cmp_band)
    options = {
        "auto": auto,
        "speed": speed_option(cur_res, best_res, cmp_band, caveats),
        "stability": stability_option(cur_res, cands, cmp_band),
    }
    strategy = getattr(args, "strategy", None) or "auto"
    picked = options.get(strategy)
    # 「按稳定换」和「自动裁决定的稳定」都不承诺带宽，界面不能把它当提速卖点
    promise_gain = bool(picked and reliable and picked.get("kind") == "speed")

    do_band = bool(picked)
    do_ch = bool(wifi and caps.get("can_set_channel", True) and wifi["best"] != cur_ch
                 and wifi["cur_score"] is not None
                 and wifi["cur_score"] - wifi["best_score"] >= MIN_CHANNEL_GAIN)

    target = {"band": picked["band"] if picked else cur_band,
              "channel": wifi["best"] if do_ch else cur_ch}
    # 信号诊断：既写进 plan.json（面板要显示），也决定「不改动」时该怎么解释。
    diag = diagnose_signal(st0.get("signal") or {})
    plan.update({
        "target": target,
        "strategy": strategy,
        "options": options,
        "auto_reason": (picked.get("reason") if picked else auto_why),
        "reliable": reliable,
        "caveats": caveats,
        "lte": lte,
        "wifi": wifi,
        "signal": diag,
        "expected": {
            "band_from": cmp_band, "band_locked": cur_band, "band_to": target["band"],
            "mbps_now": cur_mbps, "mbps_best": best_mbps,
            # 只在「确定更快」时才给提速数字；换稳定性、或数据不可信时一律不给，
            # 免得界面把「更稳」说成「更快」。
            "mbps_gain": (round(band_gain, 1)
                          if (promise_gain and band_gain is not None) else None),
            "mbps_gain_pct": (round(band_gain / cur_mbps * 100)
                              if (promise_gain and cur_mbps) else None),
            "ping_now": cur_res["avg"] if cur_res and cur_res["ok"] else None,
            "ping_best": best_res["avg"] if best_res else None,
            "channel_from": cur_ch, "channel_to": target["channel"],
            "channel_score_from": wifi["cur_score"] if wifi else None,
            "channel_score_to": wifi["best_score"] if wifi and do_ch else None,
        },
        "changes": ([] if not do_band else
                    [f"频段 {cur_label} → {_band_label(picked['band'])}"
                     + ("（换稳定性）" if picked.get("kind") == "stability" else "")])
                   + ([f"信道 {cur_ch} → {target['channel']}"] if do_ch else []),
    })
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 56)
    print("体检结论")
    print("=" * 56)
    # 先给信号诊断：用户看到「不改动」时，需要知道是「环境真没救」还是「工具没测准」。
    if diag:
        print(f"  信号：{diag['text']}")
        print(f"        {diag['advice']}")
    if cur_mbps is not None or best_mbps is not None:
        if cur_mbps is not None:
            now_s = f"{cur_mbps} Mbps"
        elif cur_res is None:
            now_s = "未测（不在本次范围）"
        else:
            now_s = "测不出（该频段无服务）"
        best_s = f"{best_mbps} Mbps" if best_mbps is not None else "—"
        print(f"  频段：{cur_label} {now_s}"
              f"  →  最优 {_band_label(best_band) if best_band else '—'} {best_s}")

    # 自动裁决放第一行（默认就按它走），两条单线列在旁边当参考/强制用。
    # 只显示其中一条会让人以为工具「没建议」。
    # 但没测频段时（--wifi-only）这三行必须省掉：那时它会印出
    # 「按速度：现在这个频段就是最快的」这种根本没测过的话（实测踩到）。
    if not lte:
        print("  （这次没测频段，所以没有频段建议 —— 只跑了信道）")
    else:
        if options["auto"]:
            print(f"  [自动] 切到 BAND {options['auto']['band']} —— {options['auto']['reason']}")
        else:
            print(f"  [自动] 保持现状 —— {auto_why}")
        print("        规则：速度强太多选快的；速度差不多选稳的；稳定性差不多选快的")
        print(f"  [A] 按速度：{_describe_speed(options['speed'], caveats, best_band, cmp_band, band_gain)}")
        print(f"  [B] 按稳定：{_describe_stability(options['stability'], cur_res)}")
        print(f"  当前策略：{'自动' if strategy == 'auto' else ('按速度' if strategy == 'speed' else '按稳定')}"
              f"（可用 --strategy speed / stability 强制单条）")

    # 有些频段是「有信号但这次没测出结果」，它不等于「比当前慢」。
    # 不点出来，用户会以为已经比过了所有频段 —— 实际是没比成。
    note = describe_unmeasured(lte)
    if note:
        print(f"  {note}")
        print("        想更确定可以过一会儿再跑一次。")

    if wifi:
        if wifi.get("auto"):
            print(f"  信道：自动（设备自行选择，当前无固定值）"
                  f"  →  扫描最干净 {wifi['best']}（干扰分 {wifi['best_score']}）")
            print("        自动模式通常比自己钉一个信道更稳，本次不建议改动。")
        else:
            gap = (round(wifi["cur_score"] - wifi["best_score"], 2)
                   if wifi["cur_score"] is not None else None)
            pct = (round(gap / wifi["cur_score"] * 100)
                   if gap is not None and wifi["cur_score"] else None)
            # gap == 0 时「最优」其实是并列 —— 直接印「最优 10」会让人以为 10 比 13 好，
            # 实际两个信道都是 0 干扰。实测（2026-10-29 电脑）就这么印过。
            tail = ("（干扰分与当前相同，属并列）" if gap == 0
                    else f"（干扰分 {wifi['best_score']}）")
            print(f"  信道：{cur_ch}（干扰分 {wifi['cur_score']}）"
                  f"  →  最优 {wifi['best']}{tail}")
            if do_ch:
                print(f"        切换后干扰下降 {gap}"
                      f"{f'（约 {pct}%）' if pct is not None else ''}，"
                      f"主要改善重传和抖动（不承诺带宽提升）")
            elif wifi["best"] == cur_ch or gap == 0:
                print("        当前信道已经很干净，没有更优的选择，不动")
            else:
                # 原来只写「改善不足阈值 0.3」，看不出差多少 —— 用户以为没算。
                # 但 cur_score 可能是 0（这个信道本来就没人抢），这时算不出百分比，
                # 印「约 None%」比不说更糟（实测出现过）。
                pct_s = f"（约 {pct}%）" if pct is not None else ""
                print(f"        只降 {gap}{pct_s}，不到阈值 {MIN_CHANNEL_GAIN}，不值得动")
    elif not args.lte_only:
        # 扫描失败时整段会消失，用户看到的就是「这次没有信道结果」。
        # 必须在这里明写一句，否则看起来像功能坏了。
        print("  信道：这次没扫到周边 AP，未参与判断（扫描本身的原因见上面 [!] 行）")

    print("-" * 56)
    if caveats:
        print("[!] 按速度这条线，数据不足以下结论：")
        for c in caveats:
            print(f"    · {c}")
        print("    原因通常是当前信号太差（看 RSRP/SINR）或测速源不可用。")
        print("    建议：换个测速源（--speed-url）、加轮次（--rounds 3）或等信号好转后再测一次。")
        print("-" * 56)
    if plan["changes"]:
        label = {"auto": "自动裁决", "speed": "速度", "stability": "稳定"}.get(strategy, strategy)
        print(f"按「{label}」可以做的改动：")
        for c in plan["changes"]:
            print(f"  · {c}")
        print(f"\n应用方式：python autotune.py --apply-plan"
              + ("" if strategy == "auto" else f" --strategy {strategy}"))
        print("           （只想看会改什么、不真改：再加 --dry-run）")
    else:
        if not lte and wifi:
            # 只测信道时不能印「当前频段没有可用数据」—— 那是没测，不是没有。
            print("本次不改动：只测了信道，信道也没有更优的选择。")
        else:
            print(_no_change_reason(strategy, options, auto_why))
        print("（结论已存 plan.json；环境变了可以重新体检一次）")
    return 0


def _describe_speed(opt, caveats, best_band, cmp_band, gain):
    if opt:
        # 信号差的时候绝对提升很小、但相对翻倍，「+2.5 Mbps」会让人以为没啥用，
        # 所以这种情形改说「保底从多少到多少」，数字对得上用户的实际体感。
        if opt.get("basis") == "ratio" and opt.get("cons_now") and opt.get("cons_best"):
            up = opt["cons_best"] / opt["cons_now"] - 1
            return (f"BAND {opt['band']}，保底从 {opt['cons_now']:g} 提到 "
                    f"{opt['cons_best']:g} Mbps（{up:+.0%}）")
        return (f"BAND {opt['band']}，预计 {opt['gain']:+.1f} Mbps"
                + (f"（{opt['gain_pct']:+d}%）" if opt.get("gain_pct") is not None else ""))
    if caveats:
        return "数据波动/不可信，这条线不敢给建议"
    if best_band is None or str(best_band) == str(cmp_band):
        return "现在这个频段就是最快的"
    if gain is not None:
        return (f"最快也只高 {gain:.1f} Mbps，且最差一轮没到 {SPEED_DECISIVE_RATIO:g} 倍"
                f"（两个门槛：快 {MIN_BAND_GAIN_MBPS} Mbps 或 保底翻 "
                f"{SPEED_DECISIVE_RATIO:g} 倍，都不满足）")
    return "没有可比的数据"


def _describe_stability(opt, cur_res):
    if opt:
        return (f"BAND {opt['band']} —— 现在 {opt['cur_range'][0]}~{opt['cur_range'][1]}"
                f"（{opt['cur_ratio']} 倍），它 {opt['best_range'][0]}~{opt['best_range'][1]}"
                f"（{opt['best_ratio']} 倍）")
    if not cur_res or not cur_res.get("ok"):
        return "当前频段没有可用数据，比不出稳不稳"
    cur_ratio = _sample_ratio(cur_res)
    if cur_ratio is None:
        return "当前频段只测了一轮，判断不了稳不稳（换 --rounds 3 再测）"
    if cur_ratio <= STABILITY_BEST_RATIO:
        return f"当前频段本来就稳（波动 {cur_ratio:.1f} 倍），不需要为了稳去换"
    return "没找到「既稳又不会更慢」的替代频段"
    return "有频段在乱跳，但没找到既稳又不比它慢的替代"


def _no_change_reason(strategy, options, auto_why):
    """没建议时把「为什么不改」说清楚，并指出别的策略是否还有路可走。"""
    if strategy == "auto":
        head = f"本次不改动：{auto_why}。"
        extra = [k for k in ("speed", "stability") if options.get(k)]
        if extra:
            tip = "、".join("按速度" if k == "speed" else "按稳定" for k in extra)
            return head + f"\n但{tip}这条线还有建议，想强制的话加 --strategy {extra[0]}。"
        return head + "\n保持现状就好。"
    head = f"按「{'速度' if strategy == 'speed' else '稳定'}」本次不改动。"
    other = "stability" if strategy == "speed" else "speed"
    if options.get(other):
        return head + f"\n但另一条线有建议，加 --strategy {other}（或用默认的自动裁决）。"
    return head + "\n另一条线同样没有更好的选择，保持现状就好。"


def _mark_applied(plan, band, channel, result):
    """把「已应用」写回 plan.json，而不是把它删掉。

    删掉文件会让界面上的结论卡片凭空消失，用户会疑惑"我刚才那份结论去哪了"。
    保留并标注应用时间，既能回顾，也能让 UI 明确显示"已应用"。
    """
    plan["applied_at"] = datetime.now().isoformat()
    plan["applied_result"] = result
    plan["after"] = {"band": band, "channel": channel}
    try:
        PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def run_mtu_check(st0, ping_host):
    """只读检测：这条线路的真实路径 MTU 和设备的设置值对不对得上。

    为什么值得单列一项：MTU 设得比路径 MTU 大时，症状是「网页能开、下载大
    文件卡死、视频一直缓冲」。这种「半通不通」最容易被当成信号差，然后开始
    反复切频段 —— 而切频段根本治不了它。

    只报告不自动改：改 MTU 要重新拨号，断网 30~60 秒，而且属于网络层设置，
    改错了连设备后台都可能进不去，不适合跟着体检自动做。
    """
    set_mtu = int(st0.get("mtu") or 0)
    print("=" * 56)
    print("路径 MTU 检测（不分片 ping，只读，不改设备）")
    print("=" * 56)
    print(f"设备当前设置：{set_mtu or '—'} 字节")

    if probe.is_termux():
        print("\n[跳过] 手机端没有 ICMP 权限，这项只能在电脑上测。")
        print("       （和频段优化无关，手机上体检不受影响）")
        return 0

    print(f"正在二分探测到 {ping_host} 能整包通过的最大尺寸（约 10~20 秒）…")
    path_mtu, note = probe.detect_path_mtu(ping_host)

    if path_mtu is None:
        print(f"\n[?] 测不出来：{note}")
        return 0

    print(f"路径 MTU：{note}")

    if not set_mtu:
        print("\n设备没报 MTU 值，无从比较。")
        return 0
    if path_mtu >= set_mtu:
        print(f"\n[√] 设置值 {set_mtu} 不超过路径 MTU {path_mtu}，大包不会被丢，不用动。")
        return 0

    print(f"\n[!] 设置值 {set_mtu} 大于路径 MTU {path_mtu}。")
    print("    症状会是：网页能开、大文件下载卡住、视频一直缓冲。")
    print("    这种「半通不通」最容易被误判成信号差，然后反复切频段（治不了）。")
    print(f"    建议把 MTU 调到 {path_mtu}（要更保险就用 {max(576, path_mtu - 8)}）。")
    print("\n    这一步我没有替你改：改 MTU 要重新拨号，会断网 30~60 秒，")
    print("    而且改错了连设备后台都可能进不去。真要改说一声，我把写入和回滚一起做。")
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

    # 策略：命令行显式指定 > 体检时用的 > 默认按速度
    strategy = getattr(args, "strategy", None) or plan.get("strategy") or "speed"
    options = plan.get("options") or {}
    if getattr(args, "strategy", None) and options:
        # 用户换了策略：改用那条线的目标。那条线没建议就如实说，不能悄悄套用另一条。
        opt = options.get(strategy)
        if not opt:
            name = "速度" if strategy == "speed" else "稳定"
            print(f"[×] 这份结论里「按{name}」没有可用的建议。")
            other = "stability" if strategy == "speed" else "speed"
            if options.get(other):
                print(f"    另一条线有建议：加 --strategy {other}")
            else:
                print("    两条线都没有更好的选择，保持现状即可。")
            return 0
        t_band = opt.get("band", t_band)

    todo = []
    if t_band != cur_band and caps.get("can_lock_band"):
        todo.append(("band", t_band))
    if t_ch != cur_ch and caps.get("can_set_channel", True):
        todo.append(("channel", t_ch))

    if not todo:
        print("[√] 当前配置已经和体检结论一致，无需改动。")
        _mark_applied(plan, cur_band, cur_ch, "nothing_to_do")
        return 0

    print(f"按体检结论应用（策略：{'速度' if strategy == 'speed' else '稳定'}）：")
    for kind, val in todo:
        if kind == "band":
            print(f"  · 频段 {_band_label(cur_band)} → {_band_label(val)}")
        else:
            print(f"  · 信道 {cur_ch} → {val}")

    if getattr(args, "dry_run", False):
        print("\n[演练] 以上是「将会做」的改动；加了 --dry-run 所以没有碰设备。")
        print("       去掉 --dry-run 就会真的切过去（约 30~60 秒，中途断网一次）。")
        return 0

    save_state({"saved_at": datetime.now().isoformat(),
                "band": cur_band, "channel": cur_ch})

    failed = False
    try:
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
    except KeyboardInterrupt:
        print("\n[!] 已中断")
        failed = True
    except Exception as e:  # noqa: BLE001
        # 设备在切换瞬间失联时，写到一半就断了：说清楚当前可能的状态，
        # 别甩一段 traceback 让人无从下手。
        print(f"\n[×] 应用过程中设备无响应：{type(e).__name__}: {e}")
        print("    设备可能已经切换成功，也可能还停在原频段。")
        print("    等 30 秒后重跑 `python autotune.py` 看当前状态；"
              "要强制回原样用 `python autotune.py --restore`。")
        failed = True

    try:
        end = dev.get_state()
    except Exception as e:  # noqa: BLE001
        print(f"\n[×] 改动已发出，但读不回设备状态（{type(e).__name__}）。")
        print("    等 30 秒后跑 `python autotune.py` 确认实际状态。")
        return 1
    append_log({"ts": datetime.now().isoformat(), "mode": "apply-plan",
                "plan": plan.get("expected"), "before": {"band": cur_band, "channel": cur_ch},
                "after": {"band": end["band"], "channel": end["channel"]}})
    print(f"\n完成：频段 {_band_label(end['band'])} ｜ 信道 {end['channel'] or '自动'}")
    if failed:
        print("（有一项没成功，已回滚到原值，详见上面的 [!] 行）")
        _mark_applied(plan, end["band"], end["channel"], "partial_failure")
        return 1
    _mark_applied(plan, end["band"], end["channel"], "ok")
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

def _run_interactive(dev, caps, st0, args, cfg, speed_url, ping_host):
    """终端里一条命令走完「体检 → 给结论 → 问一句 → 应用」。

    为什么要有这个模式：手机上跑面板需要常驻一个网页服务，安卓会为省电把它杀掉，
    用户看到的就是「网页打不开」。终端里跑没这个问题——进程在前台，系统不会静默杀，
    也不需要记地址、不需要浏览器。
    """
    print("\n接下来会逐个切换频段实测：中途断网 2~4 次（每次约 30 秒），")
    print("结束时自动切回现在的配置。全程约 3~6 分钟，中途 Ctrl+C 可随时中止。\n")

    rc = run_preview(dev, caps, st0, args, cfg, speed_url, ping_host)
    if rc != 0:
        return rc

    try:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("\n[!] 没能读到本次结论，请重跑一次。")
        return 1

    # 默认走自动裁决；只有用户想强制某一条时才需要输入字母。
    options = plan.get("options") or {}
    avail = [k for k in ("auto", "speed", "stability") if options.get(k)]
    if not avail:
        # 上面的体检结论刚说过「为什么不用改」，这里再说一遍就是啰嗦
        # （实跑截图的末尾出现过「…不值得为它断一次网」紧跟「没有更好的选择」）。
        return 0

    print("\n" + "=" * 56)
    if options.get("auto"):
        a = options["auto"]
        print(f"要现在就应用吗？回车 = 按自动建议切到 BAND {a['band']}（{a['reason']}）")
    else:
        print("要现在就应用吗？回车 = 应用上面的建议")
    if options.get("speed"):
        print("  a = 只看速度那条线")
    if options.get("stability"):
        print("  b = 只看稳定那条线")
    print("  n = 不改，保持原样（应用只切一次，约 30~60 秒，会短暂断网）")
    try:
        ans = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n已跳过，设备保持原样。")
        return 0

    if ans in ("n", "no", "q", "quit", "不", "否", "取消"):
        print("已跳过，设备保持原样。下次想应用直接跑：python autotune.py --apply-plan")
        return 0
    if ans.startswith("a") and options.get("speed"):
        args.strategy = "speed"
    elif ans.startswith("b") and options.get("stability"):
        args.strategy = "stability"
    else:
        args.strategy = "auto" if options.get("auto") else avail[0]
    if args.strategy not in [k for k in avail]:
        args.strategy = avail[0]

    try:
        cur = dev.get_state()
    except Exception as e:  # noqa: BLE001
        print(f"[×] 读取设备状态失败：{type(e).__name__}: {e}")
        return 1
    return run_apply_plan(dev, caps, cur, args, cfg, ping_host)


def main():
    cfg = load_config()

    ap = argparse.ArgumentParser(description="随身WiFi 自动调优（跨平台）")
    ap.add_argument("--apply", action="store_true", help="实际应用最优设置（默认只检测）")
    ap.add_argument("--wifi-only", action="store_true", help="只优化 WiFi 信道")
    ap.add_argument("--lte-only", action="store_true", help="只优化 LTE 频段")
    ap.add_argument("--bands", default="default",
                    help="default=设备声明的全部频段 / preferred=配置里的子集 / 逗号分隔如 3,40")
    ap.add_argument("--restore", action="store_true", help="还原为不锁频段 + 自动信道")
    ap.add_argument("--clean", action="store_true",
                    help="清掉历史运行记录与缓存（保留 config.json 和面板令牌）")
    ap.add_argument("--mtu", action="store_true",
                    help="探测这条线路的真实路径 MTU（只读，不改设备；手机端做不到）")
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
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="体检 → 给结论 → 问一句是否应用（手机上的 mifi 用的就是这个）")
    ap.add_argument("--status", action="store_true",
                    help="只看当前状态（频段/信道/信号），不测量、不改动")
    ap.add_argument("--scan", action="store_true",
                    help="只扫周边 2.4G 信道（几秒，不碰设备）——用来验证手机能不能扫")
    ap.add_argument("--strategy", "-s", choices=("auto", "speed", "stability"), default=None,
                    help="换频段的裁决方式：auto=自动（默认，速度强太多选快的、速度差不多选稳的、"
                         "稳定性差不多选快的）/ speed=只按速度 / stability=只按稳定")
    ap.add_argument("--dry-run", action="store_true",
                    help="演练：把要做的改动打出来但不碰设备（配合 --apply / --apply-plan）")
    ap.add_argument("--version", action="version",
                    version=f"mifi-autotune {__version__}")
    ap.add_argument("--watch", action="store_true",
                    help="进阶：长期挂在后台，明显劣化时才自动重测并切换")
    ap.add_argument("--watch-interval", type=int, default=300,
                    help="守护模式探测间隔秒数（默认 300）")
    ap.add_argument("--watch-cooldown", type=int, default=900,
                    help="两次自动重测之间的最小间隔秒数（默认 900）")
    ap.add_argument("--watch-dry-run", action="store_true",
                    help="守护模式演练：只报告会触发什么，不改设备")
    args = ap.parse_args()

    if args.scan:
        # 只读扫描，不需要连上设备，也不改任何设置。
        # 单独做成一个命令，是因为手机上「能不能扫」需要几十秒就能验证，
        # 不该为了验证它去跑一次 5 分钟的频段体检。
        own = args.ssid or cfg.get("own_ssid") or scanner.connected_ssid()
        print(f"扫描周边 2.4G（{args.scan_rounds} 轮合并）…")
        print("      （手机上第一次调用要先唤起 Termux:API，慢的话可能要等半分钟；"
              "卡住会在超时后直接告诉你是哪一步坏了）")
        aps = scanner.scan_2g(rounds=args.scan_rounds, own_ssid=own)
        if not aps:
            print(f"[×] 没扫到邻居 AP：{scanner.scan_failure_hint()}")
            print("      原始信息（排查用，复制这一段就能定位问题）：")
            print(scanner.scan_diagnose())
            return 1
        if own:
            print(f"（已排除自己的 AP：{own}）")
        rows = scanner.score_channels(aps)
        print(f"\n扫到 {len(aps)} 个 AP。各信道干扰分（0 最好，越低越干净）：")
        shown = rows[:5]
        for r in shown:
            print(f"  信道 {r['ch']:>2}   干扰分 {r['score']}")
        best = scanner.pick_best_channel(rows)
        # 最干净的信道经常不在上面几条里（那正是它干净的原因）。
        # 不说明的话，用户会以为「7 号没扫到」或「工具算错了」——实机反馈过。
        if best["ch"] not in {r["ch"] for r in shown}:
            print(f"  …只列干扰最高的 5 个；最干净的 {best['ch']} 号不在其中，"
                  f"正因为附近没人用")
        if rows and best["ch"] != rows[0]["ch"]:
            # 被「标准信道优先」改选过，得说清为什么不是分最低的那个。
            print(f"  （{rows[0]['ch']} 号分更低（{rows[0]['score']}），但它夹在两条信道中间、"
                  f"和 1/6/11 都不对齐；差距不到 {scanner.CHANNEL_PREF_MARGIN} 时优先标准信道）")
        print(f"\n最干净：信道 {best['ch']}（干扰分 {best['score']}）")
        print("这里只扫描，不会改设备。想连频段一起优化就跑 `mifi`。")
        return 0

    if args.list_drivers:
        print("可用驱动：")
        for d in list_drivers():
            print(f"  {d['name']:<12} {d['description']}")
        print("\n新增机型：参考 drivers/template.py，在 drivers/__init__.py 里登记一行。")
        return 0

    if args.clean:
        removed = clean_run_files()
        if removed:
            print("已清理：")
            for name in removed:
                print(f"  · {name}")
        else:
            print("没有需要清理的文件。")
        print(f"\n保留：config.json（配置）、.panel-token（面板令牌）")
        print(f"运行记录：每跑一次只留最近 {_log_keep()} 条"
              f"（改 config.json 的 log_keep 可调整，0 = 不保存）")
        return 0

    speed_url = args.speed_url or cfg["speed_url"]
    ping_host = args.ping_host or cfg["ping_host"]
    if args.speed_url:
        cfg["speed_url"] = args.speed_url
        save_config(cfg)
    if args.ping_host:
        cfg["ping_host"] = args.ping_host
        save_config(cfg)

    # 动手测之前先确认「测速源真的下得动」。
    # 实测踩过：整条链路 HTTPS 到 CDN 全失败（HTTP 正常），于是每个频段都测出
    # 0 Mbps，工具据此推荐了错误的频段。先选源能把这类假结论挡在源头。
    if not (args.watch or args.restore or args.apply_plan or args.list_drivers):
        speed_url = probe.resolve_speed_url(speed_url)

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
    print(f"当前状态：频段 {_band_label(cur_band, _serving_band(st0))} | "
          f"信道 {cur_ch or '自动'} | 带宽 {st0.get('bandwidth') or '—'} | MTU {st0.get('mtu') or '—'}")
    sig = st0.get("signal") or {}
    if sig.get("rsrp"):
        print(f"信号：RSRP {sig.get('rsrp')} | SINR {sig.get('sinr')} | PCI {sig.get('pci')}")
    print("=" * 56)

    if args.status:
        # 已经打印过设备/频段/信道/信号了，这里补一条实时延迟就够了。
        p = probe.ping_stats(6, ping_host)
        if p["avg"] is None:
            print("网络：测不到回应（可能正在注册或没有数据通路）")
        else:
            print(f"网络：延迟 {p['avg']}ms ｜ 抖动 {p['jitter']}ms ｜ 丢包 {p['loss']}%")
        diag = diagnose_signal(st0.get("signal") or {})
        if diag:
            print(f"\n信号判读：{diag['text']}")
            print(f"          {diag['advice']}")
        print("\n想看「能提升多少」就跑：mifi        （体检约 3~6 分钟）")
        if not probe.is_termux():
            # 只在电脑上提示：手机端没有 ICMP 权限，这项在手机上做不了。
            print("遇到「网页能开、大文件下载卡死」可查 MTU：mifi --mtu（只读，20 秒）")
        return 0

    if args.mtu:
        return run_mtu_check(st0, ping_host)

    if args.interactive:
        return _run_interactive(dev, caps, st0, args, cfg, speed_url, ping_host)

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
        # 保留改动前的原始记录，只补一条「什么时候还原的」。
        # 直接覆盖会把「我原来是哪个频段」抹掉，之后想查都查不到。
        saved = {}
        try:
            saved = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            saved = {}
        saved.update({"restored_at": datetime.now().isoformat(),
                      "restored_to": {"band": 0, "channel": 0}})
        save_state(saved)
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
            best = scanner.pick_best_channel(rows)
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
    caveats = []
    serving = _serving_band(st0)
    cmp_band = cur_band or serving          # 对比基准（未锁频段时用实际驻留频段）
    cur_label = _band_label(cur_band, serving)
    if not args.wifi_only:
        if not args.apply:
            st = probe.ping_stats(8, ping_host)
            print(f"\n[2/2] LTE 当前频段：{cur_label}"
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
            # 与 --preview 共用同一份「测量 + 保证还原」实现。
            # 以前两处各写一遍，--apply 那份漏了 try/finally —— 而 --watch 触发的
            # 正是 --apply，一次崩溃就把设备留在没人覆盖的频段上。
            results = measure_all_bands(dev, bands, rounds_n, cur_band, cur_label,
                                        speed_url, ping_host)

            report["lte"] = results
            # 和 --preview 用同一套判定：丢包 90% 的频段即使带宽最高也不能当最优。
            # 之前这里只看 r["ok"]，等于两套标准，--watch 可能把设备锁到打不开网页的频段。
            usable = bands_worth_comparing(results)
            usable.sort(key=lambda r: (-r["mbps"], r["avg"] or 999))
            cur_res = next((r for r in results if r["band"] == cmp_band), None)
            best_res = usable[0] if usable else None
            caveats = assess_reliability(results, cur_res, best_res)
            for r in results:
                if r.get("ok") and quality_issue(r):
                    print(f"      [!] BAND {r['band']} {quality_issue(r)}"
                          f" —— 质量不可接受，不会被选为最优")
            if usable:
                print(f"\n      最优频段：BAND {best_res['band']}"
                      f"（中位 {best_res['mbps']} Mbps，单轮 {best_res['mbps_all']}）")
            # 与 --preview 同一套裁决（自动 + 两条单线），否则 --watch 和手动跑会给出不同结论
            opts = {"auto": decide_band(cur_res, usable, cmp_band)[0],
                    "speed": speed_option(cur_res, best_res, cmp_band, caveats),
                    "stability": stability_option(cur_res, usable, cmp_band)}
            strat = getattr(args, "strategy", None) or "auto"
            report["options"] = opts
            report["strategy"] = strat
            picked = opts.get(strat)
            if picked:
                best_band = picked["band"]
                label = {"auto": "自动", "speed": "速度", "stability": "稳定"}[strat]
                print(f"      按「{label}」建议：BAND {cur_label} → BAND {best_band}"
                      f"（{picked.get('reason')}）")
            elif usable:
                print(f"      按「{strat}」这次不给建议，保持现状")
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
    do_band = (best_band is not None and str(best_band) != str(cur_band))
    print(f"建议：信道 {cur_ch} -> {best_ch if do_ch else '保持'} ｜ "
          f"频段 {cur_label} -> "
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
            # 回滚也要耐心等设备重新注册：发一条 POST 然后睡 26 秒就宣布失败，
            # 会把「还在恢复」当成「回滚失败」，让用户误以为设备废了。
            ok_r, _ = restore_band(dev, cur_band)
            if not ok_r:
                print(f"    [!] 回滚没成功，请手动在设备后台把频段改回 {cur_label}")
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
