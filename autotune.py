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
import signal
import statistics
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import probe
import scanner
from drivers import detect, list_drivers
from drivers.base import DeviceBusy, DeviceUnreachable, Unsupported

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
LOG = HERE / "autotune-log.jsonl"
CONFIG = HERE / "config.json"

# 版本号：手机上排查「我装的到底是哪一版」全靠它（mifi --version）。
# 改功能就往上加，别让两个不同的行为共用一个号。
__version__ = "0.6.9"

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
# 换频段的信号闸门：候选频段实测 RSRP 比当前驻留差这么多，就不换。
# 实机 2026-10-30 21:56（手机）：BAND 40 10.8 Mbps / -81dBm，BAND 3 11.8 Mbps，
# 裁决说「速度强太多」把设备切到 BAND 3 —— 那边只有 -95dBm，用 14dB 的余量
# 换 1 Mbps 的账面差。速度差 9%，而同一屏下面还印着「数据不足以下结论」。
BAND_RSRP_DROP = 10
MAX_USABLE_LOSS = 20                # 丢包 ≥20%：带宽数字再好看也不能选
MAX_USABLE_PING_MS = 800            # 延迟 ≥800ms：同上
OBSERVE_SEC = 90                    # 应用后盯多久，确认「能一直通」
PLAN = HERE / "plan.json"           # 体检结论，供「一键应用」复用
SITES = HERE / "sites.json"         # 位置记忆：每个 PCI 下哪些频段有覆盖、哪个最好

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

# 位置记忆：同一个地方测过几次之后，别再重复踩同样的坑。
DEAD_SKIP_MIN = 3                   # 连续这么多次「无信号」才敢跳过
DEAD_REPROBE_EVERY = 5              # 但每 N 次强制全测一遍，防止环境变了还一直跳
# 记忆会随时间过期。用户实机（2026-10-30）指出的场景：一个地方以前常来，
# 隔了很久没来 —— 这期间可能加了基站/拆了基站、邻居换了几轮、晚上八点的人流
# 也完全不同，拿旧数据跳着测就是拿过期结论当答案。所以分三档：
#   fresh  72 小时内  —— 记忆全信，可以用「只测两个」的快速模式
#   aging  3~7 天     —— 覆盖还在（有没有信号很稳定），速度作废：能收到的全测
#   stale  超过 7 天  —— 整段记忆作废，一条都不跳，全部重测
SITE_FRESH_HOURS = 72
SITE_STALE_DAYS = 7
SITE_TTL_DAYS = 30                  # 这么久没来过的位置，连记录一起删掉（搬家/换城市）
FAST_LEAD_RATIO = 1.2               # 历史最优比第二名快这么多倍，才敢只测它俩
FAST_MIN_SEEN = 2                   # 至少来过这么多次，历史才可信（一次不算）

# 滚动样本：每个频段留最近几次「几点、多快、丢包多少、延迟多少」。
# 为什么光有 best 不够：54 秒的体检碰不到「每 3~5 分钟掉一次包」这种事
# （用户实机 2026-10-30 宿舍：BAND 40 跑 41Mbps，三五分钟就丢包 30~50%），
# 而「晚上八点比凌晨慢一半」也只有按时段攒样本才看得出来。
# 只留最近 SITE_SAMPLES 次，文件不会随运行次数膨胀。
SITE_SAMPLES = 8                    # 每个频段保留几个样本
SITE_LOSS_BAD = 20                  # 单次丢包 ≥ 这个值算「这次掉包了」
SITE_FLAP_RATIO = 0.3               # 历史样本里掉包占比 ≥ 30% → 判为「会周期性掉包」
FLAP_SWAP_RATIO = 0.85              # 稳的那个只要不慢过 15%，就优先它
SITE_MERGE_MIN = 20                 # 同运营商、上次记录在这么多分钟内 → 算同一个地方
# 一个位置「见过哪些小区」。为什么不能只靠上面那条时间窗：20 分钟一过，
# 同一张桌子的两条记录就互不相认了。用户实机（2026-10-30 宿舍）就是这么分家的 ——
# 锁 BAND 3 时是 PCI 350（记着 BAND 3 有 46.8 Mbps），解锁后设备自己驻留 BAND 40
# 变成 PCI 236，于是新建了一条空记录；下次再测，46.8 那条历史完全没被看到。
# 所以位置改成「一组见过的小区」：只要当前 PCI 在这组里，就是同一处，跨天也认得出。
SITE_CELL_KEEP = 8                  # 每个位置最多记几个见过的小区

# 全自动守护（--guard）：按「人在不在动」决定锁不锁频段。
#
# PCI 用来判断移动，但它只是**必要条件，不是充分条件**。原来以为「人没动 PCI
# 就不会跳」，2026-10-30 的实机日志把这个假设推翻了：坐在宿舍里，锁着 BAND 41
# 的那三分钟，PCI 自己从 477 跳到 252（-89dBm → -105dBm）—— 信号变弱时设备
# 会自己重选小区（同频段换扇区），人在原地，PCI 照样跳。
# 所以判移动要两件事一起看：**PCI 变过几次** + **信号/链路是不是也跟着垮了**。
GUARD_INTERVAL = 30                 # 采样间隔（秒）：10 个 ping，几百字节
GUARD_SETTLE_SEC = 600              # 连续这么久 PCI 没变 = 原地不动（10 分钟）
GUARD_MOVE_WINDOW = 300             # 判定「在移动」的时间窗
# 窗口内 PCI 变这么多次 = 在移动。
# 这两个数是配着定的，试过两轮：
#   · 180s / 2 次：静止时一次弱信号重选就够数（477→252），人被判定「在移动」。
#   · 180s / 3 次：反过来了 —— 走路两分钟才换一个小区，180 秒窗口里凑不满 3 次，
#     明明在走却判「静止」，10 分钟后把频段锁死在路上（这是最危险的误判）。
#   · 300s / 2 次：走路 5 分钟内必换两次；静止时偶尔跳一次也够不到两次。
GUARD_MOVE_HITS = 2
# PCI 变了、但信号没掉这么多 dB → 是设备自己重选，不算人动，静止计时不清零。
GUARD_RESELECT_DB = 8
# 单次采样延迟超过这么多毫秒，也算这条链路不行（原来只看「不通」和「丢包过半」，
# 实机 17:24 延迟 4402ms、只丢 10%，会被当成「正常」）。
GUARD_LAG_MS = 2000
GUARD_BAD_HITS = 3                  # 连续这么多次采样不通 = 锁定的频段撑不住
GUARD_COOLDOWN = 300                # 动作后的冷却，避免反复断网（只管主动动作）
# 「刚解锁还一直不通」的判定窗口。必须比 GUARD_COOLDOWN 长，否则解锁刚把冷却
# 挂上，窗口就已经关了，后面那条「解锁仍不通 → 跑体检」永远走不到。
GUARD_RESCUE_WINDOW = 900

# 新地方（从没测过）第一次久坐时怎么处理：不现场跑 5 分钟体检，先把「当前
# 驻留的那条」锁住（设备本来就在上面，几秒、几乎不断网），并给这个位置打个
# provisional 标记；下次再来才补一次完整体检。理由：新地方多半只待一两小时，
# 花 15 分钟找全局最优不划算，而「别让它自己乱跳」当场就有收益。
GUARD_NEW_SITE = "这个位置没测过"

# 停下多久先「快测」当前这条（只测不锁）。用户 2026-10-30 的规则：人从老地方
# 走到新地方，路上本来就是解锁状态（按信号保底有网）；停下来的地方很可能只是
# 路过，这时候把频段锁死，后面走到没覆盖的地方就是断网。所以先只「看看这条够不够用」。
GUARD_QUICK_SEC = 180               # 停 3 分钟就先快测一次
# 「够用」的下限。原来是 15 Mbps，实机证明这个数卡在噪声区里：同一张桌子、
# 前后 22 分钟，快测 32 Mbps 而体检只有 17.8 / 10.8。15 落在波动区间中间，
# 同样的网络掷到 32 判「够用」（不锁，什么也不发生），掷到 12 判「不够用」
# （触发 5 分钟体检 + 断网 4 次）—— 判错的代价完全不对等。
# 所以下限压到 8：只有真的差才承认「不够用」，宁可放过，不要误动设备。
GUARD_QUICK_MIN_MBPS = 8            # 低于这个才算「不够用」
GUARD_QUICK_GOOD_MBPS = 20          # 达到这个算「明确够用」
GUARD_QUICK_MAX_LOSS = 5            # 「够用」的丢包上限
GUARD_QUICK_MAX_MS = 150            # 「够用」的延迟上限
GUARD_QUICK_PROBE_SEC = 4           # 每次快测下 4 秒（测两遍，总时长和原来相当）
GUARD_QUICK_SAMPLES = 2             # 采样几次。单次 5 秒下载的重复性太差
GUARD_QUICK_NOISY_RATIO = 3.0       # 两次差这么多倍 → 测不准，别拿它下结论
# 久坐锁定前，把「刚才快测到的当前速度」和「记忆里的历史最优」比一下。
# 用户实机（2026-10-30 18:00）：记忆说 BAND 3 有 44.6 Mbps，但那是上午、还是在
# 另一个 PCI（350）下测的；当时实测当前 BAND 40 有 32 Mbps，延迟 65ms 正常。
# 差 30% 以内就别折腾 —— 切一次频段要断几秒、要重新注册，而"记忆里的最优"
# 本身可能是别的扇区、别的时段测出来的，未必代表现在。只有明显快得多才值得切。
GUARD_QUICK_KEEP_RATIO = 0.7        # 当前 ≥ 历史最优 × 这个比例 → 就留在当前

# 锁之前先看信号，这是「记忆说它快」和「它现在能不能用」的区别。
# 用户实机（2026-10-30 宿舍）：记忆里 BAND 3 有 46.8 Mbps，可那天在这个位置
# BAND 3 只有 -105dBm、而设备实际驻留的 BAND 40 是 -77dBm。照记忆锁 BAND 3，
# 结果 8 分钟里 -95 → -110、延迟飙到 4.4 秒、最后丢包，被当成「检测到移动」解锁。
# 原因是 record_site 只记了速度，没记当时的 RSRP —— 分不清「上次条件好」和
# 「这次条件差」。所以样本里开始存 RSRP；差这么多 dB 就否决这条记忆。
GUARD_LOCK_RSRP_DROP = 10           # 记忆最优比当前驻留差这么多 dB → 不锁它

# 锁定之后判「这条不行」的线。原来是丢包 ≥20%，太敏感：宿舍 BAND 40 每 3~5 分钟
# 掉 30~50%，会导致守护反复解锁 —— 越解越像「厂商的自动」，而这正是要治的病。
# 只有「完全不通」或「丢包过半」才算真的撑不住。
GUARD_BAD_LOSS = 50

# 决赛：第二名和第一名的差距小于这个比例时，两个都要复测。
# 单轮测速的波动轻易就能有 10~20%，差 8% 的两个频段谁快谁慢根本说不清 ——
# 这时只复测第一名，等于拿运气当结论（用户实机 2026-10-30：31.5 vs 29.0）。
CONTEST_CLOSE_RATIO = 0.25

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
             "jitter": None, "loss": 100, "rounds": 0, "rsrp": None}

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
    # 记下这条频段此刻的信号强度。锁频段前要拿它跟当前驻留的比 ——
    # 「历史上很快」和「现在能不能用」是两件事（2026-10-30 实机教训）。
    rsrp = None
    try:
        sig = (dev.get_state() or {}).get("signal") or {}
        rsrp = _num(sig.get("rsrp"))
    except Exception:  # noqa: BLE001
        rsrp = None
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
        "rsrp": rsrp,
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


