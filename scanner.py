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
    return _decode(p.stdout)


def _decode(raw):
    for enc in ("oem", "utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "ignore")


def run_detail(cmd, timeout=60):
    """跑命令并同时拿到 stdout / stderr / 退出码。

    诊断「为什么扫不到」必须看 stderr：手机上 termux-wifi-scaninfo 失败时
    常常是 **stdout 空 + stderr 报错**，只看 stdout 会一律误判成「没权限」。
    """
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=_env())
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}", None
    return _decode(p.stdout), _decode(p.stderr), p.returncode


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


_LAST_TERMUX = {"stdout": "", "stderr": "", "code": None, "timeout": False, "ts": 0.0}


def _termux_recent(max_age=180):
    """拿最近一次 termux-wifi-scaninfo 的结果；没有新鲜的就现跑一次。

    「失败提示」和「原始信息」各自都要这份结果。手机卡死时一次调用要等满
    60 秒，不复用就等于让用户白等两分钟才看到同一句话。
    """
    if _LAST_TERMUX["ts"] and time.time() - _LAST_TERMUX["ts"] < max_age:
        return _LAST_TERMUX["stdout"], _LAST_TERMUX["stderr"], _LAST_TERMUX["code"]
    out, err, code = run_detail(["termux-wifi-scaninfo"], timeout=60)
    _LAST_TERMUX.update(stdout=out, stderr=err, code=code,
                        timeout="timed out" in (err or "").lower(),
                        ts=time.time())
    return out, err, code


_BRIDGE_CACHE = {"ok": None, "ts": 0.0}


def _api_bridge_ok(timeout=8):
    """探一下 Termux:API 这座「桥」本身通不通。

    手机上 termux-wifi-scaninfo 卡死（超时）有两种完全不同的原因，两者的
    修法也完全不同，只看扫描的报错分不出来：

      · Termux:API 这个 App 没装 / 被系统冻结 → 任何 API 命令都会卡住
      · App 是活的，但没给定位权限         → 扫描会立刻返回空或报 permission

    所以先用最轻的 termux-battery-status 敲一下门：
      True  = 桥通了，扫描失败是扫描自己的问题（多半是权限）
      False = 桥不通（命令卡住/报错），先修 App
      None  = 连命令都没有，判断不了

    结果缓存 60 秒：失败提示和原始信息会各问一次，不必真敲两遍门。
    """
    if _BRIDGE_CACHE["ts"] and time.time() - _BRIDGE_CACHE["ts"] < 60:
        return _BRIDGE_CACHE["ok"]
    if not shutil.which("termux-battery-status"):
        return None
    out, _err, _code = run_detail(["termux-battery-status"], timeout=timeout)
    ok = bool((out or "").strip())
    _BRIDGE_CACHE.update(ok=ok, ts=time.time())
    return ok


def _scan_termux():
    """Android 上通过 Termux:API 拿扫描结果。

    需要：装 Termux:API 应用 + `pkg install termux-api` + 授予定位权限
    （Android 10+ 把 WiFi 扫描算作位置信息，没权限会返回空数组）。

    超时给 60 秒：第一次调用要先唤起 Termux:API 的进程，冷启动 + 一次真实
    扫描在慢机型上可以跑到 20~40 秒，原来 45 秒偶尔还是不够；而卡死的情况
    无论如何都要等满，所以不如给足，再用「桥探测」快速判断到底哪里坏了。
    """
    if not shutil.which("termux-wifi-scaninfo"):
        return []
    out, err, code = run_detail(["termux-wifi-scaninfo"], timeout=60)
    _LAST_TERMUX.update(
        stdout=out, stderr=err, code=code,
        timeout="timed out" in (err or "").lower(),
        ts=time.time(),
    )
    try:
        data = json.loads(out)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    aps = []
    for ap in data:
        ssid = (ap.get("ssid") or "").strip()
        # 字段名有两套，必须都认：
        #   旧版 Termux:API → frequency / level
        #   新版（Android 10+ 直接转 WifiScanner 的 ScanResult）→ frequency_mhz / rssi
        # 只读 frequency 的话，新版机器上每个 AP 都会被当成「没有频率」扔掉 ——
        # 表现就是「明明扫到了，工具却说一个都没扫到」（2026-10-29 用户实机踩到）。
        freq = ap.get("frequency_mhz") or ap.get("frequency")
        if not ssid or not freq:
            continue
        ch = freq_to_channel(int(freq))
        if not ch:
            continue
        lvl = ap.get("rssi")
        if lvl is None:
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


