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
import shutil
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


def _ping_cmd(n, host, wait_ms=1000):
    """单包等待时间压到 1 秒。

    原来 Windows 是 2000ms、Android 是 -W 2：整条链路断了的时候，10 个包
    每个都要等满超时，一次采集光 ping 就 20 秒。实机 2026-11-02 18:44~18:47
    守护的采样时间戳是 47~49 秒一次（interval 只有 30 秒），多出来的正是这 20 秒
    —— 用户说的「检测到没网跳出太慢」就是它。局域网/RF 链路里 1 秒还没回显，
    基本就是没有回显了。
    """
    if IS_WIN:
        return ["ping", "-n", str(n), "-w", str(wait_ms), host]
    if IS_MAC:
        # macOS 的 -W 单位是毫秒
        return ["ping", "-c", str(n), "-W", str(wait_ms), host]
    return ["ping", "-c", str(n), "-W", "1", host]


# ---- 路径 MTU 探测（电脑端专用）------------------------------------------
#
# 只在电脑上做。原因不是懒：Android 不给普通应用 ICMP 权限，Termux 里
# ping 也常常不可用（拿不到 raw socket），硬做出来只会给个假数字。
# 而这项检查本来就不需要常跑——一条线路的路径 MTU 基本不变，测一次就够。

IPV4_HDR = 28              # 20 字节 IP 头 + 8 字节 ICMP 头
MTU_MAX_PAYLOAD = 1472     # MTU 1500 时最大不分片 payload


def is_termux():
    return "com.termux" in os.environ.get("PREFIX", "")


def _ping_df_ok(size, host, wait_ms=1500):
    """发一个「禁止分片」的 ICMP 包：收到回应 = 这个 payload 能整包过去。

    必须用不分片模式（-f / -M do / -D）。普通 ping 大包会被中间设备静默
    分片，看起来通了，但真实 TCP 流量一旦置 DF 就会丢——这正是 MTU 设大了
    时「网页能开、下载大文件卡死」的成因。
    """
    if IS_WIN:
        cmd = ["ping", "-n", "1", "-f", "-l", str(size), "-w", str(wait_ms), host]
    elif IS_MAC:
        cmd = ["ping", "-c", "1", "-D", "-s", str(size), "-W", "2", host]
    else:
        cmd = ["ping", "-c", "1", "-M", "do", "-s", str(size), "-W", "2", host]
    out = sh(cmd, timeout=max(4.0, wait_ms / 1000.0 + 3))
    low = out.lower()
    # 明确的「包太大」回执：Windows「需要拆分数据包」/ 英文 Frag needed / MTU 字样
    if "fragment" in low or "需要拆分" in out or "too big" in low or "mtu" in low:
        return False
    # 收到任何一次回包就算过（不同语言版本的 TTL 字样不同，退一步看 xx ms）
    return bool(_ANY_MS.search(out) or "ttl" in low)


def detect_path_mtu(host=DEFAULT_PING_HOST, lo=1200, hi=MTU_MAX_PAYLOAD):
    """二分找出到 host 的路径 MTU。返回 (mtu 或 None, 给人看的说明)。"""
    if is_termux():
        return None, ("手机端拿不到 ICMP 权限，MTU 只能在电脑上测"
                      "（这条和频段优化无关，不影响手机体检）")
    if not IS_WIN and not shutil.which("ping"):
        return None, "这台机器上没有 ping 命令，测不了"

    if _ping_df_ok(hi, host):
        return hi + IPV4_HDR, f"{hi + IPV4_HDR} 字节（标准值，不用改）"

    if not _ping_df_ok(lo, host):
        # 连 1200 都不通：要么链路真的有问题，要么 ICMP 被拦（公共 WiFi 很常见）。
        # 这种时候给任何数字都是瞎猜，必须说「测不出来」。
        return None, (f"连 {lo} 字节的大包都不通：可能是 ICMP 被中间设备拦了"
                      f"（公共网络常见），也可能是链路本身有问题。"
                      f"这种情况测不出来，别据此改 MTU")

    a, b = lo, hi                     # a 能过、b 过不去
    while b - a > 1:
        mid = (a + b) // 2
        if _ping_df_ok(mid, host):
            a = mid
        else:
            b = mid
    return a + IPV4_HDR, f"{a + IPV4_HDR} 字节（最大不分片 payload {a} 字节）"


_RTT = re.compile(r"(?:time|时间|tiempo|temps|zeit|زمان)[=<]\s*([\d.,]+)\s*ms", re.I)
_ANY_MS = re.compile(r"([\d.,]+)\s*ms", re.I)


