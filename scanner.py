"""跨平台 2.4G 扫描层：把「系统怎么扫 WiFi」抽象出来。

引擎只关心结果，不关心你在 Windows / Linux / macOS。
返回统一结构：

    [{"ssid": "DUFE", "bssid": "aa:bb:...", "ch": 6, "sig": 82, "band": "2.4"}, ...]

其中 sig 统一归一化到 0~100（因为 Windows 给百分比、Linux 给 dBm）。

支持的来源：
  - Windows : netsh wlan show networks mode=bssid（多语言：中/英/日/韩/德/法/西/意/葡/俄/繁）
  - Linux   : nmcli（NetworkManager）→ 失败则回落 iw
  - macOS   : airport -s（macOS 14+ 已移除该工具，届时请用 --scan-cmd 或手动指定信道）

多轮合并为什么必要：
  netsh 的扫描结果是缓存的，单次调用几乎必然漏掉部分 AP；不合并就会得出
  "某个信道很干净"的错误结论。默认 5 轮，同一 AP 至少出现 2 轮才计入。
"""

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time

# ---------------------------------------------------------------- 工具函数


def _env():
    """剥掉代理环境变量：扫描和测速都必须走真实链路，不能经过本地代理。"""
    return {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}


def sh(cmd, timeout=60):
    """执行命令并尽最大努力解码。Windows 控制台输出常用 OEM 代码页（简体中文为 GBK）。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=_env())
    except Exception:  # noqa: BLE001
        return ""
    for enc in ("oem", "utf-8", "gbk", "cp936", "latin-1"):
        try:
            return p.stdout.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return p.stdout.decode("utf-8", "ignore")


# ---------------------------------------------------------------- 关键词表
#
# netsh 的字段名跟着系统显示语言走，所以不能只匹配中文。
# 每个列表覆盖常见语言；匹配方式为「包含」，忽略大小写。

SIGNAL_KEYS = (
    "信号", "訊號", "信号强度", "signal", "signalstärke", "señal", "segnale",
    "intensité", "sinal", "電波", "強度", "신호", "уровень", "сигнал",
)

BAND_KEYS = (
    "波段", "频段", "頻段", "band", "バンド", "대역", "bando", "frequenzband",
    "banda", "диапазон",
)

CHANNEL_KEYS = (
    "信道", "频道", "頻道", "channel", "kanal", "canal", "canale",
    "チャンネル", "채널", "канал",
)


def _classify(key):
    """把本地化的字段名归类成 signal / band / channel / None。"""
    k = key.strip().lower()
    if any(t in k for t in CHANNEL_KEYS):
        return "channel"
    if any(t in k for t in SIGNAL_KEYS):
        return "signal"
    if any(t in k for t in BAND_KEYS):
        return "band"
    return None


def _num(s):
    m = re.search(r"-?\d+", s or "")
    return int(m.group()) if m else None


# ---------------------------------------------------------------- Windows


def _scan_windows(ssid_hint=None):
    out = sh(["netsh", "wlan", "show", "networks", "mode=bssid"], timeout=60)
    aps = []
    cur = None          # {"ssid":..., "sig":..., "band":..., "bssid":...}

    def flush():
        nonlocal cur
        if cur and cur.get("ch") is not None:
            aps.append(cur)
        cur = None

    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^SSID\s+\d+\s*:\s*(.*)$", s, re.I)
        if m:
            flush()
            name = m.group(1).strip()
            cur = {"ssid": name, "bssid": "", "sig": None, "band": None, "ch": None}
            continue
        if ":" not in s:
            continue
        key, val = s.split(":", 1)
        kind = _classify(key)
        if kind is None or cur is None:
            continue
        if kind == "signal":
            n = _num(val)
            if n is not None:
                cur["sig"] = max(0, min(100, n))
        elif kind == "band":
            cur["band"] = val.strip()
        elif kind == "channel":
            n = _num(val)
            if n is not None:
                cur["ch"] = n
                flush()          # 频道是该 BSSID 块最后一个字段
    flush()
    return aps


# ---------------------------------------------------------------- Android（Termux）


def freq_to_channel(freq):
    """中心频率 → 信道号。Termux 给的是 MHz，netsh 直接给信道号。"""
    if not freq:
        return None
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if 5000 <= freq <= 5900:
        return (freq - 5000) // 5
    return None


def _scan_termux():
    """Android 上通过 Termux:API 拿扫描结果。

    需要：装 Termux:API 应用 + `pkg install termux-api` + 授予定位权限
    （Android 10+ 把 WiFi 扫描算作位置信息，没权限会返回空数组）。
    """
    if not shutil.which("termux-wifi-scaninfo"):
        return []
    out = sh(["termux-wifi-scaninfo"], timeout=45)
    try:
        data = json.loads(out)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    aps = []
    for ap in data:
        ssid = (ap.get("ssid") or "").strip()
        freq = ap.get("frequency")
        if not ssid or not freq:
            continue
        ch = freq_to_channel(int(freq))
        if not ch:
            continue
        lvl = ap.get("level")
        # Termux 给 dBm，和 Windows 的百分比统一到 0~100
        sig = max(0, min(100, 2 * (int(lvl) + 100))) if lvl is not None else 0
        aps.append({"ssid": ssid, "bssid": ap.get("bssid", ""),
                    "ch": ch, "sig": sig, "band": None})
    return aps


# ---------------------------------------------------------------- Linux


def _nmcli_unescape(s):
    return s.replace("\\:", ":").replace("\\\\", "\\")


def _scan_nmcli():
    if not shutil.which("nmcli"):
        return []
    out = sh(["nmcli", "-t", "-f", "SSID,CHAN,SIGNAL", "dev", "wifi", "list", "--rescan", "yes"], timeout=60)
    aps = []
    for line in out.splitlines():
        parts = re.split(r"(?<!\\):", line)
        if len(parts) < 3:
            continue
        ssid = _nmcli_unescape(parts[0]).strip()
        ch = _num(parts[1])
        sig = _num(parts[2])
        if not ssid or ch is None:
            continue
        aps.append({"ssid": ssid, "bssid": "", "ch": ch,
                    "sig": max(0, min(100, sig or 0)), "band": None})
    return aps


def _scan_iw():
    """iw 需要 root 才能主动扫描，这里只解析当前缓存的结果。dBm → 百分比做近似换算。"""
    if not shutil.which("iw"):
        return []
    devs = sh(["iw", "dev"], timeout=20)
    m = re.search(r"Interface\s+(\S+)", devs)
    if not m:
        return []
    out = sh(["iw", "dev", m.group(1), "scan"], timeout=40)
    aps, cur = [], None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("BSS "):
            if cur and cur.get("ch"):
                aps.append(cur)
            cur = {"ssid": "", "bssid": s.split()[1].rstrip("("), "sig": 0,
                   "band": None, "ch": None}
        elif cur is not None:
            if s.startswith("SSID:"):
                cur["ssid"] = s.split(":", 1)[1].strip()
            elif s.startswith("signal:"):
                dbm = _num(s.split(":", 1)[1])
                if dbm is not None:                      # -30dBm≈100%，-90dBm≈20%
                    cur["sig"] = max(0, min(100, 2 * (dbm + 100)))
            elif s.startswith("DS Parameter set: channel"):
                cur["ch"] = _num(s.split("channel")[-1])
    if cur and cur.get("ch"):
        aps.append(cur)
    return aps


def _scan_linux():
    return _scan_nmcli() or _scan_iw()


# ---------------------------------------------------------------- macOS


def _scan_macos():
    tool = ("/System/Library/PrivateFrameworks/Apple80211.framework/"
            "Versions/Current/Resources/airport")
    if not os.path.exists(tool):
        return []
    out = sh([tool, "-s"], timeout=40)
    aps = []
    for line in out.splitlines()[1:]:
        m = re.match(r"^(.*?)\s+([0-9a-fA-F:]{17})\s+(-?\d+)\s+(\d+)", line)
        if not m:
            continue
        aps.append({"ssid": m.group(1).strip(), "bssid": m.group(2),
                    "sig": max(0, min(100, 2 * (int(m.group(3)) + 100))),
                    "band": None, "ch": int(m.group(4))})
    return aps


# ---------------------------------------------------------------- 统一入口


def scan_once(ssid_hint=None):
    """单轮扫描，返回原始 AP 列表（未合并、未过滤）。"""
    if sys.platform.startswith("win"):
        return _scan_windows(ssid_hint)
    if sys.platform == "darwin":
        return _scan_macos()
    # Android(Termux) 上 nmcli/iw 都不可用，先试 Termux:API
    return _scan_termux() or _scan_linux()


def scan_2g(rounds=5, delay=3, min_hits=2, own_ssid=None):
    """多轮扫描合并。

    - 同一 (ssid, ch) 在 >= min_hits 轮里出现才计入，滤掉一闪而过的幽灵 AP
    - 信号强度取多轮中位数，避免某一轮的信号骤降拉低整条结论
    - 只保留 2.4G（信道 1~14），own_ssid 指定的自己的 AP 不计入干扰源
    """
    samples, hits = {}, {}
    for i in range(rounds):
        if i:
            time.sleep(delay)
        for ap in scan_once():
            ch = ap.get("ch")
            if not ch or ch > 14:          # 5G 直接跳过
                continue
            band = (ap.get("band") or "").lower()
            if band and "2.4" not in band:
                continue
            if own_ssid and ap.get("ssid") == own_ssid:
                continue
            key = (ap.get("ssid") or "", ch)
            samples.setdefault(key, []).append(ap.get("sig") or 0)
            hits.setdefault(key, 0)
            hits[key] += 1

    aps = []
    for (ssid, ch), sigs in samples.items():
        if hits[(ssid, ch)] < min_hits:
            continue
        aps.append({"ssid": ssid, "ch": ch,
                    "sig": int(statistics.median(sigs)),
                    "hits": hits[(ssid, ch)]})
    aps.sort(key=lambda a: (-a["sig"], a["ch"]))
    return aps


def connected_ssid():
    """当前电脑连着的 WiFi 名称，用来默认排除自己的 AP。连的是 USB 共享时返回空。"""
    if sys.platform.startswith("win"):
        out = sh(["netsh", "wlan", "show", "interfaces"], timeout=20)
        for line in out.splitlines():
            s = line.strip()
            # 英文 "SSID : xxx"；中文同样是 "SSID : xxx"（不会本地化）
            if re.match(r"^SSID\s*:", s, re.I):
                v = s.split(":", 1)[1].strip()
                if v:
                    return v
                return None
    elif sys.platform == "darwin":
        out = sh(["/System/Library/PrivateFrameworks/Apple80211.framework/"
                  "Versions/Current/Resources/airport", "-I"], timeout=20)
        m = re.search(r"^\s*SSID:\s*(.+)$", out, re.M)
        return m.group(1).strip() if m else None
    else:
        out = sh(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"], timeout=20)
        for line in out.splitlines():
            if line.startswith("yes:"):
                return _nmcli_unescape(line.split(":", 1)[1]).strip()
    return None


def freq(ch):
    return 2412 + (ch - 1) * 5


def score_channels(aps, channels=None, width_mhz=20):
    """把邻居 AP 折算成对每条候选信道的干扰分。

    重叠宽度 = width - |频差|，只算正重叠；权重再乘 (信号/100)^2，
    因为强信号的影响远大于弱信号（平方比线性更贴近实际）。

    channels 传 None 表示全部 1~13；传列表则**只评估这些信道**
    （用于设备只支持部分信道的情况，例如某些地区 1~11）。
    """
    if channels is None:
        candidates = list(range(1, 14))
    else:
        candidates = sorted({int(c) for c in channels if 1 <= int(c) <= 14})
        if not candidates:
            candidates = list(range(1, 14))

    rows = []
    for ch in candidates:
        total, detail = 0.0, []
        for ap in aps:
            gap = abs(freq(ap["ch"]) - freq(ch))
            ov = width_mhz - gap
            if ov <= 0:
                continue
            total += (ap["sig"] / 100.0) ** 2 * (ov / width_mhz)
            detail.append((ap["ssid"], ap["ch"], ap["sig"], ov))
        rows.append({"ch": ch, "score": round(total, 2), "detail": detail})
    rows.sort(key=lambda r: r["score"])
    return rows
