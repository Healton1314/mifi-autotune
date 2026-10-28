#!/data/data/com.termux/files/usr/bin/bash
# mifi —— 在手机上启动面板（由 phone/install.sh 安装到 $PREFIX/bin/mifi）
#
# 行为：起一个只监听本机 127.0.0.1 的面板，然后用浏览器打开它。
# 面板活着 = 这个终端窗口活着。按 Ctrl+C 即停止，不留后台残留。
#
# 为什么要占 wake lock：安卓会为省电杀后台，面板会在你用着的时候突然失联
# （浏览器报「网页无法访问 / 拒绝了您的访问」），用户会以为是网页坏了。

DIR="$HOME/mifi-autotune"
PORT="${MIFI_PORT:-8787}"

cd "$DIR" 2>/dev/null || { echo "[×] 找不到 $DIR，请重新运行 phone/install.sh"; exit 1; }

PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "[×] 没找到 python，先执行 pkg install python"; exit 1; }

SERVER_PID=""
cleanup() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
  command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock 2>/dev/null
}
trap cleanup INT TERM EXIT

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock 2>/dev/null && echo "已获取唤醒锁（防止系统杀掉面板）"
else
  echo "[!] 没装 termux-api，系统可能杀后台；建议 pkg install termux-api"
fi

# 端口被占（上次没退干净）就直接复用，不再起第二个
if "$PY" - "$PORT" <<'PY'
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
    busy = False
except OSError:
    busy = True
finally:
    s.close()
sys.exit(0 if busy else 1)
PY
then
  echo "面板已在运行，直接打开…"
else
  "$PY" server.py --port "$PORT" >"$HOME/.mifi-panel.log" 2>&1 &
  SERVER_PID=$!
fi

# 等面板起来并读到令牌（server.py 首次运行会生成 .panel-token 并持久化）
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -s .panel-token ] && break
  sleep 0.5
done
TOKEN="$(cat .panel-token 2>/dev/null)"
URL="http://127.0.0.1:$PORT/${TOKEN:+?k=$TOKEN}"

echo "面板地址： $URL"
if command -v termux-open-url >/dev/null 2>&1; then
  termux-open-url "$URL" 2>/dev/null || echo "复制上面的地址到浏览器打开"
else
  echo "复制上面的地址到浏览器打开"
fi

echo
echo "面板在运行中 —— 只要这个窗口还在，页面就能打开。"
echo "停止：按 Ctrl+C（或关掉这个会话）。日志：~/.mifi-panel.log"
[ -n "$SERVER_PID" ] && wait "$SERVER_PID"
