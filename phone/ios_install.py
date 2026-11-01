"""在 iPhone / iPad 的 a-Shell 里安装 mifi-autotune。

在 a-Shell 里粘这一行就能跑（先装 App Store 里的 a-Shell，免费）：

    exec(__import__("urllib.request", fromlist=["x"]).urlopen(
        "https://cdn.jsdelivr.net/gh/Healton1314/mifi-autotune@main/phone/ios_install.py"
    ).read().decode())

为什么用 Python 而不是一条 curl 命令：a-Shell 里多数外部命令是 WebAssembly
编译的，**没有 socket**，curl 不一定联得上网；而它的 python3 是原生编译的，
urllib 一定可用。整个安装只依赖标准库，跟这个项目本身的零依赖一致。

装完之后 iPhone 上能做什么，见脚本末尾打印的说明——**信道优化在 iOS 上做不了**，
那是苹果的限制（第三方应用拿不到扫描周边 Wi-Fi 的接口），不是代码问题。
"""

import io
import os
import shutil
import sys
import zipfile

REPO = "Healton1314/mifi-autotune"
BRANCH = "main"

SOURCES = [
    f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}",
    f"https://ghfast.top/https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}",
    f"https://gh-proxy.com/https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}",
    f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip",
]

# 这些是「你这台设备目前的设置和位置记忆」，升级时绝不能覆盖 ——
# 覆盖了等于把用户攒下来的位置记忆清空，还得重新体检一遍。
PRESERVE = {
    "state.json", "config.json", "plan.json", "sites.json",
    "mifi-memory.json", "autotune-log.jsonl", ".panel-token",
}


def say(msg):
    print(msg, flush=True)


def is_ios():
    """a-Shell 的 HOME 形如 /private/var/mobile/Containers/Data/Application/<uuid>/Documents。"""
    return "/var/mobile" in os.environ.get("HOME", "")


def fetch():
    import urllib.request

    for url in SOURCES:
        try:
            say(f"  从 {url.split('/')[2]} 取包 …")
            req = urllib.request.Request(url, headers={"User-Agent": "mifi-autotune-ios"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if data[:2] != b"PK":
                say("    这个地址返回的不是 zip，换下一个")
                continue
            return data
        except Exception as exc:  # noqa: BLE001
            say(f"    失败：{type(exc).__name__}: {exc}")
    return None


def install(data, dest):
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        top = names[0].split("/")[0]

        keep = {}
        if os.path.isdir(dest):
            for fn in PRESERVE:
                src = os.path.join(dest, fn)
                if os.path.isfile(src):
                    with open(src, "rb") as fh:
                        keep[fn] = fh.read()

        tmp = dest + ".new"
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
        os.makedirs(tmp)

        for n in names:
            if not n.startswith(top + "/"):
                continue
            rel = n[len(top) + 1:]
            if not rel or rel.endswith("/"):
                continue
            out = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with zf.open(n) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)

        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.rename(tmp, dest)

        restored = 0
        for fn, blob in keep.items():
            with open(os.path.join(dest, fn), "wb") as fh:
                fh.write(blob)
            restored += 1

    return restored


def make_launcher(dest):
    """写一个 ~/mifi 启动器，免得每次都要 cd 进去。"""
    path = os.path.join(os.path.dirname(dest), "mifi")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f'''#!/bin/sh
# 随身WiFi 调优（iPhone / a-Shell）
cd "{dest}" || exit 1
exec python3 autotune.py "$@"
''')
    try:
        os.chmod(path, 0o755)
    except Exception:  # noqa: BLE001
        pass
    return path


def main():
    say("")
    say("=== 随身WiFi 调优 · iPhone 安装 ===")

    if not is_ios():
        say("[!] 看起来不是 iOS 环境。如果你在 Termux（安卓）上，请用 phone/install.sh。")
        say("    继续装也可以，但下面的用法说明是按 iPhone 写的。")

    py = sys.version.split()[0]
    say(f"Python {py}")
    if sys.version_info < (3, 8):
        say("[×] Python 版本太老（需要 3.8+）。a-Shell 自带的版本应该够，"
            "请更新 a-Shell 后重试。")
        return 1

    home = os.path.expanduser("~")
    dest = os.path.join(home, "mifi-autotune")
    say(f"安装到 {dest}")
    say("")

    say("[1/3] 下载")
    data = fetch()
    if data is None:
        say("")
        say("[×] 四个下载源都没成功。多半是网络问题：")
        say("    · 先确认这个 Wi-Fi 能上外网（能打开网页）")
        say("    · 换成 iSH 再试（App Store 也有，自带 apk 包管理，网络更稳）")
        say("    · 或者让电脑把 zip 传到你手机上，再用 a-Shell 执行：")
        say("      python3 -c \"import zipfile;zipfile.ZipFile('mifi.zip').extractall('.')\"")
        return 1

    say(f"    拿到 {len(data) // 1024} KB")
    say("[2/3] 解压")
    restored = install(data, dest)
    if restored:
        say(f"    保留了 {restored} 个本地文件（位置记忆 / 当前设置）")

    say("[3/3] 生成启动器")
    launcher = make_launcher(dest)

    say("")
    say("=== 装好了 ===")
    say("")
    say(f"  sh {launcher}            体检：先告诉你能快多少，再问你要不要改")
    say(f"  sh {launcher} --status   只看当前状态（几秒）")
    say(f"  sh {launcher} --restore  还原成不锁频段 + 自动信道")
    say("")
    say("第一次用先跑 --status，确认能读到设备，再跑体检。")
    say("")
    say("--- iPhone 上有三点和安卓不一样，先看一眼 ---")
    say("")
    say("1. 没有信道优化。iOS 不允许第三方应用扫描周边 Wi-Fi，")
    say("   所以工具会跳过这一项，只做频段优化。频段才是提升的大头，不影响主要收益。")
    say("")
    say("2. 体检期间别锁屏、别切走。iOS 会把后台进程挂起，")
    say("   任务会断在半路（设备可能停在临时锁定的频段上）。跑之前用 --restore 就没事。")
    say("   整个过程 3~6 分钟，中途会断网几次，属正常。")
    say("")
    say("3. 第一次访问设备后台会弹「是否允许访问本地网络」，必须点允许。")
    say("   另外建议关掉「设置 → 无线局域网 → 无线局域网助理」，")
    say("   否则设备断网那一刻 iPhone 会切到蜂窝，工具就连不上 192.168.0.1 了。")
    say("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
