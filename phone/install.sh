#!/data/data/com.termux/files/usr/bin/bash
# 在 Termux 里运行本脚本，把 mifi-autotune 装到手机上，之后手机自己就能用。
#
# 用法（在 Termux 里）：
#   bash install.sh                    # 自动找安装包（本地 zip → 电脑面板 → GitHub）
#   bash install.sh -f                 # 强制重新下载（升级用）；mifi --update 就是这个
#   PANEL=http://192.168.1.23:8787 bash install.sh
#   bash install.sh /sdcard/Download/mifi.zip
#
# 装完之后不再需要电脑。

set -u

PANEL="${PANEL:-http://192.168.0.100:8787}"
DEST="$HOME/mifi-autotune"
TMP="$HOME/.mifi-pkg.zip"
EXTRACT="$HOME/.mifi-extract"

# 两条下载源：电脑面板最快（也只有它需要电脑）；GitHub 完全不依赖电脑。
GH_URLS="https://codeload.github.com/Healton1314/mifi-autotune/zip/refs/heads/main
https://github.com/Healton1314/mifi-autotune/archive/refs/heads/main.zip"

say() { printf '\033[36m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*"; exit 1; }

# ---- 0. 确认在 Termux 里 ----
[ -n "${PREFIX:-}" ] || die "[×] 没检测到 Termux 环境（PREFIX 为空）。请在 Termux 里运行。"

# ---- 1. 装 Python ----
if command -v python >/dev/null 2>&1; then
  say "[1/4] Python 已就绪：$(python -V 2>&1)"
else
  say "[1/4] 安装 Python（首次要下载几十兆，慢但只需一次）…"
  pkg update -y >/dev/null 2>&1 || true
  pkg install -y python || die "[×] 装 Python 失败。先手动执行 pkg install python 看看报什么错。"
fi

# termux-api 用于扫周边 Wi-Fi（安卓 10+ 还需给 Termux:API 定位权限）。
# 装不上不影响频段优化，只是跳过信道部分。
if ! command -v termux-wifi-scaninfo >/dev/null 2>&1; then
  say "      尝试安装 termux-api（扫信道用，可选）…"
  pkg install -y termux-api >/dev/null 2>&1 || warn "      [!] termux-api 没装上：信道扫描会跳过，频段优化不受影响"
fi

# ---- 2. 取安装包 ----
say "[2/4] 准备安装包…"
FRESH=0
PKG="${1:-}"
case "$PKG" in
  -f|--fresh) FRESH=1; PKG="" ;;
esac
if [ -z "$PKG" ] && [ "$FRESH" -eq 0 ]; then
  for cand in "./mifi-autotune-phone.zip" "$HOME/p.zip" "$HOME/mifi-autotune-phone.zip"; do
    [ -f "$cand" ] && PKG="$cand" && break
  done
fi

if [ -n "$PKG" ] && [ -f "$PKG" ]; then
  cp "$PKG" "$TMP" && say "      用本地的包：$PKG"
else
  # python 一定在（第一步刚装好），比 curl 可靠
  python - "$TMP" "$PANEL/api/phone-package" $GH_URLS <<'PY' || die "[×] 下载失败：手机没网，或几条源都不通。可以先让电脑开着面板取包，或用浏览器手动下载安装包再传进来。"
import sys, urllib.request

out, urls = sys.argv[1], sys.argv[2:]
for u in urls:
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "mifi-install"})
        with urllib.request.urlopen(req, timeout=20) as r, open(out, "wb") as f:
            n = 0
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                n += len(chunk)
        if n < 20000:
            print("      源 %s 只给了 %d 字节，跳过" % (u, n))
            continue
        print("      已下载 %d 字节  <-  %s" % (n, u))
        sys.exit(0)
    except Exception as e:
        print("      源不可用：%s（%s）" % (u, type(e).__name__))
sys.exit(1)
PY
fi
[ -s "$TMP" ] || die "[×] 安装包是空的，重新跑一次。"

# ---- 3. 解包 ----
say "[3/4] 解包…"
rm -rf "$EXTRACT" "$DEST"
mkdir -p "$EXTRACT"
python -m zipfile -e "$TMP" "$EXTRACT" || die "[×] 解包失败（安装包可能不完整）"

# 面板打的包根目录叫 mifi-autotune；GitHub 的包叫 mifi-autotune-main。
SRC=""
for cand in "$EXTRACT/mifi-autotune" "$EXTRACT"/mifi-autotune-*; do
  [ -f "$cand/autotune.py" ] && SRC="$cand" && break
done
[ -n "$SRC" ] || die "[×] 解包后没找到 autotune.py，安装包内容不对。"

mv "$SRC" "$DEST"
rm -rf "$EXTRACT" "$TMP"
chmod +x "$DEST"/phone/*.sh 2>/dev/null || true

# ---- 4. 建 mifi 命令 ----
say "[4/4] 创建 mifi 命令…"
mkdir -p "$PREFIX/bin"
cp "$DEST/phone/mifi.sh" "$PREFIX/bin/mifi"
chmod +x "$PREFIX/bin/mifi"

# 说明文字必须读「刚从安装包里解出来的」那一份，不能写死在正在运行的脚本里：
# mifi --update 是先跑旧脚本、再下载新包，写死的话用户看到的永远是上一版说明
# （用户就踩过：明明已经有 mifi --scan，装完却看不到这一行）。
VER="$(grep -m1 '^__version__' "$DEST/autotune.py" 2>/dev/null | cut -d'"' -f2)"
[ -n "$VER" ] && say "      已装版本：$VER（之后随时 mifi --version 可查）"
if [ -f "$DEST/phone/tips.txt" ]; then
  cat "$DEST/phone/tips.txt"
else
  cat <<'EOF'

============================================================
装好了。手机上这样用：

  mifi            体检 → 看「能提升多少」→ 问你要不要优化
  mifi --status   只看当前状态（几秒）
  mifi --restore  还原成不锁频段 + 自动信道
  mifi --update   更新到最新代码
  mifi --help     看这一版支持的全部参数

第一次用，先跑 mifi --status 确认能读到设备，
再跑 mifi 做体检（约 3~6 分钟，中途断网几次，结束自动恢复原配置）。
============================================================
EOF
fi