def describe_band_state(cur_band, serving):
    """状态行里怎么描述频段：锁定值和实际驻留分开说。

    为什么必须分开：实机截图出现过「当前状态：频段 BAND 39」，而整份结论
    都在说 BAND 3 —— 用户根本分不清「我锁的是哪个」和「我现在实际在哪个」。
    两者不一致本身就是重要信息：说明锁定没生效（设备在该频段信号太差时会
    自己回落），继续以为锁着 39 只会让人白等。

    返回 (描述文本, 是否要警告)。
    """
    if cur_band and serving and str(cur_band) != str(serving):
        return f"锁定 BAND {cur_band}，实际驻留 BAND {serving}", True
    if cur_band:
        return f"BAND {cur_band}", False
    if serving:
        return f"自动（实际驻留 BAND {serving}）", False
    return "未锁定", False


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


def decide_band(cur_res, candidates, cmp_band, caveats=()):
    """自动裁决换哪个频段。返回 (option 或 None, 不换的原因)。

    规则（用户 2026-10-29 定的）：
      1) 一方速度强太多   → 选快的（哪怕它稍微不稳）
      2) 速度差不多       → 选更稳的
      3) 稳定性差不多     → 选更快的
    第 1 条刻意用「最差一轮」比较：拿峰值去说「强太多」是在骗人。

    2026-10-30 21:56 补两条前置闸门（那次真机把设备从 -81dBm 切到 -95dBm）：
      · 数据本身不可信（波动过大、测速源抽风）→ 一条建议都不出。
        原来「自动裁决」不看 caveats，于是「按速度」说「数据不足以下结论」，
        同一屏「自动」却说「速度强太多，切」，自相矛盾还真的切了。
      · 候选频段实测 RSRP 比当前差 ≥ BAND_RSRP_DROP → 从候选里剔除。
        速度是过去一分钟的三个样本，信号是结构性的；拿 14dB 换 1 Mbps 不划算。
    """
    if not cur_res or not cur_res.get("ok"):
        return None, "当前频段没有可用数据"
    cands = [r for r in (candidates or [])
             if r and r.get("ok") and str(r.get("band")) != str(cmp_band)]
    if not cands:
        return None, "没有可比的其他频段"
    if caveats:
        return None, f"测量数据本身不可信（{caveats[0].split('（')[0]}），不拿它当切换依据"
    cur_rsrp = _num((cur_res or {}).get("rsrp"))
    if cur_rsrp is not None:
        kept = [r for r in cands
                if not (r.get("rsrp") is not None
                        and r["rsrp"] <= cur_rsrp - BAND_RSRP_DROP)]
        if not kept:
            worst = min(r["rsrp"] for r in cands if r.get("rsrp") is not None)
            return None, (f"其他频段的信号都比当前差一截（最弱 {worst:.0f}dBm，"
                          f"当前 {cur_rsrp:.0f}dBm），不值得为它断一次网")
        cands = kept

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


# ---------------- 位置记忆（同一个地方，第二次起就别重头再测） ----------------

def _site_key(st):
    """这个位置怎么标识：PCI + 运营商。

    PCI 是**当前驻留的那个小区**，不是地点。同一个地方能同时看到好几个 PCI
    （每个频段一套编号，各 0~503），读到哪个取决于设备当下驻留哪条频段：
    实机（2026-10-30 宿舍）锁 BAND 3 读到 350、驻留 BAND 40 读到 236，同一张桌子。
    加运营商是为了压 PCI 复用（不同城市完全可能撞）。
    读不到就返回空串 —— 宁可这次不记忆，也不能把两地混成一个。
    """
    sig = (st or {}).get("signal") or {}
    pci = _pci_num(sig.get("pci"))
    if pci is None:
        return ""
    return f"{pci}-{(sig.get('operator') or '?').strip()}"


def _split_key(key):
    """把 `236-CMCC` 拆成 ("236", "CMCC")。拆不出来就返回 ("", "")。"""
    pci, _, op = str(key or "").partition("-")
    return pci.strip(), op.strip()


def _remember_cell(rec, key, band=None):
    """把「这处还见过哪个小区」记下来，供下次换个频段时认出是同一地点。

    为什么需要：切频段就会换服务小区，而工具偏偏一直在切。只按 PCI 记账的话，
    每切一次就把自己「搬了次家」，历史全废（用户实机 2026-10-30 宿舍的 350 / 236）。
    """
    pci, _ = _split_key(key)
    if not pci or not rec:
        return
    cells = rec.setdefault("cells", [])
    stamp = datetime.now().isoformat(timespec="seconds")
    for c in cells:
        if str(c.get("pci")) == pci:
            c["n"] = int(c.get("n") or 0) + 1
            c["last"] = stamp
            if band:
                c["band"] = int(band)
            return
    cells.append({"pci": pci, "band": int(band) if band else None, "n": 1,
                  "last": stamp})
    del cells[:-SITE_CELL_KEEP]


