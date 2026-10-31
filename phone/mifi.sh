#!/data/data/com.termux/files/usr/bin/bash
# mifi —— 手机上一条命令完成「体检 → 看结论 → 一键优化」
#
#   mifi            体检，给出「现在多少 / 能到多少 / 提多少」，问一句是否应用
#   mifi --status   只看当前状态（几秒，不测量）
#   mifi --scan     只扫周边信道（几秒，验证手机能不能扫）
#   mifi --restore  还原成「不锁频段 + 自动信道」
#   mifi --guard    全自动守护：在动就解锁频段、久坐就选优并锁定（Ctrl+C 停）
#   mifi --stop     停掉正在跑的守护（Ctrl+C 按不出来时用这个）
#   mifi --clean    清掉历史记录与缓存（只留最近一次运行结果、配置和令牌）
#   mifi --web      起本地面板（可选；需要一直留着这个窗口）
#   mifi --update   更新到最新代码（先试电脑面板，再试 GitHub）
#   mifi --help     显示上面这段；mifi --help --all 看全部参数
#
# 输出会同时留一份在 Termux 自己的目录里（$DIR/last-run.log），跑完可以
# `cat last-run.log` 把完整文字复制出来。**不写手机存储**：/sdcard 属于用户
# 的私人空间，工具没必要碰，也不该碰。
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

# ---- 输出留一份到文件（手机上只能看见最后十几行，早先的频段结果就没了） ----
# 日志位置：优先放 Termux 的存储链接（~/storage/downloads → 手机 Download 目录），
# 电脑接数据线才读得到（Termux 私有目录外部读不了）。没给存储权限就退回
# Termux 自己的目录，纯打屏也照常能用。这是用户 2026-10-30 同意开的口子，
# 只为调试期看输出用，调完就删。
LOG="$DIR/last-run.log"
for _d in "$HOME/storage/downloads"; do
  [ -d "$_d" ] && [ -w "$_d" ] && LOG="$_d/mifi-last.log" && break
done
unset _d

log_head() {
  # 单次运行超过 2MB 就换一份，别让 --guard 挂一天把目录写满
  if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 2000000 ]; then
    mv -f "$LOG" "$LOG.1" 2>/dev/null
  fi
  printf '\n===== %s  mifi %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG" 2>/dev/null
}

# 所有真正跑 autotune.py 的地方都走这里：屏幕照常输出，同时追加到日志。
# 不用 exec，因为 --guard 之后还要放 wake lock。
run() {
  log_head "$@"
  if ! : >>"$LOG" 2>/dev/null; then
    LOG=""                      # 目录不可写（只读安装等）：退回纯打屏
    "$PY" -u autotune.py "$@"
    return $?
  fi
  "$PY" -u autotune.py "$@" 2>&1 | tee -a "$LOG"
  local rc=${PIPESTATUS[0]}
  return "$rc"
}

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
    # 升级会把 $DIR 整个删掉重建。如果动手时 shell 正站在这个目录里，它脚下
    # 的 inode 就没了 —— 之后每条命令都会先刷一行
    # `getcwd: cannot access parent directories: No such file or directory`。
    # 子进程改不了父 shell 的 cwd，所以这里不 exec（exec 掉就没机会说话了），
    # 装完判断一下再明确告诉用户敲 cd ~。
    # 2026-10-31 实机：升级完 mifi --lock-band 3 前就出现了这行，容易让人以为
    # 命令本身出错。
    _was_in_dir=0
    [ "$PWD" = "$DIR" ] && _was_in_dir=1
    bash "$HOME/.mifi-install.sh" -f
    _upd_rc=$?
    if [ "$_was_in_dir" = "1" ]; then
      printf '\n\033[33m[提示] 升级重建了 %s，而你正站在里面。\033[0m\n' "$DIR"
      printf '\033[33m       执行 cd ~ 恢复（这条报错 getcwd 之后出现是正常的，\033[0m\n'
      printf '\033[33m       mifi 命令本身没受影响）。\033[0m\n'
    fi
    exit "$_upd_rc"
    ;;
  --stop)
    # 守护可能开在另一个窗口/后台，Ctrl+C 未必按得到（手机上要先进扩展键栏
    # 点 CTRL 再点 C，很容易按成别的）。这里直接按命令行认进程杀掉 ——
    # 同一个 Termux uid，不需要 root，也不像 adb 那样被权限挡住。
    # 用它停止时守护会走和 Ctrl+C 同一条收尾路径（打印「已停止」）。
    # 2026-10-30 实机：这条命令报过「没有在跑的守护进程」，可 ps 里明明有一个
    # `python -u autotune.py --guard`。原因可能是匹配过窄，也可能 kill 失败被
    # 吞掉了 —— 两种情况都只回一句「没有」，用户完全没法判断。
    # 现在：匹配放宽到「含 autotune.py 且含 --guard」，先打印找到的 pid，
    # kill 之后再查一遍，还在就 -9，最后如实说结果。
    _pids=""
    for _p in /proc/[0-9]*; do
      [ -r "$_p/cmdline" ] || continue
      case "$(tr '\0' ' ' <"$_p/cmdline" 2>/dev/null)" in
        *autotune.py*--guard*)
          _pid="${_p#/proc/}"
          [ "$_pid" = "$$" ] && continue
          _pids="$_pids $_pid"
          ;;
      esac
    done
    _pids="$(echo $_pids)"
    if [ -z "$_pids" ]; then
      echo "没有在跑的守护进程"
    else
      echo "找到守护进程：$_pids，正在停止…"
      for _pid in $_pids; do kill "$_pid" 2>/dev/null; done
      sleep 2
      _left=""
      for _pid in $_pids; do
        [ -d "/proc/$_pid" ] && _left="$_left $_pid"
      done
      if [ -n "$_left" ]; then
        echo "还没退出（$_left），强制结束"
        for _pid in $_left; do kill -9 "$_pid" 2>/dev/null; done
        sleep 1
      fi
      echo "[√] 已停止守护（设备保持当前配置，锁着的频段不会自动解开）"
    fi
    unset _p _pids _pid _left
    # 守护被外部杀掉时它自己来不及放 wake lock，这里补一下，别继续吃电。
    command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock 2>/dev/null
    exit 0
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
    run --interactive
    exit $?
    ;;
  --guard)
    # 全自动守护要长时间挂着，安卓会为省电把后台进程杀掉 —— 先拿 wake lock。
    # 用 exec 的话 trap 不会跑，锁就一直不放，所以这里等它结束再释放。
    command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock 2>/dev/null
    run --guard "${@:2}"
    RC=$?
    command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock 2>/dev/null
    exit $RC
    ;;
  *)
    run "$@"
    exit $?
    ;;
esac
