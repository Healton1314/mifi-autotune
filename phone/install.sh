#!/data/data/com.termux/files/usr/bin/bash
# 在 Termux 里运行本脚本，把 mifi-autotune 装到手机上，之后手机自己就能用。
#
# 用法（在 Termux 里）：
#   bash install.sh                          # 从默认面板地址取安装包
#   PANEL=http://192.168.1.23:8787 bash install.sh
#
# 这个脚本只做一次事：把代码放到手机上 + 建一个 mifi 命令。
# 装完之后不再需要电脑。

set -u

PANEL="${PANEL:-http://192.168.0.100:8787}"
DEST="$HOME/mifi-autotune"
TMP="$HOME/.mifi-pkg.zip"

say() { printf '\033[36m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*"; exit 1; }

# ---- 0. 确认在 Termux 里 ----
case "$(uname -o 2>/dev/null)" in
  Android) ;;
  *) warn "[!] 这个脚本是给 Termux 用的。如果你在电脑上，直接用 python autotune.py 即可。" ;;
esac
[ -n "${PREFIX:-}" ] || die "[×] 没检测到 Termux 环境（PREFIX 为空）。请在 Termux 里运行。"

# ---- 1. 装 Python ----
if command -v python >/dev/null 2>&1; then
  say "[1/4] Python 已就绪：$(python -V 2>&1)"
else
  say "[1/4] 安装 Python（首次会下载几十兆，需要网络）…"
  pkg update -y >/dev/null 2>&1 || true
  pkg install -y python || die "[×] 装 Python 失败。先手动执行 pkg install python 看看报什么错。"
fi

# termux-api 用于扫周边 Wi-Fi（安卓 10+ 还需给 Termux:API 定位权限）。
# 装不上不影响频段优化，只是跳过信道部分。
if ! command -v termux-wifi-scaninfo >/dev/null 2>&1; then
  say "      尝试安装 termux-api（用于扫信道，可选）…"
  pkg install -y termux-api >/dev/null 2>&1 || warn "      [!] termux-api 没装上，信道扫描会跳过，频段优化不受影响"
fi

# ---- 2. 取安装包 ----
# 优先级：脚本参数指定的 zip > 当前目录 > ~/p.zip > 从面板下载
# 为什么要支持本地 zip：手机上 curl 未必装好，而 python -m zipfile 一定有，
# 所以「先下载再解包再装」这条路不需要 curl。
say "[2/4] 准备安装包…"
PKG="${1:-}"
if [ -z "$PKG" ]; then
  for cand in "./mifi-autotune-phone.zip" "$HOME/p.zip" "$HOME/mifi-autotune-phone.zip"; do
    [ -f "$cand" ] && PKG="$cand" && break
  done
fi

if [ -n "$PKG" ] && [ -f "$PKG" ]; then
  cp "$PKG" "$TMP" && say "      用本地的包：$PKG"
else
  say "      从 $PANEL 下载…"
  # python 一定在（第一步刚装好），比 curl 可靠
  python - "$PANEL/api/phone-package" "$TMP" <<'PY' || die "[×] 下载失败：确认手机连着随身WiFi，且电脑上的面板开着。"
import sys, urllib.request
url, out = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(url, timeout=30) as r, open(out, "wb") as f:
    n = 0
    while True:
        chunk = r.read(65536)
        if not chunk:
            break
        f.write(chunk); n += len(chunk)
print("      已下载 %d 字节" % n)
PY
fi

# ---- 3. 解包 ----
say "[3/4] 解包到 $DEST …"
rm -rf "$DEST"
python -m zipfile -e "$TMP" "$HOME" || die "[×] 解包失败（安装包可能不完整，重新下载一次）"
[ -d "$DEST" ] || die "[×] 解包后没找到 $DEST"
rm -f "$TMP"
chmod +x "$DEST"/phone/*.sh 2>/dev/null || true

# ---- 4. 建 mifi 命令 ----
say "[4/4] 创建 mifi 命令…"
mkdir -p "$PREFIX/bin"
cp "$DEST/phone/mifi.sh" "$PREFIX/bin/mifi"
chmod +x "$PREFIX/bin/mifi"

cat <<EOF

============================================================
装好了。以后不需要电脑，手机上这样用：

  mifi                 # 启动面板并自动打开浏览器（推荐）
  cd ~/mifi-autotune
  python autotune.py --preview      # 只想看文字结论时用这条

第一次用 mifi 时，安卓可能问「是否在浏览器打开」，允许即可。
建议把页面「添加到主屏幕」，等于一个 App。

两点提醒：
  · 手机要连着这台随身WiFi（Wi-Fi 名一般是 月亮糖）
  · 换频段时「上网」会断 30 秒左右，但手机和设备的 Wi-Fi 连接不会断，
    所以面板不会失联，测完自动恢复原频段
============================================================
EOF