def scan_diagnose():
    """扫描失败时把原始事实摊开：命令在不在、退出码、stdout / stderr。

    用户遇到「每次都扫不到」时，一句结论往往猜错；直接把原始输出给出来，
    才能一次定位是缺包、缺 App、还是缺权限。
    """
    lines = []
    which = shutil.which("termux-wifi-scaninfo")
    lines.append(f"termux-wifi-scaninfo：{which or '未安装（pkg install termux-api）'}")
    if which:
        bridge = _api_bridge_ok()
        bridge_txt = {True: "通（App 在跑）", False: "不通（卡住/报错）",
                      None: "没装 termux-battery-status，无法判断"}[bridge]
        lines.append(f"Termux:API 桥探测（termux-battery-status）：{bridge_txt}")
        out, err, code = _termux_recent()
        lines.append(f"退出码：{code}")
        lines.append(f"stdout：{(out or '(空)').strip()[:200]}")
        lines.append(f"stderr：{(err or '(空)').strip()[:200]}")
    return "\n".join("      " + ln for ln in lines)


def _describe_termux_records():
    """有数据却用不上时，说清「系统到底给了什么」。

    实测（2026-10-29 用户实机）：退出码 0、API 桥也通，但只返回 1 条
    5200MHz、SSID 空、timestamp 是 2022-12-04 的记录 —— 那是系统缓存，
    不是这次扫描的结果。原来那句「可能只返回了当前连接的那个」与证据不符
    （当时连的是 2.4G，这条是 5GHz），等于把人往错的方向引。
    返回 None 表示解析不了，交给上层的兜底文案。
    """
    try:
        data = json.loads(_LAST_TERMUX.get("stdout") or "[]")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, list) or not data:
        return None

    now_ms = time.time() * 1000
    freqs, hidden, ages = [], 0, []
    for ap in data:
        f = ap.get("frequency_mhz") or ap.get("frequency")
        if f:
            try:
                freqs.append(int(f))
            except (TypeError, ValueError):
                pass
        if not (ap.get("ssid") or "").strip():
            hidden += 1
        ts = ap.get("timestamp")
        if ts:
            try:
                ages.append((now_ms - float(ts)) / 3600000)
            except (TypeError, ValueError):
                pass

    bits = [f"系统返回 {len(data)} 条记录"]
    if freqs:
        g5 = sum(1 for f in freqs if f > 3000)
        g24 = len(freqs) - g5
        bands = "、".join(x for x in ((f"2.4G {g24} 条" if g24 else ""),
                                      (f"5G {g5} 条" if g5 else "")) if x)
        bits.append(bands)
    if hidden:
        bits.append(f"其中 {hidden} 条没名字（隐藏网络）")
    head = "，".join(bits)

    if ages and min(ages) > 1:
        return (f"{head}；最新那条也是 {min(ages):.0f} 小时前的 —— "
                f"这是系统的**缓存**，不是这次扫描的结果。"
                f"打开手机「位置信息」总开关（光给权限不够），把 Termux:API 的定位"
                f"改成「始终允许」；Android 13+ 再开「附近的设备」。")

    if freqs and all(f > 3000 for f in freqs):
        return (f"{head} —— 只扫到 5GHz，2.4G 一个都没有。"
                f"要么周边确实没有 2.4G 热点，要么系统没把 2.4G 结果交出来。"
                f"信道部分跳过，频段优化不受影响。")

    return f"{head}，但没有一条能参与信道评分（名字或频率缺失）。"