_TCP_PORTS = (53, 443, 80)


def _tcp_latency(n, host, port=None, timeout=1.5):
    """用 TCP 连接握手耗时当延迟，替代 ping。

    为什么必须有这条路：Android 上普通进程拿不到 ICMP（需要 root），
    Termux 里也未必装了 iputils 的 ping。没有它，手机上所有频段都会被判成
    「有信号但数据不通」，工具等于废掉。
    TCP 握手耗时与 RTT 基本一致，对「能不能上、稳不稳」的判断足够用。

    为什么不止一个端口：手机上到 223.5.5.5 的 TCP/53 经常被运营商丢包或限速，
    只试 53 会把「这个端口不通」误判成「整条链路不通」，然后所有频段都被判
    「数据不通」——用户看到的就是「测了 6 个频段全是无服务」。
    所以按 53 → 443 → 80 依次敲，哪个通了就优先用它；连续失败的端口直接放弃，
    避免全断网时 10 次探测 × 3 端口 × 超时 把一次测量拖成 45 秒。
    """
    import socket
    ports = [port] if port else list(_TCP_PORTS)
    fails = {p: 0 for p in ports}
    rtts = []
    for _ in range(n):
        for p in list(ports):
            if fails.get(p, 0) >= 2:
                continue
            t0 = time.time()
            try:
                s = socket.create_connection((host, p), timeout=timeout)
                s.close()
            except OSError:
                fails[p] = fails.get(p, 0) + 1
                continue
            rtts.append((time.time() - t0) * 1000)
            fails[p] = 0
            ports = [p] + [x for x in ports if x != p]      # 记住能通的，下次先试
            break
    return rtts


def _icmp_rtts(n, host):
    """发 n 个 ICMP 包，返回收到的那些 RTT（ms）。平台差异都收在这里。"""
    out = sh(_ping_cmd(n, host), timeout=10 + n * 2)
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
    return rtts


def ping_stats(n=10, host=DEFAULT_PING_HOST, probe_n=3):
    """返回 {avg, min, max, jitter, loss, sent, recv, down}；全部无回显时 avg=None。

    **断网要在几秒内就认输。** 原来一次固定发 n 个包、每个等满超时：全丢就是
    20 秒，而守护一拍本来只有 30 秒 —— 于是一没网，采样周期就变成 50 秒，
    「连续 3 次不通」要两分半才攒够，用户看到的就是「检测到没网跳出太慢」
    （实机 2026-11-02 18:44:25→18:45:24→18:46:12，间隔 47~49 秒）。

    所以分两段：先发 probe_n 个。一个都不回，就已经能判「这条链路此刻不通」
    —— 100% 丢包不需要凑够 10 个样本。再用一次 TCP 握手分辨是「真断网」还是
    「这里 ICMP 被禁」，不通就直接返回 down=True。有回显才补齐剩下的包，
    正常链路一个包都不少，丢包率精度不变。
    """
    probe_n = max(1, min(probe_n, n))
    rtts = _icmp_rtts(probe_n, host)
    method = "icmp"
    sent = probe_n

    if not rtts:
        # 一个都没回来。两种可能：整条链路断了，或者这个位置不允许 ICMP。
        # 后者很常见（Android 普通应用、公共 WiFi），不能当成断网 ——
        # 用 TCP 握手分一下，最多两次失败（约 3 秒）。
        rtts = _tcp_latency(probe_n, host, port=_TCP_PORTS[0])
        method = "tcp"
        if not rtts:
            return {"avg": None, "min": None, "max": None, "jitter": None,
                    "loss": 100, "sent": sent, "recv": 0, "method": method,
                    "down": True}
    elif n > probe_n:
        rtts += _icmp_rtts(n - probe_n, host)
        sent = n

    recv = len(rtts)
    loss = round(100.0 * (sent - recv) / sent) if sent else 100
    mean = sum(rtts) / len(rtts)
    var = sum((x - mean) ** 2 for x in rtts) / len(rtts)
    return {
        "avg": int(round(mean)),
        "min": int(round(min(rtts))),
        "max": int(round(max(rtts))),
        "jitter": round(var ** 0.5, 1),
        "loss": loss,
        "sent": sent,
        "recv": recv,
        "method": method,
        # 只有「ICMP 一个都没回」和「TCP 也握不上手」同时成立才置 True。
        # 单独一个 ICMP 不通不算 —— 那更可能是权限问题，而不是没网。
        "down": False,
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