def load_sites():
    try:
        return json.loads(SITES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_sites(data):
    try:
        SITES.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _parse_ts(s):
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def site_age_hours(rec, now=None):
    """这个位置上次来是几小时前。读不出时间就返回 None（当作「不知道」，按过期处理）。

    负值（系统时间回拨、记录来自未来的机器）按 0 算，不然会把新鲜记录误判成过期的。
    """
    dt = _parse_ts((rec or {}).get("last"))
    if dt is None:
        return None
    sec = ((now or datetime.now()) - dt).total_seconds()
    return max(0.0, sec / 3600.0)


def site_memory_state(rec, now=None):
    """记忆的可信度分档：none / fresh / aging / stale。"""
    if not rec:
        return "none"
    age = site_age_hours(rec, now)
    if age is None or age > SITE_STALE_DAYS * 24:
        return "stale"
    if age > SITE_FRESH_HOURS:
        return "aging"
    return "fresh"


def _fmt_age(hours):
    if hours is None:
        return "时间未知"
    if hours < 1:
        return "不到 1 小时前"
    if hours < 48:
        return f"{hours:.0f} 小时前"
    return f"{hours / 24:.0f} 天前"


def band_plan(rec, bands, serving=None, now=None):
    """按这个位置的历史，决定测哪些、跳过哪些。

    用户实机（2026-10-30）：在宿舍测了十几二十次，BAND 38/41 每次都没信号，
    却每次都要等它们各超时 15 秒，剩下的频段也是从头重测一遍。
    跳过「这里从来没信号」的频段，并把上次最好的排在最前面 ——
    万一中途被打断，先拿到的就是最可能用的那条。

    但记忆是按时间打折的（见 SITE_* 常量）：太久没来的位置一条都不跳，
    因为「上次很快」这件事过期得比「这里收不收得到信号」快得多。

    返回 (要测的频段, 无信号跳过的, 因为太慢跳过的, 历史最优)。
    为了防「环境变了还一直跳过」，每 DEAD_REPROBE_EVERY 次强制全测一遍。
    """
    if not rec or not bands:
        return list(bands), [], [], None
    state = site_memory_state(rec, now)
    if state == "stale":
        # 隔太久（或记录里没有可用时间）：覆盖和速度都可能变了，全部重测。
        return list(bands), [], [], None
    seen = int(rec.get("seen") or 0)
    hist = rec.get("bands") or {}
    dead = {b for b, v in hist.items()
            if (v.get("dead") or 0) >= DEAD_SKIP_MIN and (v.get("ok") or 0) == 0}
    if seen and seen % DEAD_REPROBE_EVERY == 0:
        dead = set()                      # 定期抽查，别把记忆当永久真理
    keep = [b for b in bands if str(b) not in dead]
    dead_list = [b for b in bands if str(b) in dead]

    best = rec.get("best")
    try:
        best = int(best) if best is not None else None
    except (TypeError, ValueError):
        best = None
    if best is not None and best not in keep:
        best = None

    # 快速模式：历史最优领先明显时，只测「历史最优 + 当前驻留」。
    # 这是真正省时间的那一步 —— 跳过死频段只省 30 秒，而有信号的频段每个要 54 秒。
    # 只在来过 >= FAST_MIN_SEEN 次后才敢这么做，且每 DEAD_REPROBE_EVERY 次全测一遍。
    # 另外只有 fresh 档才敢用：速度是最容易变的东西，隔了 3 天以上就不认了。
    slow = []
    if (best is not None and seen >= FAST_MIN_SEEN and state == "fresh"
            and seen % DEAD_REPROBE_EVERY != 0):
        def v(b):
            return float((hist.get(str(b)) or {}).get("best") or 0)

        rest = [b for b in keep if b != best and b != serving]
        second = max(rest, key=v) if rest else None
        if second is not None and v(best) >= v(second) * FAST_LEAD_RATIO:
            chosen = [best]
            if serving and serving != best:
                chosen.append(serving)
            elif second is not None:
                # 当前驻留就是最优时，也要带上第二名：只测一个没法交叉确认，
                # 单轮测速的波动足以把结论翻转（用户 2026-10-30 指出的偶发性）。
                chosen.append(second)
            slow = [b for b in keep if b not in chosen]
            keep = chosen

    if best is not None and best in keep:
        keep = [best] + [b for b in keep if b != best]
    return keep, dead_list, slow, best


def band_flap(v):
    """这个频段是不是「会周期性掉包」。返回 (是否掉包, 掉包占比, 样本数)。

    只看攒下来的历史样本：一次 54 秒的体检根本碰不到「每 3~5 分钟掉一次」
    那个窗口，所以这个判断只能靠跨次记录，靠不了单次测量。
    """
    rows = [s for s in ((v or {}).get("samples") or [])
            if isinstance(s, (list, tuple)) and len(s) >= 4]
    if len(rows) < 2:
        return False, 0.0, len(rows)
    bad = sum(1 for s in rows if (_num(s[2]) or 0) >= SITE_LOSS_BAD)
    ratio = bad / float(len(rows))
    return ratio >= SITE_FLAP_RATIO, ratio, len(rows)


def prefer_stable_over_flap(usable, hist, fastest):
    """最快的那个如果历史上老是掉包，而另一个只慢一点点，就选稳的那个。

    返回 (最终选中的频段, 一句解释或 None)。判据写死成「备选不慢过 15%」：
    差太多就别换，掉包固然烦，但慢一半更难受，这种事让用户自己决定。
    """
    if fastest is None or not hist:
        return fastest, None
    flap, ratio, n = band_flap(hist.get(str(fastest)))
    if not flap:
        return fastest, None
    fast_mbps = next((r["mbps"] for r in usable if r["band"] == fastest), 0) or 0
    alt = None
    for r in usable:
        if r["band"] == fastest:
            continue
        if band_flap(hist.get(str(r["band"])))[0]:
            continue
        if fast_mbps and r["mbps"] >= fast_mbps * FLAP_SWAP_RATIO:
            alt = r
            break
    pct = int(round(ratio * 100))
    if alt:
        slower = int(round((1 - alt["mbps"] / fast_mbps) * 100)) if fast_mbps else 0
        return alt["band"], (f"BAND {fastest} 过去 {n} 次里有 {pct}% 丢包 ≥{SITE_LOSS_BAD}%，"
                             f"会时不时掉一下；BAND {alt['band']} 只慢 {slower}%，这次选它")
    return fastest, (f"BAND {fastest} 过去 {n} 次里有 {pct}% 丢包 ≥{SITE_LOSS_BAD}%，"
                     f"可能隔几分钟掉一次；这次没有差不多快的备选，切过去要有心理准备")


def _site_measured(rec):
    """这条记录里有没有「真测出来的数据」，而不只是「先锁过一下」。

    provisional 是「第一次来，先锁当前免得乱跳」的标记。它原来只在
    record_site 里该被清掉（现在会清了），但老数据会留下「标记为真、
    bands 里其实已经有实测数字」的状态 —— 这种记录若再被判成「没测过」，
    守护就永远走「快测当前、直接锁」，记忆永远上不了台面；更糟的是它
    也永远不会触发体检去修正自己，死循环。所以判据看数据本身。
    """
    for v in (rec.get("bands") or {}).values():
        try:
            if int((v or {}).get("ok") or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def find_site(data, key, now=None):
    """认出「刚切过频段、PCI 变了但人没挪窝」还是同一个地方。

    设备锁到哪个频段，服务小区就跟着换 —— 实测同一张桌子（2026-10-30）：
    锁 BAND 40 是 PCI 236，切到 BAND 3 变成 350。按 PCI 一条一条记的话，
    工具每切一次频段就把自己「搬了家」，记忆永远攒不起来，而它偏偏一直在切。

    三层判据，从硬到软：
      1. 当前 PCI 出现在某条记录见过的小区里（cells）—— 这处换过这个小区，
         就是同一地点。跨天也认，这是唯一能把 350 和 236 绑在一起的东西。
      2. 记录本身就叫这个 key —— 老朋友。
      3. 同运营商、且上一条记录是 SITE_MERGE_MIN 分钟内留下的 —— 刚切完频段
         就接着测。人真走到别处，通常不会二十分钟内又测一遍。
    收尾一条：自己只是个 provisional（从没实测过）时，如果同运营商有一条 24
    小时内真测过的记录，就并到那条上 —— provisional 里没有可损失的信息，并过去
    只会让这个位置更完整（实机 2026-10-30：236 那条空记录把 350 的 46.8 挡在外面）。
    """
    if not key:
        return key
    pci, op = _split_key(key)
    if not pci or not op:
        return key
    own = data.get(key) or {}
    # 「可以丢掉」的只是空记录。已经真测过的记录不能当成空壳并掉。
    prov = bool(own.get("provisional")) and not _site_measured(own)
    best = None
    for k, v in (data or {}).items():
        if _split_key(k)[1] != op:
            continue
        age = site_age_hours(v, now)
        if age is None or age > SITE_TTL_DAYS * 24:
            continue
        seen = int(v.get("seen") or 0)
        cells = {str(c.get("pci")) for c in (v.get("cells") or [])}
        if k == key:
            bonus = 1
        elif pci in cells:
            bonus = 2
        elif age <= SITE_MERGE_MIN / 60.0:
            bonus = 0
        elif prov and not v.get("provisional") and age <= SITE_FRESH_HOURS:
            bonus = 0
        else:
            continue
        # 先比「来过几次」：用得多的那条更可能是这个位置的本体；
        # 再比判据强度，最后比新鲜度。
        score = (seen, bonus, -(age or 0))
        if best is None or score > best[0]:
            best = (score, k)
    return best[1] if best else key


def _absorb_site(data, real, key):
    """把 key 那条记录并进 real，然后删掉它 —— 一个地方只留一条。

    只搬「见过哪些小区」和来过次数，不搬速度历史：两条记录的测量条件
    （不同频段、不同时段）不一样，混在一起会造出假的「历史最优」。
    """
    if not real or real == key:
        return
    old = data.pop(key, None)
    if old is None:
        return
    op = _split_key(real)[1]
    rec = data.get(real)
    if rec is None:
        old.setdefault("cells", [])
        _remember_cell(old, key, old.get("best"))
        data[real] = old
        return
    _remember_cell(rec, key, old.get("best"))
    for c in old.get("cells") or []:
        if c.get("pci"):
            _remember_cell(rec, f"{c['pci']}-{op}", c.get("band"))
    rec["seen"] = int(rec.get("seen") or 0) + int(old.get("seen") or 0)


def note_cell_change(old_key, new_key):
    """设备换了个小区、但人没动 —— 把两个 PCI 记到同一处，别让它变成「新地方」。

    这是位置记忆的桥。守护每 30 秒读一次 PCI，能看到「解锁后设备自己换到
    另一个频段」这种切换（实机 17:52：BAND 41 / PCI 477 → BAND 40 / PCI 236）。
    靠它建立联系，比靠 20 分钟并档窗口可靠得多 —— 窗口一过，同一张桌子就分家了。
    """
    if not old_key or not new_key or old_key == new_key:
        return
    if _split_key(old_key)[1] != _split_key(new_key)[1]:
        return                                  # 换了运营商，是另一张卡的事
    data = load_sites()
    real = find_site(data, old_key)
    rec = data.get(real)
    if not rec:
        return                                  # 这处还没建过记录，等体检/快测去建
    _remember_cell(rec, old_key, None)
    _remember_cell(rec, new_key, None)
    save_sites(data)


def record_site(key, results, best_band=None, reset_best=False):
    """把这次的结果记到位置上。只记「有没有信号」和「最好跑到多少」。

    reset_best：这次是全量重测（隔太久没来），历史速度数字已经不作数，
    直接用本次的成绩覆盖，而不是和旧值取最大。
    """
    if not key:
        return
    data = load_sites()
    real = find_site(data, key)
    # 认出是同一处、但历史存在另一个 PCI 名下 → 先并成一条，再往下写。
    _absorb_site(data, real, key)
    rec = data.setdefault(real, {"seen": 0, "bands": {}})
    # 记下这处还见过哪些小区：下次换个频段（PCI 跟着变）才认得出是同一张桌子。
    _remember_cell(rec, key, best_band)
    if real != key:
        # 老字段留着，人看 sites.json 时能明白「为什么只有一条记录」
        al = rec.setdefault("aliases", [])
        if key not in al:
            al.append(key)
            del al[:-8]
    now = datetime.now()
    rec["seen"] = int(rec.get("seen") or 0) + 1
    rec["last"] = now.isoformat(timespec="seconds")
    for r in results:
        b = str(r.get("band"))
        v = rec["bands"].setdefault(b, {"dead": 0, "ok": 0})
        if r.get("ok"):
            v["ok"] = int(v.get("ok") or 0) + 1
            v["dead"] = 0                      # 有信号就清零，别让旧记录一直压着
            now_mbps = float(r.get("mbps") or 0)
            v["best"] = now_mbps if reset_best else max(
                float(v.get("best") or 0), now_mbps)
            v["last_ok"] = now.isoformat(timespec="seconds")
            # 记下这条频段当时的信号强度。没有它，历史就只剩「上次多快」，
            # 分不清是「上次条件好」还是「这次条件差」—— 实机 2026-10-30 就是
            # 这么锁到 -105dBm 的 BAND 3 上，撑了 8 分钟然后掉线。
            rsrp = _num(r.get("rsrp"))
            # 滚动样本：[小时, 速度, 丢包%, 延迟ms, RSRP]。
            # 只留最近几次，文件不会长大；老数据是 4 位，读的时候兼容。
            samples = v.setdefault("samples", [])
            samples.append([now.hour, round(now_mbps, 1),
                            int(_num(r.get("loss")) or 0),
                            int(_num(r.get("avg")) or 0),
                            int(rsrp) if rsrp is not None else None])
            del samples[:-SITE_SAMPLES]
            if rsrp is not None:
                v["rsrp"] = int(rsrp)
                seen_rsrp = [s[4] for s in samples
                             if len(s) > 4 and s[4] is not None]
                if seen_rsrp:
                    v["rsrp_med"] = int(statistics.median(seen_rsrp))
        elif r.get("reason") == "no_signal":
            v["dead"] = int(v.get("dead") or 0) + 1
    # 真测过，就把「只是先锁住」的标记摘掉。这个标记本来是给「第一次来、
    # 先锁当前免得乱跳」用的，可它原来只写不清 —— 于是这个位置永远被判成
    # 「没测过」，守护每次来都走「快测当前、直接锁」，记忆里那条最优永远
    # 上不了台面。实机 2026-10-30 19:44：236-CMCC 在 18:14 已实测过四条
    # 频段，仍被当成新地方，绕过了一整轮信号比较。
    if any(r.get("ok") for r in results):
        rec.pop("provisional", None)
    if best_band:
        rec["best"] = int(best_band)
    save_sites(data)


def prune_sites(days=SITE_TTL_DAYS):
    """清掉太久没去的记录：搬家、换城市之后旧记忆只会误导。"""
    data = load_sites()
    cutoff = datetime.now() - timedelta(days=days)
    keep = {}
    for k, v in data.items():
        try:
            if datetime.fromisoformat(v.get("last", "")) >= cutoff:
                keep[k] = v
        except (TypeError, ValueError):
            continue
    if len(keep) != len(data):
        save_sites(keep)
    return data, keep


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
                # 光「指令发出去了」不算恢复。实机（2026-10-30 11:37）踩到：
                # 这里只睡 5 秒就复测，结果「BAND 3 复测没测成（无信号）」——
                # 设备刚从死频段（38/41）回来，5 秒根本不够重新注册。
                status, waited = wait_for_service(dev, cur_band, timeout=45)
                if status == "ok":
                    print(f"      已恢复（{waited}s），开始复测")
                else:
                    print(f"      [!] 等了 {waited}s 还是没注册上（{status}），"
                          f"复测多半测不出来")
            else:
                print("      [!] 没切回去，复测结果可能偏低")

            usable.sort(key=lambda r: (-r["mbps"], r["avg"] or 999))
            # 候选池摊开打印。2026-10-30 用户实机出现「只复测了当前频段、
            # 29.4 Mbps 的那条反而没复测」，而列表只有一行「补 1 轮确认」，
            # 从输出里根本看不出它凭什么入选 —— 以后遇到直接看这一行。
            print("[决赛] 候选池（按速度）：" + "、".join(
                f"BAND {r['band']} {r['mbps']}Mbps" for r in usable))
            # 候选和「当前」要分开说。之前把两者塞进一个列表打印「各补 1 轮确认」，
            # 而当前频段刚被上面判过「质量不可接受、不参与最优评选」——
            # 用户当场就问：说好不参评，怎么又进决赛了？（2026-10-29 实机截图）
            # 真相是：它确实还会被复测一次，但它不参评，只是给「提升多少」当基准。
            fastest = usable[0]
            second = usable[1] if len(usable) > 1 else None
            pairs = [(fastest["band"], "候选")]
            # 第二名咬得紧时也要复测：这种差距很可能只是单轮噪声，不复测就等于
            # 拿运气当结论。用户实机（2026-10-30）：BAND 40 31.5 vs BAND 3 29.0，
            # 只差 8%，却因为「最快的正好是当前驻留的」只复测了一个。
            if second and fastest["mbps"] and \
                    second["mbps"] >= fastest["mbps"] * (1 - CONTEST_CLOSE_RATIO):
                pairs.append((second["band"], "候选·和第一名只差 "
                              f"{(1 - second['mbps'] / fastest['mbps']) * 100:.0f}%，要确认"))
            if contest_band is not None and \
                    str(contest_band) not in {str(b) for b, _ in pairs}:
                pairs.append((contest_band, "当前·只作对照，不参评"))
            print(f"\n[决赛] 补 1 轮确认：" +
                  "、".join(f"BAND {b}（{role}）" for b, role in pairs))
            if len(pairs) == 1:
                gap_txt = (f"比第二名快 "
                           f"{(1 - second['mbps'] / fastest['mbps']) * 100:.0f}%"
                           if second and fastest["mbps"] else "没有别的可用频段")
                print(f"      只有 {_band_label(fastest['band'])} 候选"
                      f"（{gap_txt}，领先足够明显），复测它自己确认不是一次性波动")
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
    state = "none"                      # 记忆年龄档位；没有记忆/没测频段时都是 none
    flap_note = None                    # 「这个频段历史上会周期性掉包」的说明
    demoted = None                      # 因为掉包被让位的那条（后面要从候选里排除）
    if not args.wifi_only and caps.get("can_lock_band"):
        bands = resolve_bands(caps, args, cfg)
        if not bands:
            print("\n[2/2] 驱动没有声明可用频段，跳过频段实测。")
        else:
            # 这个位置以前测过就复用记忆：跳过从来没收到的频段、把上次最好的排前面。
            # 用户实机（2026-10-30 宿舍）：测了十几二十次，38/41 每次都没覆盖，
            # 却每次都白等 15 秒 × 2，剩下的也是从头重排。
            site = _site_key(st0)
            cur_site = site
            if site:
                # 太久没来的位置连记录一起清掉，免得拿着「搬家前」的历史当参考。
                prune_sites()
            data = load_sites()
            # 切过频段之后 PCI 会变（同一张桌子 236 ↔ 350），那不算搬家。
            site = find_site(data, site) if site else ""
            rec = data.get(site) if site else None
            if getattr(args, "fresh", False):
                rec = None
            state = site_memory_state(rec)
            skipped = []
            if rec:
                bands, dead_list, slow_list, known_best = band_plan(
                    rec, bands, serving=cmp_band)
                seen = int(rec.get("seen") or 0)
                hist = rec.get("bands") or {}

                def _hist_v(b):
                    return (hist.get(str(b)) or {}).get("best") or 0

                pci_now = cur_site.split("-")[0]
                if rec.get("provisional") and not seen:
                    # 只有 mark_site_provisional 动过、没真测过：说「测过 0 次，
                    # 上次来不到 1 小时前」自相矛盾（实机 2026-10-30 就是这么印的）。
                    print(f"\n[2/2] 这个位置（PCI {pci_now}）还没正式测过，"
                          f"上次只是先锁住、没量速度")
                else:
                    print(f"\n[2/2] 这个位置（PCI {pci_now}）之前测过 {seen} 次，"
                          f"上次来是 {_fmt_age(site_age_hours(rec))}")
                others = [str(c.get("pci")) for c in (rec.get("cells") or [])
                          if str(c.get("pci")) != pci_now]
                others += [a.split("-")[0] for a in (rec.get("aliases") or [])
                           if a != cur_site and a.split("-")[0] not in others]
                if others:
                    print(f"      同一处还见过 {'、'.join('PCI ' + p for p in others[:6])}"
                          f"（切频段会换小区，算同一个地方）")
                if state == "stale":
                    print(f"      隔了 {SITE_STALE_DAYS} 天以上没来，历史作废："
                          f"这次全部 {len(bands)} 个频段都重测（覆盖会变，速度更会变）")
                elif state == "aging":
                    print(f"      隔了 {SITE_FRESH_HOURS // 24} 天以上没来，"
                          f"上次的速度数字不再作数：能收到的频段全部重新测速")
                if dead_list:
                    print(f"      跳过 {dead_list}：这里从来没信号")
                if slow_list:
                    desc = " / ".join(f"BAND {b} {_hist_v(b)}" for b in slow_list)
                    print(f"      跳过 {slow_list}：历史只有 {desc} Mbps，"
                          f"比 BAND {known_best}（{_hist_v(known_best)}）慢太多")
                if known_best and state != "stale":
                    print(f"      上次最好：BAND {known_best}，这次先测它")
                tail = ("（一条都不跳）" if state == "stale"
                        else f"（每 {DEAD_REPROBE_EVERY} 次会全测一遍兜底）")
                print(f"      本次测 {len(bands)} 个：{bands}{tail}")
            else:
                why = ("按 --fresh 忽略位置记忆"
                       if getattr(args, "fresh", False)
                       else "这个地方没来过（或记忆已过期被清掉）")
                print(f"\n[2/2] {why}，设备声明的 {len(bands)} 个频段全部实测：{bands}")
            per = SETTLE_SEC + rounds * 20 + 8
            print(f"      逐个实测（每段 {rounds} 轮，有信号的约 {per} 秒，"
                  f"没信号的会提前跳过；预计 {per * len(bands) / 60:.0f} 分钟以内）")
            print("      期间会短暂断网，全部测完自动切回原频段…")

        lte = measure_all_bands(dev, bands, rounds, cur_band, cur_label,
                                speed_url, ping_host, contest_band=cmp_band)

        usable = bands_worth_comparing(lte)
        flap_note = None
        if usable:
            usable.sort(key=lambda r: (-r["mbps"], r["avg"] or 999))
            fastest = usable[0]["band"]
            # 历史样本说「这个快的会周期性掉包」，而旁边有个只慢一点点的，
            # 就换稳的那个。单次 54 秒测不出掉包周期，只能靠攒下来的记录。
            best_band, flap_note = prefer_stable_over_flap(
                usable, (rec or {}).get("bands"), fastest)
            if best_band != fastest:
                demoted = fastest          # 后面选建议时把它排除掉，否则又会被选回来
        if args.bands in ("default", "all") and not args.wifi_only:
            # 记忆过期那次是全测，速度数字就是最新的：把「历史最好」换成这次的，
            # 否则 8 天前那个偏高的旧数字会一直压着，快速模式又会被它骗一次。
            record_site(_site_key(st0), lte, best_band, reset_best=(state == "stale"))

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
    if demoted is not None:
        # 会周期性掉包的那条已经从「最优」里让位了，这里也要把它从候选里拿掉，
        # 否则自动裁决按速度又把它选回来，等于白判。
        cands = [r for r in cands if r["band"] != demoted]
    auto, auto_why = decide_band(cur_res, cands, cmp_band, caveats=caveats)
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
        "flap": flap_note,
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
        if flap_note:
            print(f"  [!] 掉包记录：{flap_note}")

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
        if not getattr(args, "interactive", False):
            # interactive 模式紧接着就会问「要不要应用」，这里再说一遍是废话。
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


def _cli():
    """提示里该用哪个命令前缀。

    手机（Termux）装的是 `mifi` 包装脚本，电脑上是 `python autotune.py`。
    原来一律写 `python autotune.py --restore`，手机用户得自己翻译一遍。
    """
    return "mifi" if probe.is_termux() else "python autotune.py"


def wait_device_back(dev, timeout=45, poll=5):
    """等设备的 web 后台重新可达。

    两种情况后台都会掉 5~20 秒：换频段（重启射频）、改信道（重启 AP）。
    手机上更慢一层 —— 手机自己就是通过这台设备的 WiFi 连着后台的，
    改信道等于把手机的网先掐了，还得等系统自动重连。
    所以「发完指令立刻读状态」必然失败，那不是出错，是还没连上。
    轮询比盲睡可靠：设备 6 秒回来就不必等 26 秒。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            dev.get_state()
            return True
        except Exception:  # noqa: BLE001
            time.sleep(poll)
    return False


def observe_after_apply(ping_host, seconds=OBSERVE_SEC, every=15, per=5):
    """切完之后盯一段时间，抓「刚切能通、过一会儿就断」的情况。

    用户实机（2026-10-30 12:34）：切到 BAND 40 后 3~5 分钟手机弹「没网」，
    几秒后自己恢复。而体检时它 20 秒跑出 29.4 Mbps（当时的 BAND 3 只有 12.0），
    完全看不出这种模式；切完只 ping 6 个包（几秒）同样看不出来。

    「峰值多高」和「能不能久待」是两件事。体检回答前者，这里回答后者。

    seconds=0 表示跳过（--observe 0）。返回 {"rounds","bad","avg"}，跳过时 None。
    """
    if not seconds or seconds <= 0:
        return None
    print(f"\n[观察] 盯 {seconds} 秒，确认切过去之后稳不稳"
          f"（这几分钟里断一下是正常的，不用管）…")
    t0 = time.time()
    lat, bad, rounds = [], 0, 0
    while time.time() - t0 < seconds:
        s = probe.ping_stats(per, ping_host)
        rounds += 1
        if s["avg"] is None:
            bad += 1
            state = "完全不通"
        elif s["loss"] and s["loss"] >= 50:
            bad += 1
            state = f"丢包 {s['loss']}%"
        else:
            lat.append(s["avg"])
            state = f"延迟 {s['avg']}ms  抖动 {s['jitter']}ms  丢包 {s['loss']}%"
        print(f"      {time.strftime('%H:%M:%S')}  {state}")
        time.sleep(max(2, every - per * 1.2))
    if not rounds:
        return None
    if bad >= 2:
        print(f"      [!] {rounds} 次里 {bad} 次不通 —— 这个配置不适合久待")
    else:
        med = round(statistics.median(lat), 1) if lat else "—"
        print(f"      [√] {rounds} 次都通（延迟中位 {med}ms）")
    return {"rounds": rounds, "bad": bad,
            "avg": round(statistics.median(lat), 1) if lat else None}


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
    # 顺序很重要：先动信道，最后动频段。
    # 换频段会让设备重启射频，web 后台跟着掉线 5~20 秒；原来把频段排在前面，
    # 紧接着再写信道，正好撞进重启窗口 —— 实机表现是
    # 「[应用] 切换 WiFi 信道到 1 … → [×] 应用过程中设备无响应」，
    # 结果频段锁好了、信道没改，用户被留在半改状态。
    if t_ch != cur_ch and caps.get("can_set_channel", True):
        todo.append(("channel", t_ch))
    if t_band != cur_band and caps.get("can_lock_band"):
        todo.append(("band", t_band))

    if not todo:
        print("[√] 当前配置已经和体检结论一致，无需改动。")
        _mark_applied(plan, cur_band, cur_ch, "nothing_to_do")
        return 0

    strat_label = {"auto": "自动裁决", "speed": "速度", "stability": "稳定"}.get(
        strategy, strategy)
    print(f"按体检结论应用（策略：{strat_label}）：")
    for kind, val in todo:
        if kind == "band":
            print(f"  · 频段 {_band_label(cur_band)} → {_band_label(val)}")
        else:
            print(f"  · 信道 {cur_ch} → {val}")

    # 体检和应用之间设备被改过（自己动的手、或设备自动换信道）：目标值是按体检时
    # 算的，仍然成立；但数字对不上会让人以为工具记错了，说一句。
    plan_ch = (plan.get("current") or {}).get("channel")
    if plan_ch and cur_ch and plan_ch != cur_ch:
        print(f"  （体检时信道是 {plan_ch}，现在是 {cur_ch} —— 期间被改过，以现在为准）")

    if getattr(args, "dry_run", False):
        print("\n[演练] 以上是「将会做」的改动；加了 --dry-run 所以没有碰设备。")
        if any(k == "band" for k, _ in todo):
            print("       去掉 --dry-run 就会真的切过去（约 60~90 秒，会断两次网）。")
        else:
            print("       去掉 --dry-run 就会真的切过去（约 20~40 秒；"
                  "设备重启 WiFi，本机断 5~20 秒后自动重连）。")
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
                if not wait_device_back(dev, timeout=60):
                    print("[!] 设备后台 60 秒没回来 —— 多半还在重启。")
                    print(f"    等 1 分钟跑 {_cli()} --status 看实际状态；"
                          f"要强制回原样用 {_cli()} --restore。")
                    failed = True
                    break
                print(f"      设备已回来，等它重新注册（{SETTLE_SEC}s）…")
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
                # 改信道 = 设备重启自己的 AP = **手机/电脑先断网**（这台设备的
                # WiFi 就是当前唯一的通道）。所以绝不能「发完就立刻读状态」，
                # 那必定 DeviceUnreachable，还会被当成「应用出错」。
                # 2026-10-30 实机：信道其实已经切成 1 了，界面却报
                # 「[×] 应用过程中设备无响应」+「读不回设备状态」，用户以为失败。
                print("      设备会重启 WiFi，这台机器的连接会断 5~20 秒（正常现象）")
                try:
                    dev.set_channel(val)
                except Unsupported as e:
                    print(f"[跳过] {e}")
                    continue
                except DeviceUnreachable:
                    # 指令多半已经送到，只是设备重启太快、响应没回来。
                    # 当失败处理会误判，先等它回来再核对。
                    print("      指令已发出（设备重启太快，响应没回来 —— 正常）")
                print("      等 WiFi 恢复…")
                if not wait_device_back(dev, timeout=90, poll=5):
                    print("[!] 90 秒还没连回来。")
                    print(f"    手机看 WiFi 是否已重连，然后跑 {_cli()} --status；"
                          f"要强制回原样用 {_cli()} --restore。")
                    failed = True
                    break
                got = dev.get_state()["channel"]
                if got != val:
                    print(f"[!] 信道未生效（当前 {got}），恢复原值 {cur_ch}")
                    try:
                        dev.set_channel(cur_ch)
                        wait_device_back(dev, timeout=90)
                    except Exception:  # noqa: BLE001
                        print(f"    恢复也没发出去，请手动在设备后台把信道改回 {cur_ch}")
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
        print(f"    等 30 秒后跑 {_cli()} --status 看当前状态；"
              f"要强制回原样用 {_cli()} --restore。")
        failed = True

    end = None
    try:
        end = dev.get_state()
    except Exception as e:  # noqa: BLE001
        # 刚切完信道时手机 WiFi 常常还没重连上。直接报错会把「成功」说成「失败」，
        # 多等一轮再下结论（2026-10-30 实机就卡在这里）。
        print(f"\n[!] 暂时读不到设备（{type(e).__name__}），等 WiFi 恢复再试…")
        if wait_device_back(dev, timeout=45, poll=8):
            try:
                end = dev.get_state()
            except Exception as e2:  # noqa: BLE001
                print(f"[×] 还是读不到（{type(e2).__name__}）。")
    if end is None:
        print(f"    改动多半已经生效，只是这台机器还没连回去。")
        print(f"    手机 WiFi 重连后跑 {_cli()} --status 确认；不行就 {_cli()} --restore。")
        return 1
    append_log({"ts": datetime.now().isoformat(), "mode": "apply-plan",
                "plan": plan.get("expected"), "before": {"band": cur_band, "channel": cur_ch},
                "after": {"band": end["band"], "channel": end["channel"]}})
    print(f"\n完成：频段 {_band_label(end['band'])} ｜ 信道 {end['channel'] or '自动'}")
    if failed:
        # 别写成「已回滚」—— 现在可能是「信道改好了、频段没成」，两条路不一样。
        print("（没全部成功，详见上面的 [!] 行；上面显示的是设备的实际状态）")
        _mark_applied(plan, end["band"], end["channel"], "partial_failure")
        return 1
    _mark_applied(plan, end["band"], end["channel"], "ok")
    print(f"原始设置已存 {STATE}，可用 --restore 还原")

    verdict = observe_after_apply(ping_host, getattr(args, "observe", OBSERVE_SEC))
    if verdict and verdict["bad"] >= 2:
        print(f"\n[!] 切过去之后多次不通，说明这个配置在你这个位置撑不住。")
        print(f"    建议回滚：{_cli()} --restore（还原成不锁频段 + 自动信道）")
        _mark_applied(plan, end["band"], end["channel"], "unstable")
        return 1
    if verdict:
        # 说实话：90 秒只能覆盖「切完就出问题」，更晚的抖动靠用户自己回来看。
        print(f"      （头几分钟最容易出问题。若之后出现短暂断网，"
              f"跑 {_cli()} --restore 回原配置）")
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


# ---------------- 全自动守护（--guard） ----------------

def _now_hm():
    return datetime.now().strftime("%H:%M:%S")


def _pci_num(v):
    """PCI 在设备里是字符串（'236'）。拿不到就返回 None —— 不能当 0 用。"""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def guard_is_moving(*, churn, pci_changed, bad_now, band_changed=False):
    """人到底动没动。抽成纯函数，因为这条判断直接决定「要不要解锁」。

    PCI 变只是**必要条件**。2026-10-30 的实测推翻了这个假设：坐在宿舍里、
    锁着 BAND 41 的那三分钟，PCI 自己从 477 跳到 252（-89 → -105dBm）——
    信号变弱时设备会自己重选小区（同频段换扇区），人在原地，PCI 照样跳。

    所以三条判据：
      · 窗口内变够多次（GUARD_MOVE_HITS）—— 真走路 25 分钟换 8 个小区；
      · 但**驻留频段变了**就不算：换频段同样会让 PCI 变、WiFi 重启、ping 全丢，
        「PCI 变 + 链路垮」两条同时成立，原来必然误判成人在动。实机 21:46
        （手机放在桌上没动）：BAND 3→40、PCI 350→236、丢包 100%，判了「移动中」。
        设备自己换频段时信号通常是**变好**的（-98 → -76），人走路不会这样。
      · 剩下那种「PCI 变了、频段没变、链路垮了」才当移动 —— 真走出覆盖时，
        这两件事一定同时发生。
    """
    if churn:
        return True
    if band_changed:
        return False
    return bool(pci_changed and bad_now)


def guard_decide(*, moving, still_sec, settle_sec, locked, bad_streak,
                 cooldown_left, outage_after_unlock):
    """守护状态机：只回答「现在该做什么」，完全不碰设备（为了能单测）。

    规则（用户 2026-10-30 定的）：
      · 在移动、且锁着频段 → 解锁。锁死一个频段，走到没覆盖的地方就断网，
        而设备本来能自己换。这是「保证一直有网」的第一道保险。
      · 原地不动够久、且没锁 → **先选优，再锁定**（跑一次体检测出最优频段）。
        只锁「当前驻留的」不一定最优，可能把一个次优频段长期锁死；
        选优只在久坐时发生一次，不会反复断网（--guard-fast 可跳过选优）。
      · 锁定后连续不通 → 先解锁自救（最快恢复）。解锁后仍然不通 → 才跑完整体检：
        那说明不是「锁错频段」，而是这个位置真的没覆盖/基站有问题，换频段才有意义。
        用户实机：切到 BAND 40 后 3~5 分钟断一次，就是靠这条抓出来的。

    返回 (action, 原因)，action ∈ {None, "unlock", "lock", "measure"}。
    """
    bad = bad_streak >= GUARD_BAD_HITS
    # 自救优先于冷却。冷却本来只管「别反复切频段」，但它要是把自救也挡住，
    # 就变成「一直不通就一直等着」—— 实机对时间才看出来：解锁后置冷却 300s，
    # 「解锁后仍不通」的窗口也正好是 300s，两者互斥，`measure` 那条永远走不到，
    # 等于「锁在坏频段上」的自救链是断的。
    if moving and locked:
        return "unlock", "检测到移动，锁死频段会限制可用覆盖"
    if bad and locked:
        return "unlock", f"锁定后连续 {bad_streak} 次不通，先解锁自救"
    if bad and not locked and outage_after_unlock:
        return "measure", f"解锁后仍然 {bad_streak} 次不通 —— 不是频段问题，换一个试试"
    if cooldown_left > 0:
        return None, f"冷却中（{int(cooldown_left)}s）"
    if not moving and not locked and still_sec >= settle_sec:
        return "lock", f"原地待了 {int(still_sec / 60)} 分钟，锁住当前频段防止它自己乱跳"
    return None, ""


def _guard_unlock(dev, cur_band):
    print(f"      解锁频段（原 BAND {cur_band}）…", flush=True)
    try:
        dev.set_band(0)
    except DeviceUnreachable:
        # 射频重启导致请求本身断掉，指令通常已经送达
        pass
    except Exception as e:  # noqa: BLE001
        print(f"      [!] 解锁失败：{type(e).__name__}: {e}", flush=True)
        return False
    ok = wait_device_back(dev, timeout=60)
    print("      已解锁，设备可以自己换频段了" if ok
          else "      [!] 后台没回来，稍后自动重试", flush=True)
    return ok


def _quick_grade(q):
    """快测结果分四档：good / fair / poor / unknown。

    为什么要分档，而不是一个布尔：单次短下载的重复性很差。实机（2026-10-30
    宿舍）同一张桌子、前后 22 分钟，守护快测 32 Mbps、体检时只有 17.8 和 10.8。
    原来 15 Mbps 的硬阈值正好卡在这个波动区中间 —— 同样的网络，掷到 32 判
    「够用」（不锁，什么也不发生），掷到 12 判「不够用」（触发 5 分钟体检 +
    断网 4 次）。判错的代价完全不对等，所以：
      · 下限压到 8 Mbps —— 只有真的差才承认不够用；
      · 两次采样差 3 倍以上 → unknown（测不准），按「别动它」处理。
    """
    if not q:
        return "unknown"
    loss = q.get("loss")
    loss = 100 if loss is None else int(loss)
    avg = q.get("avg")
    if avg is None or loss >= GUARD_QUICK_MAX_LOSS or avg > GUARD_QUICK_MAX_MS:
        return "poor"
    if q.get("noisy"):
        return "unknown"
    mbps = float(q.get("mbps") or 0)
    if mbps >= GUARD_QUICK_GOOD_MBPS:
        return "good"
    if mbps >= GUARD_QUICK_MIN_MBPS:
        return "fair"
    return "poor"


def _quick_ok(q):
    """快测结果算不算「够用」。

    unknown（两次差太多、测不准）也当够用：证据不足时不动设备，
    比凭一个不可信的数字去断网体检要好。但**压根没测出结果**（q 为空）不算够用。
    """
    if not q:
        return False
    return _quick_grade(q) in ("good", "fair", "unknown")


def guard_quick_probe(speed_url, ping_host, secs=GUARD_QUICK_PROBE_SEC,
                      times=GUARD_QUICK_SAMPLES):
    """在当前连接上快测一遍：短下载 + ping，**完全不碰设备**。

    为什么不复用 measure_band：那个第一件事就是 dev.set_band()，哪怕锁的正是
    同一条频段也可能让射频重启一下。而这一步恰恰发生在「可能只是路过」的时候，
    不该有任何断网风险。

    为什么测两次：一次 4~5 秒的下载，在晚高峰的宿舍里能从 32 Mbps 掉到 10。
    取两次里较好的那次（它更接近这条链路的能力上限），同时把「两次差多少」
    一起交出去 —— 差得离谱时调用方会判「测不准」，不拿它下结论。
    """
    runs = []
    for i in range(max(1, int(times))):
        try:
            p = probe.ping_stats(10, ping_host)
        except Exception:  # noqa: BLE001
            p = {"avg": None, "jitter": None, "loss": 100}
        mbps = 0.0
        if p.get("avg") is not None and (p.get("loss") or 0) < GUARD_BAD_LOSS:
            try:
                mbps = float(probe.throughput(secs=secs, url=speed_url) or 0)
            except Exception:  # noqa: BLE001
                mbps = 0.0
        runs.append({"mbps": mbps, "avg": p.get("avg"), "loss": p.get("loss")})
        if i < times - 1:
            time.sleep(1)

    best = max(runs, key=lambda r: r["mbps"])
    speeds = [r["mbps"] for r in runs]
    noisy = (len(speeds) >= 2 and min(speeds) > 0
             and max(speeds) / min(speeds) >= GUARD_QUICK_NOISY_RATIO)
    q = {"mbps": best["mbps"], "avg": best["avg"],
         # 丢包取最差的那次：一次通不代表链路稳
         "loss": max(r["loss"] or 0 for r in runs),
         "samples": [round(x, 1) for x in speeds], "noisy": noisy}
    q["grade"] = _quick_grade(q)
    q["ok"] = _quick_ok(q)
    return q


def guard_alt_band(st, skip=None):
    """锁定频段判为「不行」时，先在记忆里找第二条，别急着交还给设备。

    交还给设备的下场，多半是「按信号最强又选回刚才那条」——用户 2026-10-30
    的实际体感就是这样，也是守护看起来像厂商自动的原因之一。
    """
    key = _site_key(st)
    if not key:
        return None
    data = load_sites()
    rec = data.get(find_site(data, key))
    if not rec or rec.get("provisional") or site_memory_state(rec) != "fresh":
        return None
    rows = []
    for b, v in (rec.get("bands") or {}).items():
        if skip is not None and str(b) == str(skip):
            continue
        if (v.get("dead") or 0) >= DEAD_SKIP_MIN and not (v.get("ok") or 0):
            continue
        if band_flap(v)[0]:
            continue
        rows.append((float(v.get("best") or 0), int(b)))
    rows.sort(reverse=True)
    return rows[0][1] if rows else None


def guard_memo_speed(key, band):
    """记忆里这个频段的历史最好速度（Mbps）；查不到返回 0。

    只用来和「刚才快测的当前速度」比大小，不参与别的判断 ——
    它可能是几天前、在另一个 PCI 下测出来的，单看它不可信。
    """
    if not key or band is None:
        return 0.0
    try:
        data = load_sites()
        rec = data.get(find_site(data, key)) or {}
        v = (rec.get("bands") or {}).get(str(int(band))) or {}
        return float(v.get("best") or 0)
    except (TypeError, ValueError):
        return 0.0


def guard_memo_rsrp(key, band):
    """记忆里这个频段典型的 RSRP（dBm）；没记过返回 None。

    老记录里没有 RSRP（2026-10-30 之前只记速度），这时返回 None，
    调用方就跳过信号比较 —— 不能把「没记录」当成「信号很好」。
    """
    if not key or band is None:
        return None
    try:
        data = load_sites()
        rec = data.get(find_site(data, key)) or {}
        v = (rec.get("bands") or {}).get(str(int(band))) or {}
        val = v.get("rsrp_med", v.get("rsrp"))
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def guard_pick_lock(memo, memo_mbps, serving, quick,
                    memo_rsrp=None, cur_rsrp=None):
    """久坐时到底锁哪一条。返回 (频段 or None, 依据)；
    依据 ∈ {memo, keep, quick, weak_keep, weak, blind_keep, blind}。

      memo      记忆里的最优频段（None = 没有可信记忆）
      memo_mbps 记忆里那条的历史最好速度
      serving   设备当前驻留的频段
      quick     刚才的快测结果 dict（可能为 None，或测的是别的频段）
      memo_rsrp 记忆里那条频段的典型 RSRP（没记过就是 None）
      cur_rsrp  当前驻留频段此刻的 RSRP

    依据的含义：
      weak_keep 记忆那条现在信号差一截，但当前这条够用 → 留在当前
      weak      记忆那条现在信号差一截、当前也不够用 → 老实跑体检
      blind_keep 记忆没记信号，当前够用 → 不拿未知换已知，留在当前
      blind     记忆没记信号、当前也不够用 → 跑体检（顺便把 RSRP 补上）
      keep      记忆有值，但当前这条刚量出来不比它差多少 → 留在当前，不切
      memo      记忆里的最优明显更快且信号不差 → 切过去（值得断那几秒）
      quick     没有可信记忆，但当前快测够用 → 先锁当前，下次来再全比
    返回 (None, None) 表示信息不够，该老老实实跑一次完整体检。
    """
    cur_ok = bool(quick and quick.get("ok") and serving
                  and quick.get("band") == serving)
    cur_mbps = float(quick.get("mbps") or 0) if cur_ok else 0.0
    # ① 先看信号，再看速度。记忆里的速度是过去某个时刻量的，而 RSRP 差
    #    10dB 是结构性的（穿墙、距离），锁上去今天的下场就是一路掉到掉线。
    #    用户实机 2026-10-30：记忆说 BAND 3 有 46.8 Mbps，那天在这屋 BAND 3
    #    只有 -105dBm、当前驻留的 BAND 40 是 -77dBm，照着记忆锁 = 自残 8 分钟。
    if (memo is not None and memo_rsrp is not None and cur_rsrp is not None
            and str(serving) != str(memo)
            and memo_rsrp <= cur_rsrp - GUARD_LOCK_RSRP_DROP):
        if cur_ok:
            return int(serving), "weak_keep"
        return None, "weak"
    # ② 老记忆没有 RSRP（2026-10-30 之前只记速度），就无从判断那条频段现在
    #    能不能用。这种情况下的「切过去」是拿已知换未知 —— 实机已经吃过一次：
    #    记忆说 BAND 3 有 46.8，实际那天只有 -105dBm。当前够用就别动；
    #    不够用就跑一次体检，顺手把每条频段的 RSRP 补上。
    if (memo is not None and memo_rsrp is None
            and str(serving) != str(memo)):
        if cur_ok:
            return int(serving), "blind_keep"
        return None, "blind"
    if (memo is not None and cur_ok and str(serving) != str(memo)
            and memo_mbps and cur_mbps >= memo_mbps * GUARD_QUICK_KEEP_RATIO):
        return int(serving), "keep"
    if memo is not None:
        return int(memo), "memo"
    if cur_ok:
        return int(serving), "quick"
    return None, None


def mark_site_provisional(key, band, force_best=False):
    """记一笔「这个位置只是先锁住，没实测过」：下次来要补一次完整体检。

    用户 2026-10-30 提的：这套记忆逻辑默认「你去过的地方」，可人也会去新地方。
    新地方第一次不该当场花 5 分钟体检 + 断网四五次 —— 先锁当前驻留的（设备
    本来就在上面，几秒），记住「这只是权宜」，下次再来才认真测。
    """
    if not key:
        return
    data = load_sites()
    real = find_site(data, key)
    _absorb_site(data, real, key)
    rec = data.setdefault(real, {"seen": 0, "bands": {}})
    _remember_cell(rec, key, band)
    rec["last"] = datetime.now().isoformat(timespec="seconds")
    # 这条频段已经真测过 → 不降级成 provisional，也不动它的 best。
    # 否则「先锁一下」会把上次完整体检的结论盖掉：实机 2026-10-30 里，
    # 236 那次 provisional 会把 350 记着的「BAND 3 最优」直接改成 BAND 40。
    # 这处只要有任何一条频段真测过，就不是「没测过的新地方」——
    # 不标 provisional，也不动它的 best。否则「先锁一下」会把上次完整体检的
    # 结论盖掉（实机 2026-10-30：236 那次 provisional 会把 350 记着的
    # 「BAND 3 最优」直接改成 BAND 40）。
    has_data = any(int((v or {}).get("ok") or 0) > 0
                   for v in (rec.get("bands") or {}).values())
    if not has_data:
        rec["provisional"] = True
    # force_best：记忆里的最优被信号闸门否掉时（它现在比当前驻留差太多），
    # 主动把最优改成当前这条，否则下次还会拿同一条来撞闸门。
    if band and (not has_data or force_best):
        rec["best"] = int(band)
    save_sites(data)


def note_band_rsrp(key, band, rsrp):
    """把「此刻这条频段的信号」补进记忆，让下次的信号闸门有据可依。

    为什么单独写一个：老记忆（2026-10-30 之前）只记速度和丢包，没有 RSRP。
    守护因此在 blind 分支里只能跑完整体检 —— 实机 21:56 为此白等 6 分钟、
    断了 7 次网。其实锁定那一刻 RSRP 就在手上（采样里读的），顺手写进去，
    下次同一张桌子再坐满 10 分钟，闸门就能直接比较，不用再测。
    """
    if not key or band is None or rsrp is None:
        return
    try:
        data = load_sites()
        real = find_site(data, key)
        rec = data.get(real)
        if not rec:
            return
        v = (rec.get("bands") or {}).get(str(int(band)))
        if not isinstance(v, dict):
            return
        v["rsrp"] = int(round(float(rsrp)))
        v["rsrp_med"] = int(round(float(rsrp)))
        v["rsrp_at"] = datetime.now().isoformat(timespec="seconds")
        save_sites(data)
    except (TypeError, ValueError):
        return


def guard_memory_band(st):
    """久坐时：能不能凭「这个位置的历史」直接锁定？返回 (频段 or None, 说明)。

    为什么要有这一步：守护原来是「久坐 → 跑一次 5 分钟体检 → 锁」。用户体感
    （2026-10-30）是「手动测一次锁上比设备自动好，但守护用起来反而像厂商的
    自动」—— 因为它每次都从头重新决策，还要断好几次网。可这个位置最优是哪条
    频段，sites.json 里早就写着，够新就直接用。

    四条否决条件，任一条不满足就老老实实重测：
      读不到 PCI / 这个位置没测过 / 记忆不是 fresh（72 小时内）/
      记忆里的最优频段现在没覆盖、或历史上会周期性掉包。
    """
    key = _site_key(st)
    if not key:
        return None, "读不到 PCI，没法查这个位置"
    data = load_sites()
    rec = data.get(find_site(data, key))
    if not rec:
        return None, GUARD_NEW_SITE
    if rec.get("provisional") and not _site_measured(rec):
        # 上次只是「先锁住别乱跳」，没真测过：这次补一次完整体检再谈最优。
        # 注意要带「且没有实测数据」—— 老版本会留下「标记为真、数据其实有」
        # 的记录（实机 2026-10-30 的 236-CMCC），那种记录必须当成有记忆，
        # 否则永远用不上、也永远不会去补测，自己锁死自己。
        return None, "这个位置只做过快速判断（没实测速度）"
    state = site_memory_state(rec)
    if state != "fresh":
        return None, ("位置记忆已过期" if state == "stale"
                      else "位置记忆有点旧（超过 72 小时）")
    band = rec.get("best")
    try:
        band = int(band) if band is not None else None
    except (TypeError, ValueError):
        band = None
    if band is None:
        return None, "记忆里没有最优频段"
    v = (rec.get("bands") or {}).get(str(band)) or {}
    if (v.get("dead") or 0) >= DEAD_SKIP_MIN and not (v.get("ok") or 0):
        return None, f"记忆里的最优 BAND {band} 现在没覆盖"
    if band_flap(v)[0]:
        return None, f"记忆里的最优 BAND {band} 会周期性掉包"
    return band, (f"这个位置 {_fmt_age(site_age_hours(rec))}测过 "
                  f"{int(rec.get('seen') or 0)} 次，最优 BAND {band}")


def _guard_lock_band(dev, band):
    print(f"      锁到 BAND {band}（不重新测，几乎不断网）…", flush=True)
    try:
        dev.set_band(band)
    except DeviceUnreachable:
        # 射频重启导致请求本身断掉，指令通常已经送达
        pass
    except Exception as e:  # noqa: BLE001
        print(f"      [!] 锁定失败：{type(e).__name__}: {e}", flush=True)
        return False
    ok = wait_device_back(dev, timeout=60)
    print(f"      已锁定 BAND {band}" if ok
          else "      [!] 后台没回来，稍后自动重试", flush=True)
    return ok


def _guard_lock_current(dev, serving):
    if not serving:
        print("      [!] 读不到当前驻留的频段，这次先不锁", flush=True)
        return False
    print("      锁当前驻留的频段（--guard-fast，跳过选优）…", flush=True)
    return _guard_lock_band(dev, serving)


def _guard_measure(args):
    """跑一次完整体检。

    另起子进程，会自己登录设备 —— 和守护进程的长连接互相顶掉（KICKOFF），
    所以调用方必须先把 dev 放掉，跑完重连。
    """
    print("      跑完整体检选最优（会断网几次，约 5 分钟）…", flush=True)
    code = run_cycle(args)
    print(f"      体检结束（exit={code}），重新接管设备…", flush=True)
    return code


def _install_stop_signals():
    """让守护能被 SIGTERM 干净地停下（mifi --stop、kill）。

    Ctrl+C 发的是 SIGINT，本来就能停；可手机上那个组合键并不可靠 ——
    实机 2026-10-30 19:45 用户按了没反应，只能强杀整个 Termux 才停掉。
    所以另开一条路：SIGTERM 也转成 KeyboardInterrupt，走同一条收尾路径
    （打印「已停止」、放掉 wake lock），不必再牺牲终端。
    """
    def _raise(_signum, _frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _raise)
        except (ValueError, OSError):
            pass                        # 非主线程装不了，忽略


def guard_still_reset(pci_changed, moving, bad_now, band_changed=False):
    """这一拍要不要把「静止计时」清零。

    只在真发生事的时候清：PCI 真变了，或者链路垮了。**不能写 if moving** ——
    moving 在最后一次换小区之后还会保持整整 GUARD_MOVE_WINDOW（300s），
    每一拍都清零等于把起点一直往后推，把「坐满 10 分钟」拖成最多 15 分钟。
    实机 2026-10-30：19:30 人就坐下了，19:34:55 才开始计时，19:44:29 才锁上。

    换频段（band_changed）也不清：那是设备自己重选，人还坐在原处。实机 21:46
    换了 BAND 3→40，计时被清零白等 2 分钟；要是设备爱换，就会永远坐不满 10 分钟。
    """
    if band_changed:
        return False
    return bool(pci_changed or (moving and bad_now))


def run_guard(args, cfg):
    """全自动守护：打开一次就一直在，按「人在不在动」管频段锁定。"""
    interval = max(15, args.guard_interval)
    settle = max(interval * 4, args.guard_settle)
    quick_sec = max(interval * 2, min(GUARD_QUICK_SEC, settle / 2.0))
    host = cfg["ping_host"]
    dry = args.guard_dry_run

    print("=" * 62)
    print("全自动守护")
    print(f"  每 {interval}s 起一轮采样（PCI + 延迟/丢包），平时不测速")
    print(f"  在动：{GUARD_MOVE_WINDOW}s 内 PCI 变 {GUARD_MOVE_HITS} 次、或变了且链路垮了 → 解锁"
          f"（设备自己换频段不算，那只是它重启射频）")
    print(f"  停下 {int(quick_sec)}s：先快测当前这条（约 20 秒，只测不锁）—— 很可能只是路过")
    print(f"  坐满 {int(settle)}s：才锁 —— 先拿记忆里的最优跟当前的信号/速度比，都不合适才跑体检")
    print(f"  锁定后连续 {GUARD_BAD_HITS} 次不通、丢包≥{GUARD_BAD_LOSS}% 或延迟≥{GUARD_LAG_MS}ms"
          f" → 先换备选，没有才解锁")
    print(f"  Ctrl+C 停止（按不出来就在另一个窗口跑 mifi --stop）"
          f"{'  ｜ 演练模式：只报告，不改设备' if dry else ''}")
    print("=" * 62, flush=True)

    _install_stop_signals()

    dev = None
    last_pci, last_rsrp, stable_since, pci_log = None, None, time.time(), deque()
    last_serving = None
    last_key = ""
    bad_streak, next_allowed = 0, 0.0
    unlocked_at = None
    quick, quick_done = None, False      # 快测结果与「这次停留已经测过没」
    speed_url = None

    try:
        while True:
            now = time.time()
            if dev is None:
                try:
                    dev = detect(args.host, args.user, args.password, name=args.driver)
                except Exception as e:  # noqa: BLE001
                    # 只给异常类名等于没说：LookupError 的消息本身就是「找不到
                    # 这台设备的后台」这类人话，TimeoutError 才说明是没响应。
                    # 实机 2026-10-30 19:07 连打三行 LookupError，看不出是
                    # 「WiFi 还没连上」还是「设备在重启」。
                    print(f"{_now_hm()}  连不上设备：{e or type(e).__name__}"
                          f"，5 秒后重试", flush=True)
                    time.sleep(5)
                    continue
            try:
                st = dev.get_state()
            except Exception as e:  # noqa: BLE001
                print(f"{_now_hm()}  设备暂时无响应（{type(e).__name__}），稍后重连",
                      flush=True)
                dev = None
                time.sleep(interval)
                continue

            ping = probe.ping_stats(10, host)
            sig = st.get("signal") or {}
            pci = _pci_num(sig.get("pci"))
            rsrp = _num(sig.get("rsrp"))
            locked_band = st.get("band") or 0
            serving = locked_band or _serving_band(st)
            locked = locked_band != 0

            # 「这条链路现在还撑得住吗」。除了不通和丢包过半，延迟离谱也算：
            # 实机 17:24 延迟 4402ms、只丢 10%，原来会被当成完全正常。
            avg_ms = ping["avg"]
            bad_now = (avg_ms is None or (ping["loss"] or 0) >= GUARD_BAD_LOSS
                       or avg_ms >= GUARD_LAG_MS)
            bad_streak = bad_streak + 1 if bad_now else 0

            pci_changed = (pci is not None and last_pci is not None
                           and pci != last_pci)
            dropped_rsrp = (rsrp is not None and last_rsrp is not None
                            and rsrp <= last_rsrp - GUARD_RESELECT_DB)
            if pci_changed:
                pci_log.append(now)
            if pci is not None:
                last_pci = pci
            if rsrp is not None:
                last_rsrp = rsrp
            while pci_log and now - pci_log[0] > GUARD_MOVE_WINDOW:
                pci_log.popleft()

            # 驻留频段变了 = 设备自己换频段（厂商逻辑本来就不锁定、自动分配）。
            # 这个信息必须单独拎出来：它会同时造成「PCI 变」和「WiFi 重启→丢包 100%」，
            # 按旧判据必然被当成「人在动」。
            band_changed = bool(last_serving and serving
                                and str(serving) != str(last_serving))
            if serving:
                last_serving = serving

            churn = len(pci_log) >= GUARD_MOVE_HITS
            moving = guard_is_moving(churn=churn, pci_changed=pci_changed,
                                     bad_now=bad_now, band_changed=band_changed)
            if pci_changed and not moving:
                # 人没动、设备自己换的小区 → 记进同一处，这地方以后认得它
                note_cell_change(last_key, _site_key(st))
            last_key = _site_key(st)
            # 静止计时只在「这一刻真发生了事」时归零：PCI 真的变了，或者链路垮了。
            # 原来写的是 if moving —— 而 moving 在最后一次换小区之后还会继续保持
            # 整整 GUARD_MOVE_WINDOW（300s），于是每一轮都把起点往后推，把「坐满
            # 10 分钟」拖成最多 15 分钟。实机 2026-10-30 19:30 人就坐下了，
            # 19:34:55 才开始计时，19:44:29 才锁上。
            if guard_still_reset(pci_changed, moving, bad_now,
                                 band_changed=band_changed):
                stable_since = now
            still = now - stable_since
            outage_after_unlock = (unlocked_at is not None
                                   and now - unlocked_at < GUARD_RESCUE_WINDOW)

            if moving:
                quick, quick_done = None, False     # 一动，之前那次快测就作废了
            # 没锁着才需要看记忆：锁着的时候只关心「它还能不能撑住」
            memo, memo_why = (None, "") if locked else guard_memory_band(st)
            # 记忆里的最优现在信号是不是已经不行了（比当前驻留差一截）。
            # 这一刻就要算出来：既决定「要不要先快测当前」，也决定「准不准锁它」。
            memo_rsrp = (guard_memo_rsrp(_site_key(st), memo)
                         if memo is not None else None)
            memo_weak = bool(
                memo is not None and memo_rsrp is not None and rsrp is not None
                and str(serving) != str(memo)
                and memo_rsrp <= rsrp - GUARD_LOCK_RSRP_DROP)

            # ② 停下 quick_sec 秒：快测当前这条。只测、不锁、不动设备。
            # 目的有两个：万一只是路过，什么都没发生；真久坐的话，10 分钟那一步
            # 就能凭「够用 + 频段没变」直接锁，省掉一次 5 分钟体检。
            # memo_weak 时也要测：记忆那条不可用了，得看当前这条顶不顶得住。
            # memo_rsrp is None 也要测：老记忆（2026-10-30 之前）只记了速度、没记
            # 信号，这种情况下 guard_pick_lock 只能判「blind」。原来不测快测，
            # cur_ok 永远是 False → blind → 直接跑 6 分钟体检。实机 2026-10-30
            # 21:56 就这么在白等 6 分钟、断网 7 次之后，还被切到了 -95dBm。
            if (not moving and not locked and not quick_done and not bad_now
                    and still >= quick_sec
                    and (memo is None or memo_weak or memo_rsrp is None)):
                if speed_url is None:
                    speed_url = probe.resolve_speed_url(cfg.get("speed_url"),
                                                        verbose=False)
                print(f"{_now_hm()}  先快测当前这条（不锁，只看够不够用）…", flush=True)
                quick = guard_quick_probe(speed_url, host)
                quick["band"] = serving
                quick_done = True
                grade_cn = {"good": "明确够用", "fair": "勉强够用",
                            "unknown": "测不准（两次差太多）",
                            "poor": "不够用"}
                g = quick.get("grade") or ("good" if quick["ok"] else "poor")
                sam = " / ".join(f"{x:.0f}" for x in (quick.get("samples") or []))
                # 两次差 1.5 倍以上就把倍数写出来：「12 Mbps（两次 6 / 12）」
                # 本身不假，但读者不会立刻意识到这条链路的带宽在坐过山车，
                # 而这个数字接下来要拿来决定锁哪条频段。
                vals = [x for x in (quick.get("samples") or []) if x]
                spread = ""
                if len(vals) >= 2 and min(vals) > 0 and max(vals) / min(vals) >= 1.5:
                    spread = f"，两次差 {max(vals) / min(vals):.1f} 倍"
                lat = f"{quick['avg']}ms" if quick["avg"] is not None else "不通"
                print(f"      {quick['mbps']:.0f} Mbps（两次 {sam}{spread}）｜ 延迟 {lat} ｜ "
                      f"丢包 {quick['loss']}% → {grade_cn.get(g, g)}"
                      f"（{'先不动它' if quick['ok'] else '坐久了会重新选'}）",
                      flush=True)

            lat = f"{ping['avg']}ms" if ping["avg"] is not None else "不通"
            who = (f"锁定 BAND {locked_band}" if locked
                   else f"未锁（驻留 {serving or '?'}）")
            if moving:
                tag = "移动中"
            elif still >= settle:
                # 已经达标就别再印「2/1 分钟」——分母不动、分子一直涨，看着像坏了。
                # 达标后没动作只有一种原因：已经锁着了（锁定状态下不再重复锁）。
                tag = f"静止 {int(still / 60)} 分钟（够久了{'，已锁定' if locked else ''}）"
            else:
                tag = f"静止 {int(still / 60)}/{max(1, int(settle / 60))} 分钟"
            if band_changed and not moving:
                # 换频段和换小区都会被当成「PCI 变了」，必须分开写清楚：
                # 前者是设备自己换频段（WiFi 会重启，那几秒必然丢包），
                # 后者是同频段内换扇区。两者都不代表人在动。
                tag += "（设备换频段，不算人动）"
            elif pci_changed and not moving:
                # 让人看得懂「为什么 PCI 变了却还在计时」：是设备自己换的小区。
                tag += ("（换小区·设备重选，信号掉了）" if dropped_rsrp
                        else "（换小区·设备重选）")
            print(f"{_now_hm()}  {who:<18} PCI {pci if pci is not None else '—':<4} "
                  f"{sig.get('rsrp') or '—':<8} 延迟 {lat:<7} 丢包 {ping['loss']}%  {tag}",
                  flush=True)

            action, why = guard_decide(
                moving=moving, still_sec=still, settle_sec=settle, locked=locked,
                bad_streak=bad_streak, cooldown_left=max(0.0, next_allowed - now),
                outage_after_unlock=outage_after_unlock)

            if action:
                print(f"      → {why}", flush=True)
                if dry:
                    print("      （演练模式，未改动设备）", flush=True)
                elif action == "unlock" and bad_streak >= GUARD_BAD_HITS and not moving:
                    # 锁着但真的不行了：先换记忆里的备选，别直接交还给设备 ——
                    # 设备只会按信号最强又选回刚才那条（用户的真实体验）。
                    alt = guard_alt_band(st, skip=locked_band)
                    if alt:
                        print(f"      换到记忆里的备选 BAND {alt}（不交还给设备）…", flush=True)
                        _guard_lock_band(dev, alt)
                        unlocked_at = None
                    else:
                        _guard_unlock(dev, locked_band)
                        unlocked_at = time.time()
                        pci_log.clear()
                    bad_streak = 0
                    quick, quick_done = None, False
                elif action == "unlock":
                    _guard_unlock(dev, locked_band)
                    unlocked_at = time.time()
                    pci_log.clear()
                    bad_streak = 0
                    quick, quick_done = None, False
                elif action == "lock":
                    # 久坐才走到这里（路过不锁）。锁哪一条交给 guard_pick_lock：
                    # 它会把「记忆里的历史最优」和「刚才快测到的当前速度」放在一起比。
                    if getattr(args, "guard_fast", False):
                        _guard_lock_current(dev, serving)
                    else:
                        key_now = _site_key(st)
                        memo_best = guard_memo_speed(key_now, memo) if memo else 0.0
                        band, kind = guard_pick_lock(
                            memo, memo_best, serving, quick,
                            memo_rsrp=memo_rsrp, cur_rsrp=rsrp)
                        if kind == "weak_keep":
                            print(f"      → 记忆里 BAND {memo} 当时只有 {memo_rsrp:.0f}dBm，"
                                  f"当前的 BAND {serving} 是 {rsrp:.0f}dBm"
                                  f"（差 {abs(rsrp - memo_rsrp):.0f}dB，超过 "
                                  f"{GUARD_LOCK_RSRP_DROP}dB 就不锁它了），锁当前这条",
                                  flush=True)
                            _guard_lock_band(dev, band)
                            note_band_rsrp(key_now, band, rsrp)
                            mark_site_provisional(key_now, band, force_best=True)
                        elif kind == "blind_keep":
                            print(f"      → 记忆里 BAND {memo} 是 {memo_best:.0f} Mbps，"
                                  f"但当时没记信号，说不准它现在还能不能用；"
                                  f"当前这条够用，先不赌，锁当前 BAND {serving}", flush=True)
                            _guard_lock_band(dev, band)
                            note_band_rsrp(key_now, band, rsrp)
                            mark_site_provisional(key_now, band, force_best=True)
                        elif kind == "keep":
                            # 记忆里的最优可能是几天前、甚至换了 PCI 测的；
                            # 当前这条是几分钟前刚量的。差得不多就别切。
                            gap = int(round((1 - GUARD_QUICK_KEEP_RATIO) * 100))
                            print(f"      → 记忆里 BAND {memo} 是 {memo_best:.0f} Mbps，"
                                  f"但刚才实测当前 BAND {serving} 有 "
                                  f"{float(quick.get('mbps') or 0):.0f} Mbps"
                                  f"（差不到 {gap}%），锁当前这条，不切换", flush=True)
                            _guard_lock_band(dev, band)
                            note_band_rsrp(key_now, band, rsrp)
                        elif kind == "memo":
                            print(f"      → {memo_why}，直接锁定（不重测）", flush=True)
                            _guard_lock_band(dev, band)
                            note_band_rsrp(key_now, band, rsrp)
                        elif kind == "quick":
                            print(f"      → 当前 BAND {serving} 快测够用"
                                  f"（{float(quick.get('mbps') or 0):.0f} Mbps / "
                                  f"{quick['avg']}ms），直接锁它；其余频段下次来再比",
                                  flush=True)
                            _guard_lock_band(dev, band)
                            note_band_rsrp(key_now, band, rsrp)
                            mark_site_provisional(key_now, band)
                        else:
                            why = ("记忆里的最优现在信号差太多、当前这条也不够用"
                                   if kind == "weak" else
                                   "记忆里没有信号记录、当前这条也不够用"
                                   if kind == "blind" else
                                   (memo_why or "没有可信依据"))
                            print(f"      → {why}，跑一次完整体检再锁", flush=True)
                            dev = None
                            _guard_measure(args)
                    stable_since = time.time()
                    quick, quick_done = None, False
                else:  # measure：解锁后仍然不通，说明不是「锁错了」
                    dev = None
                    _guard_measure(args)
                    stable_since = time.time()
                    quick, quick_done = None, False

                next_allowed = time.time() + GUARD_COOLDOWN
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[守护] 已停止（设备保持当前配置）")
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
    changes = plan.get("changes") or []

    if not avail and not changes:
        # 频段和信道都没什么可改的：上面的结论已经说清原因，这里不再重复。
        return 0

    print("\n" + "=" * 56)
    if not avail:
        # 只有信道建议（频段那边保持现状）。
        # 原来这个分支直接 return 0 —— 用户看到「信道 11 → 7，干扰降 74%」，
        # 却没有任何「要不要应用」的提示，只能自己去敲 --apply-plan
        # （2026-10-30 实机反馈）。
        print("要现在就应用吗？回车 = 只改信道；n = 不改")
        # 断网这句话必须说实话：这台设备的 WiFi 就是当前唯一通道，
        # 改信道 = AP 重启 = 本机先断 5~20 秒。写成「不会断网」会让用户
        # 以为卡死了（2026-10-30 实机就是先看到报错再看到这句话）。
        print("（设备会重启 WiFi，本机断 5~20 秒；Termux 里不用管，会自动重连）")
    elif options.get("auto"):
        a = options["auto"]
        print(f"要现在就应用吗？回车 = 按自动建议切到 BAND {a['band']}（{a['reason']}）")
    else:
        print("要现在就应用吗？回车 = 应用上面的建议")
    if avail:
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
        print(f"已跳过，设备保持原样。下次想应用直接跑：{_cli()} --apply-plan")
        return 0
    if ans.startswith("a") and options.get("speed"):
        args.strategy = "speed"
    elif ans.startswith("b") and options.get("stability"):
        args.strategy = "stability"
    elif avail:
        args.strategy = "auto" if options.get("auto") else avail[0]
    # 只有信道建议时不动 strategy：run_apply_plan 会沿用它自己解析出的策略，
    # 而那时目标本来就只含信道。原来这里直接取 avail[0]，avail 空会 IndexError。

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
    ap.add_argument("--observe", type=int, default=OBSERVE_SEC, metavar="秒",
                    help=f"应用后盯多少秒确认稳定（0 = 不盯，默认 {OBSERVE_SEC}）")
    ap.add_argument("--fresh", action="store_true",
                    help=f"忽略位置记忆，把全部频段重测一遍"
                         f"（隔 {SITE_STALE_DAYS} 天以上没来会自动这样，一般不用手动给）")
    ap.add_argument("--forget", action="store_true",
                    help="清空位置记忆（搬过家、换过城市之后用）")
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
    ap.add_argument("--guard", action="store_true",
                    help="全自动守护：在动就解锁频段、久坐就锁住，避免锁死导致没网")
    ap.add_argument("--guard-interval", type=int, default=GUARD_INTERVAL,
                    help=f"守护采样间隔秒数（默认 {GUARD_INTERVAL}）")
    ap.add_argument("--guard-settle", type=int, default=GUARD_SETTLE_SEC,
                    help=f"连续多少秒 PCI 不变算「久坐」，久坐才锁定（默认 {GUARD_SETTLE_SEC}）")
    ap.add_argument("--guard-fast", action="store_true",
                    help="久坐时直接锁当前驻留的频段、不做选优（省几分钟，但不保证最优）")
    ap.add_argument("--guard-dry-run", action="store_true",
                    help="守护演练：只报告会做什么，不改设备")
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
        best = scanner.pick_best_channel(rows)
        std = scanner.STANDARD_2G_CHANNELS

        # 列出**全部** 13 条信道。原来只印前 5 条，用户以为「没印出来的 = 没检测到
        # = 最干净」，实际上每条都算过、0 分的就在里面（2026-10-30 用户提问）。
        print(f"\n扫到 {len(aps)} 个 AP。全部 {len(rows)} 条信道的干扰分"
              f"（0 最好，越低越干净）：")
        for r in sorted(rows, key=lambda x: x["ch"]):
            note = "标准信道" if r["ch"] in std else ""
            if r["ch"] == best["ch"]:
                note = (note + "  ← 推荐").strip()
            print(f"  信道 {r['ch']:>2}   {r['score']:>5}   {note}")

        zeros = [r["ch"] for r in rows if r["score"] == 0]
        if zeros:
            print(f"\n  {zeros} 号一条邻居都没听到（0 分）。")
            print("  但「没听到」不等于「没人用」：扫描在每个信道只停几十毫秒，")
            print("  信号弱的 AP 和隐藏网络（不广播名字）很容易漏；而且 2.4G 只有")
            print("  1/6/11 三条互不重叠，其余信道都和邻居部分重叠 —— 邻居一活跃，")
            print("  它的分数就跟着涨。所以 0 分是「现在没人用」，不是「永远干净」。")
        else:
            print(f"\n  这次 {len(rows)} 条信道全都有邻居压着，一条 0 分的都没有 ——")
            print("  「挑一条没人用的」这个选项其实不存在，只能在被占用的里面选相对干净的。")

        if best["ch"] != rows[0]["ch"]:
            # 被「标准信道优先」改选过，得说清为什么不是分最低的那个。
            # 注意分数可能并列（都是 0.0），这时说「更低」就是错话 —— 实机出现过。
            low = rows[0]
            if abs(low["score"] - best["score"]) < 0.01:
                print(f"\n  {low['ch']} 号与 {best['ch']} 号分数并列（{low['score']}）；"
                      f"同等条件下优先 1/6/11，所以选 {best['ch']}。")
            else:
                print(f"\n  {low['ch']} 号分更低（{low['score']}），但它是夹缝信道：")
                ov = low.get("max_overlap") or 0
                print(f"  它和邻居只重叠 {ov}MHz（一条 20MHz 信道的大半是空的），"
                      f"分数低是因为「错开了几 MHz」，不是因为「真的空」。")
                print(f"  那条邻居一换信道、或者只是流量变忙，它的优势立刻消失 ——")
                print(f"  而 1/6/11 是结构性优势：邻居在标准信道上就是完全重叠，")
                print(f"  分数如实反映。差距不到 {scanner.CHANNEL_PREF_MARGIN} 时优先标准信道。")

        print(f"\n推荐：信道 {best['ch']}（干扰分 {best['score']}）")
        print("这里只扫描，不会改设备。想连频段一起优化就跑 `mifi`。")
        return 0

    if args.list_drivers:
        print("可用驱动：")
        for d in list_drivers():
            print(f"  {d['name']:<12} {d['description']}")
        print("\n新增机型：参考 drivers/template.py，在 drivers/__init__.py 里登记一行。")
        return 0

    if args.forget:
        data = load_sites()
        if data:
            save_sites({})
            print(f"位置记忆已清空（原有 {len(data)} 个位置）。")
        else:
            print("还没有位置记忆，不用清。")
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

    if args.guard:
        return run_guard(args, cfg)

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
    band_txt, band_warn = describe_band_state(cur_band, _serving_band(st0))
    print(f"当前状态：频段 {band_txt} | "
          f"信道 {cur_ch or '自动'} | 带宽 {st0.get('bandwidth') or '—'} | MTU {st0.get('mtu') or '—'}")
    if band_warn:
        print("[!] 锁定的频段和设备实际在用的不是同一个 —— 锁定没生效，"
              "多半是这个频段信号太差、设备自己回落了。")
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
        # 还原是「断网时救命」的命令，绝不能崩。切频段会让射频重启，紧接着
        # 改信道必然打不进去：原来这里直接抛 DeviceUnreachable、带一屏 traceback
        # 退出；更糟的是「[还原] 信道 -> 自动」是先打印再调用，于是屏幕显示成功、
        # 实际没做（实机 2026-10-30 19:06 就这么崩的，信道一直停在 1）。
        # 现在：先做后说，等设备回来再动下一步，失败也只给一句人话。
        print("\n[还原] 频段 -> 未锁定")
        try:
            dev.set_band(0)
            time.sleep(SETTLE_SEC)
        except Unsupported as e:
            print(f"[跳过] {e}")
        except DeviceUnreachable:
            pass                    # 射频正在重启，本来就该断一下，下面统一等它
        back = wait_device_back(dev, timeout=60, poll=3)
        if not back:
            print("[!] 设备 60 秒没回来，信道这一步先跳过 —— 等它恢复后再跑一次 --restore")
        else:
            try:
                dev.set_channel(0)
                print("[还原] 信道 -> 自动")
                time.sleep(4)
            except Unsupported as e:
                print(f"[跳过] {e}")
            except DeviceUnreachable:
                # 改信道会把手机自己的 WiFi 掐掉，这条几乎必现，不算失败。
                print("[!] 改信道时连接断了一下；多半已生效，稍后用 --status 确认")
        if back:
            print("[√] 已还原。建议等待 30 秒重新注册。")
        else:
            print("[!] 只做了一半：频段已解开，信道没动（设备当时不可达）")
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
        return 0 if back else 1

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