def scan_failure_hint():
    """扫描返回空时，指出具体缺哪一步。

    原来只报「系统语言不支持或无线网卡未启用」——在手机上这句是错的：
    真正的原因通常是没装 termux-api，或者没给定位权限。用户照着这句话
    排查会白折腾。
    """
    import os

    is_termux = "com.termux" in os.environ.get("PREFIX", "") or bool(shutil.which("termux-info"))
    if is_termux:
        if not shutil.which("termux-wifi-scaninfo"):
            return ("手机上没装 termux-api 包：在 Termux 里执行 `pkg install termux-api`。"
                    "（只是跳过信道优化，频段不受影响）")

        out, err, code = _termux_recent()
        err_l = (err or "").lower()
        body = (out or "").strip()

        # 「卡住直到超时」是最常见的一种，而原来的处理只是把
        # "TimeoutExpired: Command '[...]' timed out after 25 seconds" 原样丢给用户，
        # 等于什么都没说。这里用桥探测区分两种病因，各自给对应动作。
        if "timed out" in err_l or "timeoutexpired" in err_l:
            bridge = _api_bridge_ok()
            if bridge is False or bridge is None:
                return ("termux-wifi-scaninfo 卡住直到超时 —— 这是 **Termux:API 这个 App "
                        "没装、或没在运行** 的典型表现。去 F-Droid 装 Termux:API（必须和 "
                        "Termux 同一来源），装完回 Termux 再跑 `mifi --scan`。"
                        "（只是跳过信道优化，频段不受影响）")
            return ("termux-wifi-scaninfo 超时，但 Termux:API 是活的 —— 多半是「定位」权限"
                    "没给，系统干脆不给结果。去设置里给 Termux:API 开定位（安卓 12+ 可能"
                    "还要「附近的设备」），或直接在 Termux 里跑 `termux-wifi-scaninfo` 看它的报错。")

        # 只有 Termux:API 这个 **App** 才能真的去调系统扫描。包装了这个 App 没装，
        # 命令会存在但拿不到数据 —— 这是最常见的一种，光看 stdout 会误判成「没权限」。
        if any(k in err_l for k in ("termux-api", "not installed", "unable to", "no such",
                                    "command not found", "permission denied by user")):
            return ("Termux:API 应用没装或没响应。需要两样都装：① Termux 里 "
                    "`pkg install termux-api`；② **Termux:API 这个 App**（F-Droid 搜，"
                    "必须和 Termux 同一个来源装）。频段优化不受影响。")

        if any(k in err_l for k in ("permission", "denied", "not allowed", "security",
                                    "unauthorized")):
            return ("系统拒绝了扫描：去设置里给 **Termux:API** 开「定位」权限"
                    "（安卓 10+ 把 WiFi 扫描算作位置信息），安卓 12+ 可能还要开「附近的设备」。")

        if err.strip():
            return f"termux-wifi-scaninfo 报错：{err.strip().splitlines()[0][:120]}"

        if body in ("", "[]"):
            return ("Termux:API 返回空列表 —— 十有八九是没给 **Termux:API** 的「定位」权限。"
                    "安卓 10+ 把 WiFi 扫描算作位置信息，没权限就只能拿到空结果。"
                    "注意还要打开手机的「位置信息」总开关，只给权限是不够的。"
                    "频段优化不受影响。")

        detail = _describe_termux_records()
        if detail:
            return detail
        return "拿到了扫描结果但解析不出可用 AP（可能只返回了当前连接的那个）"
    if sys.platform.startswith("win"):
        return "netsh 没抓到 BSSID：系统语言可能不在支持列表，或无线网卡被禁用"
    if sys.platform == "darwin":
        return "macOS 14+ 已移除 airport 命令，需要换扫描实现"
    return "nmcli / iw 都不可用（iw 主动扫描需要 root）"


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
        batch = scan_once()
        if not batch and i == 0:
            # 第一轮一个都没扫到，说明不是「漏扫」而是根本扫不了（命令卡死/没权限）。
            # 原来还会接着跑完剩下 4 轮，手机上就是干等两分钟才看到同一句报错。
            break
        for ap in batch:
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


STANDARD_2G_CHANNELS = (1, 6, 11)   # 2.4G 真正互不重叠的三个
CHANNEL_PREF_MARGIN = 0.15          # 干扰分差在这个范围内时，标准信道优先


def pick_best_channel(rows):
    """挑最干净的信道，但分差很小时优先标准信道（1/6/11）。

    为什么加这条：评分只看干扰分，会推荐 7 号这种「夹在 6 和 8 中间」的信道。
    那里眼下分低，只是因为还没人用；一旦邻居起来它同时被两条信道压，
    而它自己又不和任何标准信道对齐。
    实测（2026-10-29 手机）：工具推荐 7 号（0.36），设备本来在 13 号（0.46），
    只差 0.1 —— 为这点差距搬到一个夹缝信道不划算。

    返回被挑中的那一行；rows 必须是 score_channels 的输出（已按分数升序）。
    """
    if not rows:
        return None
    best = rows[0]
    if best["ch"] in STANDARD_2G_CHANNELS:
        return best
    for ch in STANDARD_2G_CHANNELS:          # rows 有序，第一个命中的就是最干净的
        for r in rows:
            if r["ch"] == ch and r["score"] <= best["score"] + CHANNEL_PREF_MARGIN:
                return r
    return best
