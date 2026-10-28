"""跨平台测速 / 延迟探测层。

设计要点：
  1. 不解析本地化的摘要文字（"平均 = 12ms" / "Average = 12ms" / "rtt min/avg/max"），
     改为数「有多少条回显行」，丢包率 = (发送 - 回显) / 发送。这样与系统语言无关。
  2. 延迟值只从回显行的 time/时间 字段取；回显行靠 TTL 识别（TTL 是协议字段，
     任何语言都不翻译）。
  3. 测速剥掉代理环境变量，否则测到的是本地代理的带宽（此坑实测踩过）。
"""

import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

DEFAULT_SPEED_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
DEFAULT_PING_HOST = "223.5.5.5"

# 测速源候选表。为什么要给这么多：
#   实测遇到过整条链路上「HTTPS 到多数 CDN 失败、HTTP 正常」的情况
#   （curl 直接 exit 28/000，而 http:// 同一文件能跑 2.9MB/s），
#   如果只写一个 https 源，工具会把「源连不上」记成「这个频段 0 Mbps」，
#   进而给出完全错误的切换建议。
SPEED_CANDIDATES = [
    "https://dldir1.qq.com/weixin/Windows/WeChatSetup.exe",
    "http://dldir1.qq.com/weixin/Windows/WeChatSetup.exe",
    "http://mirrors.tuna.tsinghua.edu.cn/speedtest/100mb.bin",
    "https://mirrors.tuna.tsinghua.edu.cn/speedtest/100mb.bin",
    "https://speed.cloudflare.com/__down?bytes=25000000",
]


def _env():
    return {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}


def sh(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=_env())
    except Exception:  # noqa: BLE001
        return ""
    for enc in ("oem", "utf-8", "gbk", "latin-1"):
        try:
            return p.stdout.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return p.stdout.decode("utf-8", "ignore")


def _ping_cmd(n, host):
    if IS_WIN:
        return ["ping", "-n", str(n), "-w", "2000", host]
    if IS_MAC:
        return ["ping", "-c", str(n), host]
    return ["ping", "-c", str(n), "-W", "2", host]


_RTT = re.compile(r"(?:time|时间|tiempo|temps|zeit|زمان)[=<]\s*([\d.,]+)\s*ms", re.I)
_ANY_MS = re.compile(r"([\d.,]+)\s*ms", re.I)


def _tcp_latency(n, host, port=53, timeout=2.0):
    """用 TCP 连接握手耗时当延迟，替代 ping。

    为什么必须有这条路：Android 上普通进程拿不到 ICMP（需要 root），
    Termux 里也未必装了 iputils 的 ping。没有它，手机上所有频段都会被判成
    「有信号但数据不通」，工具等于废掉。
    TCP 握手耗时与 RTT 基本一致，对「能不能上、稳不稳」的判断足够用。
    """
    import socket
    rtts = []
    for _ in range(n):
        t0 = time.time()
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            rtts.append((time.time() - t0) * 1000)
        except OSError:
            pass
    return rtts


def ping_stats(n=10, host=DEFAULT_PING_HOST):
    """返回 {avg, min, max, jitter, loss, sent, recv}；全部无回显时 avg=None。"""
    out = sh(_ping_cmd(n, host), timeout=60 + n * 2)
    rtts = []
    for line in out.splitlines():
        if "ttl" not in line.lower():
            continue
        m = _RTT.search(line) or _ANY_MS.search(line)
        if m:
            try:
                rtts.append(float(m.group(1).replace(",", ".")))
            except ValueError:
                pass

    method = "icmp"
    if not rtts:
        # ICMP 用不了（Android/权限受限/没装 ping）→ 退回 TCP 握手测延迟
        rtts = _tcp_latency(n, host, port=53)
        method = "tcp"

    recv = len(rtts)
    loss = round(100.0 * (n - recv) / n) if n else 100
    if not rtts:
        return {"avg": None, "min": None, "max": None, "jitter": None,
                "loss": 100, "sent": n, "recv": 0, "method": method}
    mean = sum(rtts) / len(rtts)
    var = sum((x - mean) ** 2 for x in rtts) / len(rtts)
    return {
        "avg": int(round(mean)),
        "min": int(round(min(rtts))),
        "max": int(round(max(rtts))),
        "jitter": round(var ** 0.5, 1),
        "loss": loss,
        "sent": n,
        "recv": recv,
        "method": method,
    }


# ---------------------------------------------------------------- 带宽


def _curl():
    from shutil import which
    return which("curl")


def _one_curl(url, secs):
    import shutil as _s
    p = subprocess.Popen(
        [_curl(), "--noproxy", "*", "-L", "-s", "-o", _s.os.devnull,
         "--max-time", str(secs), "-A", "Mozilla/5.0",
         "-w", "%{size_download}", url],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_env())
    try:
        out, _ = p.communicate(timeout=secs + 15)
        return int((out or b"0").decode("utf-8", "ignore").strip() or 0)
    except Exception:  # noqa: BLE001
        p.kill()
        return 0


def _one_urllib(url, secs):
    """没有 curl 时的兜底（Windows 10+ / 主流 Linux 都自带 curl，很少走到这里）。"""
    total = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=secs) as r:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                total += len(chunk)
    except Exception:  # noqa: BLE001
        pass
    return total


def throughput(conn=4, secs=8, url=DEFAULT_SPEED_URL):
    """多路并发下载，返回 Mbps。单线程测不出移动网络的上限，必须并发。"""
    if not url:
        return 0.0
    if not _curl():
        total = sum(_one_urllib(url, secs) for _ in range(conn))
        return round(total * 8 / secs / 1e6, 1)

    results, threads = [0] * conn, []
    for i in range(conn):
        t = threading.Thread(target=lambda idx: results.__setitem__(idx, _one_curl(url, secs)), args=(i,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return round(sum(results) * 8 / secs / 1e6, 1)


def throughput_any(urls, conn=4, secs=8):
    """依次试多个测速源，返回第一个测出 > 0 的结果：(mbps, 实际用的 url)。

    为什么需要：单个源会在某些链路上挂掉（实测 HTTPS 到 CDN 全失败、HTTP 正常），
    或者某个频段下这个源的 CDN 节点就是不通。一个源返回 0 不代表「这个频段 0 兆」。
    """
    for u in urls:
        if not u:
            continue
        v = throughput(conn, secs, u)
        if v > 0:
            return v, u
    return 0.0, urls[0] if urls else None


def probe_url(url, secs=3, min_bytes=100_000):
    """快速验证一个源能不能真的下到东西（3 秒内至少 min_bytes）。"""
    got = _one_curl(url, secs) if _curl() else _one_urllib(url, secs)
    return got >= min_bytes, got


def resolve_speed_url(preferred=None, verbose=True):
    """挑一个当前环境下真能下东西的测速源。

    顺序：用户指定的 → 常见国内源（含 http 版本）→ Cloudflare。
    先花最多十几秒找到可用源，比后面 6 个频段各失败一次（还可能得出错误结论）划算得多。
    """
    tried = []
    seen = set()
    for url in [preferred] + SPEED_CANDIDATES:
        if not url or url in seen:
            continue
        seen.add(url)
        ok, got = probe_url(url, secs=3)
        tried.append((url, got, ok))
        if ok:
            if verbose and url != preferred:
                print(f"      [测速源] 自动选用 {url}")
            return url
    if verbose:
        print("      [!] 所有测速源都下不动（常见原因：链路上 HTTPS 被拦、或当下流量几乎断）")
        for url, got, _ok in tried:
            print(f"          {url} → {got} 字节")
    return preferred or DEFAULT_SPEED_URL
