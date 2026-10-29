#!/data/data/com.termux/files/usr/bin/bash
# mifi —— 手机上一条命令完成「体检 → 看结论 → 一键优化」
#
#   mifi            体检，给出「现在多少 / 能到多少 / 提多少」，问一句是否应用
#   mifi --status   只看当前状态（几秒，不测量）
#   mifi --scan     只扫周边信道（几秒，验证手机能不能扫）
#   mifi --restore  还原成「不锁频段 + 自动信道」
#   mifi --clean    清掉历史记录与缓存（只留最近一次运行结果、配置和令牌）
#   mifi --web      起本地面板（可选；需要一直留着这个窗口）
#   mifi --update   更新到最新代码（先试电脑面板，再试 GitHub）
#   mifi --help     显示上面这段；mifi --help --all 看全部参数
#
# 为什么默认不再用网页面板：手机上跑网页服务需要一个常驻进程，安卓会为省电把它杀掉，
# 于是浏览器打不开、用户以为工具坏了。终端里跑没有这个问题——进程在前台，
# 系统不会静默杀掉，也不用记地址、不用浏览器。

DIR="$HOME/mifi-autotune"
PORT="${MIFI_PORT:-8787}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }

cd "$DIR" 2>/dev/null || { red "[×] 找不到 $DIR，请先运行 phone/install.sh"; exit 1; }

PY="$(command -v python 2>/dev/null || command -v python3 2>/dev/null)"
[ -n "$PY" ] || { red "[×] 没找到 python，先执行：pkg install python"; exit 1; }

case "${1:-}" in
  --help|-h)
    # 打印本文件顶部的注释块，止于第一条非注释行——比写死行号稳，
    # 以后加参数不会把帮助截断。
    [ "${2:-}" = "--all" ] && exec "$PY" -u autotune.py --help
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
    exit 0
    ;;
  --update)
    [ -f "$DIR/phone/install.sh" ] || { red "[×] 找不到 phone/install.sh"; exit 1; }
    # 先拷一份再执行：install.sh 会 rm -rf 整个目录，脚本自己也在里面
    cp "$DIR/phone/install.sh" "$HOME/.mifi-install.sh"
    exec bash "$HOME/.mifi-install.sh" -f
    ;;
  --web)
    SERVER_PID=""
    cleanup() {
      [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
      command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock 2>/dev/null
    }
    trap cleanup INT TERM EXIT
    command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock 2>/dev/null

    if "$PY" - "$PORT" <<'PYCHK'
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
PYCHK
    then
      echo "面板已在运行，直接打开…"
    else
      "$PY" server.py --port "$PORT" >"$HOME/.mifi-panel.log" 2>&1 &
      SERVER_PID=$!
    fi
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      [ -s .panel-token ] && break
      sleep 0.5
    done
    TOKEN="$(cat .panel-token 2>/dev/null)"
    URL="http://127.0.0.1:$PORT/${TOKEN:+?k=$TOKEN}"
    echo "面板地址： $URL"
    command -v termux-open-url >/dev/null 2>&1 && termux-open-url "$URL" 2>/dev/null
    echo "窗口留着页面就能打开；Ctrl+C 停止。日志：~/.mifi-panel.log"
    [ -n "$SERVER_PID" ] && wait "$SERVER_PID"
    exit 0
    ;;
  "")
    exec "$PY" -u autotune.py --interactive
    ;;
  *)
    exec "$PY" -u autotune.py "$@"
    ;;
esac
