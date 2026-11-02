"""自动调优：探测周边 2.4G 信道占用 + 实测 LTE 各频段表现，自动选择最优。

安全原则：
  1. 默认只检测、给建议，不加 --apply 绝不改设备
  2. 只动「频段」和「信道」，永不碰 MTU / 加密 / SSID / APN
  3. 每次改动前保存原始状态到 state.json，--restore 可一键还原
  4. 频段改完必须验证能重新上网，否则自动回滚

用法：
  python autotune.py                     # 只体检，不改
  python autotune.py --apply             # 体检并应用最优设置
  python autotune.py --apply --bands all # 全频段扫描（慢，约 5 分钟）
  python autotune.py --restore           # 还原为「不锁频段 + 自动信道」
"""

import argparse
import json
import os
import re
import shutil
import signal
import threading
import statistics
import subprocess
import sys
import traceback
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import probe
import scanner
from drivers import detect, list_drivers
from drivers.base import DeviceBusy, DeviceUnreachable, Unsupported

HERE = Path(__file__).resolve().parent

# 运行期文件（记忆、配置、账本、日志）放哪个目录。
#
# 默认就是代码目录（电脑上当脚本跑，行为和以前一样）。但 APK 里**必须换掉**：
# Chaquopy 把 Python 解包到 files/chaquopy/AssetFinder/app，每次装新版本都会
# 重新解包、把那个目录清空 —— 实测中 一天里就丢了两回：住处的
# `pin_band: 3`、sites.json 的位置记忆、还有下午刚攒的账本，全没了。
# 现在由 bridge.init() 调 set_data_dir() 指到应用私有目录（files/），升级不再动它。
DATA_DIR = HERE
STATE = DATA_DIR / "state.json"
LOG = DATA_DIR / "autotune-log.jsonl"
CONFIG = DATA_DIR / "config.json"

# 版本号：手机上排查「我装的到底是哪一版」全靠它（mifi --version）。
# 改功能就往上加，别让两个不同的行为共用一个号。
__version__ = "0.9.23"

SETTLE_SEC = 26                     # 换频段后等待重新注册
# 判「当地没这个频段」之前的宽限。设备从别的频段切过来要重新搜索、注册，
# 十几秒内 RSRP 读不出来是常态，不是没覆盖。少等这一会儿的代价是误杀一个
# 能用的频段，进而得出「当前设置已经最好、不用改」的错误结论 —— 得不偿失。
NO_SIGNAL_GRACE = 18
MIN_CHANNEL_GAIN = 0.30             # 信道评分改善阈值，低于此不改
MIN_BAND_GAIN_MBPS = 3              # 频段带宽改善阈值
STABILITY_BEST_RATIO = 1.6          # 候选频段「最大/最小」≤1.6 倍才算稳

# 「自动裁决」的取向（2026-10-29 用户定的规则）：
#   速度强太多 → 选快的；速度差不多 → 选稳的；稳定性差不多 → 选快的
SPEED_DECISIVE_RATIO = 1.5          # 用「最差一轮」比，快 50% 以上算强太多
SPEED_SIMILAR_RATIO = 0.9           # 中位数 ≥ 当前的 90% 视为「速度差不多」
STABILITY_DECISIVE_RATIO = 1.4      # 波动倍数好 40% 以上算「明显更稳」
STABILITY_SIMILAR_RATIO = 1.25      # 波动倍数差 25% 以内算「稳定性差不多」
SPEED_FLOOR_MBPS = 3                # 最优频段都低于这个值，说明网络太差、结论不可信
# 换频段的信号闸门：候选频段实测 RSRP 比当前驻留差这么多，就不换。
# 实机 2026-10-30 21:56（手机）：BAND 40 10.8 Mbps / -81dBm，BAND 3 11.8 Mbps，
# 裁决说「速度强太多」把设备切到 BAND 3 —— 那边只有 -95dBm，用 14dB 的余量
# 换 1 Mbps 的账面差。速度差 9%，而同一屏下面还印着「数据不足以下结论」。
BAND_RSRP_DROP = 10
MAX_USABLE_LOSS = 20                # 丢包 ≥20%：带宽数字再好看也不能选
MAX_USABLE_PING_MS = 800            # 延迟 ≥800ms：同上
OBSERVE_SEC = 90                    # 应用后盯多久，确认「能一直通」
PLAN = DATA_DIR / "plan.json"       # 体检结论，供「一键应用」复用
CHANNEL_ADVICE = DATA_DIR / "channel_advice.json"   # 「扫描信道」的建议，单独存
SITES = DATA_DIR / "sites.json"     # 位置记忆：每个 PCI 下哪些频段有覆盖、哪个最好
# 「为什么锁这条」的账本，**故意不和 sites.json 放一起**。
#
# 2026-11-02 实机踩到：守护线程和界面/状态查询线程各自「读文件 → 改 → 写回」，
# 后写的一方拿的是自己读到的旧快照，于是刚记下的账目被整段覆盖掉 —— 界面里
# 就是「明明锁过，账本还是空的」。两个线程写同一个 JSON 就会这样。
# 账本是只追加的，给它一个自己的文件，就不会被别人的读改写连累。
DECISIONS = DATA_DIR / "decisions.json"
# 「这地方这会儿没网」的记录。和 decisions 分开是同一类理由（两拨线程写同一个
# 文件必然互相覆盖），但它还有个独立用途：**冷却窗口**。
# 实测中 的设计要求原话是「在一个地方重复检测到没网，后续就不测，
# 等一段时间没来才能进行全量测试」—— 需要一个跨重启、跨线程都能读到的时间戳。
OUTAGE = DATA_DIR / "guard_outage.json"

# 守护模式（--watch）的劣化判定阈值。
# 三段式：min(上限, max(基线×倍数, 下限))
#   下限   —— 正常波动不许触发，否则一有抖动就断网重连
#   倍数   —— 本来就慢的链路（基线 120ms）按比例放宽，不误判
#   上限   —— 防止基线太高时阈值一起飘走，那样永远触发不了
WATCH_LOSS_LIMIT = 3                # 丢包 ≥3% 视为劣化
WATCH_FACTOR = 1.8                  # 相对基线的倍数

WATCH_MIN_MS = 80                   # 延迟下限
WATCH_CAP_MS = 260                  # 延迟上限
WATCH_COLD_MS = 180                 # 冷启动（还没有基线）时的默认阈值

WATCH_JITTER_MIN_MS = 25            # 抖动下限
WATCH_JITTER_CAP_MS = 80            # 抖动上限
WATCH_JITTER_COLD_MS = 40           # 冷启动抖动阈值

LOG_KEEP = 1                        # 运行记录默认只留最近一次（0 = 写完就删）

# 位置记忆：同一个地方测过几次之后，别再重复踩同样的坑。
DEAD_SKIP_MIN = 3                   # 连续这么多次「无信号」才敢跳过
DEAD_REPROBE_EVERY = 5              # 但每 N 次强制全测一遍，防止环境变了还一直跳
# 记忆会随时间过期。实测（2026-10-30）指出的场景：一个地方以前常来，
# 隔了很久没来 —— 这期间可能加了基站/拆了基站、邻居换了几轮、晚上八点的人流
# 也完全不同，拿旧数据跳着测就是拿过期结论当答案。所以分三档：
#   fresh  72 小时内  —— 记忆全信，可以用「只测两个」的快速模式
#   aging  3~7 天     —— 覆盖还在（有没有信号很稳定），速度作废：能收到的全测
#   stale  超过 7 天  —— 整段记忆作废，一条都不跳，全部重测
SITE_FRESH_HOURS = 72
SITE_STALE_DAYS = 7
SITE_TTL_DAYS = 30                  # 这么久没来过的位置，连记录一起删掉（搬家/换城市）
FAST_LEAD_RATIO = 1.2               # 历史最优比第二名快这么多倍，才敢只测它俩
FAST_MIN_SEEN = 2                   # 至少来过这么多次，历史才可信（一次不算）

# 滚动样本：每个频段留最近几次「几点、多快、丢包多少、延迟多少」。
# 为什么光有 best 不够：54 秒的体检碰不到「每 3~5 分钟掉一次包」这种事
# （实测 2026-10-30 住处：BAND 40 跑 41Mbps，三五分钟就丢包 30~50%），
# 而「晚上八点比凌晨慢一半」也只有按时段攒样本才看得出来。
# 只留最近 SITE_SAMPLES 次，文件不会随运行次数膨胀。
SITE_SAMPLES = 8                    # 每个频段保留几个样本
SITE_LOSS_BAD = 20                  # 单次丢包 ≥ 这个值算「这次掉包了」
SITE_FLAP_RATIO = 0.3               # 历史样本里掉包占比 ≥ 30% → 判为「会周期性掉包」
SITE_FLAP_MIN_BAD = 2               # 坏样本至少这么多条才定罪：单次偶发不算
# 「按时段看丢包」的窗口和门槛。
#
# 实测中：「band40 在住处尤其是晚上丢包严重，但是在其他地方他的
# 可用性还是很高的」。全天平均会把这个信息抹平 —— 晚上掉 100%、白天一次没掉，
# 平均下来只是「这条偶尔掉」，于是白天的好时段被连坐、晚上的坏时段又不够刺眼。
GUARD_FLAP_HOUR_WINDOW = 2          # 当前钟点前后几小时算「这个时段」
GUARD_FLAP_HOUR_MIN = 2             # 这个时段至少几条样本才敢用它下结论

# 「锁完回评」的判据。
#
# 实测中：「现在只记了『锁住了没垮』，没记『锁对了没』」。锁上十分钟
# 链路干净，可能本来就这么干净 —— 不跟锁之前比，这个记录说明不了任何事。
# 所以锁的那一刻存一份锁前基线，十分钟后再量一次，两个数放在一起看。
REVIEW_LOSS_GAIN = 5                # 丢包相差这么多个百分点才算「变了」
REVIEW_LAT_GAIN = 0.25              # 延迟变化超过 25% 才算「变了」
DECISIONS_KEEP = 20                 # 每个位置留最近多少条「为什么锁这条」
# 「锁过但没好处」攒到这个数、且一次好处都没见着，就不再锁它。
# 和 SITE_FLAP_MIN_BAD 一样要 2 次：一次可能是那会儿基站正好挤。
REVIEW_BAD_MIN = 2
# 比全天那条（30%）严得多：时段窗口窄、样本少，用 30% 的话「2 条里 1 条坏」
# 就能给整条频段定罪 —— 而一次偶发（隔壁开微波炉）不该让晚上不能用它。
GUARD_FLAP_HOUR_RATIO = 0.6
FLAP_SWAP_RATIO = 0.85              # 稳的那个只要不慢过 15%，就优先它
SITE_MERGE_MIN = 20                 # 同运营商、上次记录在这么多分钟内 → 算同一个地方
# 一个位置「见过哪些小区」。为什么不能只靠上面那条时间窗：20 分钟一过，
# 同一张桌子的两条记录就互不相认了。实测（2026-10-30 住处）就是这么分家的 ——
# 锁 BAND 3 时是 PCI 350（记着 BAND 3 有 46.8 Mbps），解锁后设备自己驻留 BAND 40
# 变成 PCI 236，于是新建了一条空记录；下次再测，46.8 那条历史完全没被看到。
# 所以位置改成「一组见过的小区」：只要当前 PCI 在这组里，就是同一处，跨天也认得出。
SITE_CELL_KEEP = 12                 # 每个位置最多记几个见过的小区
# 为什么是 12：一次体检就会把这张桌子的 6 条频段（6 个 PCI）全记进来，再加上
# 期间设备自己重选过的小区。原来 8 太紧 —— 截断时从最先记下的开始丢，而最先
# 记下的往往正是最常用的那条频段。

# 全自动守护（--guard）：按「人在不在动」决定锁不锁频段。
#
# PCI 用来判断移动，但它只是**必要条件，不是充分条件**。原来以为「人没动 PCI
# 就不会跳」，2026-10-30 的实机日志把这个假设推翻了：坐在住处里，锁着 BAND 41
# 的那三分钟，PCI 自己从 477 跳到 252（-89dBm → -105dBm）—— 信号变弱时设备
# 会自己重选小区（同频段换扇区），人在原地，PCI 照样跳。
# 所以判移动要两件事一起看：**PCI 变过几次** + **信号/链路是不是也跟着垮了**。
# 守护循环的停止开关。
#
# 终端里是按 Ctrl+C 停，但 APK 的界面没有 Ctrl+C —— 那边是在线程里跑的守护，
# 信号处理器装不上（非主线程装不了），只能靠这个 Event 让循环自己退出。
# 放成模块级是刻意的：一个进程只跑一个守护，两边（命令行 / app）用的是同一份。
_GUARD_STOP = threading.Event()


def guard_request_stop():
    """请求守护停下。最多等一个采样间隔（30 秒）就真的退出。"""
    _GUARD_STOP.set()
    return True


GUARD_INTERVAL = 30                 # 采样间隔（秒）：10 个 ping，几百字节
GUARD_SETTLE_SEC = 600              # 连续这么久 PCI 没变 = 原地不动（10 分钟）
GUARD_MOVE_WINDOW = 300             # 判定「在移动」的时间窗
# 窗口内 PCI 变这么多次 = 在移动。
# 这两个数是配着定的，试过两轮：
#   · 180s / 2 次：静止时一次弱信号重选就够数（477→252），人被判定「在移动」。
#   · 180s / 3 次：反过来了 —— 走路两分钟才换一个小区，180 秒窗口里凑不满 3 次，
#     明明在走却判「静止」，10 分钟后把频段锁死在路上（这是最危险的误判）。
#   · 300s / 2 次：走路 5 分钟内必换两次；静止时偶尔跳一次也够不到两次。
GUARD_MOVE_HITS = 2
# PCI 变了、但信号没掉这么多 dB → 是设备自己重选，不算人动，静止计时不清零。
GUARD_RESELECT_DB = 8
# 单次采样延迟超过这么多毫秒，也算这条链路不行（原来只看「不通」和「丢包过半」，
# 实机 17:24 延迟 4402ms、只丢 10%，会被当成「正常」）。
GUARD_LAG_MS = 2000
GUARD_BAD_HITS = 3                  # 连续这么多次采样不通 = 锁定的频段撑不住
# 锁着的时候只看「链路垮没垮」不够。人从住处走到餐厅，原来那条 BAND 3 会从
# -98dBm 掉到 -115dBm，但餐厅未必到「丢包过半 / 延迟 2 秒」那么惨 —— 于是守护
# 一路「监控中」，明明换了地方也不解锁。
# 所以另立一条：**这条的信号比锁定那一刻塌了 10dB 以上，锁的前提就没了**。
GUARD_LOCK_SIGNAL_HITS = 2          # 连续这么多拍信号塌了才动，挡住单拍抖动
GUARD_LOCK_SIGNAL_WIN = 4           # 参考值取锁定后最近几拍里最好的那个 RSRP
# 窗口内换这么多次小区，即使链路还没垮也算「在动」。
# 为什么不是 GUARD_MOVE_HITS(2)：住处坐着不动，设备自己在两个扇区之间重选也能
# 凑到 2 次（2026-10-31 实机坐了 144 分钟被误判移动）。3 次基本只有真走路才有。
GUARD_MOVE_CHURN_ONLY = 3
# 「你亲手钉的那条频段」多久之后不再自动生效。
# 一次 --lock-band 说的是「当时、那个地方」。住处里 BAND 3 好，不等于一周后办公区
# 里的 BAND 3 也好 —— 而 pin 现在排在所有判断之前，连刚测出来的数据都推翻。
# 实机 2026-11-02：办公区里程序算出来该锁 BAND 40，被 10-31 设的 pin 直接改成 3。
GUARD_PIN_TTL_DAYS = 7
# 锁着频段时的门槛，比上面那条松一档。
#
# 依据：设备被锁住之后**换不了频段**，剩下能变的只有「同频段内换小区」。而锁定
# 期间同频段换小区，要么是人挪了位置，要么是信号掉到边缘触发了重选 —— 后者在
# 一个坐着不动的位置上很少连续发生。所以锁定时 2 次就够当证据。
#
# 代价不对称：漏判一次，人带着住处那条锁在路上/办公区里待一整天（2026-11-02 上午
# 就是这样，08:22 锁 BAND 3，09:19 才解开，中间 57 分钟全是「监控中」）；误判
# 一次，只是丢掉锁、重新等 10 分钟。所以宁可松一档。
GUARD_MOVE_CHURN_LOCKED = 2
# 锁着的时候还有一条独立线索：信号一路走低。人走出覆盖时 RSRP 是单调往下掉的，
# 而设备自己重选到弱小区是「跳」一下就不是了（-89 → -105 之后一直 -105）。
# 所以要求连续几拍都在降、且累计掉够，才算「在往外走」。
GUARD_MOVE_RSRP_WIN = 3             # 看最近几拍（约 90 秒）
GUARD_MOVE_RSRP_DROP = 8            # 累计掉这么多 dB 且每拍都在降 = 人在往外走
# 锁定之后每隔这么久复核一次「锁定这条还够不够用」。
# 为什么必须有：锁上之后就再也不看记忆、不测速了（设计如此，免得反复断网），
# 于是「人换了个地方、这条变慢但还没到丢包/延迟阈值」这种情况没有任何出口。
# 若是两地挨得近（比如餐厅离住处两百米），信号几乎不掉 —— 连「信号塌了」都判不
# 出来，屏幕上只有「换小区」和「监控中」，一整个午饭都卡在那条频段上。
GUARD_LOCK_AUDIT_SEC = 1200
# 两拍之间隔了这么久，说明进程被系统挂起了（Termux 一退到后台就会被冻结）。
# 这个数只是「报出来」的门槛，不影响任何决策 —— 不报的话，用户会以为守护在跑。
# 阈值 = interval × FACTOR + SLACK。FACTOR 取 3 而不是 2，是因为正常一拍本身
# 就要十几秒（10 个 ping + 读一次状态），撞上锁定复核那一拍还要多花 20 秒量速度；
# 用 2 倍容易把「本来就慢的一拍」报成冻结。
GUARD_HANG_FACTOR = 3
GUARD_HANG_SLACK = 90
GUARD_COOLDOWN = 300                # 动作后的冷却，避免反复断网（只管主动动作）
# 「刚解锁还一直不通」的判定窗口。必须比 GUARD_COOLDOWN 长，否则解锁刚把冷却
# 挂上，窗口就已经关了，后面那条「解锁仍不通 → 跑体检」永远走不到。
GUARD_RESCUE_WINDOW = 900

# 没网的时候「测」是纯浪费。实测中 18:44~18:55 的实机日志：
#   18:45:24  延迟 不通  丢包 100%
#   18:47:46  跑一次完整体检再锁（会断网几次，约 5 分钟）
#   18:55:55  体检结束…结论「保持现状」→ 锁回 BAND 41
#   18:56:41  延迟 不通  丢包 100%      ← 八分钟过去，原地踏步
# 体检的每一个频段都要靠下载测速，而整条链路断着的时候，六个频段量出来全是 0，
# 结论必然是「保持现状」。它唯一确定产生的东西是：5 分钟断网 + 4 次射频重启。
# 所以：**链路整条不通时，不跑体检**。
#
# 然后是用户当初的设计要求（原话）：「在一个地方重复检测到没网，后续就不测，
# 等一段时间没来才能进行全量测试」。所以还要一个冷却窗口：同一个地方断过一次，
# 这半小时内不再试图全量测；网络恢复、并且时间过够，才允许重新测。
GUARD_OUTAGE_COOLDOWN = 1800        # 断网后多久内不再试全量测试（30 分钟）
GUARD_OUTAGE_FAST_SEC = 10          # 没网时采样间隔压到这么短，网络恢复要尽快发现

# 新地方（从没测过）第一次久坐时怎么处理：不现场跑 5 分钟体检，先把「当前
# 驻留的那条」锁住（设备本来就在上面，几秒、几乎不断网），并给这个位置打个
# provisional 标记；下次再来才补一次完整体检。理由：新地方多半只待一两小时，
# 花 15 分钟找全局最优不划算，而「别让它自己乱跳」当场就有收益。
GUARD_NEW_SITE = "这个位置没测过"

# 新地方第二次来，能不能不再从零试一遍？
# provisional 原来的结局是：只要没跑过完整体检，就永远是「临时记录」，下次来
# 还是等 10 分钟 + 快测 + 锁当前这条。可餐厅这种地方天天去，昨天已经证明
# 「在这儿锁 BAND 40 能用」—— 一次实测都没做过，却每次都要从头猜，说不过去。
# 折中：不算速度，只看「锁住了能不能撑住」。锁住 10 分钟、链路还干净，记一次
# 「这地方这条守得住」；攒够两次才升成正式记忆（一次可能是运气好，两次不像）。
GUARD_CONFIRM_SEC = 600             # 锁住这么久还正常 = 这地方这条守得住一次
GUARD_CONFIRM_LOCKS = 2             # 守得住这么多次，才升成正式记忆
# 升成正式记忆之后，同一个地方就不必再等满 10 分钟：站够 5 分钟就锁。
# 为什么敢缩短：10 分钟那道坎是为了「别给路过的人锁频段」，而这里是**已经**在
# 同一个 PCI、同一条频段上守过两次的地方 —— 认得出来的场景，风险小得多。
# 仍然只缩短到一半，且只在「当前驻留的正是那条」时生效：要切到别的频段去锁，
# 那是另一个场景，照旧等满。
GUARD_KNOWN_SETTLE = 300

# 停下多久先「快测」当前这条（只测不锁）。实测中 的规则：人从老地方
# 走到新地方，路上本来就是解锁状态（按信号保底有网）；停下来的地方很可能只是
# 路过，这时候把频段锁死，后面走到没覆盖的地方就是断网。所以先只「看看这条够不够用」。
GUARD_QUICK_SEC = 180               # 停 3 分钟就先快测一次
# 「够用」的下限。原来是 15 Mbps，实机证明这个数卡在噪声区里：同一张桌子、
# 前后 22 分钟，快测 32 Mbps 而体检只有 17.8 / 10.8。15 落在波动区间中间，
# 同样的网络掷到 32 判「够用」（不锁，什么也不发生），掷到 12 判「不够用」
# （触发 5 分钟体检 + 断网 4 次）—— 判错的代价完全不对等。
# 所以下限压到 8：只有真的差才承认「不够用」，宁可放过，不要误动设备。
GUARD_QUICK_MIN_MBPS = 8            # 低于这个才算「不够用」
GUARD_QUICK_GOOD_MBPS = 20          # 达到这个算「明确够用」
GUARD_QUICK_MAX_LOSS = 5            # 「够用」的丢包上限
GUARD_QUICK_MAX_MS = 150            # 「够用」的延迟上限
GUARD_QUICK_PROBE_SEC = 4           # 每次快测下 4 秒（测两遍，总时长和原来相当）
GUARD_QUICK_SAMPLES = 2             # 采样几次。单次 5 秒下载的重复性太差
GUARD_QUICK_NOISY_RATIO = 3.0       # 两次差这么多倍 → 测不准，别拿它下结论
# 快测结果最多算它「当时」有效多久。停 3 分钟先测一次，坐满 10 分钟才下锁 ——
# 中间那 7 分钟里链路完全可能变一个样（晚高峰尤甚），而原来这个数字会一路
# 带到锁定那一刻。实机 2026-11-01 19:28：18:21:13 测出 9 Mbps（两次 2 / 9，
# 当场判「测不准」），18:28:01 下锁时又拿同一个 9 Mbps 说「够用」—— 一组数字
# 得出两个相反的结论，而且早已过期。过期就重测一次：只测不切，20 秒，不断网。
GUARD_QUICK_MAX_AGE = 240           # 快测结果超过这么久（秒）就不能当锁定依据
# 久坐锁定前，把「刚才快测到的当前速度」和「记忆里的历史最优」比一下。
# 实测（2026-10-30 18:00）：记忆说 BAND 3 有 44.6 Mbps，但那是上午、还是在
# 另一个 PCI（350）下测的；当时实测当前 BAND 40 有 32 Mbps，延迟 65ms 正常。
# 差 30% 以内就别折腾 —— 切一次频段要断几秒、要重新注册，而"记忆里的最优"
# 本身可能是别的扇区、别的时段测出来的，未必代表现在。只有明显快得多才值得切。
# 2026-10-31 实机改判：这个比例原来是 0.7（当前达到记忆最优的七成就别切），
# 结果 13:0x 的体检刚把「BAND 3 = 21.4 Mbps」写进记忆，14:12 快测当前 BAND 40
# 有 30 Mbps（30 ≥ 21.4×0.7）就判 keep、锁了 40，把「这个位置的最优」留在了 3
# 却锁住 40。用户的原话是「熟悉的地方要优先锁历史记录里最优的」—— 20 秒快测
# 没资格推翻多轮体检的结论，尤其是 BAND 40 当晚体检只有 1.3 Mbps / 678ms、
# 丢包 80% 那条链路。
# 所以比例改成 1.5：当前这条要不切换，必须**明显**快过记忆最优（快 50% 以上），
# 「差不多」不再算理由。
GUARD_QUICK_KEEP_RATIO = 1.5        # 当前必须 ≥ 历史最优 × 1.5 才留在当前

# 锁之前先看信号，这是「记忆说它快」和「它现在能不能用」的区别。
# 实测（2026-10-30 住处）：记忆里 BAND 3 有 46.8 Mbps，可那天在这个位置
# BAND 3 只有 -105dBm、而设备实际驻留的 BAND 40 是 -77dBm。照记忆锁 BAND 3，
# 结果 8 分钟里 -95 → -110、延迟飙到 4.4 秒、最后丢包，被当成「检测到移动」解锁。
# 原因是 record_site 只记了速度，没记当时的 RSRP —— 分不清「上次条件好」和
# 「这次条件差」。所以样本里开始存 RSRP；差这么多 dB 就否决这条记忆。
GUARD_LOCK_RSRP_DROP = 10           # 记忆最优比当前驻留差这么多 dB → 不锁它

# 锁定之后判「这条不行」的线。原来是丢包 ≥20%，太敏感：住处 BAND 40 每 3~5 分钟
# 掉 30~50%，会导致守护反复解锁 —— 越解越像「厂商的自动」，而这正是要治的病。
# 只有「完全不通」或「丢包过半」才算真的撑不住。
GUARD_BAD_LOSS = 50

# 「去验一次记忆里的最优」的最短间隔。记忆里那条最优没记过信号时只能实测，
# 但实测 = 切两次频段 ≈ 2~3 分钟断网，所以每处限频，绝不能变成周期性测速
# （用户明确反对：发热、卡顿）。实机 2026-10-31 一整早都没验过，就是因为
# 压根没有这条路 —— 只有「不敢锁」和「干脆锁当前」两个选项。
GUARD_VERIFY_HOURS = 20
# 丢包留档的最小间隔：样本槽只有 8 个，30 秒一条会四分钟就填满，
# 全被同一次掉包占满，反而看不出「周期性」。
GUARD_LOSS_NOTE_SEC = 120
# 「守护采样到的信号」写进记忆的最小间隔。守护和半自动（体检 / --apply-plan）
# 共用同一份 sites.json，但守护原来只在「锁定的那一刻」写一次 RSRP ——
# 于是记忆里长期没有信号，下次来判「这条还能不能用」时只能瞎猜（blind）。
# 采样每分钟都有，5 分钟写一次就够，不折腾文件。
GUARD_RSRP_NOTE_SEC = 300

# 决赛：第二名和第一名的差距小于这个比例时，两个都要复测。
# 单轮测速的波动轻易就能有 10~20%，差 8% 的两个频段谁快谁慢根本说不清 ——
# 这时只复测第一名，等于拿运气当结论（实测 2026-10-30：31.5 vs 29.0）。
CONTEST_CLOSE_RATIO = 0.25

DEFAULT_CONFIG = {
    "own_ssid": "",                 # 自己设备的 WiFi 名，用于从干扰源里排除
    "speed_url": probe.DEFAULT_SPEED_URL,
    "ping_host": probe.DEFAULT_PING_HOST,
    "candidate_bands": [3, 40],     # 仅 --bands preferred 时用来缩小范围
    "log_keep": LOG_KEEP,           # 想留更多历史就改成 5、10；0 = 不保存
}


# ---------------- 配置 ----------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    return cfg


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def save_state(payload):
    STATE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _log_keep():
    """运行记录保留几条：环境变量 > config.json > 默认 1。"""
    raw = os.environ.get("MIFI_LOG_KEEP")
    if raw is None or str(raw).strip() == "":
        raw = load_config().get("log_keep", LOG_KEEP)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return LOG_KEEP


def append_log(entry):
    """写一条运行记录，并只保留最近 log_keep 条（默认 1 条 = 上一次）。

    为什么不是纯追加：手机上每跑一次体检就多一份完整报告，跑十次就是十份
    堆在那儿；用户要的是「保留最近一次」，不是一本流水账。
    裁剪用「写临时文件 + 原子替换」，避免写到一半断电把仅有的记录也弄坏。
    """
    keep = _log_keep()
    if keep <= 0:
        try:
            LOG.unlink()
        except OSError:
            pass
        return

    lines = []
    if LOG.exists():
        try:
            lines = [ln for ln in LOG.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            lines = []
    lines.append(json.dumps(entry, ensure_ascii=False))
    if len(lines) > keep:
        lines = lines[-keep:]

    tmp = LOG.with_name(LOG.name + ".tmp")
    try:
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, LOG)
    except OSError:
        # 裁剪失败不能让体检白跑：退回普通追加
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def clean_run_files():
    """清掉运行产物（保留 config.json 和面板令牌）。

    手机上只想留「上一次」，历史残留（旧报告、缓存）留着只会占地方、
    还会让 `ls` 里一堆 .pyc 看着很乱。
    """
    removed = []
    for p in (PLAN, STATE, LOG):
        try:
            if p.exists():
                p.unlink()
                removed.append(p.name)
        except OSError:
            pass
    for cache in HERE.rglob("__pycache__"):
        try:
            shutil.rmtree(cache)
            removed.append(str(cache.relative_to(HERE)))
        except OSError:
            pass
    return removed


# ---------------- LTE 频段 ----------------

def _signal_usable(st):
    """设备是否报了可用的服务小区。

    RSRP 常见的"没服务"取值：空、0、-0、0dBm、--。不同固件写法不一样，
    所以只做保守判断：拿得到一个像样的负数 dBm 才算有信号。
    """
    sig = st.get("signal") or {}
    raw = str(sig.get("rsrp") or "").strip()
    m = re.search(r"-?\d+", raw)
    if not m:
        return False
    return int(m.group()) < 0          # RSRP 一定是负值，0/正数都代表无效


SIG_RSRP_WEAK = -110     # 低于这个：基本没法用（覆盖不足）
SIG_RSRP_OK = -100       # 高于这个：覆盖够用
SIG_SINR_BAD = 3         # 低于等于这个：干扰很重
SIG_SINR_OK = 10         # 高于等于这个：信号干净


def _num(v):
    """从设备/日志里的字符串抠出数字。抓不到返回 None。

    注意 0 是合法数字：丢包 0%、延迟 0 都要能读出来。老写法是
    `str(v or "")`，把 0 变成了空串 → 返回 None ——「0% 丢包」这条最健康的
    读数会被当成「没读到」。2026-11-02 做锁后回评时就踩在这上面：
    锁前 20% 丢包、锁后 0%，因为后一个 0 变成了 None，结论是「跟锁之前差不多」。
    """
    if v is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def diagnose_signal(sig):
    """按 RSRP / SINR 判断「换频段到底有没有用」。

    为什么要单独诊断：用户看到「本次不改动」时无从判断是「工具测不准」
    还是「环境真没救」。实测（2026-10-29）同一位置隔一小时，RSRP 从 -118dBm
    回到 -94dBm，两种情形下换频段的收益完全不同，必须区分：

      · 覆盖不足（RSRP ≤ -110）  → 所有频段都不会好，换频段救不了，挪位置才行
      · 干扰主导（SINR ≤ 3）     → 覆盖够但信号脏，换频段成功率高
      · 两者都好                 → 环境本来不差，切换收益有限

    返回 None 表示设备没报出可解析的信号值（不能猜）。
    """
    rsrp = _num((sig or {}).get("rsrp"))
    if rsrp is None or rsrp >= 0:       # 0 / 正数都是"无服务"的写法
        return None
    sinr = _num((sig or {}).get("sinr"))
    head = f"RSRP {rsrp:g}dBm" + (f"、SINR {sinr:g}dB" if sinr is not None else "")

    if rsrp <= SIG_RSRP_WEAK:
        return {"level": "weak", "text": f"{head} —— 覆盖不足",
                "advice": "这个强度换频段救不了（哪个频段都会很差）。"
                          "把设备挪到窗边、抬高、避开金属和承重墙，比任何软件调优都有效。"}
    if sinr is not None and sinr <= SIG_SINR_BAD:
        # RSRP -103dBm 这种「偏弱」不能说成「覆盖够」—— 实跑出现过
        # 「RSRP -103dBm、SINR -1dB —— 覆盖够但干扰重」，与数值不符。
        weak_cover = rsrp < SIG_RSRP_OK
        return {"level": "noisy",
                "text": f"{head} —— {'覆盖偏弱' if weak_cover else '覆盖够'}但干扰重",
                "advice": ("属于「干扰主导」：这类环境下换频段收益通常明显，"
                           "如果下面给出了切换建议，值得照做。"
                           + ("另外覆盖本身也偏弱，位置能改善就一起改善。" if weak_cover else ""))}
    if rsrp >= SIG_RSRP_OK and (sinr is None or sinr >= SIG_SINR_OK):
        return {"level": "good", "text": f"{head} —— 信号良好",
                "advice": "环境本来就不差，切换频段的收益有限，别指望大幅提速。"}
    return {"level": "ok", "text": head,
            "advice": "信号中等，切换效果取决于目标频段当时的负载。"}


def wait_for_service(dev, band, timeout=None, step=2.5, dead_confirm=3, grace=None):
    """等设备在目标频段重新注册，并尽快识别"这个频段根本没信号"。

    为什么不能只 sleep 固定秒数：
      - 有信号的频段可能 12 秒就注册上了，傻等 26 秒纯属浪费；
      - 没信号的频段（运营商在当地没部署）等再久也没用，越早跳过早省事。
    所以改成轮询设备状态，两个退出条件：
      1) 报出可用的 RSRP 且当前频段=目标 → 成功
      2) 连续 dead_confirm 次仍无可用信号 → 判定无服务，立即返回
    返回 (status, 秒数)，status ∈ {"ok", "no_signal", "timeout"}。
    """
    timeout = SETTLE_SEC + 14 if timeout is None else timeout
    grace = NO_SIGNAL_GRACE if grace is None else grace
    t0 = time.time()
    miss = 0
    seen_band = False
    while time.time() - t0 < timeout:
        time.sleep(step)
        try:
            st = dev.get_state()
        except Exception:  # noqa: BLE001
            continue
        now_band = st.get("band")
        on_target = (not band) or str(now_band) == str(band)
        if on_target:
            seen_band = True
        usable = _signal_usable(st)

        if usable and on_target:
            return "ok", round(time.time() - t0, 1)

        # 「读不到信号」只有在**确认设备已经站在目标频段上**之后才有意义。
        # 设备还停在旧频段（指令刚下发、或这次切换被拒）时读不到，是因为它
        # 压根没切过去，不是当地没覆盖 —— 那种情况不能累计 miss。
        #
        # 2026-11-01 实机教训：用户在自己家里反复测过很多次都有 BAND 3，
        # 这次却被判「无信号（当地没这个频段）」。原因是旧逻辑连续 3 次、
        # 每 2.5 秒读一次，7.5 秒就下结论，而设备从别的频段切回来重新注册
        # 通常要十几秒 —— 它只是还没注册上，被当成了没覆盖。
        if on_target and not usable:
            miss += 1

        if (seen_band and on_target and miss >= dead_confirm
                and time.time() - t0 >= grace):
            return "no_signal", round(time.time() - t0, 1)

        if not seen_band and time.time() - t0 > timeout - step:
            break
    # 最后再看一眼：可能刚注册上
    try:
        st = dev.get_state()
        if _signal_usable(st) and (not band or str(st.get("band")) == str(band)):
            return "ok", round(time.time() - t0, 1)
    except Exception:  # noqa: BLE001
        pass
    return "timeout", round(time.time() - t0, 1)


def restore_band(dev, band, tries=3, poll=45, step=5):
    """把设备切回原频段，然后**耐心等它重新注册**到能读状态为止。

    为什么不能「发一次 POST，睡 26 秒，读一次」：
    实测（2026-10-29）在无覆盖频段上，设备是**在线**的、也照常发认证挑战，
    但响应体是空的 —— 也就是切回好频段之后，它需要一段时间重新注册才会重新
    吐 XML。这期间读状态会失败，但设备本身没问题。固定等一次就判定失败，
    会把「还在恢复」误判成「还原失败」，也让最后一行结论不可信。
    """
    last = None
    for i in range(tries):
        try:
            st = dev.get_state()
            if str(st.get("band") or "") == str(band or ""):
                return True, st
        except Exception as e:  # noqa: BLE001
            last = e            # 读不到就继续往下发指令，不要卡在这里

        try:
            dev.set_band(band)   # 实测：状态读不出时，这条 POST 依然能改频段
        except Exception as e:   # noqa: BLE001
            last = e
            if i < tries - 1:
                print(f"      [!] 第 {i + 1} 次还原没成功（{type(e).__name__}），稍后重试…")
                time.sleep(min(step * (i + 1), 15))
                continue

        # 发出去了，等它重新注册（这段时间读不到状态是正常的）
        deadline = time.time() + poll
        while time.time() < deadline:
            time.sleep(step)
            try:
                st = dev.get_state()
            except Exception as e:  # noqa: BLE001
                last = e
                continue
            if str(st.get("band") or "") == str(band or ""):
                return True, st
        if i < tries - 1:
            print(f"      [!] 第 {i + 1} 次还原后仍读不到状态，继续等…")
    if last is not None:
        print(f"      （最后一次错误：{type(last).__name__}: {last}）")
    return False, None


def measure_band(dev, band, rounds=3, speed_url=None, ping_host=None):
    """单频段多轮实测，取中位数，同时保留离散度用于判断可信程度。

    无服务的频段会在 wait_for_service 阶段就被判定并跳过，
    不再白等固定的 26 秒，也不会去跑根本跑不动的测速。

    任何一步失败（设备切频段瞬间失联、写频段被拒、测速异常）都只让**这一个
    频段**判为不可用，绝不允许异常冒出去把整轮体检打断 —— 否则设备会被留在
    半途的频段上（实测踩过：停在 BAND 8，丢包 60%）。
    """
    empty = {"band": band, "ok": False, "mbps": 0, "mbps_all": [],
             "mbps_min": 0, "mbps_max": 0, "avg": None, "max": None,
             "jitter": None, "loss": 100, "rounds": 0, "rsrp": None,
             "pci": None}

    try:
        dev.set_band(band)
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        return dict(empty, reason="device_error", error=f"{type(e).__name__}: {e}")

    try:
        status, waited = wait_for_service(dev, band)
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        return dict(empty, reason="device_error", error=f"{type(e).__name__}: {e}")

    if status != "ok":
        return dict(empty, reason=status, waited=waited)

    # 注册成功 ≠ 数据通道就绪。实测（2026-10-29 手机端）：BAND 40 在 4.6 秒就被判
    # 「有信号但数据不通」，而它实际能跑 27~31 Mbps —— 刚注册完那一两秒 ping 全丢，
    # 拿一次结果就下结论会漏掉真正更快的频段。这里多确认几次再判死。
    st = {"avg": None, "loss": 100}
    for attempt in range(4):
        try:
            st = probe.ping_stats(6, ping_host)
        except Exception:  # noqa: BLE001
            st = {"avg": None, "loss": 100}
        if st.get("avg") is not None and st.get("loss") != 100:
            break
        if attempt < 3:
            time.sleep(5)
    if st.get("avg") is None or st.get("loss") == 100:
        return dict(empty, reason="no_data", waited=waited)

    samples = []
    # 记下这条频段此刻的信号强度。锁频段前要拿它跟当前驻留的比 ——
    # 「历史上很快」和「现在能不能用」是两件事（2026-10-30 实机教训）。
    # 顺手把 PCI 也带回来：锁到哪条频段就换哪个服务小区，这一条是「同一个
    # 地方的另一张脸」，是位置记忆唯一的桥（见 record_site 里的 cells）。
    rsrp = None
    pci = None
    try:
        sig = (dev.get_state() or {}).get("signal") or {}
        rsrp = _num(sig.get("rsrp"))
        pci = _pci_num(sig.get("pci"))
    except Exception:  # noqa: BLE001
        rsrp = None
        pci = None
    # 注册完成不等于数据通道就绪：实测 ping 已经通了、下载还拿不到字节，
    # 这时候测出来就是 0 Mbps，会被误判成「该频段没有数据能力」。
    # 给 3 秒缓冲，再开始正式测量。
    time.sleep(3)
    candidates = [speed_url] + [u for u in probe.SPEED_CANDIDATES if u != speed_url]
    for i in range(rounds):
        try:
            mbps, _used = probe.throughput_any(candidates, 4, 8)
        except Exception:  # noqa: BLE001
            mbps = 0.0
        p = probe.ping_stats(10, ping_host)
        samples.append((mbps, p["avg"], p["max"], p["jitter"], p["loss"]))
        if i < rounds - 1:
            time.sleep(1.5)

    # 全部轮次都测不出速度，但 ping 是通的 → 是测速源/出口的问题，不是频段差。
    # 这种情况必须单独标出来：拿 0 Mbps 去和别的频段比，会得出完全错误的结论。
    if samples and all(s[0] <= 0 for s in samples):
        return dict(empty, reason="speed_fail", waited=waited)

    mbps_list = [s[0] for s in samples]
    avg_list = [s[1] for s in samples if s[1] is not None]
    max_list = [s[2] for s in samples if s[2] is not None]
    jit_list = [s[3] for s in samples if s[3] is not None]
    loss_list = [s[4] for s in samples if s[4] is not None]
    return {
        "band": band, "ok": True,
        "mbps": round(statistics.median(mbps_list), 1),
        "mbps_all": mbps_list,
        "mbps_min": min(mbps_list), "mbps_max": max(mbps_list),
        "avg": int(statistics.median(avg_list)) if avg_list else None,
        "max": max(max_list) if max_list else None,
        "jitter": statistics.median(jit_list) if jit_list else None,
        "loss": max(loss_list) if loss_list else 0,
        "rounds": len(samples),
        "rsrp": rsrp,
        "pci": pci,
    }


# ---------------- 体检预估 → 一键应用 ----------------
#
# 这是主推的使用方式：
#   1) --preview   实测一遍，输出「现在多少 → 能到多少」的对比表，最后恢复原状
#   2) --apply-plan 直接按结论改一次，不再重测（只切一次频段，最快）
#
# 为什么预览阶段也要切频段：不实测就没法给出可信的提升数字。设备只能上报
# 当前服务小区的信号，看不到邻区，所以「其它频段能跑多快」只有切过去测才知道。
# 但预览结束时一定会切回原频段，中间断几次网，最终状态不变。

def _band_label(band, serving=None):
    """频段的人类可读标签。

    band=0 表示「没锁频段（自动）」，但用户真正关心的是**它此刻驻留在哪个频段**，
    所以带上 serving（来自设备 status1 的 lte_band）。不带上就会出现「频段 自动
    → 最优 BAND 40」这种看不出提升多少的句子。
    """
    if band:
        return f"BAND {band}"
    if serving:
        return f"自动（当前 BAND {serving}）"
    return "自动"


def describe_band_state(cur_band, serving):
    """状态行里怎么描述频段：锁定值和实际驻留分开说。

    为什么必须分开：实机截图出现过「当前状态：频段 BAND 39」，而整份结论
    都在说 BAND 3 —— 用户根本分不清「我锁的是哪个」和「我现在实际在哪个」。
    两者不一致本身就是重要信息：说明锁定没生效（设备在该频段信号太差时会
    自己回落），继续以为锁着 39 只会让人白等。

    返回 (描述文本, 是否要警告)。
    """
    if cur_band and serving and str(cur_band) != str(serving):
        return f"锁定 BAND {cur_band}，实际驻留 BAND {serving}", True
    if cur_band:
        return f"BAND {cur_band}", False
    if serving:
        return f"自动（实际驻留 BAND {serving}）", False
    return "未锁定", False


def _serving_band(st):
    """设备当前**实际驻留**的频段。

    get_state()["band"] 是「锁定值」，0 表示自动；一台没锁过频段的设备（出厂态、
    或刚 --restore 完）拿到的永远是 0。这时如果直接拿 0 去和实测结果比，就永远
    比不出「现在多少 → 能到多少」，用户跑了 5 分钟只得到一句「未测」。
    真实驻留频段在 status1 的 lte_band 里，这里兜住它。
    """
    if st.get("band"):
        return st["band"]
    sig = st.get("signal") or {}
    m = re.search(r"\d+", str(sig.get("band") or ""))
    band = int(m.group()) if m else 0
    return band or None


# 公开别名。面板和 APK 的「频段」格子都要这个值：锁定值 0（自动）时，
# 光看 band 就是个空的，用户会以为设备出问题了 —— 其实频段好好地在那儿。
serving_band = _serving_band


def quality_issue(res):
    """这个频段算不算「能用的」？返回问题描述（空串 = 干净可用）。

    为什么不能只看带宽：2026-10-29 手机实测里，BAND 39 丢包 90%、延迟 1068ms，
    却照样测出 13.4 Mbps。那天它带宽不最高所以没被选上；但只要它带宽高一点点，
    就会被推荐成「最优」—— 而它连网页都打不开。带宽好看 ≠ 能用。
    """
    if not res.get("ok"):
        return ""
    issues = []
    loss = res.get("loss") or 0
    if loss >= MAX_USABLE_LOSS:
        issues.append(f"丢包 {loss}%")
    ping = res.get("avg")
    if ping is not None and ping >= MAX_USABLE_PING_MS:
        issues.append(f"延迟 {ping}ms")
    return "、".join(issues)


def bands_worth_comparing(lte):
    """能参与「最优」评选的频段：测得出来 + 质量可接受。

    丢包 90% 的频段即使带宽数字漂亮也不能选，否则会把人推荐到打不开网页的网络里。
    但如果所有能用的都质量差，就原样返回 —— 不能因此得出「没有可用频段」的假结论，
    那种情况该由 assess_reliability 说「这次结论不可信」。
    """
    ok = [r for r in lte if r.get("ok")]
    clean = [r for r in ok if not quality_issue(r)]
    return clean or ok


def _sample_ratio(res):
    """多轮「最大/最小」= 波动倍数。样本不足 2 条时返回 None（无法判断稳不稳）。"""
    vs = [x for x in (res.get("mbps_all") or []) if x > 0]
    return (max(vs) / min(vs)) if len(vs) >= 2 else None


def _cons_mbps(res):
    """保守值：多轮取最小（最差那一轮），只有一轮就用它本身。

    判断「谁更强」不能拿峰值比 —— 峰值是运气，最差那一轮才是你真正会遇到的速度。
    """
    vs = [x for x in (res.get("mbps_all") or []) if x > 0]
    if len(vs) >= 2:
        return min(vs)
    return res.get("mbps")


def _gain_enough(cur_res, cand):
    """提升够不够大：绝对差 ≥3 Mbps，或者保底（最差一轮）快 1.5 倍以上。

    为什么不能只看绝对差：实测（2026-10-29 手机，信号很差那一轮）BAND 3 只有
    2.6 Mbps，BAND 8 是 5.1 —— 慢了近一倍，可绝对差只有 2.5 Mbps，卡在 3 Mbps
    阈值下面。于是「按速度」说「不到阈值，不值得切」，而「自动裁决」同一份数据
    说「强太多，切」—— 两个结论并排印出来，用户只会觉得这工具在胡说。
    现在三条线共用这一个判定。相对口径一律用最差一轮比，不拿峰值吹牛。
    """
    gain = (cand.get("mbps") or 0) - (cur_res.get("mbps") or 0)
    if gain >= MIN_BAND_GAIN_MBPS:
        return True, "abs"
    cur_cons, cand_cons = _cons_mbps(cur_res), _cons_mbps(cand)
    if cur_cons and cand_cons and cand_cons / cur_cons >= SPEED_DECISIVE_RATIO:
        return True, "ratio"
    return False, "none"


# assess_reliability 里「某个频段自己波动大」的那条，格式：
#   BAND 40 多轮结果波动过大（[3.7, 17.8]，最大是最小的 4.8 倍）
# 它只说明那个频段这次没测准，不该连坐到别的频段。
_RE_SHAKY_BAND = re.compile(r"^BAND (\d+) 多轮结果波动过大")


def split_caveats(caveats):
    """把「某个频段自己没测准」和「整份结论都不成立」分开。

    前者只是把那个频段踢出比较，后者才该闭嘴。2026-11-01 实测：
    BAND 40 在晚上本来就容易丢包（自己飘是真的），但不该连累 BAND 3 ——
    他的原话：「band40 数据不可信，为什么 band3 数据可信而且明显比 band8
    更好却也不让应用」。
    """
    shaky, hard = set(), []
    for c in caveats or ():
        m = _RE_SHAKY_BAND.match(str(c))
        if m:
            shaky.add(m.group(1))
        else:
            hard.append(str(c))
    return shaky, hard


def decide_band(cur_res, candidates, cmp_band, caveats=()):
    """自动裁决换哪个频段。返回 (option 或 None, 不换的原因)。

    规则（实测中 定的）：
      1) 一方速度强太多   → 选快的（哪怕它稍微不稳）
      2) 速度差不多       → 选更稳的
      3) 稳定性差不多     → 选更快的
    第 1 条刻意用「最差一轮」比较：拿峰值去说「强太多」是在骗人。

    2026-10-30 21:56 补两条前置闸门（那次真机把设备从 -81dBm 切到 -95dBm）：
      · 测速源抽风、当前频段测不出、最优频段本身质量不过关 → 数字不能当承诺。
      · 候选频段实测 RSRP 比当前差 ≥ BAND_RSRP_DROP → 从候选里剔除。
        速度是过去一分钟的三个样本，信号是结构性的；拿 14dB 换 1 Mbps 不划算。

    2026-11-01 修订：单个频段「多轮结果波动过大」只算它自己不参与比较，
    不再让整份结论作废 —— 详见下面 shaky 那段。
    """
    if not cur_res or not cur_res.get("ok"):
        return None, "当前频段没有可用数据"
    cands = [r for r in (candidates or [])
             if r and r.get("ok") and str(r.get("band")) != str(cmp_band)]
    if not cands:
        return None, "没有可比的其他频段"
    cur_rsrp = _num((cur_res or {}).get("rsrp"))
    cur_med = cur_res.get("mbps") or 0

    # 「某个频段自己波动大」只是那个频段的数据不可信，不该连坐。
    # 详见 split_caveats 的说明。
    shaky, hard = split_caveats(caveats)
    if shaky:
        cands = [r for r in cands if str(r.get("band")) not in shaky]
        if not cands:
            return None, "其他频段这次都没测准（波动过大），先不动"

    if hard:
        # 这些是「整份结论都不成立」的问题：当前频段根本测不出、被选为最优的
        # 那个本身质量也不过关。这种时候数字不能当承诺，但仍然照实交出来 ——
        # 用户要的是裁定 + 决定权，不是被人替他闭麦。
        faster = [r for r in cands
                  if (r.get("mbps") or 0) - cur_med >= MIN_BAND_GAIN_MBPS]
        if faster:
            w = max(faster, key=lambda r: r.get("mbps") or 0)
            gain = (w.get("mbps") or 0) - cur_med
            why = hard[0].split("（")[0]
            return {"band": w["band"], "kind": "speed", "small": True, "risky": True,
                    "reason": f"它快 {gain:.1f} Mbps，但这次测量{why} —— "
                              f"数字不一定准，试不试你自己定",
                    "gain": round(gain, 1)}, ""
        return None, f"测量数据本身不可信（{hard[0].split('（')[0]}），不拿它当切换依据"
    if cur_rsrp is not None:
        kept = [r for r in cands
                if not (r.get("rsrp") is not None
                        and r["rsrp"] <= cur_rsrp - BAND_RSRP_DROP)]
        if not kept:
            # 所有候选都被「信号差太多」这道闸门挡下了。
            #
            # 但挡下不等于「不该让用户知道」。2026-11-01 用户原话：
            # 「体检就是检测和裁定，切不切换在我，而不是他自己就说不建议就不切换」。
            # 这里不替他否掉，而是把两边的代价都摆出来 —— 速度快多少、信号弱多少，
            # 剩下的是他的判断。risky 标记让界面知道这条建议附带代价。
            faster = [r for r in cands
                      if (r.get("mbps") or 0) > cur_med and r.get("rsrp") is not None]
            if faster:
                w = max(faster, key=lambda r: r.get("mbps") or 0)
                gain = (w.get("mbps") or 0) - cur_med
                drop = cur_rsrp - w["rsrp"]
                return {"band": w["band"], "kind": "speed", "small": True, "risky": True,
                        "rsrp_drop": round(drop),
                        "reason": f"它快 {gain:.1f} Mbps，但信号弱 {drop:.0f}dB —— "
                                  f"可能不稳，试不试你自己定",
                        "gain": round(gain, 1)}, ""
            worst = min(r["rsrp"] for r in cands if r.get("rsrp") is not None)
            return None, (f"其他频段的信号都比当前差一截（最弱 {worst:.0f}dBm，"
                          f"当前 {cur_rsrp:.0f}dBm），而且没有一个更快，不值得断一次网")
        cands = kept

    cur_cons = _cons_mbps(cur_res)
    cur_ratio = _sample_ratio(cur_res)

    # 1) 速度强太多：比最差一轮
    strong = [r for r in cands
              if cur_cons and _cons_mbps(r) and _cons_mbps(r) / cur_cons >= SPEED_DECISIVE_RATIO]
    if strong:
        w = max(strong, key=lambda r: r.get("mbps") or 0)
        return {"band": w["band"], "kind": "speed",
                "reason": f"速度强太多 —— 它最差一轮 {_cons_mbps(w)} Mbps，"
                          f"现在最差一轮 {cur_cons} Mbps",
                "gain": round((w.get("mbps") or 0) - cur_med, 1)}, ""

    # 2) 速度差不多 → 选明显更稳的（与「按稳定」那条线共用同一份判定）
    st = stability_option(cur_res, cands, cmp_band)
    if st:
        return st, ""

    # 3) 稳定性差不多（双方都不乱跳）→ 选更快的，仍要过阈值
    w = max(cands, key=lambda r: r.get("mbps") or 0)
    w_ratio = _sample_ratio(w)
    both_steady = ((cur_ratio is None or cur_ratio <= STABILITY_BEST_RATIO)
                   and (w_ratio is None or w_ratio <= STABILITY_BEST_RATIO))
    gain = (w.get("mbps") or 0) - cur_med
    enough, _basis = _gain_enough(cur_res, w)
    if both_steady and enough:
        return {"band": w["band"], "kind": "speed",
                "reason": f"稳定性差不多（波动 {cur_ratio or 1:.1f} 倍 vs {w_ratio or 1:.1f} 倍），"
                          f"但它快 {gain:.1f} Mbps",
                "gain": round(gain, 1)}, ""

    # 候选里确实有更快的，只是没到「值得为它断一次网」的幅度。
    #
    # 引擎不再替用户按下这个决定，但也不该一声不吭 —— 2026-11-01 用户原话：
    # 「他可以给我建议，但是我也有权拒绝」「切不切换在我，而不是他自己就
    # 说不建议就不切换」。之前这里直接返回「都差不多」，app 上就只剩一句
    # 「当前设置已经合适，不用动」，想锁频段的人根本无处可点。
    #
    # 但它必须还是**稳定的**候选：一个 12~98 Mbps 乱跳的频段中位数再高也不能推，
    # 那不是「快一点」，那是随时会掉下来的坑。
    faster = [r for r in cands
              if (r.get("mbps") or 0) > cur_med
              and (_sample_ratio(r) is None
                   or _sample_ratio(r) <= STABILITY_BEST_RATIO)]
    if faster and cur_med > 0:
        w = max(faster, key=lambda r: r.get("mbps") or 0)
        gain = (w.get("mbps") or 0) - cur_med
        return {"band": w["band"], "kind": "speed", "small": True,
                "reason": f"它快 {gain:.1f} Mbps，差距不大 —— 想换就换，不换也合理",
                "gain": round(gain, 1)}, ""
    return None, "速度和稳定性都差不多，不值得为它断一次网"


def speed_option(cur_res, best_res, cmp_band, caveats):
    """「按速度换」的建议：只有数据可信、且提升够大，才给。

    这里和「自动裁决」共用 _gain_enough —— 两条线对「够不够大」必须是同一个
    标准，否则会出现「A 说不到阈值、自动却切了」的自相矛盾输出。
    """
    if caveats or not best_res or best_res.get("band") is None:
        return None
    if str(best_res["band"]) == str(cmp_band):
        return None
    if not cur_res or not cur_res.get("ok"):
        return None
    enough, basis = _gain_enough(cur_res, best_res)
    if not enough:
        return None
    gain = best_res["mbps"] - cur_res["mbps"]
    return {"band": best_res["band"], "gain": round(gain, 1), "basis": basis,
            "cons_now": _cons_mbps(cur_res), "cons_best": _cons_mbps(best_res),
            "gain_pct": (round(gain / cur_res["mbps"] * 100) if cur_res.get("mbps") else None)}


def stability_option(cur_res, candidates, cmp_band):
    """「按稳定换」的建议：速度差不多、但另一个频段明显更稳时给。

    为什么需要这条：实测（2026-10-29 本机）BAND 3 是 28.0~49.7（1.78 倍），
    BAND 40 是 33.7~41.1（1.22 倍），中位数只差 4%。按速度那条线会说「现在最快」、
    什么都不做；但对实际体验来说，一个会在 28~50 之间来回的链路，比稳在 37 的差得多。
    所以：只承诺稳定，不承诺更快，也不给提速百分比。

    candidates 传**所有**可用频段（不是只有最快的那个）：最快的不一定最稳。
    """
    if not cur_res or not cur_res.get("ok"):
        return None
    cur_ratio = _sample_ratio(cur_res)
    if cur_ratio is None:
        return None                     # 当前只测了一轮，不谈稳不稳
    cur_med = cur_res.get("mbps") or 0

    best = None
    for r in candidates or []:
        if not r or not r.get("ok") or str(r.get("band")) == str(cmp_band):
            continue
        if (r.get("mbps") or 0) < cur_med * SPEED_SIMILAR_RATIO:
            continue                    # 明显更慢，换过去是降速
        rr = _sample_ratio(r)
        if rr is None or rr > STABILITY_BEST_RATIO:
            continue                    # 样本不够 / 候选自己也乱跳
        if cur_ratio / rr < STABILITY_DECISIVE_RATIO:
            continue                    # 稳定性差得不够明显，不值得为它断网
        if best is None or (r.get("mbps") or 0) > (best.get("mbps") or 0):
            best = r
    if best is None:
        return None
    bs = [x for x in best["mbps_all"] if x > 0]
    cs = [x for x in cur_res["mbps_all"] if x > 0]
    return {"band": best["band"], "kind": "stability",
            "reason": f"速度差不多（{best.get('mbps')} vs {cur_med} Mbps），"
                      f"但它稳得多（{bs} vs 现在 {cs}）",
            "cur_range": cs, "best_range": bs,
            "cur_ratio": round(cur_ratio, 1),
            "best_ratio": round(max(bs) / min(bs), 1),
            "delta": round((best.get("mbps") or 0) - cur_med, 1)}


def describe_unmeasured(lte):
    """「有信号但这次没测出结果」的频段，用一句话说清楚。

    这类频段**不等于比当前慢**，只是没测成。不点出来，用户会以为所有频段
    都比过了，从而完全相信「当前已是最优」。实测（2026-10-29 手机端）：
    BAND 40 被 4.6 秒判成「数据不通」直接跳过，而它实际能跑 27~31 Mbps。
    """
    reason_cn = {"no_data": "数据不通", "speed_fail": "测速失败",
                 "timeout": "注册超时", "device_error": "通信中断"}
    skipped = [r for r in lte
               if not r.get("ok") and r.get("reason") not in (None, "no_signal")]
    if not skipped:
        return ""
    names = "、".join(f"BAND {r.get('band')}（{reason_cn.get(r.get('reason'), '不可用')}）"
                      for r in skipped)
    return f"另有 {names}没测出结果，所以「最优」只在这些能测出来的频段里成立。"


def assess_reliability(lte, cur_res, best_res):
    """这次测量的结论能不能信？返回不可信的原因列表（空 = 可信）。

    为什么要这一层：网络很差或测速源抽风时，单轮波动的幅度会盖过频段之间的
    真实差异。这时候给出「切过去能快 20Mbps」是假的，用户白断一次网还更差。
    宁可明确说「测不准」。
    """
    caveats = []
    if cur_res and not cur_res.get("ok") and cur_res.get("reason") == "speed_fail":
        caveats.append("当前频段测不出速度（测速源不可用，或该频段几乎没有数据能力）")
    if best_res and best_res.get("mbps") is not None and best_res["mbps"] < SPEED_FLOOR_MBPS:
        caveats.append(f"最优频段也只有 {best_res['mbps']} Mbps，低于 {SPEED_FLOOR_MBPS} Mbps 参考线")
    if best_res and quality_issue(best_res):
        caveats.append(f"被选为最优的 BAND {best_res['band']} 本身质量不过关"
                       f"（{quality_issue(best_res)}），说明当前网络整体状态不好")
    for r in lte:
        samples = r.get("mbps_all") or []
        if r.get("ok") and len(samples) >= 2 and min(samples) > 0 \
                and max(samples) / min(samples) >= 4:
            caveats.append(f"BAND {r['band']} 多轮结果波动过大"
                           f"（{samples}，最大是最小的 {max(samples) / min(samples):.1f} 倍）")
            break
    return caveats


# ---------------- 位置记忆（同一个地方，第二次起就别重头再测） ----------------

def _site_key(st):
    """这个位置怎么标识：PCI + 运营商。

    PCI 是**当前驻留的那个小区**，不是地点。同一个地方能同时看到好几个 PCI
    （每个频段一套编号，各 0~503），读到哪个取决于设备当下驻留哪条频段：
    实机（2026-10-30 住处）锁 BAND 3 读到 350、驻留 BAND 40 读到 236，同一张桌子。
    加运营商是为了压 PCI 复用（不同城市完全可能撞）。
    读不到就返回空串 —— 宁可这次不记忆，也不能把两地混成一个。
    """
    sig = (st or {}).get("signal") or {}
    pci = _pci_num(sig.get("pci"))
    if pci is None:
        return ""
    return f"{pci}-{(sig.get('operator') or '?').strip()}"


def _split_key(key):
    """把 `236-CMCC` 拆成 ("236", "CMCC")。拆不出来就返回 ("", "")。"""
    pci, _, op = str(key or "").partition("-")
    return pci.strip(), op.strip()


def _remember_cell(rec, key, band=None):
    """把「这处还见过哪个小区」记下来，供下次换个频段时认出是同一地点。

    为什么需要：切频段就会换服务小区，而工具偏偏一直在切。只按 PCI 记账的话，
    每切一次就把自己「搬了次家」，历史全废（实测 2026-10-30 住处的 350 / 236）。
    """
    pci, _ = _split_key(key)
    if not pci or not rec:
        return
    cells = rec.setdefault("cells", [])
    stamp = datetime.now().isoformat(timespec="seconds")
    for c in cells:
        if str(c.get("pci")) == pci:
            c["n"] = int(c.get("n") or 0) + 1
            c["last"] = stamp
            if band:
                c["band"] = int(band)
            return
    cells.append({"pci": pci, "band": int(band) if band else None, "n": 1,
                  "last": stamp})
    del cells[:-SITE_CELL_KEEP]


def load_sites():
    try:
        return json.loads(SITES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def set_data_dir(root):
    """把运行期文件换到 root 下（APK 里由 bridge.init 调用）。

    为什么要搬：老版本把 sites.json / config.json 写在代码解包目录里，
    那儿每次升级都会被清空。第一次切过来时如果新目录还是空的，就把老目录里
    那份复制过来 —— 用户的记忆不该因为一次搬迁丢掉。

    返回真正生效的目录。
    """
    global DATA_DIR, STATE, LOG, CONFIG, PLAN, CHANNEL_ADVICE, SITES, DECISIONS
    global OUTAGE
    root = Path(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return DATA_DIR                    # 建不出来就维持原样，别把功能弄挂
    old_root = DATA_DIR
    DATA_DIR = root
    STATE = root / "state.json"
    LOG = root / "autotune-log.jsonl"
    CONFIG = root / "config.json"
    PLAN = root / "plan.json"
    CHANNEL_ADVICE = root / "channel_advice.json"
    SITES = root / "sites.json"
    DECISIONS = root / "decisions.json"
    OUTAGE = root / "guard_outage.json"
    if old_root != root:
        for name in ("state.json", "autotune-log.jsonl", "config.json",
                     "plan.json", "channel_advice.json", "sites.json",
                     "decisions.json", "guard_outage.json"):
            src, dst = old_root / name, root / name
            try:
                if src.exists() and not dst.exists():
                    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            except (OSError, ValueError):
                pass
    return DATA_DIR


def save_sites(data):
    try:
        SITES.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


_DEC_LOCK = threading.RLock()       # 同一进程里只有一条守护线程，但界面线程也会读


def load_decisions():
    """读账本：{位置键: [账目, ...]}。读不到就是空账本。"""
    with _DEC_LOCK:
        try:
            data = json.loads(DECISIONS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


def save_decisions(data):
    with _DEC_LOCK:
        try:
            DECISIONS.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            return True
        except OSError:
            return False


def _parse_ts(s):
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def site_age_hours(rec, now=None):
    """这个位置上次来是几小时前。读不出时间就返回 None（当作「不知道」，按过期处理）。

    负值（系统时间回拨、记录来自未来的机器）按 0 算，不然会把新鲜记录误判成过期的。
    """
    dt = _parse_ts((rec or {}).get("last"))
    if dt is None:
        return None
    sec = ((now or datetime.now()) - dt).total_seconds()
    return max(0.0, sec / 3600.0)


def site_memory_state(rec, now=None):
    """记忆的可信度分档：none / fresh / aging / stale。"""
    if not rec:
        return "none"
    age = site_age_hours(rec, now)
    if age is None or age > SITE_STALE_DAYS * 24:
        return "stale"
    if age > SITE_FRESH_HOURS:
        return "aging"
    return "fresh"


def _fmt_age(hours):
    if hours is None:
        return "时间未知"
    if hours < 1:
        return "不到 1 小时前"
    if hours < 48:
        return f"{hours:.0f} 小时前"
    return f"{hours / 24:.0f} 天前"


def band_plan(rec, bands, serving=None, now=None):
    """按这个位置的历史，决定测哪些、跳过哪些。

    实测（2026-10-30）：在住处测了十几二十次，BAND 38/41 每次都没信号，
    却每次都要等它们各超时 15 秒，剩下的频段也是从头重测一遍。
    跳过「这里从来没信号」的频段，并把上次最好的排在最前面 ——
    万一中途被打断，先拿到的就是最可能用的那条。

    但记忆是按时间打折的（见 SITE_* 常量）：太久没来的位置一条都不跳，
    因为「上次很快」这件事过期得比「这里收不收得到信号」快得多。

    返回 (要测的频段, 无信号跳过的, 因为太慢跳过的, 历史最优)。
    为了防「环境变了还一直跳过」，每 DEAD_REPROBE_EVERY 次强制全测一遍。
    """
    if not rec or not bands:
        return list(bands), [], [], None
    state = site_memory_state(rec, now)
    if state == "stale":
        # 隔太久（或记录里没有可用时间）：覆盖和速度都可能变了，全部重测。
        return list(bands), [], [], None
    seen = int(rec.get("seen") or 0)
    hist = rec.get("bands") or {}
    dead = {b for b, v in hist.items()
            if (v.get("dead") or 0) >= DEAD_SKIP_MIN and (v.get("ok") or 0) == 0}
    if seen and seen % DEAD_REPROBE_EVERY == 0:
        dead = set()                      # 定期抽查，别把记忆当永久真理
    keep = [b for b in bands if str(b) not in dead]
    dead_list = [b for b in bands if str(b) in dead]

    best = rec.get("best")
    try:
        best = int(best) if best is not None else None
    except (TypeError, ValueError):
        best = None
    if best is not None and best not in keep:
        best = None

    # 快速模式：历史最优领先明显时，只测「历史最优 + 当前驻留」。
    # 这是真正省时间的那一步 —— 跳过死频段只省 30 秒，而有信号的频段每个要 54 秒。
    # 只在来过 >= FAST_MIN_SEEN 次后才敢这么做，且每 DEAD_REPROBE_EVERY 次全测一遍。
    # 另外只有 fresh 档才敢用：速度是最容易变的东西，隔了 3 天以上就不认了。
    slow = []
    if (best is not None and seen >= FAST_MIN_SEEN and state == "fresh"
            and seen % DEAD_REPROBE_EVERY != 0):
        def v(b):
            return float((hist.get(str(b)) or {}).get("best") or 0)

        rest = [b for b in keep if b != best and b != serving]
        second = max(rest, key=v) if rest else None
        if second is not None and v(best) >= v(second) * FAST_LEAD_RATIO:
            chosen = [best]
            if serving and serving != best:
                chosen.append(serving)
            elif second is not None:
                # 当前驻留就是最优时，也要带上第二名：只测一个没法交叉确认，
                # 单轮测速的波动足以把结论翻转（实测中 指出的偶发性）。
                chosen.append(second)
            slow = [b for b in keep if b not in chosen]
            keep = chosen

    if best is not None and best in keep:
        keep = [best] + [b for b in keep if b != best]
    return keep, dead_list, slow, best


def _hour_dist(a, b):
    """两个钟点差几个小时（跨午夜，23 点和 1 点差 2 而不是 22）。"""
    d = abs(int(a) - int(b)) % 24
    return min(d, 24 - d)


def band_flap(v, hour=None):
    """这个频段是不是「会周期性掉包」。返回 (是否掉包, 掉包占比, 样本数)。

    只看攒下来的历史样本：一次 54 秒的体检根本碰不到「每 3~5 分钟掉一次」
    那个窗口，所以这个判断只能靠跨次记录，靠不了单次测量。

    定罪要**至少 SITE_FLAP_MIN_BAD 条坏样本**，不能只看占比。只看占比时
    3 条样本里 1 条掉包就是 33%，刚好越过 30% 的线 —— 隔壁开一会儿微波炉、
    电梯里过一下，就能把一条好频段打成「会周期性掉包」。而洗掉这个污点很慢：
    守护链路正常时不写样本，只有体检每次给每个频段写 1 条，得攒够 8 次
    体检才把它挤出样本池。等于一次偶发 = 长期判死刑。

    **hour 给了就先按时段看**（实测中 的原话：「band40 在住处尤其是
    晚上丢包严重，但是在其他地方他的可用性还是很高的」）。只看当前钟点前后
    GUARD_FLAP_HOUR_WINDOW 小时的样本：

      · 那个时段样本够、而且坏得多 → 就是「这个点儿不行」，判掉包；
      · 那个时段干干净净 → 明确不判，白天照常可以用它；
      · 那个时段有坏样本但不够定罪 → 落回全天判定（宁可保守）。

    为什么要额外一层：全天判定把「晚上掉 100%」和「白天一次没掉」平均成一个
    百分比，结论只剩「这条偶尔掉」—— 于是白天的好时段被连坐，晚上的坏时段
    反而不够刺眼。时段判定让「几点该用哪条」真的能学出来。
    """
    rows = [s for s in ((v or {}).get("samples") or [])
            if isinstance(s, (list, tuple)) and len(s) >= 4]
    if len(rows) < 2:
        return False, 0.0, len(rows)

    if hour is not None:
        near = [s for s in rows
                if _hour_dist(_num(s[0]) or 0, hour) <= GUARD_FLAP_HOUR_WINDOW]
        if len(near) >= GUARD_FLAP_HOUR_MIN:
            nbad = sum(1 for s in near if (_num(s[2]) or 0) >= SITE_LOSS_BAD)
            nratio = nbad / float(len(near))
            if nbad and nratio >= GUARD_FLAP_HOUR_RATIO:
                return True, nratio, len(near)
            if nbad == 0:
                return False, 0.0, len(near)
            # 有几条坏的但不够定罪 —— 别放行，交给下面的全天判定

    bad = sum(1 for s in rows if (_num(s[2]) or 0) >= SITE_LOSS_BAD)
    ratio = bad / float(len(rows))
    if bad < SITE_FLAP_MIN_BAD:
        return False, ratio, len(rows)
    return ratio >= SITE_FLAP_RATIO, ratio, len(rows)


def site_band_flap(st, band, hour=None):
    """这个位置、这条频段在记忆里是不是「会周期性掉包」。

    信号强度和「会不会周期性掉包」是两件独立的事，而且后者只在历史样本里
    看得出来（见 band_flap 的注释：一次 54 秒的体检碰不到「每 3~5 分钟掉一次」
    那个窗口）。所以选带时必须单独查一次，不能拿 RSRP 代替。

    hour 默认取当前钟点 —— 判定要按「现在这个点儿」看，而不是把一整天平均。
    """
    if not band:
        return False
    key = _site_key(st)
    if not key:
        return False
    try:
        data = load_sites()
        rec = data.get(find_site(data, key)) or {}
    except (OSError, ValueError):
        return False
    v = (rec.get("bands") or {}).get(str(int(band)))
    if not isinstance(v, dict):
        return False
    try:
        h = datetime.now().hour if hour is None else int(hour)
    except (TypeError, ValueError):
        h = None
    return band_flap(v, hour=h)[0]


def band_regret(v):
    """这条频段在本地「锁上去反而更差」的记录。返回 (坏次数, 好次数)。

    和 band_flap 是两件事，别混：
      flap   说的是「这条链路本身会周期性掉包」—— 信号可能很漂亮；
      regret 说的是「锁到这条上，比锁之前还差」—— 链路本身没准是好的，
             只是当时那条（或者这个时辰的那条）比它更合适。
    两者都指向「别锁它」，但原因不同。混成一句会让人以为是信号问题，
    实际是选错了。
    """
    rv = (v or {}).get("review")
    if not isinstance(rv, dict):
        return 0, 0
    return int(rv.get("bad") or 0), int(rv.get("good") or 0)


def site_band_regret(st, band):
    """这个位置、这条频段是不是「锁过几次都没好处」。"""
    if not band:
        return False
    key = _site_key(st)
    if not key:
        return False
    try:
        data = load_sites()
        rec = data.get(find_site(data, key)) or {}
    except (OSError, ValueError):
        return False
    v = (rec.get("bands") or {}).get(str(int(band)))
    if not isinstance(v, dict):
        return False
    bad, good = band_regret(v)
    return bad >= REVIEW_BAD_MIN and good == 0


def prefer_stable_over_flap(usable, hist, fastest):
    """最快的那个如果历史上老是掉包，而另一个只慢一点点，就选稳的那个。

    返回 (最终选中的频段, 一句解释或 None)。判据写死成「备选不慢过 15%」：
    差太多就别换，掉包固然烦，但慢一半更难受，这种事让用户自己决定。
    """
    if fastest is None or not hist:
        return fastest, None
    flap, ratio, n = band_flap(hist.get(str(fastest)))
    if not flap:
        return fastest, None
    fast_mbps = next((r["mbps"] for r in usable if r["band"] == fastest), 0) or 0
    alt = None
    for r in usable:
        if r["band"] == fastest:
            continue
        if band_flap(hist.get(str(r["band"])))[0]:
            continue
        if fast_mbps and r["mbps"] >= fast_mbps * FLAP_SWAP_RATIO:
            alt = r
            break
    pct = int(round(ratio * 100))
    if alt:
        slower = int(round((1 - alt["mbps"] / fast_mbps) * 100)) if fast_mbps else 0
        return alt["band"], (f"BAND {fastest} 过去 {n} 次里有 {pct}% 丢包 ≥{SITE_LOSS_BAD}%，"
                             f"会时不时掉一下；BAND {alt['band']} 只慢 {slower}%，这次选它")
    return fastest, (f"BAND {fastest} 过去 {n} 次里有 {pct}% 丢包 ≥{SITE_LOSS_BAD}%，"
                     f"可能隔几分钟掉一次；这次没有差不多快的备选，切过去要有心理准备")


def _site_measured(rec):
    """这条记录里有没有「真测出来的数据」，而不只是「先锁过一下」。

    provisional 是「第一次来，先锁当前免得乱跳」的标记。它原来只在
    record_site 里该被清掉（现在会清了），但老数据会留下「标记为真、
    bands 里其实已经有实测数字」的状态 —— 这种记录若再被判成「没测过」，
    守护就永远走「快测当前、直接锁」，记忆永远上不了台面；更糟的是它
    也永远不会触发体检去修正自己，死循环。所以判据看数据本身。
    """
    for v in (rec.get("bands") or {}).values():
        try:
            if int((v or {}).get("ok") or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def find_site(data, key, now=None):
    """认出「刚切过频段、PCI 变了但人没挪窝」还是同一个地方。

    设备锁到哪个频段，服务小区就跟着换 —— 实测同一张桌子（2026-10-30）：
    锁 BAND 40 是 PCI 236，切到 BAND 3 变成 350。按 PCI 一条一条记的话，
    工具每切一次频段就把自己「搬了家」，记忆永远攒不起来，而它偏偏一直在切。

    三层判据，从硬到软：
      1. 当前 PCI 出现在某条记录见过的小区里（cells）—— 这处换过这个小区，
         就是同一地点。跨天也认，这是唯一能把 350 和 236 绑在一起的东西。
      2. 记录本身就叫这个 key —— 老朋友。
      3. 同运营商、且上一条记录是 SITE_MERGE_MIN 分钟内留下的 —— 刚切完频段
         就接着测。人真走到别处，通常不会二十分钟内又测一遍。
    收尾一条：自己只是个 provisional（从没实测过）时，如果同运营商有一条 24
    小时内真测过的记录，就并到那条上 —— provisional 里没有可损失的信息，并过去
    只会让这个位置更完整（实机 2026-10-30：236 那条空记录把 350 的 46.8 挡在外面）。
    """
    if not key:
        return key
    pci, op = _split_key(key)
    if not pci or not op:
        return key
    own = data.get(key) or {}
    # 「可以丢掉」的只是空记录。已经真测过的记录不能当成空壳并掉。
    prov = bool(own.get("provisional")) and not _site_measured(own)
    best = None
    for k, v in (data or {}).items():
        if _split_key(k)[1] != op:
            continue
        named = bool(str(v.get("name") or "").strip())
        age = site_age_hours(v, now)
        if age is None and named:
            # 命名过但还没测过（`--here` 之后没跑过体检）：用「来过」的时间算年龄。
            # 否则一条记着名字的记录永远匹配不上，名字等于白起 —— 而名字恰恰是
            # 唯一不依赖「见过几次切换」的锚点。
            age = site_age_hours({"last": v.get("observed")}, now)
        if age is None:
            # 匿名观测记录（守护路过时建的）不参与认位置：它可能是任何一张桌子，
            # 让它当锚点就会把两处混成一个。它靠 note_cell_change 桥接。
            continue
        if age > SITE_TTL_DAYS * 24 and not named:
            # 隔太久作废；命名过的地方例外 —— 记忆该不该信由 site_memory_state
            # 判（stale → 全测一遍），不认地方却会让名字彻底失联。
            continue
        seen = int(v.get("seen") or 0)
        cells = {str(c.get("pci")) for c in (v.get("cells") or [])}
        if k == key:
            bonus = 1
        elif pci in cells:
            bonus = 2
        elif age <= SITE_MERGE_MIN / 60.0:
            bonus = 0
        elif prov and not v.get("provisional") and age <= SITE_FRESH_HOURS:
            bonus = 0
        else:
            continue
        # 先比「来过几次」：用得多的那条更可能是这个位置的本体；
        # 再比判据强度，最后比新鲜度。
        score = (seen, bonus, -(age or 0))
        if best is None or score > best[0]:
            best = (score, k)
    return best[1] if best else key


def _absorb_site(data, real, key):
    """把 key 那条记录并进 real，然后删掉它 —— 一个地方只留一条。

    只搬「见过哪些小区」和来过次数，不搬速度历史：两条记录的测量条件
    （不同频段、不同时段）不一样，混在一起会造出假的「历史最优」。
    """
    if not real or real == key:
        return
    old = data.pop(key, None)
    if old is None:
        return
    op = _split_key(real)[1]
    rec = data.get(real)
    if rec is None:
        old.setdefault("cells", [])
        _remember_cell(old, key, old.get("best"))
        data[real] = old
        return
    _remember_cell(rec, key, old.get("best"))
    for c in old.get("cells") or []:
        if c.get("pci"):
            _remember_cell(rec, f"{c['pci']}-{op}", c.get("band"))
    rec["seen"] = int(rec.get("seen") or 0) + int(old.get("seen") or 0)
    # 被并掉的这条如果只是「守护路过时看到的」（没有 last、也没实测过），
    # 它手里的信号和丢包是花时间采来的，别跟着记录一起消失 —— 守护在陌生
    # PCI 下攒的那点观测，正是「两个模式共用一套记忆」里守卫那一半。
    # 只补空缺：速度 / ok / best 一律不动，那是测量的账，混进来会造假最优。
    if not old.get("last") and not _site_measured(old):
        for b, ov in (old.get("bands") or {}).items():
            if not isinstance(ov, dict):
                continue
            tv = rec.setdefault("bands", {}).setdefault(b, {})
            if not isinstance(tv, dict):
                continue
            if not tv:
                tv.update(ov)
                continue
            if tv.get("rsrp") is None and ov.get("rsrp") is not None:
                tv["rsrp"], tv["rsrp_med"] = ov.get("rsrp"), ov.get("rsrp_med")
            if ov.get("samples") and not tv.get("samples"):
                tv["samples"] = ov["samples"]


def note_cell_change(old_key, new_key):
    """设备换了个小区、但人没动 —— 把两个 PCI 记到同一处，别让它变成「新地方」。

    这是位置记忆的桥。守护每 30 秒读一次 PCI，能看到「解锁后设备自己换到
    另一个频段」这种切换（实机 17:52：BAND 41 / PCI 477 → BAND 40 / PCI 236）。
    靠它建立联系，比靠 20 分钟并档窗口可靠得多 —— 窗口一过，同一张桌子就分家了。
    """
    if not old_key or not new_key or old_key == new_key:
        return
    if _split_key(old_key)[1] != _split_key(new_key)[1]:
        return                                  # 换了运营商，是另一张卡的事
    data = load_sites()
    real = find_site(data, old_key)
    rec = data.get(real)
    if not rec:
        return                                  # 这处还没建过记录，等体检/快测去建
    # 锚点必须选「真测过」的那一条。守护的顺序躲不开：先写信号、后判「设备
    # 换小区了」，所以切频段那一拍 old_key 常常正好是一条刚建的空壳观测记录；
    # 拿它当本体，等于把位置记忆挂到空壳上，有数据的那条永远读不到。
    if not rec.get("last") and not _site_measured(rec):
        nv = data.get(new_key) or {}
        if nv.get("last") or _site_measured(nv):
            real, rec = new_key, nv
    _remember_cell(rec, old_key, None)
    _remember_cell(rec, new_key, None)
    # 两边的纯观测记录（有 observed、没 last、没实测）都在这一刻并进来。
    # 不并的话，它要等到下一次写记忆（最长 5 分钟）才被吸收 —— 这段时间里
    # 守护读到的记忆还是「没有信号」，该 blind 还是 blind。
    for k in (old_key, new_key):
        if k == real:
            continue
        ov = data.get(k) or {}
        if ov and not ov.get("last") and not _site_measured(ov):
            _absorb_site(data, real, k)
    save_sites(data)


MEMORY_KIND = "mifi-memory"


def export_memory(origin=None):
    """把本机的位置记忆导出成一份可以搬走的 JSON。

    只搬「真测过」和「起过名」的：provisional 和守护路过时写的纯观测记录是
    半成品（连 `last` 都没有），搬到别的机器上只会变成一条来历不明的假记忆。

    这份文件里有真实的 PCI、实测速度和到访时间 —— 它属于**本机数据**，
    和 sites.json 一个级别，永远不该进仓库、不该上传。
    """
    data = load_sites()
    sites = {}
    for k, v in (data or {}).items():
        if not isinstance(v, dict):
            continue
        if not _site_measured(v) and not str(v.get("name") or "").strip():
            continue
        sites[k] = v
    return {
        "kind": MEMORY_KIND,
        "version": __version__,
        "exported": datetime.now().isoformat(timespec="seconds"),
        "origin": (origin or "").strip() or "另一台机器",
        "sites": sites,
    }


def _merge_band(dst, src):
    """把一个频段的实测结果并进另一个。返回合并后的那个 dict。

    原则和 _absorb_site 一样但放宽一档：导入的数据是**真的测出来的**（不是观测），
    所以速度可以取更快、ok 可以累加；但 `last`/`seen`/`best` 这些「本机的账」
    不在这里动，由调用方决定。
    """
    if not dst:
        return dict(src or {})
    if not src:
        return dst
    try:
        dst["ok"] = int(dst.get("ok") or 0) + int(src.get("ok") or 0)
    except (TypeError, ValueError):
        pass
    if not dst.get("best"):
        # 本机这条根本没测出速度（上次是死的），导入的更快就用它。
        dst["best"] = src.get("best")
    # dead 取小：另一台测过是活的，就说明这条频段在这个位置能用，
    # 「死」很可能只是那一次方位不好，不该被另一台机器的结论锁死。
    try:
        dst["dead"] = min(int(dst.get("dead") or 0), int(src.get("dead") or 0))
    except (TypeError, ValueError):
        pass
    for k in ("rsrp", "rsrp_med", "rsrq", "sinr"):
        if dst.get(k) is None and src.get(k) is not None:
            dst[k] = src[k]
    if src.get("samples"):
        merged = list(dst.get("samples") or []) + list(src["samples"])
        dst["samples"] = merged[-SITE_SAMPLES:]
    return dst


def import_memory(payload, data=None):
    """把别处导出的记忆并进本机。返回一份说明用的统计。

    合并的方向是「只补空缺、不夺权」：
      * 本机没有这个位置 → 整条搬进来，但不改 `last`（保留原始时间，
        所以它会按真实年龄被判 aging/stale，不会假装刚测过）；
      * 本机已有 → 只补本机缺的频段数据、本机没有的速度、本机没见过的 PCI，
        绝不覆盖本机的 `last`/`seen`/`best`/**名字**。
    这样「拿电脑的历史喂手机」不会把手机上真机测出来的结论挤掉。
    """
    if not isinstance(payload, dict) or payload.get("kind") != MEMORY_KIND:
        raise ValueError("这不是 mifi 导出的记忆文件（缺少 kind 标记）")
    src_sites = payload.get("sites")
    if src_sites is None:
        src_sites = {}
    if not isinstance(src_sites, dict):
        raise ValueError("记忆文件里的 sites 不是对象")
    data = load_sites() if data is None else data
    origin = str(payload.get("origin") or "另一台机器")
    stamp = datetime.now().isoformat(timespec="seconds")
    stat = {"added": 0, "merged": 0, "skipped": 0, "bands": 0}

    for key, rec in src_sites.items():
        if not isinstance(rec, dict):
            stat["skipped"] += 1
            continue
        _, op = _split_key(key)
        if not op:
            stat["skipped"] += 1
            continue
        target = key
        if key not in data:
            # 这处本机没记过，但可能已经有另一条记录见过同一个 PCI ——
            # 那就并到那条上，别在同一个地方留两条。
            pci = _split_key(key)[0]
            for kk, vv in data.items():
                if _split_key(kk)[1] != op:
                    continue
                if not isinstance(vv, dict):
                    continue
                if pci in {str(c.get("pci")) for c in (vv.get("cells") or [])}:
                    target = kk
                    break

        dst = data.get(target)
        if dst is None:
            new = json.loads(json.dumps(rec))       # 深拷贝，别和 payload 共享引用
            new.setdefault("cells", [])
            _remember_cell(new, key, new.get("best"))
            new["imported_at"] = stamp
            new["imported_from"] = origin
            data[target] = new
            stat["added"] += 1
            stat["bands"] += len(new.get("bands") or {})
            continue

        # —— 已有记录：只补空缺 ——
        bands = dst.setdefault("bands", {})
        for b, bv in (rec.get("bands") or {}).items():
            if not isinstance(bv, dict):
                continue
            before = json.dumps(bands.get(b), sort_keys=True)
            bands[b] = _merge_band(bands.get(b), bv)
            if json.dumps(bands.get(b), sort_keys=True) != before:
                stat["bands"] += 1
        for c in rec.get("cells") or []:
            if c.get("pci"):
                _remember_cell(dst, f"{c['pci']}-{op}", c.get("band"))
        if not str(dst.get("name") or "").strip() and str(rec.get("name") or "").strip():
            dst["name"] = rec["name"]
        if not dst.get("best") and rec.get("best"):
            dst["best"] = rec["best"]
        dst["imported_at"] = stamp
        dst["imported_from"] = origin
        stat["merged"] += 1

    save_sites(data)
    return stat


def record_site(key, results, best_band=None, reset_best=False):
    """把这次的结果记到位置上。只记「有没有信号」和「最好跑到多少」。

    reset_best：这次是全量重测（隔太久没来），历史速度数字已经不作数，
    直接用本次的成绩覆盖，而不是和旧值取最大。
    """
    if not key:
        return
    data = load_sites()
    real = find_site(data, key)
    # 认出是同一处、但历史存在另一个 PCI 名下 → 先并成一条，再往下写。
    _absorb_site(data, real, key)
    rec = data.setdefault(real, {"seen": 0, "bands": {}})
    # 记下这处还见过哪些小区：下次换个频段（PCI 跟着变）才认得出是同一张桌子。
    _remember_cell(rec, key, best_band)
    if real != key:
        # 老字段留着，人看 sites.json 时能明白「为什么只有一条记录」
        al = rec.setdefault("aliases", [])
        if key not in al:
            al.append(key)
            del al[:-8]
    now = datetime.now()
    rec["seen"] = int(rec.get("seen") or 0) + 1
    rec["last"] = now.isoformat(timespec="seconds")
    # 体检是唯一「人在原地、把每条频段都锁一遍」的时刻，也就是唯一能一次性
    # 采齐「这张桌子有几个服务小区」的机会：锁 BAND 3 是 PCI 350、BAND 40 是
    # 236、BAND 41 又是 477……它们是同一个地方，只是设备驻留的频段不同。
    # 以前只记了开始那一轮的 PCI，于是位置记忆只能靠「20 分钟并档窗口」或
    # 「亲眼看到一次切换」把两张脸凑到一起，窗口一过就分家（实测上锁 3
    # 之后守护认不出住处，就是这一条）。逐条记下来，同一个地方从此一次认全。
    op = _split_key(key)[1]
    for r in results:
        band_of = r.get("band")
        band_pci = _pci_num(r.get("pci"))
        if band_pci is not None and op:
            _remember_cell(rec, f"{band_pci}-{op}", band_of)
    for r in results:
        b = str(r.get("band"))
        v = rec["bands"].setdefault(b, {"dead": 0, "ok": 0})
        if r.get("ok"):
            v["ok"] = int(v.get("ok") or 0) + 1
            v["dead"] = 0                      # 有信号就清零，别让旧记录一直压着
            now_mbps = float(r.get("mbps") or 0)
            v["best"] = now_mbps if reset_best else max(
                float(v.get("best") or 0), now_mbps)
            v["last_ok"] = now.isoformat(timespec="seconds")
            # 记下这条频段当时的信号强度。没有它，历史就只剩「上次多快」，
            # 分不清是「上次条件好」还是「这次条件差」—— 实机 2026-10-30 就是
            # 这么锁到 -105dBm 的 BAND 3 上，撑了 8 分钟然后掉线。
            rsrp = _num(r.get("rsrp"))
            # 滚动样本：[小时, 速度, 丢包%, 延迟ms, RSRP]。
            # 只留最近几次，文件不会长大；老数据是 4 位，读的时候兼容。
            samples = v.setdefault("samples", [])
            samples.append([now.hour, round(now_mbps, 1),
                            int(_num(r.get("loss")) or 0),
                            int(_num(r.get("avg")) or 0),
                            int(rsrp) if rsrp is not None else None])
            del samples[:-SITE_SAMPLES]
            if rsrp is not None:
                v["rsrp"] = int(rsrp)
                seen_rsrp = [s[4] for s in samples
                             if len(s) > 4 and s[4] is not None]
                if seen_rsrp:
                    v["rsrp_med"] = int(statistics.median(seen_rsrp))
        elif r.get("reason") == "no_signal":
            v["dead"] = int(v.get("dead") or 0) + 1
    # 真测过，就把「只是先锁住」的标记摘掉。这个标记本来是给「第一次来、
    # 先锁当前免得乱跳」用的，可它原来只写不清 —— 于是这个位置永远被判成
    # 「没测过」，守护每次来都走「快测当前、直接锁」，记忆里那条最优永远
    # 上不了台面。实机 2026-10-30 19:44：236-CMCC 在 18:14 已实测过四条
    # 频段，仍被当成新地方，绕过了一整轮信号比较。
    if any(r.get("ok") for r in results):
        rec.pop("provisional", None)
    if best_band:
        rec["best"] = int(best_band)
    save_sites(data)


def prune_sites(days=SITE_TTL_DAYS):
    """清掉太久没去的记录：搬家、换城市之后旧记忆只会误导。

    但**命名过的地方不删**：名字是你亲手给的（`--here 住处`），搬家也是「这处
    以后用不上了」而不是「这个名字过期了」。留着它的代价只是 site_memory_state
    判 stale、下次来全测一遍，跟删掉一样安全。
    """
    data = load_sites()
    cutoff = datetime.now() - timedelta(days=days)
    keep = {}
    for k, v in data.items():
        if str(v.get("name") or "").strip():
            keep[k] = v
            continue
        try:
            if datetime.fromisoformat(v.get("last", "")) >= cutoff:
                keep[k] = v
        except (TypeError, ValueError):
            continue
    if len(keep) != len(data):
        save_sites(keep)
    return data, keep


def resolve_bands(caps, args, cfg):
    """决定要实测哪些频段。

    默认走「设备自己声明的全部频段」——先问设备支持什么，再逐个测，
    而不是拿一份写死的清单去猜。config 里的 candidate_bands 只在
    --bands preferred 时作为缩小范围用。
    """
    supported = list(caps.get("bands") or [])
    if args.bands in ("default", "all"):
        return supported or list(cfg.get("candidate_bands") or [])
    if args.bands == "preferred":
        pref = [b for b in (cfg.get("candidate_bands") or []) if b in supported]
        return pref or supported
    return [int(x) for x in args.bands.split(",") if x.strip()]


def measure_all_bands(dev, bands, rounds, cur_band, cur_label,
                      speed_url=None, ping_host=None, contest_band=None):
    """逐个实测频段，并**保证**无论怎么退出都把设备切回 cur_band。

    为什么抽成一个函数：--preview 和 --apply（--watch 触发的就是它）都要这份
    「测量 + 还原」逻辑，以前各写一遍，结果 --apply 那份漏了 try/finally ——
    中途崩溃就把设备留在没人覆盖的频段上（真实事故：停在 BAND 8、丢包 60%）。
    只有一份实现，才不会再次漂移。

    contest_band 给定时，粗测完再对它和最优各补 1 轮（「决赛」），
    因为两者的差值正是决策依据，值得多花 90 秒把单轮波动压掉。
    """
    results = []
    t0 = time.time()
    try:
        for idx, b in enumerate(bands, 1):
            print(f"\n      [{idx}/{len(bands)}] BAND {b} 测试中…")
            r = measure_band(dev, b, rounds=rounds, speed_url=speed_url, ping_host=ping_host)
            results.append(r)
            if r["ok"]:
                jit = f"{r['jitter']}ms" if r["jitter"] is not None else "—"
                print(f"      {_band_label(b):<8} {r['mbps']:>5} Mbps  "
                      f"延迟 {r['avg']}ms  抖动 {jit}  丢包 {r['loss']}%")
                bad = quality_issue(r)
                if bad:
                    print(f"       [!] {bad} —— 质量不可接受，不会被选为最优"
                          f"（决赛时只当对照，用来算「提升多少」）")
            else:
                why = {"no_signal": "无信号（当地没这个频段）",
                       "timeout": "注册超时",
                       "no_data": "有信号但数据不通",
                       "speed_fail": "有信号但测不出速度（测速源不可用，或该频段几无数据能力）",
                       "device_error": "设备通信中断"}.get(r.get("reason"), "不可用")
                print(f"      {_band_label(b):<8} {why}  已跳过（{r.get('waited', '?')}s）")
                if r.get("error"):
                    print(f"      原因：{r['error']}")
            left = len(bands) - idx

            # 这个频段测失败（没覆盖 / 读不到状态）时，先退回原频段再测下一个。
            # 2026-10-29 实测：切到 BAND 38（当地无覆盖）后设备仍在线、仍发认证挑战，
            # 但响应体是空的 —— 状态读不出来，于是后面每个频段都被判「通信中断」，
            # 连自动还原都失败，用户被留在没信号的频段上。
            # 从健康状态出发测下一个频段，就不会被上一个频段的坏状态连累。
            if not r["ok"] and str(b) != str(cur_band) and left:
                print("      退回原频段，避免在坏状态上继续切…")
                ok_r, _ = restore_band(dev, cur_band, tries=2, poll=45)
                if not ok_r:
                    print("      [!] 退不回去，剩下的频段先不测了")
                    break
            if left:
                used = (time.time() - t0) / idx
                print(f"      （还剩 {left} 个，预计 {used * left / 60:.1f} 分钟）")

        usable = bands_worth_comparing(results)
        if contest_band and usable and len(bands) > 2:
            # 决赛前先把设备从「上一个频段」拉回健康状态。
            # 实测（连续两轮，2026-10-29）：BAND 41（当地无覆盖）刚跳过，紧接着决赛
            # BAND 3，两轮复测都只有 8.7 / 9.9 Mbps，而第一轮是 40.1 / 46.6 Mbps ——
            # 同一个频段、同样 20 秒，差 4~5 倍。原因不是频段本身，是设备刚从死频段
            # 切回来、数据通路还没恢复；measure_band 内部 3 秒预热不够。
            # 决赛是决策依据，必须在设备健康时测，否则整轮结论会被判「波动过大」而作废。
            print("\n[决赛] 先把设备切回原频段并等它恢复…")
            ok_r, _ = restore_band(dev, cur_band)
            if ok_r:
                # 光「指令发出去了」不算恢复。实机（2026-10-30 11:37）踩到：
                # 这里只睡 5 秒就复测，结果「BAND 3 复测没测成（无信号）」——
                # 设备刚从死频段（38/41）回来，5 秒根本不够重新注册。
                status, waited = wait_for_service(dev, cur_band, timeout=45)
                if status == "ok":
                    print(f"      已恢复（{waited}s），开始复测")
                else:
                    print(f"      [!] 等了 {waited}s 还是没注册上（{status}），"
                          f"复测多半测不出来")
            else:
                print("      [!] 没切回去，复测结果可能偏低")

            usable.sort(key=lambda r: (-r["mbps"], r["avg"] or 999))
            # 候选池摊开打印。2026-10-30 实测出现「只复测了当前频段、
            # 29.4 Mbps 的那条反而没复测」，而列表只有一行「补 1 轮确认」，
            # 从输出里根本看不出它凭什么入选 —— 以后遇到直接看这一行。
            print("[决赛] 候选池（按速度）：" + "、".join(
                f"BAND {r['band']} {r['mbps']}Mbps" for r in usable))
            # 候选和「当前」要分开说。之前把两者塞进一个列表打印「各补 1 轮确认」，
            # 而当前频段刚被上面判过「质量不可接受、不参与最优评选」——
            # 用户当场就问：说好不参评，怎么又进决赛了？（2026-10-29 实机截图）
            # 真相是：它确实还会被复测一次，但它不参评，只是给「提升多少」当基准。
            fastest = usable[0]
            second = usable[1] if len(usable) > 1 else None
            pairs = [(fastest["band"], "候选")]
            # 第二名咬得紧时也要复测：这种差距很可能只是单轮噪声，不复测就等于
            # 拿运气当结论。实测（2026-10-30）：BAND 40 31.5 vs BAND 3 29.0，
            # 只差 8%，却因为「最快的正好是当前驻留的」只复测了一个。
            if second and fastest["mbps"] and \
                    second["mbps"] >= fastest["mbps"] * (1 - CONTEST_CLOSE_RATIO):
                pairs.append((second["band"], "候选·和第一名只差 "
                              f"{(1 - second['mbps'] / fastest['mbps']) * 100:.0f}%，要确认"))
            if contest_band is not None and \
                    str(contest_band) not in {str(b) for b, _ in pairs}:
                pairs.append((contest_band, "当前·只作对照，不参评"))
            print(f"\n[决赛] 补 1 轮确认：" +
                  "、".join(f"BAND {b}（{role}）" for b, role in pairs))
            if len(pairs) == 1:
                gap_txt = (f"比第二名快 "
                           f"{(1 - second['mbps'] / fastest['mbps']) * 100:.0f}%"
                           if second and fastest["mbps"] else "没有别的可用频段")
                print(f"      只有 {_band_label(fastest['band'])} 候选"
                      f"（{gap_txt}，领先足够明显），复测它自己确认不是一次性波动")
            by_band = {r["band"]: r for r in results}
            for b, role in pairs:
                prev = by_band.get(b)
                if not prev or not prev["ok"]:
                    continue
                extra = measure_band(dev, b, rounds=1, speed_url=speed_url, ping_host=ping_host)
                if not extra.get("ok"):
                    # 复测失败必须说出来。原来这里没有 else，名单里列了它、
                    # 后面一行结果都没有 —— 用户会以为「测了但没变化」，实际是没测成。
                    why = {"no_data": "数据不通", "speed_fail": "测不出速度",
                           "timeout": "注册超时", "no_signal": "无信号",
                           "device_error": "设备通信中断"}.get(extra.get("reason"), "不可用")
                    print(f"      {_band_label(b):<8} 复测没测成（{why}），"
                          f"仍用粗测的 {prev['mbps']} Mbps")
                    continue
                merged = sorted(prev["mbps_all"] + extra["mbps_all"])
                prev["mbps"] = round(statistics.median(merged), 1)
                prev["mbps_all"] = merged
                prev["mbps_min"] = min(merged)
                prev["mbps_max"] = max(merged)
                prev["rounds"] = len(merged)
                print(f"      {_band_label(b):<8} 复测后 {prev['mbps']} Mbps"
                      f"（{merged}）")
    except KeyboardInterrupt:
        print("\n[!] 已中断，马上切回原频段…")
    finally:
        # 无论正常结束、抛异常还是 Ctrl+C，都必须还原。
        # 这条是血的教训：之前在测 BAND 8 时驱动超时崩溃，整个进程退出，
        # 设备被留在 BAND 8（丢包 60%），用户当场上不了网。
        print(f"\n[还原] 切回 {cur_label} …")
        ok, _st = restore_band(dev, cur_band)
        if ok:
            print("      已切回")
        else:
            print(f"      [!] 自动还原失败。请手动在设备后台把频段改回 {cur_label}")
            print("          设备可能刚重启射频，等 1 分钟再试 `mifi --restore` 也行。")
    return results


def run_preview(dev, caps, st0, args, cfg, speed_url, ping_host):
    cur_band, cur_ch = st0["band"], st0["channel"]
    serving = _serving_band(st0)
    # 用于「现在多少」的对比基准：锁了频段就是锁定值；没锁（0=自动）就用设备
    # 实际驻留的频段，否则整轮测完也只能说「未测」，用户拿不到提升数字。
    cmp_band = cur_band or serving
    cur_label = _band_label(cur_band, serving)
    rounds = args.rounds or 1          # 体检默认 1 轮粗筛，精度靠后面的「决赛」补
    plan = {"ts": datetime.now().isoformat(),
            "device": getattr(dev, "description", dev.name),
            "current": {"band": cur_band, "channel": cur_ch, "serving_band": serving}}

    # ---- 一、WiFi 信道 ----
    wifi = None
    if not args.lte_only:
        own = args.ssid or cfg.get("own_ssid") or scanner.connected_ssid() or st0.get("ssid")
        print(f"\n[1/2] 扫描周边 2.4G 信道（{args.scan_rounds} 轮合并）…")
        aps = scanner.scan_2g(rounds=args.scan_rounds, own_ssid=own)
        if not aps:
            # 一次空结果不等于「扫不了」：netsh/nmcli 的结果是缓存的，刚开机、
            # 网卡刚切过信道、或系统正在后台扫描时都可能一轮全空（本机实测遇到过）。
            # 直接跳过整个信道部分，用户看到的就是「这次没有信道结果」——
            # 所以先重试，还不行就把原始诊断摊开，而不是猜一句「语言不支持」。
            print("      [!] 这一轮一个都没扫到，等 3 秒重试一次…")
            time.sleep(3)
            aps = scanner.scan_2g(rounds=max(3, args.scan_rounds - 2), own_ssid=own)
        if aps:
            rows = scanner.score_channels(aps, caps.get("channels") or range(1, 14))
            ch_auto = not cur_ch            # 0/None = 设备处于「自动信道」
            cur_score = (None if ch_auto
                         else next((r["score"] for r in rows if r["ch"] == cur_ch), None))
            best = scanner.pick_best_channel(rows)
            wifi = {"current": cur_ch, "auto": ch_auto, "cur_score": cur_score,
                    "best": best["ch"], "best_score": best["score"],
                    "neighbors": len(aps)}
            print(f"      {len(aps)} 个邻居 AP")
            if ch_auto:
                print("      当前信道：自动（设备自己选，无法比较「现在有多差」）")
                print(f"      扫描最干净的是 {best['ch']} 号（干扰分 {best['score']}），仅供参考")
            else:
                print(f"      当前信道 {cur_ch} 干扰分 {cur_score} ｜ "
                      f"最优 {best['ch']} 干扰分 {best['score']}")
        else:
            print(f"      [!] 重试也扫不到，本次跳过信道部分：{scanner.scan_failure_hint()}")
            print("          原始信息（排查用，复制这一段就能定位问题）：")
            print(scanner.scan_diagnose())

    # ---- 二、LTE 频段 ----
    lte = []
    best_band = None
    state = "none"                      # 记忆年龄档位；没有记忆/没测频段时都是 none
    flap_note = None                    # 「这个频段历史上会周期性掉包」的说明
    demoted = None                      # 因为掉包被让位的那条（后面要从候选里排除）
    if not args.wifi_only and caps.get("can_lock_band"):
        bands = resolve_bands(caps, args, cfg)
        if not bands:
            print("\n[2/2] 驱动没有声明可用频段，跳过频段实测。")
        else:
            # 这个位置以前测过就复用记忆：跳过从来没收到的频段、把上次最好的排前面。
            # 实测（2026-10-30 住处）：测了十几二十次，38/41 每次都没覆盖，
            # 却每次都白等 15 秒 × 2，剩下的也是从头重排。
            site = _site_key(st0)
            cur_site = site
            if site:
                # 太久没来的位置连记录一起清掉，免得拿着「搬家前」的历史当参考。
                prune_sites()
            data = load_sites()
            # 切过频段之后 PCI 会变（同一张桌子 236 ↔ 350），那不算搬家。
            site = find_site(data, site) if site else ""
            rec = data.get(site) if site else None
            if getattr(args, "fresh", False):
                rec = None
            state = site_memory_state(rec)
            skipped = []
            if rec:
                bands, dead_list, slow_list, known_best = band_plan(
                    rec, bands, serving=cmp_band)
                seen = int(rec.get("seen") or 0)
                hist = rec.get("bands") or {}

                def _hist_v(b):
                    return (hist.get(str(b)) or {}).get("best") or 0

                pci_now = cur_site.split("-")[0]
                if rec.get("provisional") and not seen:
                    # 只有 mark_site_provisional 动过、没真测过：说「测过 0 次，
                    # 上次来不到 1 小时前」自相矛盾（实机 2026-10-30 就是这么印的）。
                    print(f"\n[2/2] 这个位置（PCI {pci_now}）还没正式测过，"
                          f"上次只是先锁住、没量速度")
                else:
                    print(f"\n[2/2] 这个位置（PCI {pci_now}）之前测过 {seen} 次，"
                          f"上次来是 {_fmt_age(site_age_hours(rec))}")
                others = [str(c.get("pci")) for c in (rec.get("cells") or [])
                          if str(c.get("pci")) != pci_now]
                others += [a.split("-")[0] for a in (rec.get("aliases") or [])
                           if a != cur_site and a.split("-")[0] not in others]
                nm = str(rec.get("name") or "").strip()
                if nm:
                    print(f"      这里你命名过：{nm}")
                if others:
                    print(f"      同一处还见过 {'、'.join('PCI ' + p for p in others[:6])}"
                          f"（切频段会换小区，算同一个地方）")
                if state == "stale":
                    print(f"      隔了 {SITE_STALE_DAYS} 天以上没来，历史作废："
                          f"这次全部 {len(bands)} 个频段都重测（覆盖会变，速度更会变）")
                elif state == "aging":
                    print(f"      隔了 {SITE_FRESH_HOURS // 24} 天以上没来，"
                          f"上次的速度数字不再作数：能收到的频段全部重新测速")
                if dead_list:
                    print(f"      跳过 {dead_list}：这里从来没信号")
                if slow_list:
                    desc = " / ".join(f"BAND {b} {_hist_v(b)}" for b in slow_list)
                    print(f"      跳过 {slow_list}：历史只有 {desc} Mbps，"
                          f"比 BAND {known_best}（{_hist_v(known_best)}）慢太多")
                if known_best and state != "stale":
                    print(f"      上次最好：BAND {known_best}，这次先测它")
                tail = ("（一条都不跳）" if state == "stale"
                        else f"（每 {DEAD_REPROBE_EVERY} 次会全测一遍兜底）")
                print(f"      本次测 {len(bands)} 个：{bands}{tail}")
            else:
                why = ("按 --fresh 忽略位置记忆"
                       if getattr(args, "fresh", False)
                       else "这个地方没来过（或记忆已过期被清掉）")
                print(f"\n[2/2] {why}，设备声明的 {len(bands)} 个频段全部实测：{bands}")
            per = SETTLE_SEC + rounds * 20 + 8
            print(f"      逐个实测（每段 {rounds} 轮，有信号的约 {per} 秒，"
                  f"没信号的会提前跳过；预计 {per * len(bands) / 60:.0f} 分钟以内）")
            print("      期间会短暂断网，全部测完自动切回原频段…")

        lte = measure_all_bands(dev, bands, rounds, cur_band, cur_label,
                                speed_url, ping_host, contest_band=cmp_band)

        usable = bands_worth_comparing(lte)
        flap_note = None
        if usable:
            usable.sort(key=lambda r: (-r["mbps"], r["avg"] or 999))
            fastest = usable[0]["band"]
            # 历史样本说「这个快的会周期性掉包」，而旁边有个只慢一点点的，
            # 就换稳的那个。单次 54 秒测不出掉包周期，只能靠攒下来的记录。
            best_band, flap_note = prefer_stable_over_flap(
                usable, (rec or {}).get("bands"), fastest)
            if best_band != fastest:
                demoted = fastest          # 后面选建议时把它排除掉，否则又会被选回来
        if args.bands in ("default", "all") and not args.wifi_only:
            # 记忆过期那次是全测，速度数字就是最新的：把「历史最好」换成这次的，
            # 否则 8 天前那个偏高的旧数字会一直压着，快速模式又会被它骗一次。
            record_site(_site_key(st0), lte, best_band, reset_best=(state == "stale"))

    # ---- 三、结论 ----
    cur_res = next((r for r in lte if r["band"] == cmp_band), None)
    best_res = next((r for r in lte if r["band"] == best_band), None)
    cur_mbps = cur_res["mbps"] if cur_res and cur_res["ok"] else None
    best_mbps = best_res["mbps"] if best_res else None
    band_gain = (best_mbps - cur_mbps) if (cur_mbps is not None and best_mbps is not None) else None

    # 可信度检查：网络很差、或测速源抽风时，单轮波动会盖过频段之间的真实差异。
    # 这种时候给出「建议切换」是害人 —— 宁可说「测不准」，也不要让人白折腾一次断网。
    caveats = assess_reliability(lte, cur_res, best_res)
    reliable = not caveats

    # 两条独立的建议线，用户自己选走哪条：
    #   speed     —— 只认「更快」，数据不可信时不出建议（保守）
    #   stability —— 只认「更稳」，当前频段自己在乱跳、另有一个稳定且不慢的频段时给建议
    # 两者通常互斥（乱跳本身就会让速度结论不可信），所以不是二选一之后再二选一。
    # 三条建议：自动裁决（默认）+ 两条手动强制的单线
    cands = bands_worth_comparing(lte)
    if demoted is not None:
        # 会周期性掉包的那条已经从「最优」里让位了，这里也要把它从候选里拿掉，
        # 否则自动裁决按速度又把它选回来，等于白判。
        cands = [r for r in cands if r["band"] != demoted]
    auto, auto_why = decide_band(cur_res, cands, cmp_band, caveats=caveats)

    # 两条手动线跟自动裁决共用同一个「谁不可信」的口径：某个频段自己飘，
    # 只是它自己不参加比较，别把整个结论拖下水（否则一屏之内又会自相矛盾：
    # 自动说「可以换」，按速度说「不敢给建议」）。
    shaky, hard = split_caveats(caveats)
    trusted = [r for r in cands if str(r.get("band")) not in shaky] if shaky else cands
    best_trusted = max(trusted, key=lambda r: r.get("mbps") or 0) if trusted else None
    options = {
        "auto": auto,
        "speed": speed_option(cur_res, best_trusted, cmp_band, hard),
        "stability": stability_option(cur_res, cands, cmp_band),
    }
    strategy = getattr(args, "strategy", None) or "auto"
    picked = options.get(strategy)
    # 「按稳定换」和「自动裁决定的稳定」都不承诺带宽，界面不能把它当提速卖点
    promise_gain = bool(picked and reliable and picked.get("kind") == "speed")

    do_band = bool(picked)
    do_ch = bool(wifi and caps.get("can_set_channel", True) and wifi["best"] != cur_ch
                 and wifi["cur_score"] is not None
                 and wifi["cur_score"] - wifi["best_score"] >= MIN_CHANNEL_GAIN)

    target = {"band": picked["band"] if picked else cur_band,
              "channel": wifi["best"] if do_ch else cur_ch}
    # 信号诊断：既写进 plan.json（面板要显示），也决定「不改动」时该怎么解释。
    diag = diagnose_signal(st0.get("signal") or {})
    plan.update({
        "target": target,
        "strategy": strategy,
        "options": options,
        "auto_reason": (picked.get("reason") if picked else auto_why),
        "reliable": reliable,
        "caveats": caveats,
        "flap": flap_note,
        "lte": lte,
        "wifi": wifi,
        "signal": diag,
        "expected": {
            "band_from": cmp_band, "band_locked": cur_band, "band_to": target["band"],
            "mbps_now": cur_mbps, "mbps_best": best_mbps,
            # 只在「确定更快」时才给提速数字；换稳定性、或数据不可信时一律不给，
            # 免得界面把「更稳」说成「更快」。
            "mbps_gain": (round(band_gain, 1)
                          if (promise_gain and band_gain is not None) else None),
            "mbps_gain_pct": (round(band_gain / cur_mbps * 100)
                              if (promise_gain and cur_mbps) else None),
            "ping_now": cur_res["avg"] if cur_res and cur_res["ok"] else None,
            "ping_best": best_res["avg"] if best_res else None,
            "channel_from": cur_ch, "channel_to": target["channel"],
            "channel_score_from": wifi["cur_score"] if wifi else None,
            "channel_score_to": wifi["best_score"] if wifi and do_ch else None,
        },
        "changes": ([] if not do_band else
                    [f"频段 {cur_label} → {_band_label(picked['band'])}"
                     + ("（换稳定性）" if picked.get("kind") == "stability" else "")
                     # 带代价的建议必须把代价写在建议本身里，不能只在日志深处提一句：
                     # app 的结论卡片上只显示这一行，用户要能一眼看到「这不是白赚的」。
                     + (f"（信号弱 {picked['rsrp_drop']}dB）"
                        if picked.get("risky") and picked.get("rsrp_drop") else "")
                     # 另一种「不是白赚」：整份数据本身不稳，只能当参考。
                     + ("（这次测得飘，仅供参考）"
                        if picked.get("risky") and not picked.get("rsrp_drop") else "")])
                   + ([f"信道 {cur_ch} → {target['channel']}"] if do_ch else []),
    })
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 56)
    print("体检结论")
    print("=" * 56)
    # 先给信号诊断：用户看到「不改动」时，需要知道是「环境真没救」还是「工具没测准」。
    if diag:
        print(f"  信号：{diag['text']}")
        print(f"        {diag['advice']}")
    if cur_mbps is not None or best_mbps is not None:
        if cur_mbps is not None:
            now_s = f"{cur_mbps} Mbps"
        elif cur_res is None:
            now_s = "未测（不在本次范围）"
        else:
            now_s = "测不出（该频段无服务）"
        best_s = f"{best_mbps} Mbps" if best_mbps is not None else "—"
        print(f"  频段：{cur_label} {now_s}"
              f"  →  最优 {_band_label(best_band) if best_band else '—'} {best_s}")
        if flap_note:
            print(f"  [!] 掉包记录：{flap_note}")

    # 自动裁决放第一行（默认就按它走），两条单线列在旁边当参考/强制用。
    # 只显示其中一条会让人以为工具「没建议」。
    # 但没测频段时（--wifi-only）这三行必须省掉：那时它会印出
    # 「按速度：现在这个频段就是最快的」这种根本没测过的话（实测踩到）。
    if not lte:
        print("  （这次没测频段，所以没有频段建议 —— 只跑了信道）")
    else:
        if options["auto"]:
            print(f"  [自动] 切到 BAND {options['auto']['band']} —— {options['auto']['reason']}")
        else:
            print(f"  [自动] 保持现状 —— {auto_why}")
        print("        规则：速度强太多选快的；速度差不多选稳的；稳定性差不多选快的")
        print(f"  [A] 按速度：{_describe_speed(options['speed'], caveats, best_band, cmp_band, band_gain)}")
        print(f"  [B] 按稳定：{_describe_stability(options['stability'], cur_res)}")
        print(f"  当前策略：{'自动' if strategy == 'auto' else ('按速度' if strategy == 'speed' else '按稳定')}"
              f"（可用 --strategy speed / stability 强制单条）")

    # 有些频段是「有信号但这次没测出结果」，它不等于「比当前慢」。
    # 不点出来，用户会以为已经比过了所有频段 —— 实际是没比成。
    note = describe_unmeasured(lte)
    if note:
        print(f"  {note}")
        print("        想更确定可以过一会儿再跑一次。")

    if wifi:
        if wifi.get("auto"):
            print(f"  信道：自动（设备自行选择，当前无固定值）"
                  f"  →  扫描最干净 {wifi['best']}（干扰分 {wifi['best_score']}）")
            print("        自动模式通常比自己钉一个信道更稳，本次不建议改动。")
        else:
            gap = (round(wifi["cur_score"] - wifi["best_score"], 2)
                   if wifi["cur_score"] is not None else None)
            pct = (round(gap / wifi["cur_score"] * 100)
                   if gap is not None and wifi["cur_score"] else None)
            # gap == 0 时「最优」其实是并列 —— 直接印「最优 10」会让人以为 10 比 13 好，
            # 实际两个信道都是 0 干扰。实测（2026-10-29 电脑）就这么印过。
            tail = ("（干扰分与当前相同，属并列）" if gap == 0
                    else f"（干扰分 {wifi['best_score']}）")
            print(f"  信道：{cur_ch}（干扰分 {wifi['cur_score']}）"
                  f"  →  最优 {wifi['best']}{tail}")
            if do_ch:
                print(f"        切换后干扰下降 {gap}"
                      f"{f'（约 {pct}%）' if pct is not None else ''}，"
                      f"主要改善重传和抖动（不承诺带宽提升）")
            elif wifi["best"] == cur_ch or gap == 0:
                print("        当前信道已经很干净，没有更优的选择，不动")
            else:
                # 原来只写「改善不足阈值 0.3」，看不出差多少 —— 用户以为没算。
                # 但 cur_score 可能是 0（这个信道本来就没人抢），这时算不出百分比，
                # 印「约 None%」比不说更糟（实测出现过）。
                pct_s = f"（约 {pct}%）" if pct is not None else ""
                print(f"        只降 {gap}{pct_s}，不到阈值 {MIN_CHANNEL_GAIN}，不值得动")
    elif not args.lte_only:
        # 扫描失败时整段会消失，用户看到的就是「这次没有信道结果」。
        # 必须在这里明写一句，否则看起来像功能坏了。
        print("  信道：这次没扫到周边 AP，未参与判断（扫描本身的原因见上面 [!] 行）")

    print("-" * 56)
    if hard:
        print("[!] 按速度这条线，数据不足以下结论：")
        for c in hard:
            print(f"    · {c}")
        print("    原因通常是当前信号太差（看 RSRP/SINR）或测速源不可用。")
        print("    建议：换个测速源（--speed-url）、加轮次（--rounds 3）或等信号好转后再测一次。")
        print("-" * 56)
    if shaky:
        # 「某个频段自己飘」是局部信息，不是全盘作废 —— 单独说，别混进上面那段。
        print("[!] 这几个频段这次自己测得不稳，不参与比较：")
        for b in sorted(shaky):
            print(f"    · BAND {b}")
        print("    「自己飘」本身就是信息：这个时段它本来就不耐用，换走可能更好。")
        print("-" * 56)
    if plan["changes"]:
        label = {"auto": "自动裁决", "speed": "速度", "stability": "稳定"}.get(strategy, strategy)
        print(f"按「{label}」可以做的改动：")
        for c in plan["changes"]:
            print(f"  · {c}")
        if not getattr(args, "interactive", False):
            # interactive 模式紧接着就会问「要不要应用」，这里再说一遍是废话。
            print(f"\n应用方式：python autotune.py --apply-plan"
                  + ("" if strategy == "auto" else f" --strategy {strategy}"))
            print("           （只想看会改什么、不真改：再加 --dry-run）")
    else:
        if not lte and wifi:
            # 只测信道时不能印「当前频段没有可用数据」—— 那是没测，不是没有。
            print("本次不改动：只测了信道，信道也没有更优的选择。")
        else:
            print(_no_change_reason(strategy, options, auto_why))
        print("（结论已存 plan.json；环境变了可以重新体检一次）")
    return 0


def _describe_speed(opt, caveats, best_band, cmp_band, gain):
    if opt:
        # 信号差的时候绝对提升很小、但相对翻倍，「+2.5 Mbps」会让人以为没啥用，
        # 所以这种情形改说「保底从多少到多少」，数字对得上用户的实际体感。
        if opt.get("basis") == "ratio" and opt.get("cons_now") and opt.get("cons_best"):
            up = opt["cons_best"] / opt["cons_now"] - 1
            return (f"BAND {opt['band']}，保底从 {opt['cons_now']:g} 提到 "
                    f"{opt['cons_best']:g} Mbps（{up:+.0%}）")
        return (f"BAND {opt['band']}，预计 {opt['gain']:+.1f} Mbps"
                + (f"（{opt['gain_pct']:+d}%）" if opt.get("gain_pct") is not None else ""))
    if caveats:
        return "数据波动/不可信，这条线不敢给建议"
    if best_band is None or str(best_band) == str(cmp_band):
        return "现在这个频段就是最快的"
    if gain is not None:
        return (f"最快也只高 {gain:.1f} Mbps，且最差一轮没到 {SPEED_DECISIVE_RATIO:g} 倍"
                f"（两个门槛：快 {MIN_BAND_GAIN_MBPS} Mbps 或 保底翻 "
                f"{SPEED_DECISIVE_RATIO:g} 倍，都不满足）")
    return "没有可比的数据"


def _describe_stability(opt, cur_res):
    if opt:
        return (f"BAND {opt['band']} —— 现在 {opt['cur_range'][0]}~{opt['cur_range'][1]}"
                f"（{opt['cur_ratio']} 倍），它 {opt['best_range'][0]}~{opt['best_range'][1]}"
                f"（{opt['best_ratio']} 倍）")
    if not cur_res or not cur_res.get("ok"):
        return "当前频段没有可用数据，比不出稳不稳"
    cur_ratio = _sample_ratio(cur_res)
    if cur_ratio is None:
        return "当前频段只测了一轮，判断不了稳不稳（换 --rounds 3 再测）"
    if cur_ratio <= STABILITY_BEST_RATIO:
        return f"当前频段本来就稳（波动 {cur_ratio:.1f} 倍），不需要为了稳去换"
    return "没找到「既稳又不会更慢」的替代频段"
    return "有频段在乱跳，但没找到既稳又不比它慢的替代"


def _no_change_reason(strategy, options, auto_why):
    """没建议时把「为什么不改」说清楚，并指出别的策略是否还有路可走。"""
    if strategy == "auto":
        head = f"本次不改动：{auto_why}。"
        extra = [k for k in ("speed", "stability") if options.get(k)]
        if extra:
            tip = "、".join("按速度" if k == "speed" else "按稳定" for k in extra)
            return head + f"\n但{tip}这条线还有建议，想强制的话加 --strategy {extra[0]}。"
        return head + "\n保持现状就好。"
    head = f"按「{'速度' if strategy == 'speed' else '稳定'}」本次不改动。"
    other = "stability" if strategy == "speed" else "speed"
    if options.get(other):
        return head + f"\n但另一条线有建议，加 --strategy {other}（或用默认的自动裁决）。"
    return head + "\n另一条线同样没有更好的选择，保持现状就好。"


def _mark_applied(plan, band, channel, result):
    """把「已应用」写回 plan.json，而不是把它删掉。

    删掉文件会让界面上的结论卡片凭空消失，用户会疑惑"我刚才那份结论去哪了"。
    保留并标注应用时间，既能回顾，也能让 UI 明确显示"已应用"。
    """
    plan["applied_at"] = datetime.now().isoformat()
    plan["applied_result"] = result
    plan["after"] = {"band": band, "channel": channel}
    try:
        PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def run_mtu_check(st0, ping_host):
    """只读检测：这条线路的真实路径 MTU 和设备的设置值对不对得上。

    为什么值得单列一项：MTU 设得比路径 MTU 大时，症状是「网页能开、下载大
    文件卡死、视频一直缓冲」。这种「半通不通」最容易被当成信号差，然后开始
    反复切频段 —— 而切频段根本治不了它。

    只报告不自动改：改 MTU 要重新拨号，断网 30~60 秒，而且属于网络层设置，
    改错了连设备后台都可能进不去，不适合跟着体检自动做。
    """
    set_mtu = int(st0.get("mtu") or 0)
    print("=" * 56)
    print("路径 MTU 检测（不分片 ping，只读，不改设备）")
    print("=" * 56)
    print(f"设备当前设置：{set_mtu or '—'} 字节")

    if probe.is_termux():
        print("\n[跳过] 手机端没有 ICMP 权限，这项只能在电脑上测。")
        print("       （和频段优化无关，手机上体检不受影响）")
        return 0

    print(f"正在二分探测到 {ping_host} 能整包通过的最大尺寸（约 10~20 秒）…")
    path_mtu, note = probe.detect_path_mtu(ping_host)

    if path_mtu is None:
        print(f"\n[?] 测不出来：{note}")
        return 0

    print(f"路径 MTU：{note}")

    if not set_mtu:
        print("\n设备没报 MTU 值，无从比较。")
        return 0
    if path_mtu >= set_mtu:
        print(f"\n[√] 设置值 {set_mtu} 不超过路径 MTU {path_mtu}，大包不会被丢，不用动。")
        return 0

    print(f"\n[!] 设置值 {set_mtu} 大于路径 MTU {path_mtu}。")
    print("    症状会是：网页能开、大文件下载卡住、视频一直缓冲。")
    print("    这种「半通不通」最容易被误判成信号差，然后反复切频段（治不了）。")
    print(f"    建议把 MTU 调到 {path_mtu}（要更保险就用 {max(576, path_mtu - 8)}）。")
    print("\n    这一步我没有替你改：改 MTU 要重新拨号，会断网 30~60 秒，")
    print("    而且改错了连设备后台都可能进不去。真要改说一声，我把写入和回滚一起做。")
    return 0


def _cli():
    """提示里该用哪个命令前缀。

    手机（Termux）装的是 `mifi` 包装脚本，电脑上是 `python autotune.py`。
    原来一律写 `python autotune.py --restore`，手机用户得自己翻译一遍。
    """
    return "mifi" if probe.is_termux() else "python autotune.py"


def _tcp_up(host, port=80, timeout=1.0):
    """端口通不通。只用来判断「设备回来了没有」，不读任何数据。"""
    import socket
    try:
        with socket.create_connection((host, port), timeout=min(timeout, 1.0)):
            return True
    except OSError:
        return False


def wait_device_back(dev, timeout=45, poll=5):
    """等设备的 web 后台重新可达。

    两种情况后台都会掉 5~20 秒：换频段（重启射频）、改信道（重启 AP）。
    手机上更慢一层 —— 手机自己就是通过这台设备的 WiFi 连着后台的，
    改信道等于把手机的网先掐了，还得等系统自动重连。
    所以「发完指令立刻读状态」必然失败，那不是出错，是还没连上。
    轮询比盲睡可靠：设备 6 秒回来就不必等 26 秒。

    先用 TCP 连接探活，再读状态：一次 get_state 要发 3 个请求、每个都要
    重新认证，在这个 20~40 秒的重连窗口里按 1.5 秒轮询，光等待就能打出
    上百个请求，还要和手机自身的重连抢带宽。端口探活便宜得多，也能探得更勤。
    """
    t0 = time.time()
    host = getattr(dev, "host", None)
    if host:
        while time.time() - t0 < timeout:
            if _tcp_up(host):
                break
            time.sleep(min(poll, 0.75))
    # 设备刚重启过射频，手里那个 Digest nonce 必然已经作废。先丢掉，让第一次读
    # 直接从干净的挑战开始；留着它只会先吃一次「还没就绪」再重发。
    drop = getattr(dev, "drop_session", None)
    if drop:
        drop()
    while time.time() - t0 < timeout:
        try:
            dev.get_state()
            return True
        except Exception:  # noqa: BLE001
            time.sleep(poll)
    return False


def observe_after_apply(ping_host, seconds=OBSERVE_SEC, every=15, per=5):
    """切完之后盯一段时间，抓「刚切能通、过一会儿就断」的情况。

    实测（2026-10-30 12:34）：切到 BAND 40 后 3~5 分钟手机弹「没网」，
    几秒后自己恢复。而体检时它 20 秒跑出 29.4 Mbps（当时的 BAND 3 只有 12.0），
    完全看不出这种模式；切完只 ping 6 个包（几秒）同样看不出来。

    「峰值多高」和「能不能久待」是两件事。体检回答前者，这里回答后者。

    seconds=0 表示跳过（--observe 0）。返回 {"rounds","bad","avg"}，跳过时 None。
    """
    if not seconds or seconds <= 0:
        return None
    print(f"\n[观察] 盯 {seconds} 秒，确认切过去之后稳不稳"
          f"（这几分钟里断一下是正常的，不用管）…")
    t0 = time.time()
    lat, bad, rounds = [], 0, 0
    while time.time() - t0 < seconds:
        s = probe.ping_stats(per, ping_host)
        rounds += 1
        if s["avg"] is None:
            bad += 1
            state = "完全不通"
        elif s["loss"] and s["loss"] >= 50:
            bad += 1
            state = f"丢包 {s['loss']}%"
        else:
            lat.append(s["avg"])
            state = f"延迟 {s['avg']}ms  抖动 {s['jitter']}ms  丢包 {s['loss']}%"
        print(f"      {time.strftime('%H:%M:%S')}  {state}")
        time.sleep(max(2, every - per * 1.2))
    if not rounds:
        return None
    if bad >= 2:
        print(f"      [!] {rounds} 次里 {bad} 次不通 —— 这个配置不适合久待")
    else:
        med = round(statistics.median(lat), 1) if lat else "—"
        print(f"      [√] {rounds} 次都通（延迟中位 {med}ms）")
    return {"rounds": rounds, "bad": bad,
            "avg": round(statistics.median(lat), 1) if lat else None}


def run_apply_plan(dev, caps, st0, args, cfg, ping_host):
    if not PLAN.exists():
        print("[×] 还没有体检结论。先跑一次：python autotune.py --preview")
        return 1
    try:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        print("[×] plan.json 损坏，请重新体检")
        return 1

    cur_band, cur_ch = st0["band"], st0["channel"]
    target = plan.get("target") or {}
    t_band, t_ch = target.get("band", cur_band), target.get("channel", cur_ch)

    # 策略：命令行显式指定 > 体检时用的 > 默认按速度
    strategy = getattr(args, "strategy", None) or plan.get("strategy") or "speed"
    options = plan.get("options") or {}
    if getattr(args, "strategy", None) and options:
        # 用户换了策略：改用那条线的目标。那条线没建议就如实说，不能悄悄套用另一条。
        opt = options.get(strategy)
        if not opt:
            name = "速度" if strategy == "speed" else "稳定"
            print(f"[×] 这份结论里「按{name}」没有可用的建议。")
            other = "stability" if strategy == "speed" else "speed"
            if options.get(other):
                print(f"    另一条线有建议：加 --strategy {other}")
            else:
                print("    两条线都没有更好的选择，保持现状即可。")
            return 0
        t_band = opt.get("band", t_band)

    todo = []
    # 顺序很重要：先动信道，最后动频段。
    # 换频段会让设备重启射频，web 后台跟着掉线 5~20 秒；原来把频段排在前面，
    # 紧接着再写信道，正好撞进重启窗口 —— 实机表现是
    # 「[应用] 切换 WiFi 信道到 1 … → [×] 应用过程中设备无响应」，
    # 结果频段锁好了、信道没改，用户被留在半改状态。
    if t_ch != cur_ch and caps.get("can_set_channel", True):
        todo.append(("channel", t_ch))
    if t_band != cur_band and caps.get("can_lock_band"):
        todo.append(("band", t_band))

    if not todo:
        print("[√] 当前配置已经和体检结论一致，无需改动。")
        _mark_applied(plan, cur_band, cur_ch, "nothing_to_do")
        return 0

    strat_label = {"auto": "自动裁决", "speed": "速度", "stability": "稳定"}.get(
        strategy, strategy)
    print(f"按体检结论应用（策略：{strat_label}）：")
    for kind, val in todo:
        if kind == "band":
            print(f"  · 频段 {_band_label(cur_band)} → {_band_label(val)}")
        else:
            print(f"  · 信道 {cur_ch} → {val}")

    # 体检和应用之间设备被改过（自己动的手、或设备自动换信道）：目标值是按体检时
    # 算的，仍然成立；但数字对不上会让人以为工具记错了，说一句。
    plan_ch = (plan.get("current") or {}).get("channel")
    if plan_ch and cur_ch and plan_ch != cur_ch:
        print(f"  （体检时信道是 {plan_ch}，现在是 {cur_ch} —— 期间被改过，以现在为准）")

    if getattr(args, "dry_run", False):
        print("\n[演练] 以上是「将会做」的改动；加了 --dry-run 所以没有碰设备。")
        if any(k == "band" for k, _ in todo):
            print("       去掉 --dry-run 就会真的切过去（约 60~90 秒，会断两次网）。")
        else:
            print("       去掉 --dry-run 就会真的切过去（约 20~40 秒；"
                  "设备重启 WiFi，本机断 5~20 秒后自动重连）。")
        return 0

    save_state({"saved_at": datetime.now().isoformat(),
                "band": cur_band, "channel": cur_ch})

    failed = False
    try:
        for kind, val in todo:
            if kind == "band":
                print(f"\n[应用] 锁定 {_band_label(val)} …")
                try:
                    dev.set_band(val)
                except Unsupported as e:
                    print(f"[跳过] {e}")
                    continue
                if not wait_device_back(dev, timeout=60):
                    print("[!] 设备后台 60 秒没回来 —— 多半还在重启。")
                    print(f"    等 1 分钟跑 {_cli()} --status 看实际状态；"
                          f"要强制回原样用 {_cli()} --restore。")
                    failed = True
                    break
                print(f"      设备已回来，等它重新注册（{SETTLE_SEC}s）…")
                time.sleep(SETTLE_SEC)
                chk = probe.ping_stats(6, ping_host)
                if chk["avg"] is None:
                    print("[!] 切换后无法上网，自动回滚")
                    dev.set_band(cur_band)
                    time.sleep(SETTLE_SEC)
                    failed = True
                    break
                print(f"[√] 已生效，延迟 {chk['avg']}ms")
            else:
                print(f"\n[应用] 切换 WiFi 信道到 {val} …")
                # 改信道 = 设备重启自己的 AP = **手机/电脑先断网**（这台设备的
                # WiFi 就是当前唯一的通道）。所以绝不能「发完就立刻读状态」，
                # 那必定 DeviceUnreachable，还会被当成「应用出错」。
                # 2026-10-30 实机：信道其实已经切成 1 了，界面却报
                # 「[×] 应用过程中设备无响应」+「读不回设备状态」，用户以为失败。
                print("      设备会重启 WiFi，这台机器的连接会断 5~20 秒（正常现象）")
                try:
                    dev.set_channel(val)
                except Unsupported as e:
                    print(f"[跳过] {e}")
                    continue
                except DeviceUnreachable:
                    # 指令多半已经送到，只是设备重启太快、响应没回来。
                    # 当失败处理会误判，先等它回来再核对。
                    print("      指令已发出（设备重启太快，响应没回来 —— 正常）")
                print("      等 WiFi 恢复…")
                if not wait_device_back(dev, timeout=90, poll=5):
                    print("[!] 90 秒还没连回来。")
                    print(f"    手机看 WiFi 是否已重连，然后跑 {_cli()} --status；"
                          f"要强制回原样用 {_cli()} --restore。")
                    failed = True
                    break
                got = dev.get_state()["channel"]
                if got != val:
                    print(f"[!] 信道未生效（当前 {got}），恢复原值 {cur_ch}")
                    try:
                        dev.set_channel(cur_ch)
                        wait_device_back(dev, timeout=90)
                    except Exception:  # noqa: BLE001
                        print(f"    恢复也没发出去，请手动在设备后台把信道改回 {cur_ch}")
                    failed = True
                else:
                    print("[√] 已生效")
    except KeyboardInterrupt:
        print("\n[!] 已中断")
        failed = True
    except Exception as e:  # noqa: BLE001
        # 设备在切换瞬间失联时，写到一半就断了：说清楚当前可能的状态，
        # 别甩一段 traceback 让人无从下手。
        print(f"\n[×] 应用过程中设备无响应：{type(e).__name__}: {e}")
        print("    设备可能已经切换成功，也可能还停在原频段。")
        print(f"    等 30 秒后跑 {_cli()} --status 看当前状态；"
              f"要强制回原样用 {_cli()} --restore。")
        failed = True

    end = None
    try:
        end = dev.get_state()
    except Exception as e:  # noqa: BLE001
        # 刚切完信道时手机 WiFi 常常还没重连上。直接报错会把「成功」说成「失败」，
        # 多等一轮再下结论（2026-10-30 实机就卡在这里）。
        print(f"\n[!] 暂时读不到设备（{type(e).__name__}），等 WiFi 恢复再试…")
        if wait_device_back(dev, timeout=45, poll=8):
            try:
                end = dev.get_state()
            except Exception as e2:  # noqa: BLE001
                print(f"[×] 还是读不到（{type(e2).__name__}）。")
    if end is None:
        print(f"    改动多半已经生效，只是这台机器还没连回去。")
        print(f"    手机 WiFi 重连后跑 {_cli()} --status 确认；不行就 {_cli()} --restore。")
        return 1
    append_log({"ts": datetime.now().isoformat(), "mode": "apply-plan",
                "plan": plan.get("expected"), "before": {"band": cur_band, "channel": cur_ch},
                "after": {"band": end["band"], "channel": end["channel"]}})
    print(f"\n完成：频段 {_band_label(end['band'])} ｜ 信道 {end['channel'] or '自动'}")
    if failed:
        # 别写成「已回滚」—— 现在可能是「信道改好了、频段没成」，两条路不一样。
        print("（没全部成功，详见上面的 [!] 行；上面显示的是设备的实际状态）")
        _mark_applied(plan, end["band"], end["channel"], "partial_failure")
        return 1
    _mark_applied(plan, end["band"], end["channel"], "ok")
    print(f"原始设置已存 {STATE}，可用 --restore 还原")

    verdict = observe_after_apply(ping_host, getattr(args, "observe", OBSERVE_SEC))
    if verdict and verdict["bad"] >= 2:
        print(f"\n[!] 切过去之后多次不通，说明这个配置在你这个位置撑不住。")
        print(f"    建议回滚：{_cli()} --restore（还原成不锁频段 + 自动信道）")
        _mark_applied(plan, end["band"], end["channel"], "unstable")
        return 1
    if verdict:
        # 说实话：90 秒只能覆盖「切完就出问题」，更晚的抖动靠用户自己回来看。
        print(f"      （头几分钟最容易出问题。若之后出现短暂断网，"
              f"跑 {_cli()} --restore 回原配置）")
    return 0


# ---------------- 只改信道（「扫描信道」那条建议的落地） ----------------

def save_channel_advice(cur, best):
    """把刚扫出来的信道建议写下来。

    app 里界面和引擎不在同一个进程，只能靠文件传：界面上点「应用建议」时，
    引擎这边得知道用户同意的是哪个信道。写失败不影响扫描本身 —— 大不了
    应用的时候说一句「建议过期了，重扫一次」。
    """
    try:
        CHANNEL_ADVICE.write_text(json.dumps(
            {"current": cur, "best": best,
             "saved_at": datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False), encoding="utf-8")
        return True
    except OSError:
        return False


def load_channel_advice():
    try:
        return json.loads(CHANNEL_ADVICE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_apply_channel(dev, caps, st0, args, ping_host):
    """只把信道锁到刚扫出来的那个。

    为什么不并进 run_apply_plan：那份结论是按体检当时的频段算的，可能已经
    过期；信道建议是刚刚扫的，两件事的时效差很远。硬塞进同一份 plan，就会
    出现「用户只想改信道，结果频段也被切走」—— 那不叫建议，叫替人做主。
    """
    adv = load_channel_advice()
    if not adv or not adv.get("best"):
        print("[×] 没有可用的信道建议。先点一次「扫描信道」。")
        return 1
    target = adv["best"]
    cur_ch = st0["channel"]

    if not caps.get("can_set_channel", True):
        print("[×] 这台设备的固件不让改信道（后台里没有这个选项）。")
        return 1
    if target == cur_ch:
        print(f"[√] 现在就是信道 {target}，不用改。")
        return 0

    print(f"按扫描结果应用：信道 {cur_ch or '自动'} → {target}")
    print("      设备会重启 WiFi，这台机器的连接会断 5~20 秒（正常现象）")
    save_state({"saved_at": datetime.now().isoformat(),
                "band": st0["band"], "channel": cur_ch})

    try:
        dev.set_channel(target)
    except Unsupported as e:
        print(f"[跳过] {e}")
        return 1
    except DeviceUnreachable:
        # 指令多半已经送到，只是设备重启太快、响应没回来。当失败会误判。
        print("      指令已发出（设备重启太快，响应没回来 —— 正常）")

    print("      等 WiFi 恢复…")
    if not wait_device_back(dev, timeout=90, poll=5):
        print("[!] 90 秒还没连回来。")
        print(f"    手机看 WiFi 是否已重连，再跑 {_cli()} --status；"
              f"要回原样用 {_cli()} --restore。")
        return 1

    try:
        got = dev.get_state()["channel"]
    except Exception as e:  # noqa: BLE001
        print(f"[!] 暂时读不到设备（{type(e).__name__}），等会儿再 {_cli()} --status 确认。")
        return 1

    if got != target:
        print(f"[!] 信道未生效（当前 {got}），恢复原值 {cur_ch or '自动'}")
        try:
            dev.set_channel(cur_ch or 0)
            wait_device_back(dev, timeout=90)
        except Exception:  # noqa: BLE001
            print(f"    恢复也没发出去，请手动在设备后台把信道改回 {cur_ch or '自动'}")
        return 1

    print(f"[√] 已生效，信道 {got}")
    append_log({"ts": datetime.now().isoformat(), "mode": "apply-channel",
                "before": {"band": st0["band"], "channel": cur_ch},
                "after": {"band": st0["band"], "channel": got}})

    verdict = observe_after_apply(ping_host, getattr(args, "observe", OBSERVE_SEC))
    if verdict and verdict["bad"] >= 2:
        print("\n[!] 切过去之后多次不通，这个信道在你这个位置撑不住。")
        print(f"    建议回滚：{_cli()} --restore")
        return 1
    return 0


# ---------------- 守护模式 ----------------

def _threshold(base, floor_ms, cold_ms, cap_ms):
    """算出触发阈值：min(上限, max(基线×倍数, 下限))；没有基线时用冷启动默认值。"""
    if base is None:
        return cold_ms
    return min(cap_ms, max(base * WATCH_FACTOR, floor_ms))


def judge_degradation(stats, base_avg=None, base_jitter=None):
    """判断一次探测是否算「网络劣化」，返回命中的原因列表（空列表 = 健康）。

    阈值同时看绝对值和相对基线：只比绝对值会在本来就慢的链路上频繁误报；
    只比相对基线，又会让已经很差的链路永远算「正常」。所以两边都设边界。
    """
    reasons = []
    if stats.get("avg") is None:
        return ["无法上网"]
    if (stats.get("loss") or 0) >= WATCH_LOSS_LIMIT:
        reasons.append(f"丢包 {stats['loss']}%")

    avg_floor = _threshold(base_avg, WATCH_MIN_MS, WATCH_COLD_MS, WATCH_CAP_MS)
    if stats["avg"] > avg_floor:
        reasons.append(f"延迟 {stats['avg']}ms 超过 {int(avg_floor)}ms")

    jit = stats.get("jitter") or 0
    jit_floor = _threshold(base_jitter, WATCH_JITTER_MIN_MS,
                           WATCH_JITTER_COLD_MS, WATCH_JITTER_CAP_MS)
    if jit > jit_floor:
        reasons.append(f"抖动 {jit}ms 超过 {int(jit_floor)}ms")
    return reasons


def _can_fork_python():
    """这台机器上能不能真的起一个 python 子进程。

    APK 里不能：Chaquopy 把解释器嵌在进程内，sys.executable 指向的东西并不
    存在。守护一旦走「体检」那条路就会在这里炸 —— 实测中 下午报的
    「守护模式跑全局体检会报错」就是这个。判据用 sys.getandroidapilevel()，
    它只有 Chaquopy 才有，比猜路径可靠。
    """
    if hasattr(sys, "getandroidapilevel"):
        return False
    exe = sys.executable or ""
    return bool(exe) and Path(exe).exists()


def _run_cycle_inprocess(args, lte_only=False):
    """同一个解释器里跑一次「体检 + 应用」。

    用的还是 main() 那一套（临时换 sys.argv）：参数定义只此一份，不会漏。
    调用方（守护）已经把设备连接放掉了，所以这里重新登录不会互相顶。
    """
    argv = []
    if getattr(args, "wifi_only", False):
        argv.append("--wifi-only")
    if lte_only or getattr(args, "lte_only", False):
        argv.append("--lte-only")
    if getattr(args, "bands", "default") != "default":
        argv += ["--bands", args.bands]
    if getattr(args, "ssid", None):
        argv += ["--ssid", args.ssid]
    argv.append("--apply")
    old = sys.argv
    sys.argv = [str(Path(__file__).resolve())] + argv
    try:
        main()
        return 0
    except SystemExit as e:          # argparse 出错 / --help
        return int(e.code or 0)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    finally:
        sys.argv = old


def run_cycle(args, lte_only=False):
    """跑一次完整的体检 + 应用，返回退出码。

    lte_only=True 时跳过 2.4G 信道扫描。守护要用这个模式：它管的是「锁哪条
    频段」，信道是另一件事，而扫一遍要 5 轮 AP 扫描（一分钟上下）。实机
    2026-11-02 18:47 那轮扫完的结论是「当前信道 0 评分 None，保持现状」——
    守护拿它一个字都用不上。
    """
    if not _can_fork_python():
        return _run_cycle_inprocess(args, lte_only=lte_only)
    cmd = [sys.executable, "-u", str(Path(__file__).resolve())]
    if args.wifi_only:
        cmd.append("--wifi-only")
    if lte_only or args.lte_only:
        cmd.append("--lte-only")
    if args.bands != "default":
        cmd += ["--bands", args.bands]
    if args.ssid:
        cmd += ["--ssid", args.ssid]
    cmd.append("--apply")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("utf-8", "ignore").rstrip()
        if line:
            print("      " + line, flush=True)
    p.wait()
    return p.returncode


def watch(args, cfg):
    """守护模式：低成本探测，只在明显劣化时才触发完整重测。

    为什么不是「每隔几分钟测一次速」：测速要跑几十兆流量，本身就干扰你在用的
    网络，长期跑不现实。所以平时只打 10 个 ping（几百字节），只有延迟/抖动/丢包
    明显偏离基线时，才付出「断网几十秒」的代价。
    """
    interval = max(10, args.watch_interval)
    cooldown = max(interval, args.watch_cooldown)
    host = cfg["ping_host"]
    hist, jit_hist = deque(maxlen=12), deque(maxlen=12)
    next_allowed, dry = 0.0, args.watch_dry_run

    print("=" * 62)
    print(f"守护模式：每 {interval}s 探测 {host}，明显劣化才触发重测")
    print(f"  触发：丢包≥{WATCH_LOSS_LIMIT}% ｜ 延迟>{WATCH_MIN_MS}~{WATCH_CAP_MS}ms"
          f"（基线×{WATCH_FACTOR}）｜ 抖动>{WATCH_JITTER_MIN_MS}~{WATCH_JITTER_CAP_MS}ms")
    print(f"  冷却：{cooldown}s（避免反复断网）"
          f"{'  ｜ 演练模式：只报告，不改设备' if dry else ''}")
    print("=" * 62)
    try:
        while True:
            st = probe.ping_stats(10, host)
            base_avg = statistics.median(hist) if len(hist) >= 3 else None
            base_jit = statistics.median(jit_hist) if len(jit_hist) >= 3 else None
            reasons = judge_degradation(st, base_avg, base_jit)

            ts = datetime.now().strftime("%H:%M:%S")
            line = (f"{ts}  延迟 {st['avg'] if st['avg'] is not None else '—'}ms"
                    f"  抖动 {st['jitter'] if st['jitter'] is not None else '—'}ms"
                    f"  丢包 {st['loss']}%"
                    f"  基线 {str(int(base_avg)) + 'ms' if base_avg else '采样中'}")

            if not reasons:
                print(line + "  ✓", flush=True)
                if st["avg"] is not None:
                    hist.append(st["avg"])
                if st["jitter"] is not None:
                    jit_hist.append(st["jitter"])
            elif time.time() < next_allowed:
                print(line + f"  ⚠ {'/'.join(reasons)}（冷却中，"
                             f"{int(next_allowed - time.time())}s 后再评估）", flush=True)
            else:
                print(line + f"  ✗ 判定劣化：{'/'.join(reasons)}", flush=True)
                if dry:
                    print("      （演练模式，未改动设备）", flush=True)
                else:
                    print("      → 开始完整重测…", flush=True)
                    code = run_cycle(args)
                    print(f"      → 重测结束（exit={code}）", flush=True)
                    hist.clear()
                    jit_hist.clear()
                next_allowed = time.time() + cooldown
            time.sleep(interval)     # watch 模式不认 _GUARD_STOP，别拿它当等待
    except KeyboardInterrupt:
        print("\n[守护模式] 已停止")
        return 0


# ---------------- 全自动守护（--guard） ----------------

def _now_hm():
    return datetime.now().strftime("%H:%M:%S")


def _pci_num(v):
    """PCI 在设备里是字符串（'236'）。拿不到就返回 None —— 不能当 0 用。"""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def rsrp_is_falling(hist, drop=GUARD_MOVE_RSRP_DROP):
    """信号是不是在「一路走低」。hist 是最近几拍的 RSRP（新→旧都行，按时间顺序）。

    为什么要单独一条：锁着频段时链路（丢包/延迟）往往是好的 —— 走出一栋楼，
    对面小区照样给你 -100dBm、延迟 80ms。这时候「PCI 变了 + 链路没垮」的判据
    全部落空，人明明在走，守护却一路「监控中」。

    和「设备重选到弱小区」的区别在形状：重选是**跳一下**（-89 → -105 之后稳在
    -105），人是**一路往下**（-78 → -85 → -92 → -100）。所以要求每一拍都比上
    一拍低，且累计掉够 drop —— 单调下降这个条件把绝大部分重选挡在外面。
    """
    vals = [v for v in hist if v is not None]
    if len(vals) < 3:
        return False
    for a, b in zip(vals, vals[1:]):
        if b >= a:                      # 有一拍没降就不算「一路」
            return False
    return (vals[0] - vals[-1]) >= drop


def guard_is_moving(*, churn, pci_changed, bad_now, band_changed=False, churn_n=0,
                    locked=False, rsrp_falling=False):
    """人到底动没动。抽成纯函数，因为这条判断直接决定「要不要解锁」。

    PCI 变只是**必要条件**。2026-10-30 的实测推翻了这个假设：坐在住处里、
    锁着 BAND 41 的那三分钟，PCI 自己从 477 跳到 252（-89 → -105dBm）——
    信号变弱时设备会自己重选小区（同频段换扇区），人在原地，PCI 照样跳。

    所以三条判据：
      · 窗口内变够多次（GUARD_MOVE_HITS）—— 真走路 25 分钟换 8 个小区；
      · 但**驻留频段变了**就不算：换频段同样会让 PCI 变、WiFi 重启、ping 全丢，
        「PCI 变 + 链路垮」两条同时成立，原来必然误判成人在动。实机 21:46
        （手机放在桌上没动）：BAND 3→40、PCI 350→236、丢包 100%，判了「移动中」。
        设备自己换频段时信号通常是**变好**的（-98 → -76），人走路不会这样。
      · 剩下那种「PCI 变了、频段没变、链路垮了」才当移动 —— 真走出覆盖时，
        这两件事一定同时发生。
      · 还有一条不用看链路的：窗口内换够 GUARD_MOVE_CHURN_ONLY 次小区。
        走路的特征就是一直换小区，未必每一拍都在丢包。
    """
    if band_changed:
        # 先判这一条，再判「换了几次小区」。设备自己换频段时 PCI 必变、
        # churn 必定累积，人却坐着没动 —— 2026-10-31 15:59 就是这么被解锁的。
        return False
    if locked and churn_n >= GUARD_MOVE_CHURN_LOCKED:
        # 锁着频段时门槛降一档，理由见 GUARD_MOVE_CHURN_LOCKED。
        # 2026-11-02 上午漏判的就是这条：锁 BAND 3 期间换了 2 个小区、链路一直
        # 还行（延迟 72~85ms、丢包 0%），于是三个判据全不成立，人走了 57 分钟
        # 没被发现，屏幕一直写「监控中」。
        return True
    if locked and rsrp_falling and pci_changed:
        # 换着小区、信号还一路往下 —— 走出覆盖的形状。链路没垮也认。
        return True
    if churn and bad_now:
        # 光「换了几次小区」不够：2026-10-31 实机在住处坐了 144 分钟，
        # 设备在两个扇区之间自己重选，5 分钟内变 2 次 → churn 成立 → 判「移动中」，
        # 于是解锁、静止计时清零、白等 10 分钟。那一刻丢包 0%、延迟 110ms，
        # 链路好好的 —— 人根本没走。真走出覆盖时，换小区和链路垮一定同时发生。
        return True
    if churn_n >= GUARD_MOVE_CHURN_ONLY:
        # 但「链路垮」这个附加条件也会漏判：2026-11-01 中午人从住处走到餐厅，
        # 5 分钟里换了 3 个小区，可每个新小区的信号都不算差、ping 也没丢，
        # 于是每一拍都落在「PCI 变了但链路没垮」→ 判「人没动」→ 带着住处那条
        # BAND 3 的锁在餐厅坐了一中午。换到 3 次就够说明问题了，别再要求链路垮。
        return True
    return bool(pci_changed and bad_now)


UNLOCK_SIGNAL_COLLAPSE = "锁着的这条信号塌了，锁的前提没了"


def lock_signal_collapsed(lock_rsrp, cur_rsrp, drop=GUARD_LOCK_RSRP_DROP):
    """锁着的那条现在还剩多少信号 —— 比锁定那一刻塌了 drop 以上就算塌了。

    为什么单独拎一条：链路指标（丢包/延迟）是**结果**，信号是**原因**。人走开
    之后信号先掉、链路过一会儿才垮，甚至根本不垮（餐厅的 BAND 3 只是慢）。
    2026-11-01 中午就是这个样子：守护一路「监控中」，用户看着「换了小区」的
    提示等它解锁，一中午都没等到。
    """
    if lock_rsrp is None or cur_rsrp is None:
        return False
    return cur_rsrp <= lock_rsrp - drop


def lock_collapse_cause(*, lock_ref, cur_rsrp, locked_band, pci_changed,
                        band_changed):
    """「锁着的这条信号塌了」到底是哪一种塌 —— 只负责说人话，不影响裁决。

    两种成因的处置完全一样（解锁，交还给设备），但日志里必须分清楚，否则看的人
    会以为程序在瞎猜。实机 2026-11-01 19:31：锁着 BAND 40，PCI 288 是 -61dBm，
    两拍内 PCI 变成 222、-97dBm —— 人正坐在餐厅吃饭，根本没走开。原来的文案写
    死了「多半是走开了」，把「设备在同一频段里挑了个弱小区」硬说成「人走了」。
    """
    if band_changed:
        return "设备已经换到别的频段了"
    if lock_ref is None or cur_rsrp is None:
        return "拿不到信号读数，按「不再死守」处理"
    if pci_changed:
        return (f"人没动，是设备在 BAND {locked_band} 里换到了弱小区"
                f"（{int(lock_ref)}→{cur_rsrp}dBm）—— 锁着就换不出去")
    return f"信号从 {int(lock_ref)} 掉到 {cur_rsrp}dBm，多半是走开了"


def _mbps_text(v):
    """速度数字的显示格式。

    0.4 Mbps 印成「0」是错的 —— 实测日志里出现过「两次 3 / 0，两次差 10.3 倍」，
    0 和 3 差 10 倍这件事，看的人只会以为程序算错了。低于 1 就写「<1」。
    """
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "0"
    if v < 1:
        return "<1"
    return f"{v:.0f}"


def quick_line(q):
    """把一次快测结果排成一行人话。

    「停 3 分钟先测一次」和「下锁前发现过期、再测一次」两处都要打这一行，
    原来各写一套：一处带「两次差 3.5 倍」，另一处没有 —— 而恰恰是后面那处
    的差异（2 / 9）决定了能不能信这个数字。所以只留一份实现。
    """
    gcn = {"good": "明确够用", "fair": "勉强够用",
           "unknown": "测不准（两次差太多）", "poor": "不够用"}
    g = q.get("grade") or ("good" if q.get("ok") else "poor")
    sam = " / ".join(_mbps_text(x) for x in (q.get("samples") or []))
    vals = [x for x in (q.get("samples") or []) if x]
    spread = ""
    if len(vals) >= 2 and min(vals) > 0 and max(vals) / min(vals) >= 1.5:
        spread = f"，两次差 {max(vals) / min(vals):.1f} 倍"
    lat = f"{q['avg']}ms" if q.get("avg") is not None else "不通"
    return (f"{_mbps_text(q.get('mbps'))} Mbps（两次 {sam}{spread}）｜ "
            f"延迟 {lat} ｜ 丢包 {q.get('loss')}% → {gcn.get(g, g)}"
            f"（{'先不动它' if q.get('ok') else '坐久了会重新选'}）")


def quick_is_stale(q, now, max_age=GUARD_QUICK_MAX_AGE):
    """这份快测结果还能不能当依据。

    快测发生在「停够 3 分钟」，下锁在「坐满 10 分钟」，中间隔着 7 分钟；
    晚高峰的链路几分钟就能变一个样。实机 2026-11-01 19:28 就是拿 18:21:13
    的 9 Mbps（两次 2 / 9，当场判「测不准」）去下锁，说「快测够用」。
    没有时间戳的旧结果（老版本导出的）一律算过期 —— 宁可多测 20 秒。
    """
    if not q:
        return False
    at = q.get("at")
    if at is None:
        return True
    return (now - float(at)) > max_age


def guard_decide(*, moving, still_sec, settle_sec, locked, bad_streak,
                 cooldown_left, outage_after_unlock, signal_collapsed=False,
                 net_ok=True):
    """守护状态机：只回答「现在该做什么」，完全不碰设备（为了能单测）。

    规则（实测中 定的）：
      · 在移动、且锁着频段 → 解锁。锁死一个频段，走到没覆盖的地方就断网，
        而设备本来能自己换。这是「保证一直有网」的第一道保险。
      · 锁着、但这条的信号比锁定那一刻塌了 10dB 以上（连续两拍）→ 也解锁。
        原先只有「人动」和「连续不通」两条路会解锁，而「人走开了、信号掉了、
        链路还没垮」正好卡在中间，能锁一整天。
      · 原地不动够久、且没锁 → **先选优，再锁定**（跑一次体检测出最优频段）。
        只锁「当前驻留的」不一定最优，可能把一个次优频段长期锁死；
        选优只在久坐时发生一次，不会反复断网（--guard-fast 可跳过选优）。
      · 锁定后连续不通 → 先解锁自救（最快恢复）。解锁后仍然不通 → 才跑完整体检：
        那说明不是「锁错频段」，而是这个位置真的没覆盖/基站有问题，换频段才有意义。
        实测：切到 BAND 40 后 3~5 分钟断一次，就是靠这条抓出来的。

    返回 (action, 原因)，action ∈ {None, "unlock", "lock", "measure"}。

    net_ok=False 表示「此刻整条链路不通」。这种时候 measure 是不许走的：
    体检逐段下载测速，断网时六段全 0，结论必然「保持现状」，只会白断 5 分钟网。
    """
    bad = bad_streak >= GUARD_BAD_HITS
    # 自救优先于冷却。冷却本来只管「别反复切频段」，但它要是把自救也挡住，
    # 就变成「一直不通就一直等着」—— 实机对时间才看出来：解锁后置冷却 300s，
    # 「解锁后仍不通」的窗口也正好是 300s，两者互斥，`measure` 那条永远走不到，
    # 等于「锁在坏频段上」的自救链是断的。
    if moving and locked:
        return "unlock", "检测到移动，锁死频段会限制可用覆盖"
    if signal_collapsed and locked:
        # 不等到「丢包过半」才认：信号塌了就是锁的前提（这个位置这条频段好）没了。
        return "unlock", UNLOCK_SIGNAL_COLLAPSE
    if bad and locked:
        return "unlock", f"锁定后连续 {bad_streak} 次不通，先解锁自救"
    if bad and not locked and outage_after_unlock:
        if not net_ok:
            # 解锁了还是不通、而且这会儿整条链路是断的：体检给不出任何信息。
            # 实机 2026-11-02 18:47 就是在这种状态下被拉去跑了 8 分钟，
            # 六段频段三个是 0、一个「无信号」，回来锁回原频段照样 100% 丢包。
            return None, ("解锁后仍然不通，而且整条链路此刻是断的 —— 体检只会测出 0，"
                          "先等网络恢复（不反复折腾设备）")
        return "measure", f"解锁后仍然 {bad_streak} 次不通 —— 不是频段问题，换一个试试"
    if cooldown_left > 0 and not locked:
        # 锁着的时候不报「冷却中」。冷却管的是「别反复主动切频段」，而已经锁好
        # 的状态本来就没有动作可做 —— 报出来只是噪音。实机 2026-11-02
        # 20:01~20:07 锁稳 BAND 3 之后，屏幕上连着五行
        # 「→ 冷却中（269s）(228s)(186s)…」，看着像还在等什么，其实什么都不用做。
        return None, f"冷却中（{int(cooldown_left)}s）"
    if not moving and not locked and still_sec >= settle_sec:
        return "lock", f"原地待了 {int(still_sec / 60)} 分钟，该把频段定下来了"
    return None, ""


def _guard_unlock(dev, cur_band):
    print(f"      解锁频段（原 BAND {cur_band}）…", flush=True)
    try:
        dev.set_band(0)
    except DeviceUnreachable:
        # 射频重启导致请求本身断掉，指令通常已经送达
        pass
    except Exception as e:  # noqa: BLE001
        print(f"      [!] 解锁失败：{type(e).__name__}: {e}", flush=True)
        return False
    ok = wait_device_back(dev, timeout=60)
    print("      已解锁，设备可以自己换频段了" if ok
          else "      [!] 后台没回来，稍后自动重试", flush=True)
    return ok


def _quick_grade(q):
    """快测结果分四档：good / fair / poor / unknown。

    为什么要分档，而不是一个布尔：单次短下载的重复性很差。实机（2026-10-30
    住处）同一张桌子、前后 22 分钟，守护快测 32 Mbps、体检时只有 17.8 和 10.8。
    原来 15 Mbps 的硬阈值正好卡在这个波动区中间 —— 同样的网络，掷到 32 判
    「够用」（不锁，什么也不发生），掷到 12 判「不够用」（触发 5 分钟体检 +
    断网 4 次）。判错的代价完全不对等，所以：
      · 下限压到 8 Mbps —— 只有真的差才承认不够用；
      · 两次采样差 3 倍以上 → unknown（测不准），按「别动它」处理。
    """
    if not q:
        return "unknown"
    loss = q.get("loss")
    loss = 100 if loss is None else int(loss)
    avg = q.get("avg")
    if avg is None or loss >= GUARD_QUICK_MAX_LOSS or avg > GUARD_QUICK_MAX_MS:
        return "poor"
    if q.get("noisy"):
        return "unknown"
    mbps = float(q.get("mbps") or 0)
    if mbps >= GUARD_QUICK_GOOD_MBPS:
        return "good"
    if mbps >= GUARD_QUICK_MIN_MBPS:
        return "fair"
    return "poor"


def _quick_ok(q):
    """快测结果算不算「够用」。

    unknown（两次差太多、测不准）也当够用：证据不足时不动设备，
    比凭一个不可信的数字去断网体检要好。但**压根没测出结果**（q 为空）不算够用。
    """
    if not q:
        return False
    return _quick_grade(q) in ("good", "fair", "unknown")


def guard_quick_probe(speed_url, ping_host, secs=GUARD_QUICK_PROBE_SEC,
                      times=GUARD_QUICK_SAMPLES):
    """在当前连接上快测一遍：短下载 + ping，**完全不碰设备**。

    为什么不复用 measure_band：那个第一件事就是 dev.set_band()，哪怕锁的正是
    同一条频段也可能让射频重启一下。而这一步恰恰发生在「可能只是路过」的时候，
    不该有任何断网风险。

    为什么测两次：一次 4~5 秒的下载，在晚高峰的住处里能从 32 Mbps 掉到 10。
    取两次里较好的那次（它更接近这条链路的能力上限），同时把「两次差多少」
    一起交出去 —— 差得离谱时调用方会判「测不准」，不拿它下结论。
    """
    runs = []
    for i in range(max(1, int(times))):
        try:
            p = probe.ping_stats(10, ping_host)
        except Exception:  # noqa: BLE001
            p = {"avg": None, "jitter": None, "loss": 100}
        mbps = 0.0
        if p.get("avg") is not None and (p.get("loss") or 0) < GUARD_BAD_LOSS:
            try:
                mbps = float(probe.throughput(secs=secs, url=speed_url) or 0)
            except Exception:  # noqa: BLE001
                mbps = 0.0
        runs.append({"mbps": mbps, "avg": p.get("avg"), "loss": p.get("loss")})
        if i < times - 1:
            time.sleep(1)

    best = max(runs, key=lambda r: r["mbps"])
    speeds = [r["mbps"] for r in runs]
    noisy = (len(speeds) >= 2 and min(speeds) > 0
             and max(speeds) / min(speeds) >= GUARD_QUICK_NOISY_RATIO)
    q = {"mbps": best["mbps"], "avg": best["avg"],
         # 丢包取最差的那次：一次通不代表链路稳
         "loss": max(r["loss"] or 0 for r in runs),
         "samples": [round(x, 1) for x in speeds], "noisy": noisy,
         # 量出来的时刻。调用方可能过好几分钟才拿它下结论，得能算出「这数字多旧了」
         "at": time.time()}
    q["grade"] = _quick_grade(q)
    q["ok"] = _quick_ok(q)
    return q


def guard_alt_band(st, skip=None):
    """锁定频段判为「不行」时，先在记忆里找第二条，别急着交还给设备。

    交还给设备的下场，多半是「按信号最强又选回刚才那条」——实测中
    的实际体感就是这样，也是守护看起来像厂商自动的原因之一。
    """
    key = _site_key(st)
    if not key:
        return None
    data = load_sites()
    rec = data.get(find_site(data, key))
    if not rec or rec.get("provisional") or site_memory_state(rec) != "fresh":
        return None
    rows = []
    for b, v in (rec.get("bands") or {}).items():
        if skip is not None and str(b) == str(skip):
            continue
        if (v.get("dead") or 0) >= DEAD_SKIP_MIN and not (v.get("ok") or 0):
            continue
        if band_flap(v)[0]:
            continue
        rows.append((float(v.get("best") or 0), int(b)))
    rows.sort(reverse=True)
    return rows[0][1] if rows else None


def guard_memo_speed(key, band):
    """记忆里这个频段的历史最好速度（Mbps）；查不到返回 0。

    只用来和「刚才快测的当前速度」比大小，不参与别的判断 ——
    它可能是几天前、在另一个 PCI 下测出来的，单看它不可信。
    """
    if not key or band is None:
        return 0.0
    try:
        data = load_sites()
        rec = data.get(find_site(data, key)) or {}
        v = (rec.get("bands") or {}).get(str(int(band))) or {}
        return float(v.get("best") or 0)
    except (TypeError, ValueError):
        return 0.0


_OUTAGE_LOCK = threading.RLock()


def _outage_key(st=None):
    """断网这件事记在哪个键上。

    PCI 读不到时（设备没注册上，而这正是断网时的常态）记在 "*" 上 ——
    记的是「这台设备刚断过」，不是「某个我叫不出名字的地方断过」。
    宁可粗一点，也别因为记不下来就每一拍重新折腾一遍。
    """
    return _site_key(st) or "*"


def load_outage():
    with _OUTAGE_LOCK:
        try:
            data = json.loads(OUTAGE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


def note_outage(st=None, *, loss=None, band=None):
    """记一笔「这儿这会儿没网」，返回这次记下的内容。"""
    key = _outage_key(st)
    now = datetime.now()
    with _OUTAGE_LOCK:
        data = load_outage()
        rec = data.get(key) or {}
        rec["t"] = now.isoformat(timespec="seconds")
        rec["loss"] = int(loss) if loss is not None else None
        rec["band"] = int(band) if band else None
        rec["n"] = int(rec.get("n") or 0) + 1      # 同一处断了几次
        data[key] = rec
        if len(data) > 12:
            # 只留最近 12 处。这是个短命的时间戳文件，不是账本 ——
            # 攒着它没有任何好处，而文件长大会让每拍的读取变慢。
            data = dict(sorted(data.items(),
                               key=lambda kv: kv[1].get("t") or "")[-12:])
        try:
            OUTAGE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        except OSError:
            pass
        return rec


def outage_left(st=None, now=None, window=GUARD_OUTAGE_COOLDOWN):
    """这地方离「允许再全量测一次」还剩多少秒。0 = 可以测。

    实测中 的原话就是这个功能的需求：「在一个地方重复检测到没网，
    后续就不测，等一段时间没来才能进行全量测试」。
    """
    data = load_outage()
    keys = [k for k in (_site_key(st), "*") if k]
    now = now or datetime.now()
    left = 0.0
    for k in keys:
        ts = (data.get(k) or {}).get("t")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(str(ts))
        except (TypeError, ValueError):
            continue
        left = max(left, window - (now - t).total_seconds())
    return max(0.0, left)


def guard_memo_rsrp(key, band):
    """记忆里这个频段典型的 RSRP（dBm）；没记过返回 None。

    老记录里没有 RSRP（2026-10-30 之前只记速度），这时返回 None，
    调用方就跳过信号比较 —— 不能把「没记录」当成「信号很好」。
    """
    if not key or band is None:
        return None
    try:
        data = load_sites()
        rec = data.get(find_site(data, key)) or {}
        v = (rec.get("bands") or {}).get(str(int(band))) or {}
        val = v.get("rsrp_med", v.get("rsrp"))
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def site_pinned(st):
    """用户亲手指定过的频段（`--lock-band N` 留下的 pinned）。None = 没指定过。

    和 best 的分工：best 是程序算出来的「这个位置最优」，会过期、会被体检覆盖、
    也参与信号闸门；pinned 只由人的一条命令写入，只有人能改。用户已经明说
    「BAND 40 丢包严重，就要 BAND 3」的时候，再拿 RSRP 去反驳他毫无意义。
    """
    key = _site_key(st)
    if not key:
        return None
    try:
        data = load_sites()
        rec = data.get(find_site(data, key))
    except (OSError, ValueError):
        return None
    if not rec:
        return None
    try:
        p = rec.get("pinned")
        if not p:
            return None
        # 这条**不设时效**。位置级 pin 表达的是「这个地方这条更稳」，那是位置的
        # 长期属性 —— 实测中 的原话：住处的 BAND 40 信号和速度有时比 3
        # 好，程序就锁 40，可 40 会周期性丢包、用起来一会一卡，所以他要在这儿
        # 优先 3。给它加个七天过期，等于让他每星期被坑一次。
        # 跨位置误伤由 device_pin_here() 负责（设备级那条记了来源位置）。
        return int(p)
    except (TypeError, ValueError):
        return None


def clear_site_pins(reason=""):
    """把所有位置上的「你亲手指定过」清掉。`--restore` 用这个。

    位置级 pinned 挂在 `PCI-运营商` 这个键上，而 `--restore` 之后设备会重新选
    小区 —— 只清设备级的话，位置级那份还留在原地等着，下次守护一来又把它锁
    回去，和「回到自动」正好相反。

    实测（2026-10-31 20:56，一台手机）：`--restore` 明明打印了「已取消之前指定
    的频段」，紧接着的守护演练却报「本来会锁 BAND 3，依据 pinned」—— 设备级清
    了、位置级没清，而守护优先读位置级。
    """
    data = load_sites()
    hits = []
    for k, v in (data or {}).items():
        if isinstance(v, dict) and v.get("pinned"):
            v.pop("pinned", None)
            hits.append(k)
    if hits:
        save_sites(data)
        if reason:
            print(f"      → {reason}", flush=True)
    return hits


def device_pin():
    """设备级「上次你亲手指定的频段」。None = 没指定过（或已经过期）。

    为什么不能只靠 site_pinned：那个挂在 `PCI-运营商` 键上，而 PCI 是**当前驻留
    小区**，同一条频段才有意义。锁 BAND 3 读到 350，设备回 auto 后驻留 BAND 40
    读到 236 —— 同一张桌子两个键。实机 2026-10-31 16:47 就是这么翻车的：
    `--lock-band 3` 把 pinned 写进 350 那条，`--restore` 后设备停回 40，守护读到
    236 那条（今早体检写的 best=40、没有 pinned）→ 直接锁回 40，pinned 白加。

    人说的是「这台设备要 3」，不是「这个小区的这条记录要 3」。所以另存一份设备级，
    放在 config.json 里，跟位置无关；`--restore` 会清掉它（那是「我要 auto」的意思）。

    **时效**：一次 `--lock-band` 说的只是「当时、那个地方」。住处里 BAND 3 好，不
    等于一周后办公区里的 BAND 3 也好；而这条 pin 会一直压在所有判断之上（连刚测出
    来的数据都推翻）。所以超过 GUARD_PIN_TTL_DAYS 天就不再自动生效 —— 想继续用
    就再设一次，那本来也只要一条命令。
    """
    try:
        cfg = load_config()
        b = cfg.get("pin_band")
        if not b:
            return None
        if _pin_age_days(cfg) > GUARD_PIN_TTL_DAYS:
            return None
        return int(b)
    except (TypeError, ValueError):
        return None


def _days_since(iso_text):
    """某个 ISO 时间戳离现在几天。缺失或写坏了 → 当作很久以前。

    老数据说不清是什么时候写的，而它恰恰是最可能过时的那一批 —— 2026-10-31
    钉的 BAND 3，到 11-02 还在推翻「办公区该锁 BAND 40」的结论。
    """
    if not iso_text:
        return 1e9
    try:
        t = datetime.fromisoformat(str(iso_text))
    except (TypeError, ValueError):
        return 1e9
    return (datetime.now() - t).total_seconds() / 86400.0


def _pin_age_days(cfg):
    """设备级 pin 是几天前设的。没有时间戳（老版本写的）一律当作「已经过期」。"""
    return _days_since(cfg.get("pin_at"))


def pin_note():
    """给日志用的一句话：这条 pin 是哪天设的、还剩多久。空字符串 = 没有 pin。"""
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return ""
    b = cfg.get("pin_band")
    if not b:
        return ""
    age = _pin_age_days(cfg)
    if age > GUARD_PIN_TTL_DAYS:
        return (f"BAND {b}（{int(age)} 天前设的，已超过 {GUARD_PIN_TTL_DAYS} 天，"
                f"这次按实测重新判断）")
    return f"BAND {b}（{int(age)} 天前设的）"


def set_device_pin(band, st=None):
    """记下「就要这条」，并把失败计数归零（重新给了一次机会）。

    st 传进来时同时记下**是在哪个地方**下的这条命令。为什么要记：`--lock-band 3`
    说的从来不是「这台设备永远要 3」，而是「我在这个地方试过，3 比 40 稳」。
    实测中 说得很清楚：住处的 BAND 40 信号和速度有时比 3 好，程序就
    锁 40，可 40 会周期性丢包、用起来一会一卡，所以他在住处优先锁 3 —— 结果
    到了办公区，程序算出来该锁 40，被这条住处的 pin 直接改成 3。
    """
    if not band:
        return
    try:
        cfg = load_config()
        cfg["pin_band"] = int(band)
        cfg["pin_at"] = datetime.now().isoformat(timespec="seconds")
        cfg["pin_fails"] = 0
        if st is not None:
            key = _site_key(st)
            if key:
                cfg["pin_site"] = key
                # 把「这个地方见过的小区」一起记下来：位置键是当前驻留那个 PCI，
                # 设备在同一间屋子里重选到别的小区就会换键，只认一个键太脆。
                try:
                    data = load_sites()
                    rec = data.get(find_site(data, key)) or {}
                    cells = [_pci_num(c.get("pci")) for c in (rec.get("cells") or [])]
                    cfg["pin_cells"] = [c for c in cells if c is not None]
                except (OSError, ValueError):
                    cfg["pin_cells"] = []
        save_config(cfg)
    except (OSError, TypeError, ValueError):
        pass


def _pin_band_matches(st, band):
    """当前驻留的频段是不是 pin 说的那条（读不到频段时无从否认，算命中）。

    PCI 只有 0~503，**每个频段各有一套编号** —— 住处 BAND 3 读到 350，
    不代表别处 BAND 40 上的 350 是同一个小区。所以「同一个 PCI」只有在
    同一条频段上才等于「同一个地方」。实测中 说 pin 权限太高
    （在餐厅不敢锁别的），这是误伤的路径之一：位置键只按 PCI-运营商 拼，
    换个频段读到同样的数字就认成了老地方。
    """
    cur = st.get("band") or _serving_band(st)
    return not cur or str(cur) == str(band)


def device_pin_here(st=None):
    """这条 pin 对**现在这个地方**算不算数。

    规则：pin 记了来源位置（pin_site / pin_cells）时，当前小区必须在那组里、
    而且要在同一条频段上 —— 否则就是「别处说的话」，不生效。没记来源
    （老数据）时退回原来的行为。

    为什么要这一层：住处的偏好被当成设备级偏好用，就是办公区那次误伤的全部原因。
    """
    band = device_pin()
    if band is None or st is None:
        return band
    try:
        cfg = load_config()
    except (OSError, ValueError):
        return band
    site = cfg.get("pin_site")
    if not site:
        return band                  # 老数据：没记来源，按原样生效（有时效兜着）
    if _site_key(st) == site and _pin_band_matches(st, band):
        return band
    try:
        here = _pci_num((st.get("signal") or {}).get("pci"))
    except (TypeError, ValueError):
        here = None
    if here is not None and here in (cfg.get("pin_cells") or []):
        if _pin_band_matches(st, band):
            return band                  # 同一处的另一个小区
    return None


def _clear_device_pin():
    """把「就要这条」撤掉 —— 用户手动选了「自动」。

    不清的话会出一个很难解释的现象：他从界面上选了「自动」，设备也确实
    变回自动了，可守护下次一启动又把 cfg 里那条 pin 捡回来、重新锁上去。
    口头说的是自动，实际又被锁住，这比不给选项更糟。
    """
    try:
        cfg = load_config()
        if not cfg.get("pin_band") and not cfg.get("pin_fails"):
            return
        cfg["pin_band"] = 0
        cfg["pin_fails"] = 0
        save_config(cfg)
    except (OSError, TypeError, ValueError):
        pass


def clear_device_pin(reason=""):
    if reason:
        print(f"      → {reason}", flush=True)
    try:
        cfg = load_config()
        had = cfg.get("pin_band") or cfg.get("pin_fails")
        cfg.pop("pin_band", None)
        cfg["pin_fails"] = 0
        if had:
            save_config(cfg)
    except (OSError, TypeError, ValueError):
        pass


def note_pin_failure(band):
    """pin 的这条锁上后仍然不通：记一次，连续两次就取消，别让人一直断网。

    没这个闸门的话，某个频段在当地根本没覆盖时，守护会「锁 → 不通 → 解锁 →
    体检 → 又照 pin 锁回去」无限循环，每次断网 87 秒。人指定的优先级最高，
    但不该高到把人网彻底弄没。
    """
    pin = device_pin()
    if pin is None or str(pin) != str(band):
        return
    try:
        cfg = load_config()
        n = int(cfg.get("pin_fails") or 0) + 1
        cfg["pin_fails"] = n
        save_config(cfg)
    except (OSError, TypeError, ValueError):
        return
    if n >= 2:
        clear_device_pin(f"你指定的 BAND {band} 锁上后连续 {n} 次不通，"
                         "已取消这个指定（不再强锁，按记忆重新挑）")


def pin_overrides_lock(locked_band, pin_goal):
    """设备锁着的频段不是人指定的那条 → 该按「没锁对」处理，不是「已锁好」。

    只判「有锁定值且和指定值不同」。设备没锁（locked_band=0）时由正常流程接管；
    人没指定（pin_goal=None）时不动它 —— 别把用户自己手动锁的频段随便改掉。
    """
    return bool(locked_band and pin_goal and str(locked_band) != str(pin_goal))


_KIND_TEXT = {
    "pinned": "这个频段是你亲手指定的",
    "memo": "记忆里这处的最优",
    "keep": "当前这条不比记忆里差",
    "quick": "当前这条快测够用",
    "weak_keep": "记忆那条现在信号差一截、当前够用",
    "blind_keep": "记忆里没记信号、当前够用",
    "memo_bad_alt": "记忆里的最优刚测出不够用，换次优",
    "memo_bad": "记忆里的最优刚测出不够用",
    "flap_avoid": "当前这条在记忆里会周期性掉包，别被信号强度骗了",
    "flap_no_alt": "当前这条在记忆里会周期性掉包，而记忆里没有能换的备选",
    "regret_avoid": "当前这条以前锁过、锁完反倒更差，换备选",
    "regret_no_alt": "当前这条以前锁过、锁完反倒更差，又没备选可换",
    "measure": "体检量出来的结论",
    "adopt": "启动时设备本来就锁在这条，不是这次决定的",
    "verify": "记忆里那条没记过信号，切过去实测确认",
    "rescue_alt": "锁的这条实在不行了，换记忆里的备选",
    "outage_try": "整条链路不通，换一条有依据的试试",
    "unlock_bad": "锁着这条实在不行了，解开自救",
    "unlock_move": "人在走动，松开让它自己选",
    "unlock_signal": "锁的这条信号塌了，交还给设备",
    "weak": "记忆里那条信号差太多、当前也不够用",
    "blind": "记忆里没记信号、当前也不够用",
}


def _kind_text(kind):
    """把判定依据翻成人话 —— 屏幕上别出现 `pinned` 这种只有代码里才用的词。"""
    return _KIND_TEXT.get(kind, "没有可信依据")


def guard_pick_lock(memo, memo_mbps, serving, quick,
                    memo_rsrp=None, cur_rsrp=None, pinned_band=None,
                    cur_loss=None, alt_band=None, cur_flap=False,
                    cur_regret=False):
    """久坐时到底锁哪一条。返回 (频段 or None, 依据)；
    依据 ∈ {memo, keep, quick, weak_keep, weak, blind_keep, blind,
            memo_bad, memo_bad_alt, flap_avoid}。

      memo      记忆里的最优频段（None = 没有可信记忆）
      memo_mbps 记忆里那条的历史最好速度
      serving   设备当前驻留的频段
      quick     刚才的快测结果 dict（可能为 None，或测的是别的频段）
      memo_rsrp 记忆里那条频段的典型 RSRP（没记过就是 None）
      cur_rsrp  当前驻留频段此刻的 RSRP
      cur_loss  当前驻留频段此刻的丢包%；≥GUARD_BAD_LOSS 时「留在当前」
                这个选项直接作废 —— 正在大量丢包的链路不能算「够用」
      cur_flap  当前驻留这条在记忆里被判「会周期性掉包」
      cur_regret 当前驻留这条以前锁过、锁完反而更差（回评攒出来的结论）
      alt_band  记忆里的次优频段（当前这条被证伪时用它；没有就是 None）

    依据的含义：
      weak_keep 记忆那条现在信号差一截，但当前这条够用 → 留在当前
      weak      记忆那条现在信号差一截、当前也不够用 → 老实跑体检
      blind_keep 记忆没记信号，当前够用 → 不拿未知换已知，留在当前
      blind     记忆没记信号、当前也不够用 → 跑体检（顺便把 RSRP 补上）
      keep      记忆有值，但当前这条刚量出来不比它差多少 → 留在当前，不切
      memo      记忆里的最优明显更快且信号不差 → 切过去（值得断那几秒）
      quick     没有可信记忆，但当前快测够用 → 先锁当前，下次来再全比
      pinned    用户用 --lock-band 亲手指定过 → 照办；
                但「这条此刻就驻留在上面、刚测出来又不行」时不再照办，
                让实测说话（换备选或跑体检）。pin 本身的时效由 device_pin() 把关。
      memo_bad     记忆里的最优就是当前这条，可它刚被实测判为不够用 → 跑体检
      memo_bad_alt 同上，但记忆里还有一条能换 → 换过去，别锁这条
    返回 (None, None) 表示信息不够，该老老实实跑一次完整体检。
    """
    # 「当前这条够用吗」。快测说够用还不够，得看**此刻**的链路：
    # 2026-10-31 17:34:21 实机，BAND 40 明明在丢 100% 的包，快测那个数字是
    # 几分钟前量的，于是照样判「当前够用」→ 17:45 锁回了这条烂频段。
    # 正在大量丢包的链路不能算够用，哪怕它刚才测出来挺快。
    #
    # 这两个值要放在「用户亲手钉的」之前算：pin 也得过这道闸门，见下面。
    dropped = cur_loss is not None and cur_loss >= GUARD_BAD_LOSS
    cur_ok = bool(quick and quick.get("ok") and serving
                  and quick.get("band") == serving and not dropped)
    cur_mbps = float(quick.get("mbps") or 0) if cur_ok else 0.0
    # ⓪ 用户亲手钉的优先。放在所有启发式之前：那些规则是「程序猜」，而
    #    --lock-band 是「人已经测过、比过了、不想再被劝」。
    #
    #    只有一种例外：这条 pin 此刻就驻留在上面、而且**正在大量丢包**。
    #    注意只看丢包，不看速度 —— 实测中 说得很清楚，他设 BAND 3
    #    恰恰是因为它「慢一点但稳」，用速度去反驳等于把他的理由当没听见：
    #    「住处的 BAND 40 网速和信号有时候检测会比 BAND 3 好，就会导致直接锁
    #     BAND 40，但 BAND 40 丢包就会用起来一会一卡，就设置在住处优先锁 BAND 3」。
    #    实机当时 BAND 3 只有 2 Mbps、延迟 72ms、丢包 0% —— 那是他要的。
    if pinned_band:
        same_as_pin = str(serving) == str(pinned_band)
        if not (same_as_pin and dropped):
            return int(serving if same_as_pin else pinned_band), "pinned"
    # ⓪c 当前驻留这条在记忆里「会周期性掉包」→ 信号再强也不留。
    #     2026-10-31 21:14 实机：BAND 40 是 -77dBm、比记忆里的 BAND 3 强 21dB，
    #     信号闸门（差超过 10dB 就留在当前）当场放行，锁定 BAND 40；
    #     21:16:31 它丢了 50% 的包。信号强度和「会不会周期性掉包」是两件
    #     独立的事，而后者只有历史样本看得出来 —— RSRP 再漂亮也证明不了。
    if (memo is not None and cur_flap and str(serving) != str(memo)):
        return int(memo), "flap_avoid"

    # ⓪d 当前这条「会周期性掉包」→ 不锁它，哪怕它此刻信号最好、速度最快。
    #     这是用户反复被锁到 BAND 40 的根子：他住处的 BAND 40 是 -77dBm、
    #     10.7 Mbps，比 BAND 3 好看，可丢包样本是 [0, 20, 100] —— 用起来一会一卡。
    #     原来的判断只在「要换到别的频段」时生效（上面 ⓪c），于是程序一边知道
    #     这条会掉包、一边照样把它锁死。
    #     「别处可用性很高」这条也顾到了：cur_flap 是按**当前位置**的历史查的
    #     （site_band_flap(st, serving)），不会因为住处的记录牵连别的办公区。
    #     没有备选就不锁，老老实实跑体检去找一条不掉的。
    if cur_flap:
        if alt_band and str(alt_band) != str(serving):
            return int(alt_band), "flap_avoid"
        return None, "flap_no_alt"
    # ⓪e 当前这条「以前锁过、锁完反倒更差」（回评攒出来的结论）→ 同样别锁它。
    #     和上面的 flap 是两条不同的证据，所以分开记、分开说：
    #     flap 是「这条链路自己会掉包」，regret 是「锁到这条上并不比锁之前好」。
    #     后者只有跟锁前基线比才看得出来 —— 锁完 0% 丢包并不等于锁对了，
    #     那可能本来就不丢（见 review_last_lock）。
    if cur_regret:
        if alt_band and str(alt_band) != str(serving):
            return int(alt_band), "regret_avoid"
        return None, "regret_no_alt"
    # ⓪b 记忆里的「最优」正好就是当前驻留的这条，而刚才的实测判它不够用。
    #     这是被「基站拥塞」咬得最准的一种情况：信号指标漂亮，速度塌了。
    #     2026-10-31 20:13 实机就长这样 —— BAND 40 是 -78dBm / SINR 21dB（比
    #     BAND 3 强 20dB），快测只有 3 Mbps、延迟 84ms，当场判「不够用」；
    #     一分钟后的决策却是「原地待了 3 分钟，锁住当前频段防止它自己乱跳」，
    #     把这条 3 Mbps 的锁死了。原因就是「记忆说它最优」压过了「一分钟前刚
    #     量出来的数字」。眼前这个数字必须赢，否则一整晚都卡在这条上。
    if (memo is not None and str(serving) == str(memo)
            and quick and quick.get("band") == serving
            and (_quick_grade(quick) == "poor" or dropped)):
        # dropped 也算：快测那个数字是几分钟前量的，此刻这条已经在丢一半以上的
        # 包了。上面「正在大量丢包的链路不能算够用」这句话，在这里同样成立 ——
        # 否则 pin 让路之后，这一条又会把它锁回去，等于白让。
        if alt_band:
            return int(alt_band), "memo_bad_alt"
        return None, "memo_bad"
    # ① 先看信号，再看速度。记忆里的速度是过去某个时刻量的，而 RSRP 差
    #    10dB 是结构性的（穿墙、距离），锁上去今天的下场就是一路掉到掉线。
    #    实测 2026-10-30：记忆说 BAND 3 有 46.8 Mbps，那天在这屋 BAND 3
    #    只有 -105dBm、当前驻留的 BAND 40 是 -77dBm，照着记忆锁 = 自残 8 分钟。
    if (memo is not None and memo_rsrp is not None and cur_rsrp is not None
            and str(serving) != str(memo)
            and memo_rsrp <= cur_rsrp - GUARD_LOCK_RSRP_DROP):
        if cur_ok:
            return int(serving), "weak_keep"
        return None, "weak"
    # ② 老记忆没有 RSRP（2026-10-30 之前只记速度），就无从判断那条频段现在
    #    能不能用。这种情况下的「切过去」是拿已知换未知 —— 实机已经吃过一次：
    #    记忆说 BAND 3 有 46.8，实际那天只有 -105dBm。当前够用就别动；
    #    不够用就跑一次体检，顺手把每条频段的 RSRP 补上。
    if (memo is not None and memo_rsrp is None
            and str(serving) != str(memo)):
        if cur_ok:
            return int(serving), "blind_keep"
        return None, "blind"
    if (memo is not None and cur_ok and str(serving) != str(memo)
            and memo_mbps and cur_mbps >= memo_mbps * GUARD_QUICK_KEEP_RATIO):
        return int(serving), "keep"
    if memo is not None:
        return int(memo), "memo"
    if cur_ok:
        return int(serving), "quick"
    return None, None


def guard_plan_lock(st, memo, serving, quick, cur_rsrp, cur_loss, pinned,
                    memo_rsrp=None):
    """把「锁哪一条」这一步的输入整理和裁决收在一处，真跑和演练共用。

    单独抽出来的原因很实际：`--guard-dry-run` 原来只印一句「未改动设备」，
    锁哪一条、凭什么，全看不到 —— 而这恰恰是演练唯一想确认的事。2026-10-31
    晚上就是这么白跑了一次演练：只看到「锁住当前频段防止它自己乱跳」，
    根本不知道它指的是 BAND 40。

    返回 (key, band, kind, memo_best)；band 为 None 表示「该跑体检再定」。
    """
    key = _site_key(st)
    memo_best = guard_memo_speed(key, memo) if memo else 0.0
    alt = guard_alt_band(st, skip=serving) if memo is not None else None
    band, kind = guard_pick_lock(
        memo, memo_best, serving, quick,
        memo_rsrp=memo_rsrp, cur_rsrp=cur_rsrp,
        pinned_band=pinned, cur_loss=cur_loss, alt_band=alt,
        cur_flap=site_band_flap(st, serving),
        cur_regret=site_band_regret(st, serving))
    return key, band, kind, memo_best


def guard_good_bands(exclude=None, days=30):
    """从**别的**位置的记忆里借几条「测出过能用」的频段，按新鲜度排好。

    为什么允许跨位置借：人到了从没来过的地方，当前位置又读不到 PCI（没注册上），
    本位置一条记录都没有 —— 这时候「等」是最差的选项。实测中 就在
    一条无覆盖的频段上等了八分钟，最后自己手动换的。
    借的依据只有一条：这条频段最近在**任何**地方真测出过数据，而不是靠信号好看。

    返回列表而不是单条：第一条试了不通，还得有下一条可试 —— 只给一条的话，
    「换一条」变成「换过一次就没了」，又回到干等。
    """
    try:
        data = load_sites()
    except (OSError, ValueError):
        return []
    cutoff = datetime.now() - timedelta(days=days)
    best = {}
    for rec in data.values():
        if not isinstance(rec, dict):
            continue
        for b, v in (rec.get("bands") or {}).items():
            if not isinstance(v, dict) or not v.get("last_ok"):
                continue
            if exclude is not None and str(b) == str(exclude):
                continue
            if (v.get("dead") or 0) >= DEAD_SKIP_MIN and not (v.get("ok") or 0):
                continue
            if band_flap(v)[0]:
                continue
            try:
                t = datetime.fromisoformat(str(v.get("last_ok")))
            except (TypeError, ValueError):
                continue
            if t < cutoff:
                continue
            # 同一条频段可能出现在多个位置，取最近那次。
            if str(b) not in best or t > best[str(b)][0]:
                best[str(b)] = (t, float(v.get("best") or 0))
    rows = sorted(best.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)
    out = []
    for b, _ in rows:
        try:
            out.append(int(b))
        except (TypeError, ValueError):
            continue
    return out


def guard_any_good_band(exclude=None, days=30):
    """第一条能借的（`guard_good_bands` 的简写）。借不到返回 None。"""
    rows = guard_good_bands(exclude=exclude, days=days)
    return rows[0] if rows else None


def guard_outage_choice(st, *, serving=None, pinned=None, tried=None):
    """**整条链路不通**的时候，要不要试着换一条频段。返回 (band, 理由)。

    这是「没信号还等很久」那条路的出口。原来的状态机只在两种情况下会动：
    锁着且连续不通（解锁）、或者解锁后仍不通（体检）。而「设备没锁、驻留在
    一条无覆盖的频段上」既不满足前者也不满足后者 —— 于是什么都不做，一直等，
    等到用户自己动手（实机 2026-11-02 18:56~19:04）。

    判断原则：只做有依据的切换，不猜。
      1. 有人指定过这条（位置对得上）→ 试它，人的判断比程序硬。
      2. 本位置的记忆里有另一条测出过能用的 → 试它。
      3. 哪儿都没有 → 从「任何地方测出过能用」的里面借一条（见 guard_any_good_band）。
      4. 连借都借不到 → 不动，如实说「没有依据，先等着」。
    tried：这一轮断网里已经试过的频段，避免在两条之间来回跳。
    """
    tried = {str(x) for x in (tried or ()) if x}

    def _now(band, why):
        if str(band) in tried:
            return None, f"BAND {band} 这一轮已经试过了（{why}），不来回折腾"
        return int(band), why

    if pinned and str(pinned) not in tried:
        return _now(pinned, "你指定过这条")
    alt = guard_alt_band(st, skip=serving)
    if alt:
        band, why = _now(alt, "这处的记忆里还有一条能用的")
        if band:
            return band, why
    # 借的时候要挑「这一轮还没试过的第一条」，不能永远返回同一条 ——
    # 否则试过它就再没候选，「换一条」变成「换过一次就没了」（模拟里踩到）。
    for borrowed in guard_good_bands(exclude=serving):
        if str(borrowed) in tried:
            continue
        return int(borrowed), "别处最近测出过能用"
    # 走到这儿说明要么真没依据，要么这一轮该试的都试过了 —— 两句话对用户
    # 意义完全不同（「我不知道换啥」vs「我换过了，等它恢复」），不能都返回空。
    return None, ("这一轮能试的都试过了" if tried else "")


def mark_site_provisional(key, band, force_best=False):
    """记一笔「这个位置只是先锁住，没实测过」：下次来要补一次完整体检。

    实测中 提的：这套记忆逻辑默认「你去过的地方」，可人也会去新地方。
    新地方第一次不该当场花 5 分钟体检 + 断网四五次 —— 先锁当前驻留的（设备
    本来就在上面，几秒），记住「这只是权宜」，下次再来才认真测。
    """
    if not key:
        return
    data = load_sites()
    real = find_site(data, key)
    _absorb_site(data, real, key)
    rec = data.setdefault(real, {"seen": 0, "bands": {}})
    _remember_cell(rec, key, band)
    rec["last"] = datetime.now().isoformat(timespec="seconds")
    # 这条频段已经真测过 → 不降级成 provisional，也不动它的 best。
    # 否则「先锁一下」会把上次完整体检的结论盖掉：实机 2026-10-30 里，
    # 236 那次 provisional 会把 350 记着的「BAND 3 最优」直接改成 BAND 40。
    # 这处只要有任何一条频段真测过，就不是「没测过的新地方」——
    # 不标 provisional，也不动它的 best。否则「先锁一下」会把上次完整体检的
    # 结论盖掉（实机 2026-10-30：236 那次 provisional 会把 350 记着的
    # 「BAND 3 最优」直接改成 BAND 40）。
    has_data = any(int((v or {}).get("ok") or 0) > 0
                   for v in (rec.get("bands") or {}).values())
    if not has_data:
        rec["provisional"] = True
    # force_best：记忆里的最优被信号闸门否掉时（它现在比当前驻留差太多），
    # 主动把最优改成当前这条，否则下次还会拿同一条来撞闸门。
    if band and (not has_data or force_best):
        rec["best"] = int(band)
    save_sites(data)


def _band_slot(data, key, band):
    """取（必要时新建）「某处某条频段」的记忆槽，顺手把小区记进去。

    守护和半自动（体检 / --apply-plan）共用 sites.json，但守护原来只在
    「这条记录已经存在」时才写。同一张桌子换个频段就是另一个 PCI（锁 BAND 3
    是 350、auto 驻留 BAND 40 是 236），于是守护在一张没建过档的 PCI 下采到的
    信号和丢包全被丢掉 —— 实测中：「守护模式和半自动的那个模式要
    共享一套记忆」，这就是没共享的那一半。

    新建的记录故意不写 last / seen / ok：它只是「我来过、看到了这些」，
    不等于「测过」。site_memory_state 会判 stale，选带时照旧走 blind 分支，
    不会拿它假装这里有可信记忆。
    """
    if not key or band is None:
        return None
    real = find_site(data, key)
    _absorb_site(data, real, key)
    rec = data.setdefault(real, {"seen": 0, "bands": {}})
    if not rec.get("last"):
        # 「来过」和「测过」是两件事，必须分开记：last 只给真测过的写
        # （site_memory_state 拿它判断这份记忆能不能信），observed 只说明
        # 守护在这儿看到过信号。没有它，一条纯观测记录在别处看起来和
        # 「坏了的老记录」一模一样，排错时根本分不清。
        rec.setdefault("observed", datetime.now().isoformat(timespec="seconds"))
    _remember_cell(rec, key, band)
    bands = rec.setdefault("bands", {})
    return bands.setdefault(str(int(band)), {})


def _guard_confirmed(rec):
    """这个位置，守护「锁住并守住了」够次数的频段。返回 (频段, 次数) 或 None。

    只看守护自己攒的账（`confirmed`），不看体检数据：这是「没测过速度，但确实
    在这儿守住了两回」的那一档，专门用来兜住 provisional 永远升不了级的问题。
    多条都够次数时取次数最多的；一样多就取小的，保证同样的记录每次算出同一个答案。
    """
    best = None
    for b, s in ((rec or {}).get("confirmed") or {}).items():
        try:
            n = int((s or {}).get("n") or 0)
            band = int(b)
        except (TypeError, ValueError):
            continue
        if n < GUARD_CONFIRM_LOCKS:
            continue
        if best is None or n > best[1] or (n == best[1] and band < best[0]):
            best = (band, n)
    return best


def note_guard_confirm(key, band, loss=None, avg=None, rsrp=None):
    """守护锁住一条频段、守住了十分钟 → 替这个位置记一次「这条能行」。

    返回 (累计次数, 是否刚升成正式记忆, 没升的原因)。

    为什么不写速度、也不动 ok：这里没有测速，只有「锁住了没垮」。把速度格留空、
    ok 留 0，这条记录在数据上仍然是「没实测过」—— 将来真跑一次体检，该测的
    还是会测；但它不再是「什么都不知道」，够两次就能当记忆用。

    为什么不干脆凭一次就升级：一次可能是运气（那会儿基站不挤、邻居没下载），
    两次落在不同时间点上还成立，才像「这地方这频段就是能用」。

    升不升级还要过 band_flap：样本是守护自己写进来的（note_band_loss），
    要是这条在本地记着周期性掉包，攒够次数也不升 —— 否则等于亲手把一条
    「信号好看但会掉包」的频段封成「这个位置的最优」。
    """
    if not key or band is None:
        return 0, False, ""
    try:
        data = load_sites()
        rec = data.get(find_site(data, key))
        if not rec:
            # 连记录都没有：不凭一次「锁住了」就凭空造一条位置记忆。
            # 记录本身由 mark_site_provisional / _band_slot 建，这里只管升级。
            return 0, False, ""
        conf = rec.setdefault("confirmed", {})
        slot = conf.setdefault(str(int(band)), {"n": 0})
        slot["n"] = int(slot.get("n") or 0) + 1
        slot["last"] = datetime.now().isoformat(timespec="seconds")
        for name, v in (("loss", loss), ("avg", avg), ("rsrp", rsrp)):
            num = _num(v)
            if num is not None:
                slot[name] = int(round(num))
        if rsrp is not None:
            # 这条正是当前驻留的，不存在「把别人的 RSRP 记到它名下」那个坑
            # （见 note_band_rsrp_at）。写进去下次信号闸门才有据可依。
            v = _band_slot(data, key, band)
            if isinstance(v, dict):
                v["rsrp"] = v["rsrp_med"] = int(round(float(rsrp)))
                v["rsrp_at"] = slot["last"]
        n = slot["n"]
        if n < GUARD_CONFIRM_LOCKS or not rec.get("provisional"):
            save_sites(data)
            return n, False, ""
        v = (rec.get("bands") or {}).get(str(int(band))) or {}
        if band_flap(v)[0] or (v.get("dead") or 0) >= DEAD_SKIP_MIN:
            save_sites(data)
            return n, False, (f"不升成正式记忆：记忆里 BAND {band} 在本地"
                              f"会周期性掉包，守着好看不代表能用")
        rec.pop("provisional", None)
        rec["best"] = int(band)
        rec["last"] = slot["last"]
        rec["promoted_at"] = slot["last"]
        save_sites(data)
        return n, True, ""
    except (TypeError, ValueError):
        return 0, False, ""


def note_decision(key, entry, only_if_empty=False):
    """记一笔「这次锁哪条、凭什么、锁前什么样」，留给十分钟后回评。

    为什么要单独存一份，而不是只在运行日志里打一行：

      · 手机上 `log_keep` 是 1，历史只留最后一轮 —— 用户想知道「刚才为什么
        没锁 BAND 40」，翻日志是翻不到的（2026-11-02 就是这么问起来的）。
      · 日志是散文，回答不了「这条频段我锁过几次、哪几次是白锁的」。要攒出
        这个结论，得先有结构化的账本。

    存在独立的 decisions.json 里（按位置键分组），每处只留最近 DECISIONS_KEEP
    条：这是个账本，不是档案，旧的不该无限长。为什么不塞进 sites.json ——
    两个线程读改写同一个文件会互相覆盖，见 DECISIONS 那里的注释。

    返回：写成功 → 那条账目；没写 → 一句原因（调用方可以原样打给人看）。
    """
    if not key and entry:
        # 读不到 PCI 时（断网/没注册上）也要记。原来这里直接返回「没有位置键」，
        # 于是最需要留痕的那几分钟恰好一页空白 —— 实测中 点开
        # 「锁频段的依据」想看那八分钟发生了什么，什么也看不到。
        # 归到一个占位键下：账本是「这次为什么锁这条」，不是位置记忆，
        # 混在一处不影响任何判断，只是条目按时间排在一起。
        key = "?-" + str((entry or {}).get("op") or "未知")
    if not key or not entry:
        return "没有位置键"
    with _DEC_LOCK:
        data = load_decisions()
        rows = data.setdefault(key, [])
        if only_if_empty and rows:
            return f"已有 {len(rows)} 条账目"
        rows.append(entry)
        del rows[:-DECISIONS_KEEP]
        if not save_decisions(data):
            return "写不进去（磁盘）"
        return entry


def _save_decisions_with_rows(key, rows):
    """把某处改过的账目写回去。rows 是刚就地改过的那一份。"""
    with _DEC_LOCK:
        data = load_decisions()
        data[key] = rows
        return save_decisions(data)


def review_last_lock(key, band, loss=None, avg=None, rsrp=None, now=None):
    """锁上十来分钟之后回一句：这次锁对了没。

    返回 (结论, 锁前, 锁后, 有没有真的换了频段)。
    结论 ∈ {better, same, worse, unknown}。

    为什么非得跟锁前比：锁完看到 0% 丢包、70ms 延迟，那可能本来就这样。
    实测中 要的就是这一步 ——「现在只记了锁住了没垮，没记锁对了没」。

    比什么、怎么算：
      · 先看丢包（差 REVIEW_LOSS_GAIN 个百分点以上才算变了）；
      · 丢包分不出高下再看延迟（差 25% 以上才算）；
      · 两个都差不多就是 same —— 「没变差」也是一条有用的结论，不该被
        硬说成「变好了」。
    没有锁前基线（老版本留下的账本）就返回 unknown，不猜。

    第四个返回值（有没有换频段）很要紧：锁的如果本来就是当前驻留那条，
    前后带宽没变过，这十分钟里的差别只是环境自己在动 —— 把它记成「锁对了」
    或「锁错了」都是栽赃。只有真换过频段的那次才算数。

    为什么按频段找、不按位置键找：换频段必然换 PCI，于是「记的时候在某处、
    评的时候在另一处」。按 key 查会查不到，账就永远评不上（实机 2026-11-02
    就这么空转了一次）。这里扫全部位置，取最近一条同频段、还没评过、有基线的。
    """
    if band is None:
        return "unknown", None, None, False
    post = {}
    for name, v in (("loss", loss), ("avg", avg), ("rsrp", rsrp)):
        num = _num(v)
        if num is not None:
            post[name] = int(round(num))
    try:
        book = load_decisions()
        owner, hit = None, None
        for k, rows in book.items():
            for e in rows or []:
                if not isinstance(e, dict) or str(e.get("band")) != str(int(band)):
                    continue
                if str(e.get("kind") or "").startswith("unlock"):
                    continue    # 解锁也记账，但它不是「锁到这条上」，不能拿来评
                if e.get("out"):
                    continue    # 评过了
                if not isinstance(e.get("pre"), dict) or not e.get("pre"):
                    # 没有锁前基线（接手记录 / 老版本账本）：评不了，跳过。
                    continue
                if hit is None or str(e.get("t") or "") > str(hit.get("t") or ""):
                    owner, hit = k, e
        if hit is None or owner is None:
            return "unknown", None, None, False
        rows = book[owner]
        key = owner if not key else key     # 结论落到「记它的那处」的频段槽里
        pre = hit["pre"]
        verdict = lock_verdict(pre, post)
        stamp = (now or datetime.now()).isoformat(timespec="seconds")
        before = _num(hit.get("serving"))
        switched = before is not None and str(int(before)) != str(int(band))
        hit["out"] = dict(post, verdict=verdict, t=stamp, switched=switched)
        if not switched:
            # 锁的就是当前这条，前后没换过频段 —— 结论照记，但不计入
            # 「这条锁得好/不好」的账，否则纯粹是在给噪声记账。
            _save_decisions_with_rows(owner, rows)
            return verdict, pre, post, False
        _save_decisions_with_rows(owner, rows)
        # 结论落到频段槽里，选带时能用到（site_band_regret）。这一步写的是
        # sites.json，和账本分开：账本丢了不影响选带，选带那份丢了也不影响复盘。
        data = load_sites()
        v = _band_slot(data, key, band)
        if isinstance(v, dict):
            rv = v.setdefault("review", {"good": 0, "bad": 0})
            if verdict == "better":
                rv["good"] = int(rv.get("good") or 0) + 1
            elif verdict == "worse":
                rv["bad"] = int(rv.get("bad") or 0) + 1
            rv["last"] = stamp
            rv["last_verdict"] = verdict
        save_sites(data)
        return verdict, pre, post, True
    except (OSError, TypeError, ValueError):
        return "unknown", None, None, False


def lock_verdict(pre, post):
    """锁前 vs 锁后 → better / same / worse。纯函数，方便单测。"""
    pl, ql = _num((pre or {}).get("loss")), _num((post or {}).get("loss"))
    if pl is not None and ql is not None:
        if ql <= pl - REVIEW_LOSS_GAIN:
            return "better"
        if ql >= pl + REVIEW_LOSS_GAIN:
            return "worse"
    pa, qa = _num((pre or {}).get("avg")), _num((post or {}).get("avg"))
    if pa and qa:
        ratio = (qa - pa) / float(pa)
        if ratio <= -REVIEW_LAT_GAIN:
            return "better"
        if ratio >= REVIEW_LAT_GAIN:
            return "worse"
    return "same"


def review_line(verdict, pre, post):
    """把回评结论说成人话，一行。"""
    def _fmt(d, key, unit):
        v = (d or {}).get(key)
        return "—" if v is None else f"{v}{unit}"

    before = f"丢包 {_fmt(pre, 'loss', '%')} 延迟 {_fmt(pre, 'avg', 'ms')}"
    after = f"丢包 {_fmt(post, 'loss', '%')} 延迟 {_fmt(post, 'avg', 'ms')}"
    word = {"better": "比锁之前好", "worse": "比锁之前差",
            "same": "跟锁之前差不多", "unknown": "说不清"}[verdict]
    if verdict == "unknown":
        return f"锁后回评：{word}（没有锁前基线，不猜）"
    return f"锁后回评：{word}（{before} → {after}）"


def decision_line(e):
    """一笔账目压成一行，给人看（命令行和日志共用）。"""
    if not isinstance(e, dict):
        return ""
    t = str(e.get("t") or "")[11:16]
    band = e.get("band")
    head = (f"{t}  BAND {band}  ← {e.get('kind')}"
            f"（{_kind_text(e.get('kind'))}）") if band is not None else \
           f"{t}  {e.get('kind')}"
    bits = []
    pre = e.get("pre") or {}
    if pre:
        bits.append(f"锁前 丢包 {pre.get('loss', '—')}% 延迟 {pre.get('avg', '—')}ms")
    if e.get("serving") is not None:
        bits.append(f"当时驻留 {e.get('serving')}")
    q = e.get("quick") or {}
    if q.get("mbps") is not None:
        bits.append(f"快测 {float(q['mbps']):.0f}Mbps/{q.get('loss')}%")
    out = e.get("out") or {}
    if out:
        word = {"better": "比锁前好", "worse": "比锁前差",
                "same": "跟锁前差不多"}.get(out.get("verdict"), out.get("verdict"))
        if out.get("switched") is False:
            word += "·没换频段"
        bits.append(f"回评 {word}（丢包 {out.get('loss', '—')}%）")
    return head + ("｜" + "、".join(bits) if bits else "")


def decisions_report(n=5, site=None):
    """把「最近为什么锁这条」排成人能读的几行。返回行列表（方便单测）。

    这份账是给用户复盘用的：手机上运行日志只留最后一轮（log_keep），而
    「刚才为什么没锁 BAND 40」「这条锁过几次、哪几次白锁了」这类问题，
    只能靠结构化的账本回答。
    """
    lines = []
    data = load_decisions()
    if not data:
        return ["还没有账目：守护下一次锁定开始记。"]
    try:
        sites = load_sites()
    except (OSError, ValueError):  # 名字读不到不影响看账
        sites = {}
    keys = [site] if site else list(data.keys())
    shown = 0
    for key in keys:
        rec = sites.get(find_site(sites, key) if sites else key) or {}
        rows = [e for e in (data.get(key) or []) if isinstance(e, dict)]
        if not rows:
            continue
        shown += 1
        lines.append(f"{key}")
        for e in sorted(rows, key=lambda x: str(x.get("t") or ""),
                        reverse=True)[:n]:
            lines.append("  " + decision_line(e))
        # 回评账：哪条锁过之后变好、哪条变差
        rv = []
        for band, v in (rec.get("bands") or {}).items():  # 回评累计还在 sites.json
            if not isinstance(v, dict):
                continue
            bad, good = band_regret(v)
            if good or bad:
                rv.append(f"BAND {band} 好{good}/差{bad}")
        if rv:
            lines.append("  回评累计：" + "；".join(sorted(rv)))
    if not shown:
        lines.append("这个位置还没有账目：守护下一次锁定开始记。" if site
                     else "还没有账目：守护下一次锁定开始记。")
    return lines


def guard_known_settle(st, serving, base):
    """认得的位置可以少等一会儿。返回 (该等多久, 说明)。

    只认两种情况同时成立：这地方被守护守住过 GUARD_CONFIRM_LOCKS 次，而且
    **当前驻留的正是那条**。要切到别的频段去锁是另一个场景，照旧等满。
    记忆本身还得是 fresh —— 一个月前的「守得住」不算数。
    """
    if base <= GUARD_KNOWN_SETTLE:
        return base, ""
    key = _site_key(st)
    if not key:
        return base, ""
    data = load_sites()
    rec = data.get(find_site(data, key)) or {}
    got = _guard_confirmed(rec)
    if not got:
        return base, ""
    band, n = got
    if str(band) != str(serving):
        return base, ""
    if site_memory_state(rec) != "fresh":
        return base, ""
    return GUARD_KNOWN_SETTLE, (f"这地方守过 BAND {band} {n} 次、现在就驻留在它上面，"
                                f"站够 {max(1, int(GUARD_KNOWN_SETTLE / 60))} 分钟就锁")



def note_band_rsrp(key, band, rsrp):
    """把「此刻这条频段的信号」补进记忆，让下次的信号闸门有据可依。

    为什么单独写一个：老记忆（2026-10-30 之前）只记速度和丢包，没有 RSRP。
    守护因此在 blind 分支里只能跑完整体检 —— 实机 21:56 为此白等 6 分钟、
    断了 7 次网。其实锁定那一刻 RSRP 就在手上（采样里读的），顺手写进去，
    下次同一张桌子再坐满 10 分钟，闸门就能直接比较，不用再测。
    """
    if not key or band is None or rsrp is None:
        return
    try:
        data = load_sites()
        v = _band_slot(data, key, band)
        if not isinstance(v, dict):
            return
        v["rsrp"] = int(round(float(rsrp)))
        v["rsrp_med"] = int(round(float(rsrp)))
        v["rsrp_at"] = datetime.now().isoformat(timespec="seconds")
        save_sites(data)
    except (TypeError, ValueError):
        return


def note_band_rsrp_at(key, band, serving, rsrp):
    """只有「要锁的这条」正是刚才读信号的那条，才把 RSRP 写进记忆。

    守护循环里那个 rsrp 是**当前驻留频段**的信号。切到别的频段去锁的时候，
    这个数字属于别人 —— 记到目标频段名下就是造假，而且会反过来污染下一次
    的信号闸门：BAND 3 实际是 -100dBm，被写成 BAND 40 的 -77dBm，下次就
    会判「记忆里那条信号很好」而锁上去，锁上去才发现根本不通。

    缺的那次采样 5 分钟后的定时采样（GUARD_RSRP_NOTE_SEC）会补，不会丢。
    """
    if serving is None or band is None or str(band) != str(serving):
        return
    note_band_rsrp(key, band, rsrp)


def site_verify_due(key, now=None, hours=GUARD_VERIFY_HOURS):
    """这个位置该不该「去验一次记忆里的最优」。

    只在这种情况问：记忆里那条最优没记过信号 —— 靠猜永远猜不出它现在值不值得锁，
    只能切过去实测一次。限频是为了别把「验证」变成用户最烦的周期性测速。
    """
    if not key:
        return False
    data = load_sites()
    rec = data.get(find_site(data, key)) or {}
    ts = rec.get("verified_at")
    if not ts:
        return True
    try:
        last = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return True
    return (now or datetime.now()) - last >= timedelta(hours=hours)


def mark_site_verified(key):
    """记下「这个位置的最优刚验过」，给 site_verify_due 限频用。"""
    if not key:
        return
    data = load_sites()
    rec = data.get(find_site(data, key))
    if not rec:
        return
    rec["verified_at"] = datetime.now().isoformat(timespec="seconds")
    save_sites(data)


def site_name(key):
    """这个位置叫什么名字（用户用 `--here` 起的）。没命名过返回空串。

    名字只是给人和日志看的标签，不参与选带决策 —— 位置本身照旧靠「见过哪些
    小区」来认。分开的理由：让「我记得这里是住处」变成一句可核对的话，
    而不是藏在 PCI 数字里让人猜。
    """
    if not key:
        return ""
    data = load_sites()
    rec = data.get(find_site(data, key)) or {}
    return str(rec.get("name") or "").strip()


def describe_site_memory(st):
    """一句话说清「这是什么地方、记忆里怎么说」—— 给 `mifi --status` 用。

    用户反复问的其实是同一件事：「守护到底有没有用上记忆」。以前这条信息只
    活在日志和 sites.json 里，`--status` 只看得到设备、看不到记忆，于是每次
    都得由人去翻文件核对。把记忆摊在状态里，他自己一眼就能对：这里叫什么、
    记忆里的最优是哪条、多久前测的、工具一共见过这处的几副面孔。

    「还没有记忆」也要说出来 —— 那不是「没东西可显示」，而是一句结论：
    这地方工具还没数，下次坐久了会跑一次体检。
    """
    key = _site_key(st)
    if not key:
        return []
    data = load_sites()
    real = find_site(data, key)
    rec = data.get(real) or {}
    place = str(rec.get("name") or "").strip()
    head = place or real
    cells = [str(c.get("pci")) for c in (rec.get("cells") or []) if c.get("pci")]

    state = site_memory_state(rec)
    best = rec.get("best")
    faces = ""
    if cells:
        more = "…" if len(cells) > 6 else ""
        faces = f"      这处见过 {len(cells)} 个小区：{'/'.join(cells[:6])}{more}"

    if not best:
        head_line = f"位置：{head}｜这处还没测过（坐久了会自动跑一次体检）"
    elif state == "stale":
        head_line = (f"位置：{head}｜记忆已过期"
                     f"（上次 {_fmt_age(site_age_hours(rec))}），下次会全部重测")
    else:
        # 同 guard_memory_band：「测过」和「守住过」必须分开写。守护攒的
        # confirmed 记录里没有速度，说成「测过 N 次」会让人以为这地方全测过了。
        if _site_measured(rec):
            how = f"{_fmt_age(site_age_hours(rec))}测过 {int(rec.get('seen') or 0)} 次"
            tail = ""
        else:
            got = _guard_confirmed(rec)
            how = f"守护守住过 {got[1]} 次、没跑过体检" if got else "没有实测数据"
            tail = (f"，站够 {max(1, int(GUARD_KNOWN_SETTLE / 60))} 分钟就锁"
                    if got else "")
        head_line = (f"位置：{head}｜记忆里最优 BAND {int(best)}"
                     f"（{how}{tail}）")
        # 记忆里的「最优」会周期性掉包时，守护不会锁它，而是换记忆里另一条稳的。
        # 状态行得跟着说同一件事，否则用户看到「最优 BAND 40」、守护却锁了 3，
        # 只会以为工具又没听话。（2026-10-31 22:07 实机：BAND 40 当场测出
        # 23 Mbps / 0% 丢包，守护仍然锁 BAND 3 —— 因为记忆里 40 会周期性掉包。）
        v = (rec.get("bands") or {}).get(str(int(best))) or {}
        if band_flap(v)[0]:
            alt = guard_alt_band(st, skip=int(best))
            tail = (f"｜但它会周期性掉包，守护会改用稳定的 BAND {alt}"
                    if alt else "｜但它会周期性掉包")
            head_line += tail
    out = [head_line]
    if faces:
        out.append(faces)
    if not place and best:
        out.append("      还没起名：mifi --here 住处（只是加个标签，不影响选带）")
    return out


def note_band_loss(key, band, loss, avg=None, rsrp=None):
    """守护采样里出现高丢包 → 记进这个位置该频段的滚动样本。

    为什么必须留痕：band_flap 只认样本，而样本原来只有体检才写。于是
    BAND 40 昨晚体检 80% 丢包、今天 12:21 又 40% 丢包，记忆里却一直是
    「40.7 Mbps、干净」，守护每次来都优先锁它 —— 用户的原话是
    「BAND 40 丢包很严重，放我住处的次优项」。丢包不留痕，就永远选不出次优。
    """
    if not key or band is None:
        return
    try:
        data = load_sites()
        v = _band_slot(data, key, band)
        if not isinstance(v, dict):
            return
        samples = v.setdefault("samples", [])
        now = datetime.now()
        r = _num(rsrp)
        # 速度那一格写 None（不是 0.0）：守护只 ping 不测速，这里没有速度可言。
        # 写 0.0 等于在记忆里造一条「0 Mbps」的假数据，日后谁按速度统计它就被带偏。
        samples.append([now.hour, None, int(round(float(loss or 0))),
                        int(_num(avg) or 0), int(r) if r is not None else None])
        del samples[:-SITE_SAMPLES]
        save_sites(data)
    except (TypeError, ValueError):
        return


def guard_memory_band(st):
    """久坐时：能不能凭「这个位置的历史」直接锁定？返回 (频段 or None, 说明)。

    为什么要有这一步：守护原来是「久坐 → 跑一次 5 分钟体检 → 锁」。用户体感
    （2026-10-30）是「手动测一次锁上比设备自动好，但守护用起来反而像厂商的
    自动」—— 因为它每次都从头重新决策，还要断好几次网。可这个位置最优是哪条
    频段，sites.json 里早就写着，够新就直接用。

    四条否决条件，任一条不满足就老老实实重测：
      读不到 PCI / 这个位置没测过 / 记忆不是 fresh（72 小时内）/
      记忆里的最优频段现在没覆盖、或历史上会周期性掉包。
    """
    key = _site_key(st)
    if not key:
        return None, "读不到 PCI，没法查这个位置"
    data = load_sites()
    rec = data.get(find_site(data, key))
    if not rec:
        return None, GUARD_NEW_SITE
    if rec.get("provisional") and not _site_measured(rec):
        # 上次只是「先锁住别乱跳」，没真测过：这次补一次完整体检再谈最优。
        # 注意要带「且没有实测数据」—— 老版本会留下「标记为真、数据其实有」
        # 的记录（实机 2026-10-30 的 236-CMCC），那种记录必须当成有记忆，
        # 否则永远用不上、也永远不会去补测，自己锁死自己。
        return None, "这个位置只做过快速判断（没实测速度）"
    state = site_memory_state(rec)
    if state != "fresh":
        return None, ("位置记忆已过期" if state == "stale"
                      else "位置记忆有点旧（超过 72 小时）")
    band = rec.get("best")
    try:
        band = int(band) if band is not None else None
    except (TypeError, ValueError):
        band = None
    if band is None:
        return None, "记忆里没有最优频段"
    v = (rec.get("bands") or {}).get(str(band)) or {}
    if (v.get("dead") or 0) >= DEAD_SKIP_MIN and not (v.get("ok") or 0):
        return None, f"记忆里的最优 BAND {band} 现在没覆盖"
    if band_flap(v)[0]:
        # 最优会周期性掉包 → 退而求其次，用记忆里另一条稳的。
        # 直接跑体检要断好几次网、耗 5 分钟；而「哪条稳」记忆里早写着。
        # 2026-10-31 21:36 实机就是这么白跑了一次体检：明知道 BAND 40 会掉包，
        # 也知道 BAND 3 不会，却还是从头把 6 条频段各测一遍。
        alt = guard_alt_band(st, skip=band)
        if alt:
            return alt, (f"记忆里的最优 BAND {band} 会周期性掉包，"
                         f"改用稳的 BAND {alt}")
        return None, f"记忆里的最优 BAND {band} 会周期性掉包"
    nm = str(rec.get("name") or "").strip()
    where = f"「{nm}」" if nm else "这个位置"
    # 「测过 N 次」和「守住过 N 次」是两件事，不能混着说：前者是真跑过体检、
    # 每条频段都量过速度；后者只是「在这儿锁住、十分钟没垮」。写成前者就是撒谎，
    # 而且会让人以为「这地方已经全测过了、不用再测」。
    if _site_measured(rec):
        how = f"{_fmt_age(site_age_hours(rec))}测过 {int(rec.get('seen') or 0)} 次"
    else:
        got = _guard_confirmed(rec)
        how = (f"守护守住过 {got[1]} 次（没跑过完整体检）" if got
               else f"{_fmt_age(site_age_hours(rec))}没测过")
    return band, f"{where}{how}，最优 BAND {band}"


def _guard_lock_band(dev, band, trace=None):
    print(f"      锁到 BAND {band}（不重新测，几乎不断网）…", flush=True)
    try:
        dev.set_band(band)
    except DeviceUnreachable:
        # 射频重启导致请求本身断掉，指令通常已经送达
        pass
    except Exception as e:  # noqa: BLE001
        print(f"      [!] 锁定失败：{type(e).__name__}: {e}", flush=True)
        return False
    ok = wait_device_back(dev, timeout=60)
    print(f"      已锁定 BAND {band}" if ok
          else "      [!] 后台没回来，稍后自动重试", flush=True)
    if ok and trace is not None:
        # 只在真锁上了才记账：「决定了但没锁成」不算一次决策，记下来会让
        # 回评去比一条根本没生效的锁。
        e = dict(trace)
        e["band"] = int(band)
        e.setdefault("t", datetime.now().isoformat(timespec="seconds"))
        note_decision(e.pop("_key", ""), e)
    return ok


def _guard_lock_current(dev, serving):
    if not serving:
        print("      [!] 读不到当前驻留的频段，这次先不锁", flush=True)
        return False
    print("      锁当前驻留的频段（--guard-fast，跳过选优）…", flush=True)
    return _guard_lock_band(dev, serving)


def _guard_plan_band(prefer=None):
    """从体检留下的 plan.json 里取出「该锁哪条频段」。

    守护的目的是「把最优那条锁住，别让设备自己乱跳」，所以体检结论哪怕是
    「保持现状、没必要换」，也得把当前这条锁上 —— 不锁等于什么都没做。

    实机 2026-10-30 22:53：原地坐满 10 分钟 → 跑体检 7.5 分钟、断网 6 次 →
    结论「BAND 3 最优（22 Mbps），按 auto 保持现状」→ 于是**一条都没锁**，
    设备继续 auto，用户只能自己手动锁。这条路径原来只打印不动作。

    prefer：当前驻留频段。数据不可信（波动太大/测速源抽风）时就用它 ——
    0.6.9 的教训是「拿不可信的数据切频段」，这里同理：宁可锁现在这条，
    也别照着过山车一样的数字跳。

    **但「当前这条体检判了无信号」是硬结论，不受可信度影响。**
    实机 2026-11-02 18:55：设备锁在 BAND 41，体检里 41 明确是
    「无信号（当地没这个频段）」，可 BAND 3 那一轮单轮 [27.0, 23.4, 2.8]
    波动太大被判 reliable=False，于是退回 prefer=41 —— 把设备锁回了一条
    刚刚被自己判死的频段，18:56:41 又是丢包 100%。数据可不可信是一回事，
    「这条有没有覆盖」是另一回事，后者只认 RSRP，不认速度。

    返回 None 表示连退路都没有，调用方应如实说「没锁上」。
    """
    try:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return prefer
    rows = plan.get("lte") or []
    dead = {r.get("band") for r in rows
            if not r.get("ok") and r.get("reason") == "no_signal"}
    if plan.get("reliable") is False:
        if prefer and str(prefer) not in {str(b) for b in dead}:
            return prefer
        # 当前这条没覆盖 → 从有信号的里面挑一条。这里不排序：数据不可信时
        # 排序就是在噪声上做决定，而「有没有信号」不需要精度。
        for r in rows:
            if r.get("ok"):
                return r.get("band")
        return None if prefer else prefer
    usable = bands_worth_comparing(rows)
    if usable:
        usable.sort(key=lambda r: (-(r.get("mbps") or 0), r.get("avg") or 999))
        return usable[0].get("band") or prefer
    # 一个都没测出来（全无覆盖）：退回落盘的目标频段；那也可能是 0（自动）。
    band = (plan.get("target") or {}).get("band")
    if band:
        return band
    if prefer and str(prefer) in {str(b) for b in dead}:
        return None                     # 别把设备锁在没有覆盖的频段上
    return prefer


def _guard_verify_memo(dev, memo, serving, key, speed_url, host, trace=None):
    """切到记忆里那条最优，实地验一次；不够用就切回原频段。

    实测中：「在一个熟悉的地方要优先锁历史记录里面最优的，最近这几次
    锁定看起来没有验证记忆」。以前只有两条路 —— 记忆没记信号就「不敢锁它」，
    然后锁当前这条。结果是记忆里的最优永远上不了台面，也永远攒不到信号，
    一直 blind 下去（实机一整早都是这样，最后锁在 BAND 40，而用户说
    BAND 40 丢包严重、只是次优项）。

    返回 (最终停在哪个频段, 记忆那条到底顶不顶用)。顶用 → 调用方直接认定已锁；
    不顶用 → 调用方按原逻辑往下走（当前这条够用就锁它，不够用才跑体检）。
    """
    print(f"      → 记忆里的最优是 BAND {memo}，但从来没记过信号 —— 与其猜，"
          f"切过去实测一次（两次切换，约 2~3 分钟断网）", flush=True)
    # 这一步也是一次真锁定，得进账本 —— 实测中 报「中午锁过 BAND 39
    # 但账本里没有」，走的就是这条路（记忆里那条没记过信号，切过去实测）。
    _t = dict(trace) if trace else None
    if _t is not None:
        _t["kind"] = "verify"
    if not _guard_lock_band(dev, memo, trace=_t):
        print(f"      [!] BAND {memo} 切不过去，这次还是按原逻辑处理", flush=True)
        return None, False
    try:
        st = dev.get_state()
    except Exception:  # noqa: BLE001
        st = {}
    rsrp = _num((st.get("signal") or {}).get("rsrp"))
    if speed_url is None:
        speed_url = probe.resolve_speed_url(None, verbose=False)
    quick = guard_quick_probe(speed_url, host)
    quick["band"] = memo
    mbps = float(quick.get("mbps") or 0)
    ok = bool(quick.get("ok"))
    sig = f"{rsrp:.0f}dBm" if rsrp is not None else "—"
    print(f"      → BAND {memo}：{mbps:.0f} Mbps ｜ 延迟 {quick.get('avg')}ms ｜ "
          f"丢包 {quick.get('loss')}% ｜ RSRP {sig} → "
          f"{'够用，就留在这一条' if ok else '不够用，切回去'}", flush=True)
    record_site(key, [{"band": memo, "ok": ok, "mbps": mbps,
                       "avg": quick.get("avg"), "loss": quick.get("loss"),
                       "rsrp": rsrp}])
    mark_site_verified(key)
    if ok:
        return memo, True
    if serving and str(serving) != str(memo):
        _guard_lock_band(dev, serving)
        return serving, False
    return None, False


def _measure_eta():
    """估一下这次体检要多久，给等待的人一个准数。

    原来写死「约 5 分钟」，实机 2026-11-02 18:47 那轮跑了 8 分 9 秒 ——
    等的人看着「5 分钟」早就该好了，会以为卡死。按上次测过的频段数算，
    每段 3 轮约 85 秒、没覆盖的会提前退出，所以取 70 秒/段。
    """
    try:
        rows = json.loads(PLAN.read_text(encoding="utf-8")).get("lte") or []
        n = len(rows)
    except (OSError, ValueError, AttributeError):
        n = 0
    if not n:
        return "约 5~8 分钟"
    return f"约 {max(2, round(n * 70 / 60))} 分钟（{n} 个频段）"


def _guard_measure(args, prefer=None, *, net_down=False):
    """跑一次完整体检，返回它选出的最优频段（读不到就是 None）。

    另起子进程，会自己登录设备 —— 和守护进程的长连接互相顶掉（KICKOFF），
    所以调用方必须先把 dev 放掉，跑完重连。

    net_down=True（此刻整条链路不通）时**直接拒绝**，一次都不跑。
    体检的每一个频段都要靠下载测速，链路断着的时候六段全是 0，结论必然是
    「保持现状」；唯一确定产出的是 5 分钟断网和四次射频重启。实机
    2026-11-02 18:47:46 → 18:55:55 就是这么白跑的，回来还是丢包 100%。
    """
    if net_down:
        print("      [!] 现在整条链路不通，体检测出来会全是 0（唯一确定的结果是"
              "再断网几分钟）—— 这次不测，等网络恢复", flush=True)
        return None
    print(f"      跑完整体检选最优（会断网几次，{_measure_eta()}）…", flush=True)
    code = run_cycle(args, lte_only=True)
    band = _guard_plan_band(prefer) if code == 0 else prefer
    print(f"      体检结束（exit={code}），重新接管设备…", flush=True)
    return band


def _install_stop_signals():
    """让守护能被 SIGTERM 干净地停下（mifi --stop、kill）。

    Ctrl+C 发的是 SIGINT，本来就能停；可手机上那个组合键并不可靠 ——
    实机 2026-10-30 19:45 用户按了没反应，只能强杀整个 Termux 才停掉。
    所以另开一条路：SIGTERM 也转成 KeyboardInterrupt，走同一条收尾路径
    （打印「已停止」、放掉 wake lock），不必再牺牲终端。
    """
    def _raise(_signum, _frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _raise)
        except (ValueError, OSError):
            pass                        # 非主线程装不了，忽略


def _acquire_wakelock():
    """尽量别让系统把 Termux 冻住（能拿就拿，拿不到不影响守护运行）。

    症状：切到别的应用几分钟再回来，屏幕上还停在切走前那一行 —— 这期间一拍都没
    采。这是 Android 的后台管理，不是代码能绕过的 —— 但 Termux 自带
    `termux-wake-lock`（就是通知栏那个 Acquire wakelock 按钮的等价物），有就替
    你按一下，省得忘了。
    """
    exe = shutil.which("termux-wake-lock")
    if not exe:
        return False
    try:
        subprocess.run([exe], timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _release_wakelock():
    exe = shutil.which("termux-wake-unlock")
    if not exe:
        return
    try:
        subprocess.run([exe], timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def guard_still_reset(*, pci_changed, moving, band_changed=False,
                      strong_moving=None):
    """这一拍要不要把「静止计时」清零。

    只在**位置信息真的变了**时清：人在动、且 PCI 确实换了。**不能写 if moving** ——
    moving 在最后一次换小区之后还会保持整整 GUARD_MOVE_WINDOW（300s），
    每一拍都清零等于把起点一直往后推，把「坐满 10 分钟」拖成最多 15 分钟。
    实机 2026-10-30：19:30 人就坐下了，19:34:55 才开始计时，19:44:29 才锁上。

    换频段（band_changed）也不清：那是设备自己重选，人还坐在原处。实机 21:46
    换了 BAND 3→40，计时被清零白等 2 分钟；要是设备爱换，就会永远坐不满 10 分钟。

    **丢包/延迟（原来那个 bad_now）也不再清零**。2026-10-31 17:39 实测：
    锁着 BAND 40，40 丢包 100%，第一拍就把已经攒够的静止时间清零回 0/10，
    人还坐在原处没动。链路垮是链路的事，它该走「换备选 / 解锁自救」那条路；
    顺手把「我在这儿坐够了」这个已经成立的结论作废，只是让人白等。

    **strong_moving：清零要用的证据比解锁更硬**。
    实测中 把取舍说得很清楚：「不想它识别不出我在移动，但也不想它
    一直判我在动、坐不到十分钟一直不锁」。这两件事的代价完全不对称：

      · 误判「在动」→ 只是解锁一次，设备自己换频段，几十秒就回来了；
      · 误判「位置变了」→ 把已经攒了九分钟的静止时间清零，人就得从头再坐。

    所以解锁用宽的判据（锁定期间 2 次小区就算），清零只认窄的那套 ——
    `moving_strict` 就是没放宽的那个结论。人在走时它同样成立，所以不会漏；
    坐着被误判时它不成立，于是计时照常往前走，坐满十分钟照样能锁。
    """
    if band_changed:
        return False
    if strong_moving is None:
        strong_moving = moving
    # 「PCI 变了」本身也不清。设备会在两个扇区之间自己重选，人还坐在原处：
    # 2026-10-31 实机静止 144 分钟（早够 10 分钟了），15:59 设备换了个小区，
    # 计时直接归零、回到「未锁 0/10」，等于白等。真正人在动时 pci_changed 与
    # moving 必然同时成立，所以这一条就够了。
    return bool(pci_changed and moving and strong_moving)


def run_guard(args, cfg):
    """全自动守护：打开一次就一直在，按「人在不在动」管频段锁定。"""
    # 上一轮如果是被界面停掉的，标志还留着 —— 不清掉这一轮会立刻退出
    _GUARD_STOP.clear()
    interval = max(15, args.guard_interval)
    settle = max(interval * 4, args.guard_settle)
    quick_sec = max(interval * 2, min(GUARD_QUICK_SEC, settle / 2.0))
    host = cfg["ping_host"]
    dry = args.guard_dry_run

    print("=" * 62)
    print("全自动守护")
    print(f"  每 {interval}s 起一轮采样（PCI + 延迟/丢包），平时不测速")
    print(f"  在动：{GUARD_MOVE_WINDOW}s 内 PCI 变 {GUARD_MOVE_CHURN_ONLY} 次，"
          f"或变了且链路垮了 → 解锁（设备自己换频段不算，那只是它重启射频）")
    print(f"        锁着的时候门槛降到 {GUARD_MOVE_CHURN_LOCKED} 次 —— 锁住之后设备"
          f"换不了频段，同频段还换小区基本就是人在挪；"
          f"信号一路走低 {GUARD_MOVE_RSRP_DROP}dB 也算")
    print(f"        但「坐够了」的计时不会因为放宽而清零：清零只认严格那套，"
          f"免得一直被误判、坐不满 {int(settle)}s")
    print(f"  停下 {int(quick_sec)}s：先快测当前这条（约 20 秒，只测不锁）—— 很可能只是路过")
    print(f"  坐满 {int(settle)}s：才锁 —— 先拿记忆里的最优跟当前的信号/速度比，都不合适才跑体检")
    print(f"  认得的点（这地方同一条频段守过 {GUARD_CONFIRM_LOCKS} 次、现在正驻留在它上面）"
          f"不用等满：站够 {int(GUARD_KNOWN_SETTLE)}s 就锁")
    print(f"  锁住 {int(GUARD_CONFIRM_SEC / 60)} 分钟还正常 → 记一次「这儿这条守得住」，"
          f"攒够 {GUARD_CONFIRM_LOCKS} 次就升成正式记忆")
    print(f"  同一时刻还做一次回评：跟锁之前那一拍比丢包/延迟 —— "
          f"锁完更差的记一笔，同一条攒够 {REVIEW_BAD_MIN} 次就不再优先锁它")
    print("  记忆里的「最优」要是刚测出不够用 → 换记忆里的次优"
          "（信号好看不代表快：基站拥塞时 -78dBm 也可能只有 3 Mbps）")
    print(f"  锁定后连续 {GUARD_BAD_HITS} 次不通、丢包≥{GUARD_BAD_LOSS}% 或延迟≥{GUARD_LAG_MS}ms"
          f" → 先换备选，没有才解锁")
    print(f"  锁定后每 {int(GUARD_LOCK_AUDIT_SEC / 60)} 分钟复核一次"
          f"（只测不切：够不够用，不够就换备选/解锁）")
    print(f"  Ctrl+C 停止（按不出来就在另一个窗口跑 mifi --stop）"
          f"{'  ｜ 演练模式：只报告，不改设备' if dry else ''}")
    # 打上版本号：APK 里 Python 是打进 assets 的，装完新包到底跑的是哪一版，
    # 光看界面看不出来（Kotlin 换新了、pyc 还是旧的那种情况排查起来极费劲）。
    print(f"  引擎版本 {__version__}")
    print("=" * 62, flush=True)
    # 把「你亲手钉的那条」是什么时候设的说出来。实测中 在办公区眼看着
    # 程序要锁 BAND 40、结果被改成 BAND 3，屏幕上完全没有线索说明为什么 ——
    # 不说清楚，下次还是同一个疑问。
    _pin = pin_note()
    if _pin:
        print(f"  你亲手指定的频段：{_pin}", flush=True)

    _install_stop_signals()

    if not dry and _acquire_wakelock():
        print("  已替你按下 Termux 的 wakelock（切走应用也不容易被系统冻住）", flush=True)

    dev = None
    last_pci, last_rsrp, stable_since, pci_log = None, None, time.time(), deque()
    # 最近几拍的 RSRP，只用来判「信号是不是一路走低」（见 rsrp_is_falling）。
    move_rsrp_hist = deque(maxlen=GUARD_MOVE_RSRP_WIN)
    last_serving = None
    last_key = ""
    bad_streak, next_allowed = 0, 0.0
    unlocked_at = None
    quick, quick_done = None, False      # 快测结果与「这次停留已经测过没」
    speed_url = None
    pending_lock = None                  # 体检跑完后要落地的频段（见下面 after-exam 分支）
    pending_tries = 0                    # 落地失败的重试次数（3 次就认，别无限折腾设备）
    last_loss_note = 0.0                 # 上次把「高丢包」写进记忆的时刻
    last_rsrp_note = 0.0                 # 上次把「采样到的信号」写进记忆的时刻
    last_outage_note = 0.0               # 上次把「这儿没网」写进 outage 的时刻
    outage_tried = set()                 # 这一轮断网里已经试过的频段（别来回跳）
    last_quiet_why = ""                  # 上次说过的「什么都不做」的理由（防刷屏）
    place, place_key = "", ""            # 这地方叫什么（`--here` 起的名字），按 PCI 缓存
    lock_rsrp_hist = deque(maxlen=GUARD_LOCK_SIGNAL_WIN)   # 锁住这条原本有多少信号
    lock_ref_band = 0                    # 参考值是给哪条频段记的（换条就重来）
    lock_weak_hits = 0                   # 连续几拍发现「锁的这条信号塌了」
    locked_since = 0.0                   # 这一轮锁定是从哪一刻开始的
    lock_confirmed = False               # 这一轮锁定已经记过账了（一次锁定只记一次）
    lock_reviewed = False                # 这一轮回评过了（锁前 vs 锁后）
    adopted = False                      # 启动时那条「本来就锁着」记过账没有
    first_tick = True                    # 这是不是本次守护的第一拍
    last_beat = 0.0                      # 上一拍的开始时刻（用来发现被系统挂起）
    hangs = 0                            # 被系统挂起过几次
    next_audit = 0.0                     # 下次「锁定复核」的时刻

    try:
        while not _GUARD_STOP.is_set():
            now = time.time()
            # 手机把 Termux 退到后台就会冻结它（Android 12+ 的 phantom process
            # killer 更狠，直接杀）。冻结期间一拍都不采，可屏幕上最后一行还写着
            # 「监控中」，看的人以为它在盯着 —— 界面切走一次再回来，才发现整段
            # 时间什么都没发生。
            # 这里只负责把它**说出来**：真正防冻结要去通知栏点 Acquire wakelock、
            # 或把 Termux 的电池策略设成「不受限制」。
            if last_beat and now - last_beat > interval * GUARD_HANG_FACTOR + GUARD_HANG_SLACK:
                hangs += 1
                print(f"{_now_hm()}  [!] 上一拍到这一拍隔了 {int(now - last_beat)} 秒"
                      f"（正常约 {interval} 秒）—— 这段时间被系统挂起了，没在采样",
                      flush=True)
                if hangs == 1:
                    print("      这段时间它没在看。让它别被冻住：设置 → 应用 → 本应用 →"
                          " 电池 → 不受限制（Termux 里跑的话就点通知栏的 Acquire wakelock）",
                          flush=True)
                # 冻结期间一拍都没采，旧信息全部作废 —— 人可能已经走了、设备可能
                # 已经换了小区。不重置的话，醒来第一拍 still 会是个巨大的数，
                # 直接满足「坐满 10 分钟」→ 立刻下锁，锁的还是冻结前那个位置的
                # 结论。实机 2026-11-02 出现 238 秒空档的那次就埋着这个风险。
                last_beat = now
                stable_since = now
                pci_log.clear()
                move_rsrp_hist.clear()
                quick, quick_done = None, False
                # 冻了这么久，Digest nonce 多半也过期了，别拿旧的去撞一次
                drop = getattr(dev, "drop_session", None) if dev else None
                if drop:
                    drop()
                continue        # 这一拍不作决策，立刻重新采一次
            last_beat = now
            if dev is None:
                try:
                    dev = detect(args.host, args.user, args.password, name=args.driver)
                except Exception as e:  # noqa: BLE001
                    # 只给异常类名等于没说：LookupError 的消息本身就是「找不到
                    # 这台设备的后台」这类人话，TimeoutError 才说明是没响应。
                    # 实机 2026-10-30 19:07 连打三行 LookupError，看不出是
                    # 「WiFi 还没连上」还是「设备在重启」。
                    print(f"{_now_hm()}  连不上设备：{e or type(e).__name__}"
                          f"，5 秒后重试", flush=True)
                    _GUARD_STOP.wait(5)
                    continue
            try:
                st = dev.get_state()
            except Exception as e:  # noqa: BLE001
                print(f"{_now_hm()}  设备暂时无响应（{type(e).__name__}），稍后重连",
                      flush=True)
                dev = None
                _GUARD_STOP.wait(interval)
                continue

            ping = probe.ping_stats(10, host)
            sig = st.get("signal") or {}
            pci = _pci_num(sig.get("pci"))
            rsrp = _num(sig.get("rsrp"))
            locked_band = st.get("band") or 0
            serving = locked_band or _serving_band(st)
            locked = locked_band != 0

            # 设备锁着「不是人要的那条」时，不能当成「我已经锁好了」。
            # 实机 2026-10-31 17:11：「--lock-band 3」写下了设备级 pin=3，随后
            # 设备被外部锁到 BAND 40（模拟断电后固件自己恢复的老设置）。守护
            # 读到 st["band"]=40，就认定「锁定 BAND 40」开始纯监控 —— 屏幕写着
            # 锁定，用户要的却是 3，pinned 连读都没读到。
            # 这里把「锁错」降级成「没锁」，让它走正常的选带流程（pin 优先）。
            #
            # 必须用 device_pin_here 而不是 device_pin：后者是**全局**的，
            # 把住处说的「优先 3」带到了所有地方。实机 2026-11-02 19:00 用户
            # 的原话是「pin band3 的权限太高，我在餐厅的时候不敢锁」—— 餐厅里
            # 设备锁着一条能用的，却因为不是 3 被判「没锁对」，状态回退成未锁，
            # 重新决策时又读不到 PCI，最后跑了 5 分钟体检（18:47→18:55）。
            # 位置对不上的地方，这条 pin 本来就不该开口。
            pin_goal = site_pinned(st) or device_pin_here(st)
            if locked and pin_overrides_lock(locked_band, pin_goal):
                print(f"{_now_hm()}  设备当前锁在 BAND {locked_band}，"
                      f"但你指定过 BAND {pin_goal} —— 按「没锁对」处理",
                      flush=True)
                locked = False

            # 「锁的这条还剩多少信号」的参考值。取锁定后最近几拍里**最好**的那个
            # RSRP：信号一点点往下滑不算塌，只有真的掉了一大截（人走开了）才算。
            # 参考值按频段记：设备中途自己换了频段，就得重新积累，不能拿上一条的
            # -78 去比这一条的 -103。
            if locked:
                if lock_ref_band != locked_band:
                    lock_rsrp_hist.clear()
                    lock_weak_hits = 0
                    next_audit = now + GUARD_LOCK_AUDIT_SEC
                    # 换了一条频段 = 新的一轮锁定：「守了多久」从头算，
                    # 「这一轮记过账没有」也清零。
                    locked_since = now
                    lock_confirmed = False
                    lock_reviewed = False
                lock_ref_band = locked_band
                if rsrp is not None:
                    lock_rsrp_hist.append(rsrp)
            else:
                lock_ref_band = 0
                lock_rsrp_hist.clear()
                lock_weak_hits = 0
                locked_since = 0.0
                lock_confirmed = False
                lock_reviewed = False
            if first_tick and locked and not adopted:
                # 只在**第一拍**认「本来就锁着」。不这么限的话，守护自己刚锁完
                # 的那一拍就会被当成「启动时设备已锁」，账本里多出一条没人做过的
                # 决策（2026-11-02 模拟里就是这么冒出来的）。
                adopted = True
                # 启动时设备本来就锁着一条 —— 这不是这次的决定，但账本第一次
                # 见到这个位置时值得记一笔：否则用户点开「锁频段的依据」只有
                # 一片空白，会以为功能没生效。only_if_empty 保证每处只记这一次，
                # 不会每次重启守护都塞一条。
                _k = _site_key(st)
                _r = note_decision(_k, {
                    "t": datetime.now().isoformat(timespec="seconds"),
                    "kind": "adopt", "band": int(locked_band),
                    "serving": locked_band,
                }, only_if_empty=True)
                print(f"      · 启动时设备已经锁在 BAND {locked_band}"
                      f"（位置 {_k or '未知'}）："
                      f"{'已记进账本' if not isinstance(_r, str) else _r}",
                      flush=True)
            first_tick = False
            lock_ref = max(lock_rsrp_hist) if lock_rsrp_hist else None
            lock_weak_hits = (lock_weak_hits + 1
                              if lock_signal_collapsed(lock_ref, rsrp) else 0)
            signal_collapsed = lock_weak_hits >= GUARD_LOCK_SIGNAL_HITS

            # 体检刚跑完：把它的结论真的落到设备上。
            # 这段必须在「重连成功 + 读到状态」之后做 —— 体检子进程要抢登录，
            # 体检期间 dev 已经放掉了。
            # 实机 2026-10-30 22:53 的教训：体检 7.5 分钟、断网 6 次，结论是
            # 「BAND 3 最优，保持现状」，然后守护什么都没锁就走了 —— 白等、白断网，
            # 最后靠用户手动锁。
            if pending_lock:
                want, pending_lock = pending_lock, None
                if locked_band and str(want) == str(locked_band):
                    # 已经是这条了（体检还原时设备自己又锁上了），不用再动
                    print(f"{_now_hm()}  体检结论就是当前锁着的 BAND {want}，不用动", flush=True)
                else:
                    print(f"{_now_hm()}  按体检结论锁到 BAND {want}"
                          f"（当前驻留 {serving or '?'}）…", flush=True)
                    # 位置键在锁之前取：锁完 PCI 会变（换频段必换小区），
                    # 拿新 PCI 去算 key 会把信号写到「另一个地方」的账上。
                    key_now = _site_key(st)
                    # 体检结论也记账：它同样是「一次锁定的决定」，只是依据来自
                    # 那 3~6 分钟的完整实测，而不是这一拍的状态。
                    if _guard_lock_band(dev, want, trace={
                            "_key": key_now,
                            "t": datetime.now().isoformat(timespec="seconds"),
                            "kind": "measure",
                            "serving": serving,
                            "rsrp": int(rsrp) if rsrp is not None else None,
                            "hour": datetime.now().hour,
                            "pre": {"loss": ping.get("loss"),
                                    "avg": ping.get("avg")},
                    }):
                        serving = locked_band = want
                        locked, bad_streak = True, 0
                        stable_since = now
                        # 锁完把状态重新读一遍：信号/PCI 都要按新频段显示，
                        # 否则下一行会拿旧频段的 -99dBm 说新频段的事。
                        try:
                            st = dev.get_state()
                            sig = st.get("signal") or {}
                            pci = _pci_num(sig.get("pci"))
                            rsrp = _num(sig.get("rsrp"))
                        except Exception:  # noqa: BLE001
                            pass
                        last_serving, last_pci = serving, pci
                        if rsrp is not None:
                            note_band_rsrp(key_now, want, rsrp)
                        pending_tries = 0
                    else:
                        # 失败就别默默放弃：体检花了 7 分钟断网 6 次，锁不上等于又一次白干。
                        # 30 秒后重试，最多 3 次；再不行就如实说，别假装在等。
                        pending_tries += 1
                        if pending_tries < 3:
                            pending_lock = want
                            print(f"{_now_hm()}  [!] 没锁上 BAND {want}，30 秒后重试"
                                  f"（第 {pending_tries}/3 次）", flush=True)
                        else:
                            pending_tries = 0
                            print(f"{_now_hm()}  [!] BAND {want} 连续 3 次没锁上，"
                                  f"这次放弃；下一轮按记忆重新判断", flush=True)

            # 「这条链路现在还撑得住吗」。除了不通和丢包过半，延迟离谱也算：
            # 实机 17:24 延迟 4402ms、只丢 10%，原来会被当成完全正常。
            # ping 现在带 down 标记：ICMP 全无回显**且** TCP 也握不上手才算
            # 「整条链路不通」，光 ICMP 不通只说明这个位置禁 ICMP。
            avg_ms = ping["avg"]
            net_down = bool(ping.get("down"))
            bad_now = (net_down or avg_ms is None
                       or (ping["loss"] or 0) >= GUARD_BAD_LOSS
                       or avg_ms >= GUARD_LAG_MS)
            bad_streak = bad_streak + 1 if bad_now else 0
            if not bad_now:
                outage_tried.clear()        # 网络回来了，下一轮断网可以重新试
            elif serving:
                # 当前这条已经证明不行了，记下来：这一轮别再往它身上试。
                outage_tried.add(str(serving))
            # 断网要留痕（也用来算冷却窗口）。节流到每 60 秒一次：
            # 这是给「别反复测」用的时间戳，不是采样日志。
            if not dry and bad_now and now - last_outage_note >= 60:
                note_outage(st, loss=ping.get("loss"), band=serving)
                last_outage_note = now

            # 高丢包必须留痕。以前只有体检才写样本，于是「这条会周期性掉包」
            # 这个判断永远拿不到证据：BAND 40 昨晚 80%、今天 12:21 又 40%，
            # 记忆却一直干净，守护每次都优先锁它（用户原话：「BAND 40 丢包很严重」）。
            if (not dry and serving and (ping["loss"] or 0) >= SITE_LOSS_BAD
                    and now - last_loss_note >= GUARD_LOSS_NOTE_SEC):
                note_band_loss(_site_key(st), serving, ping["loss"], avg_ms, rsrp)
                last_loss_note = now

            # 信号也要留档。守护和半自动（体检 / --apply-plan）共用 sites.json，
            # 但守护原来只在「锁定那一刻」写一次 RSRP，其它时间采样到的信号全丢 ——
            # 于是记忆里长期是「有速度、没信号」，下次判「这条还能不能用」只能
            # blind：要么白跑 6 分钟体检，要么不敢切。实测中：
            # 「守护模式和半自动的那个模式要共享一套记忆」—— 这就是没共享的那一半。
            if (not dry and serving and rsrp is not None
                    and now - last_rsrp_note >= GUARD_RSRP_NOTE_SEC):
                note_band_rsrp(_site_key(st), serving, rsrp)
                last_rsrp_note = now

            pci_changed = (pci is not None and last_pci is not None
                           and pci != last_pci)
            dropped_rsrp = (rsrp is not None and last_rsrp is not None
                            and rsrp <= last_rsrp - GUARD_RESELECT_DB)
            if pci_changed:
                pci_log.append(now)
            if pci is not None:
                last_pci = pci
            if rsrp is not None:
                last_rsrp = rsrp
                move_rsrp_hist.append(rsrp)
            while pci_log and now - pci_log[0] > GUARD_MOVE_WINDOW:
                pci_log.popleft()

            # 驻留频段变了 = 设备自己换频段（厂商逻辑本来就不锁定、自动分配）。
            # 这个信息必须单独拎出来：它会同时造成「PCI 变」和「WiFi 重启→丢包 100%」，
            # 按旧判据必然被当成「人在动」。
            band_changed = bool(last_serving and serving
                                and str(serving) != str(last_serving))
            if serving:
                last_serving = serving

            churn = len(pci_log) >= GUARD_MOVE_HITS
            rsrp_falling = rsrp_is_falling(move_rsrp_hist)
            # 两套判据，用途不同 —— 这是「既不漏判、又不永远坐不满」的关键：
            #   moving        要不要解锁。锁定时放宽（GUARD_MOVE_CHURN_LOCKED）。
            #   moving_strict 要不要把「我在这儿坐够了」作废。永远用严格那套。
            # 放宽只会带来「多解锁一次」，代价几十秒；而清零会让人从头再坐十分钟。
            moving_strict = guard_is_moving(churn=churn, pci_changed=pci_changed,
                                            bad_now=bad_now,
                                            band_changed=band_changed,
                                            churn_n=len(pci_log))
            moving = guard_is_moving(churn=churn, pci_changed=pci_changed,
                                     bad_now=bad_now, band_changed=band_changed,
                                     churn_n=len(pci_log), locked=locked,
                                     rsrp_falling=rsrp_falling)
            if pci_changed and not moving:
                # 人没动、设备自己换的小区 → 记进同一处，这地方以后认得它
                note_cell_change(last_key, _site_key(st))
            last_key = _site_key(st)
            # 静止计时只在「位置真的变了」时归零：人在动、且 PCI 确实换了。
            # 原来写的是 if moving —— 而 moving 在最后一次换小区之后还会继续保持
            # 整整 GUARD_MOVE_WINDOW（300s），于是每一轮都把起点往后推，把「坐满
            # 10 分钟」拖成最多 15 分钟。实机 2026-10-30 19:30 人就坐下了，
            # 19:34:55 才开始计时，19:44:29 才锁上。
            # 丢包/延迟也不清了：2026-10-31 17:39 锁着 BAND 40、40 丢包 100%，
            # 第一拍就把坐够的时间作废，人还在原地。
            if guard_still_reset(pci_changed=pci_changed, moving=moving,
                                 band_changed=band_changed,
                                 strong_moving=moving_strict):
                stable_since = now
            still = now - stable_since
            outage_after_unlock = (unlocked_at is not None
                                   and now - unlocked_at < GUARD_RESCUE_WINDOW)

            if moving:
                quick, quick_done = None, False     # 一动，之前那次快测就作废了
            # 没锁着才需要看记忆：锁着的时候只关心「它还能不能撑住」
            memo, memo_why = (None, "") if locked else guard_memory_band(st)
            # 认得的位置（守护在这儿守过两次同一条，且现在就驻留在它上面）
            # 不必等满 10 分钟：那道坎是给「路过的人」设的，而这儿已经认出来了。
            settle_eff = settle
            if not locked:
                settle_eff, _ = guard_known_settle(st, serving, settle)
            # 「用户亲手指定过这条」优先级最高：--lock-band 3 是人的判断，
            # 不是程序猜的。信号闸门（差 10dB 就不锁）在这里必须让路 ——
            # 实机 2026-10-31 16:13：BAND 3 是 -103dBm、BAND 40 是 -82dBm，
            # 差 21dB，但 BAND 40 实测丢包 80%、BAND 3 延迟 55ms 丢包 0%。
            # 只看 RSRP 会把用户反复踢回那条烂链路。
            # 两种来源都算「人说的」：位置级（这个 PCI 记录上钉的）和设备级
            # （config 里的 pin_band，--restore 后设备换频段也还在）。
            pinned_site = None if locked else site_pinned(st)
            # 设备级那条只在「当初设它的那个地方」算数：住处说的「优先 3」不能
            # 跟到办公区去（实测中 办公区里就是这么被改掉的）。
            pinned = pinned_site or (None if locked else device_pin_here(st))
            # 记忆里的最优现在信号是不是已经不行了（比当前驻留差一截）。
            # 这一刻就要算出来：既决定「要不要先快测当前」，也决定「准不准锁它」。
            memo_rsrp = (guard_memo_rsrp(_site_key(st), memo)
                         if memo is not None else None)
            memo_weak = bool(
                memo is not None and memo_rsrp is not None and rsrp is not None
                and str(serving) != str(memo)
                and memo_rsrp <= rsrp - GUARD_LOCK_RSRP_DROP)

            # ② 停下 quick_sec 秒：快测当前这条。只测、不锁、不动设备。
            # 目的有两个：万一只是路过，什么都没发生；真久坐的话，10 分钟那一步
            # 就能凭「够用 + 频段没变」直接锁，省掉一次 5 分钟体检。
            # memo_weak 时也要测：记忆那条不可用了，得看当前这条顶不顶得住。
            # memo_rsrp is None 也要测：老记忆（2026-10-30 之前）只记了速度、没记
            # 信号，这种情况下 guard_pick_lock 只能判「blind」。原来不测快测，
            # cur_ok 永远是 False → blind → 直接跑 6 分钟体检。实机 2026-10-30
            # 21:56 就这么在白等 6 分钟、断网 7 次之后，还被切到了 -95dBm。
            if (not moving and not locked and not quick_done and not bad_now
                    and still >= quick_sec
                    and (memo is None or memo_weak or memo_rsrp is None)):
                if speed_url is None:
                    speed_url = probe.resolve_speed_url(cfg.get("speed_url"),
                                                        verbose=False)
                print(f"{_now_hm()}  先快测当前这条（不锁，只看够不够用）…", flush=True)
                quick = guard_quick_probe(speed_url, host)
                quick["band"] = serving
                quick_done = True
                print("      " + quick_line(quick), flush=True)

            lat = f"{ping['avg']}ms" if ping["avg"] is not None else "不通"
            # 这地方你命名过的话，状态行带上名字 —— 日志里能一眼看出「现在说的是住处」，
            # 而不是自己去比对一串 PCI。按 PCI 变化才重读一次，别每 30 秒读文件。
            key_here = _site_key(st)
            if key_here != place_key:
                place_key, place = key_here, site_name(key_here)
            who = (f"锁定 BAND {locked_band}" if locked
                   else f"未锁（驻留 {serving or '?'}）")
            if place:
                who += f"·{place}"
            if moving:
                # 把「凭什么判的移动」说出来。实测中 在路上看到一整屏
                # 「监控中」，既不知道有没有在判、也不知道差多少才够 —— 判据
                # 藏在代码里，等于没法验证。
                why = []
                if locked and len(pci_log) >= GUARD_MOVE_CHURN_LOCKED:
                    why.append(f"锁定期间换了 {len(pci_log)} 个小区")
                if rsrp_falling:
                    first = next((v for v in move_rsrp_hist if v is not None), None)
                    if first is not None and rsrp is not None:
                        why.append(f"信号一路走低 {int(first)}→{int(rsrp)}dBm")
                tag = "移动中" + (f"（{'，'.join(why)}）" if why else "")
            elif locked:
                # 锁着的时候这个倒计时不驱动任何事（锁定的分支里根本不读记忆、
                # 也不会再锁一次），印「静止 0/10 分钟」只会让人以为「再过十分钟
                # 还有动作」。实机 2026-10-31 19:42 用户就是这么看的。
                # 只有真要等到点才决策的状态才值得倒计时。
                tag = "监控中"
            elif still >= settle_eff:
                # 已经达标就别再印「2/1 分钟」——分母不动、分子一直涨，看着像坏了。
                tag = f"静止 {int(still / 60)} 分钟（够久了）"
                _cd = max(0.0, next_allowed - now)
                if _cd > 0:
                    # 坐够了但还在冷却里。不写出来的话，看的人只会觉得
                    # 「明明够久了，它怎么不锁」—— 实机 2026-11-02 09:22~09:24
                    # 连着三行「静止 25/26/27 分钟（够久了）」，一锁都没下，
                    # 屏幕上没有任何解释（解锁后那 300 秒冷却）。
                    tag += f"（冷却中，{int(_cd)}s 后可动）"
            else:
                tag = f"静止 {int(still / 60)}/{max(1, int(settle_eff / 60))} 分钟"
                if settle_eff < settle:
                    # 分母变小了，得说清凭什么 —— 否则看的人以为是 bug。
                    tag += "（认得的点）"
            if bad_now:
                # 断网时把「现在到底在干嘛」写进同一行。实测中 在一条
                # 无覆盖的频段上等了八分钟，屏幕上只有「延迟 不通 丢包 100%」，
                # 看不出程序是在等、在测、还是会自己换 —— 最后是他手动换的。
                tag += ("（锁着也救不回来，先换备选/解锁）" if locked
                        else "（不通就换一条，不干等）")
            if band_changed and not moving:
                # 换频段和换小区都会被当成「PCI 变了」，必须分开写清楚：
                # 前者是设备自己换频段（WiFi 会重启，那几秒必然丢包），
                # 后者是同频段内换扇区。两者都不代表人在动。
                tag += "（设备换频段，不算人动）"
            elif pci_changed and not moving:
                # 让人看得懂「为什么 PCI 变了却还在计时」：是设备自己换的小区。
                tag += ("（换小区·设备重选，信号掉了）" if dropped_rsrp
                        else "（换小区·设备重选）")
            if serving and not moving and site_band_flap(st, serving):
                # 把「它学到了什么」摆出来。按时段判了这条会掉包，可屏幕上只有
                # 「监控中」，用户根本不知道这条已经被记为「这个点儿不行」。
                tag += "（这个点儿这条会掉包）"
            print(f"{_now_hm()}  {who:<18} PCI {pci if pci is not None else '—':<4} "
                  f"{sig.get('rsrp') or '—':<8} 延迟 {lat:<7} 丢包 {ping['loss']}%  {tag}",
                  flush=True)

            # 「这地方这条守不守得住」—— 锁住十来分钟、链路还干净，就记一次。
            # 攒够两次，这个位置就能从「临时记录」升成正式记忆，下次来不再从零试。
            # 为什么不直接用 20 分钟复核：那要真坐满 20 分钟才跑到，一顿饭常常等不到；
            # 而「锁了 10 分钟没垮」比「锁上那一刻信号好看」硬得多，还不额外测速。
            if (locked and not dry and not lock_confirmed and not moving and not bad_now
                    and locked_since and now - locked_since >= GUARD_CONFIRM_SEC
                    and str(serving) == str(locked_band)):
                n, promoted, blocked = note_guard_confirm(
                    _site_key(st), locked_band, ping.get("loss"),
                    ping.get("avg"), rsrp)
                lock_confirmed = True
                if n:
                    wait = int((now - locked_since) / 60)
                    print(f"      · 这条在本地守了 {wait} 分钟、链路还正常", flush=True)
                    if promoted:
                        print(f"      · 这儿攒够 {n} 次了 → 记成正式记忆，"
                              f"下次来站 {max(1, int(GUARD_KNOWN_SETTLE / 60))} 分钟就锁",
                              flush=True)
                    elif blocked:
                        print(f"      · {blocked}", flush=True)
                    elif n < GUARD_CONFIRM_LOCKS:
                        print(f"      · 第 {n}/{GUARD_CONFIRM_LOCKS} 次，"
                              f"再来一次这地方就能当记性用", flush=True)

            # 锁后回评：拿锁前那一拍当基线，看这次切换到底有没有用。
            #
            # 为什么不和上面那段合并：上面那句「守住了」要求链路干净（not bad_now），
            # 而真正需要回评的恰恰是「锁完更差」—— 那种情况下 bad_now 为真，合并
            # 进来就永远评不到最该评的那次。两件事，分开判。
            if (locked and not dry and not lock_reviewed and locked_since
                    and now - locked_since >= GUARD_CONFIRM_SEC
                    and str(serving) == str(locked_band)):
                lock_reviewed = True
                verdict, pre, post, switched = review_last_lock(
                    _site_key(st), locked_band, ping.get("loss"),
                    ping.get("avg"), rsrp)
                if verdict == "unknown":
                    pass
                elif not switched:
                    # 锁的就是本来驻留的那条：这十分钟的差别跟「锁」无关，
                    # 只能说明这条链路自己在变。照实说，不揽功也不背锅。
                    print("      · " + review_line(verdict, pre, post)
                          + "（锁的就是当前这条，没换频段）", flush=True)
                else:
                    print("      · " + review_line(verdict, pre, post), flush=True)
                    if verdict == "worse":
                        print("      · 这次锁上去比锁之前还差 —— 记一笔，"
                              "同一条再锁坏一次，这个位置就不再优先锁它",
                              flush=True)

            # 锁定复核：锁上之后按设计就不再看记忆、不测速了，免得反复断网 ——
            # 但「人换了个地方、这条变慢、还没慢到丢包/延迟阈值」就没了出口。
            # 2026-11-01 中午在餐厅就是这样：一顿饭卡在住处锁的 BAND 3 上，屏幕
            # 只有「换小区」和「监控中」。所以每隔 GUARD_LOCK_AUDIT_SEC 量一次，
            # 只测不切；判「不够用」就走自救那条路（先换备选，没有才解锁）。
            if locked and not dry and not moving and now >= next_audit:
                if speed_url is None:
                    speed_url = probe.resolve_speed_url(cfg.get("speed_url"),
                                                        verbose=False)
                print(f"{_now_hm()}  锁定复核：量一下 BAND {locked_band} 还够不够用"
                      f"（只测，不切）…", flush=True)
                audit = guard_quick_probe(speed_url, host)
                audit["band"] = locked_band
                next_audit = time.time() + GUARD_LOCK_AUDIT_SEC
                g = _quick_grade(audit)
                mbps = audit.get("mbps") or 0
                if g == "poor":
                    print(f"      [!] 复核结论：{mbps:.0f} Mbps、延迟 "
                          f"{audit.get('avg') or '—'}ms —— 这条已经不够用了，"
                          f"不再死守", flush=True)
                    # 把它当成「连续不通」那一档：guard_decide 会走自救
                    # （先换记忆里的备选，没有才解锁），不用另开一套逻辑。
                    bad_streak = max(bad_streak, GUARD_BAD_HITS)
                elif g == "good":
                    print(f"      复核结论：{mbps:.0f} Mbps，够用，继续守着", flush=True)

            action, why = guard_decide(
                moving=moving, still_sec=still, settle_sec=settle_eff, locked=locked,
                bad_streak=bad_streak, cooldown_left=max(0.0, next_allowed - now),
                outage_after_unlock=outage_after_unlock,
                signal_collapsed=signal_collapsed, net_ok=not bad_now)

            # 「设备没锁、驻留在一条不通的频段上」原来是个没有任何出口的状态：
            # 解锁那条要求 locked，体检那条要求「刚解锁过」，两条都不满足就只等。
            # 实机 2026-11-02：18:56 起一直「未锁（驻留 41）延迟不通 丢包 100%」，
            # 八分钟没动静，最后是用户自己手动换的频段。
            #
            # 这里有个反直觉的地方：**断网时冷却必须让路**。冷却本来是为了
            # 「别反复切频段」，可断网的时候切换是唯一的出路，一条不行就换下一条
            # 才是用户要的。防止来回跳靠 outage_tried（同一轮里每条只试一次），
            # 不靠时间。模拟里第一次就踩到了：换 39 → 仍不通 → 解锁 → 冷却 300 秒，
            # 屏幕上又是一动不动（tests/manual/sim_outage.py）。
            if (not action and not locked and not moving and bad_now
                    and bad_streak >= GUARD_BAD_HITS):
                cand, cwhy = guard_outage_choice(st, serving=serving,
                                                 pinned=pinned, tried=outage_tried)
                if cand:
                    action, why = "lock", (
                        f"当前 BAND {serving or '?'} 已经连续 {bad_streak} 次不通 —— "
                        f"换 BAND {cand} 试试（{cwhy}）")
                elif not why:
                    why = ("没有可换的依据（这处没记录、也没指定过），先等网络恢复；"
                           "恢复不了就把设备挪到窗边试试")

            # 演练模式原来只印一句「未改动设备」，锁哪一条、凭什么全都看不到 ——
            # 而这恰恰是演练唯一想看的。所以「锁哪条」的裁决在打印之前就算出来，
            # 真跑和演练共用同一份结果。
            plan = None
            if action == "lock" and not getattr(args, "guard_fast", False):
                # 「当前这条够不够用」是快测给的，而快测发生在停够 3 分钟那会儿，
                # 下锁是 10 分钟以后 —— 中间这 7 分钟晚高峰能把 9 Mbps 变成 2，
                # 也能反过来。拿一个过期数字决定「锁哪条」等于蒙。
                # 实机 2026-11-01 19:28 就是这么锁的 BAND 40，4 分钟后信号塌到
                # -97dBm 又解锁。重测只测不切，约 20 秒，不断网，比锁错一轮划算。
                age = (now - float(quick.get("at") or 0)) if quick else 0.0
                if quick and not dry and quick_is_stale(quick, now):
                    if speed_url is None:
                        speed_url = probe.resolve_speed_url(cfg.get("speed_url"),
                                                            verbose=False)
                    print(f"{_now_hm()}  刚才那次快测是 {int(age / 60)} 分钟前量的，"
                          f"下锁前重测一次（只测不切）…", flush=True)
                    quick = guard_quick_probe(speed_url, host)
                    quick["band"] = serving
                    print("      " + quick_line(quick), flush=True)
                plan = guard_plan_lock(st, memo, serving, quick, rsrp,
                                       ping["loss"], pinned, memo_rsrp)

            if not action and why and why != last_quiet_why:
                # 「什么都不做」也要说出来，但只在这个理由第一次出现时说 ——
                # 断网时每 10 秒一拍，同一句话刷一屏等于没说。
                print(f"      → {why}", flush=True)
                last_quiet_why = why
            if action:
                print(f"      → {why}", flush=True)
                last_quiet_why = ""
                if dry:
                    if plan:
                        aim = f"BAND {plan[1]}" if plan[1] else "（先体检再定）"
                        print(f"      （演练模式，未改动设备；本来会锁 {aim} —— "
                              f"{_kind_text(plan[2])}）", flush=True)
                    else:
                        print("      （演练模式，未改动设备）", flush=True)
                elif action == "unlock" and bad_streak >= GUARD_BAD_HITS and not moving:
                    # 锁着但真的不行了：先换记忆里的备选，别直接交还给设备 ——
                    # 设备只会按信号最强又选回刚才那条（用户的真实体验）。
                    note_pin_failure(locked_band)   # 若是你指定的那条救不回来，记一笔
                    alt = guard_alt_band(st, skip=locked_band)
                    if alt:
                        print(f"      换到记忆里的备选 BAND {alt}（不交还给设备）…", flush=True)
                        _guard_lock_band(dev, alt, trace={
                            "_key": _site_key(st),
                            "t": datetime.now().isoformat(timespec="seconds"),
                            "kind": "rescue_alt",
                            "serving": locked_band,
                            "rsrp": int(rsrp) if rsrp is not None else None,
                            "hour": datetime.now().hour,
                            "pre": {"loss": ping.get("loss"),
                                    "avg": ping.get("avg")},
                        })
                        unlocked_at = None
                    else:
                        _guard_unlock(dev, locked_band)
                        note_decision(_site_key(st), {
                            "t": datetime.now().isoformat(timespec="seconds"),
                            "kind": "unlock_bad", "band": int(locked_band),
                            "serving": serving,
                            "pre": {"loss": ping.get("loss"),
                                    "avg": ping.get("avg")},
                        })
                        unlocked_at = time.time()
                        pci_log.clear()
                    bad_streak = 0
                    quick, quick_done = None, False
                    # 自救之后要给它一点时间落地。静止计时现在不再被丢包清零，
                    # 解锁后的下一拍 still 早就 >= settle，不拦一下就会「解锁 →
                    # 立刻锁回刚被证伪的那条 → 又 100% 丢包」，白断一轮网。
                    # 冷却只挡「主动锁」，不挡自救（unlock/measure 都在它前面判）。
                    #
                    # 但**断网时不挂冷却**：这条频段已经证伪，出路是换下一条，
                    # 而 outage_tried 保证每条只试一次。挂上冷却等于回到「干等」。
                    next_allowed = 0.0 if bad_now else time.time() + GUARD_COOLDOWN
                elif action == "unlock":
                    if why == UNLOCK_SIGNAL_COLLAPSE:
                        # 「信号塌了」有两种完全不同的成因，不能都怪到「人走开了」头上：
                        #   ① 人真的走开了 —— 频段换了，或者一路掉下去；
                        #   ② 人没动，设备在**同一个频段里**换到了一个弱小区。
                        # 结论（解锁）一样，但日志必须写清是哪一种，否则看的人
                        # 会以为程序在瞎猜。判据收在 lock_collapse_cause 里，可单测。
                        print("      （" + lock_collapse_cause(
                            lock_ref=lock_ref, cur_rsrp=rsrp,
                            locked_band=locked_band, pci_changed=pci_changed,
                            band_changed=band_changed)
                            + "；交还给设备，它会挑一条能用的）", flush=True)
                        # 这条是你指定过的、现在当地已经不行了 → 记一笔。
                        # 连续两次就会自动取消 pin，否则「解锁 → 下一拍又照 pin
                        # 锁回来」，解锁等于没解（2026-11-01 中午在餐厅就是这样）。
                        note_pin_failure(locked_band)
                    _guard_unlock(dev, locked_band)
                    note_decision(_site_key(st), {
                        "t": datetime.now().isoformat(timespec="seconds"),
                        "kind": "unlock_move" if moving else "unlock_signal",
                        "band": int(locked_band),
                        "serving": serving,
                        "pre": {"loss": ping.get("loss"),
                                "avg": ping.get("avg")},
                    })
                    unlocked_at = time.time()
                    pci_log.clear()
                    bad_streak = 0
                    quick, quick_done = None, False
                    next_allowed = 0.0 if bad_now else time.time() + GUARD_COOLDOWN
                elif action == "lock":
                    # 久坐才走到这里（路过不锁）。锁哪一条交给 guard_pick_lock：
                    # 它会把「记忆里的历史最优」和「刚才快测到的当前速度」放在一起比。
                    if getattr(args, "guard_fast", False):
                        _guard_lock_current(dev, serving)
                    else:
                        key_now, band, kind, memo_best = plan
                        # 这一次「为什么锁它」的账目。锁前那一拍刚好就是最好的
                        # 基线：它就是「不锁的状态下链路什么样」，十分钟后拿它比
                        # 才知道这次切换到底救了什么（review_last_lock）。
                        # 键要在锁之前取：换频段必换小区，锁完再算就是另一个地方了。
                        trace = {
                            "_key": key_now,
                            "t": datetime.now().isoformat(timespec="seconds"),
                            "kind": kind,
                            "serving": serving,
                            "rsrp": int(rsrp) if rsrp is not None else None,
                            "memo": memo,
                            "memo_mbps": memo_best,
                            "pin": pinned,
                            "hour": datetime.now().hour,
                            "quick": ({"band": quick.get("band"),
                                       "mbps": quick.get("mbps"),
                                       "loss": quick.get("loss")}
                                      if isinstance(quick, dict) else None),
                            "pre": {"loss": ping.get("loss"),
                                    "avg": ping.get("avg")},
                        }
                        # 记忆里的最优「没记过信号」时，不能永远绕过去。
                        # 实测中：「在一个熟悉的地方要优先锁历史记录里
                        # 最优的，最近这几次锁定看起来没有验证记忆」。
                        # 日志里确实如此 —— 10:11 那次记忆写着 BAND 3 是 23 Mbps，
                        # 因为没记信号就绕开，锁了 BAND 40，还把记忆的最优改成了 40，
                        # 从此 BAND 3 再没被提起。所以这里先去看一眼。
                        verified = False
                        if (kind in ("blind_keep", "blind") and memo is not None
                                and site_verify_due(key_now)):
                            landed, memo_ok = _guard_verify_memo(
                                dev, int(memo), serving, key_now, speed_url,
                                host, trace=trace)
                            if memo_ok:
                                band = landed
                                serving = locked_band = landed
                                locked, bad_streak = True, 0
                                verified = True
                            # 记忆那条不顶用就落回原来的分支：当前够用 → 锁当前；
                            # 不够用 → 跑体检。别在这里硬锁一条刚被证伪的。
                        if verified:
                            pass
                        elif kind == "weak_keep":
                            print(f"      → 记忆里 BAND {memo} 当时只有 {memo_rsrp:.0f}dBm，"
                                  f"当前的 BAND {serving} 是 {rsrp:.0f}dBm"
                                  f"（差 {abs(rsrp - memo_rsrp):.0f}dB，超过 "
                                  f"{GUARD_LOCK_RSRP_DROP}dB 就不锁它了），锁当前这条",
                                  flush=True)
                            _guard_lock_band(dev, band, trace=trace)
                            note_band_rsrp_at(key_now, band, serving, rsrp)
                            mark_site_provisional(key_now, band, force_best=True)
                        elif kind == "blind_keep":
                            print(f"      → 记忆里 BAND {memo} 是 {memo_best:.0f} Mbps，"
                                  f"但当时没记信号，说不准它现在还能不能用；"
                                  f"当前这条够用，先不赌，锁当前 BAND {serving}", flush=True)
                            _guard_lock_band(dev, band, trace=trace)
                            note_band_rsrp_at(key_now, band, serving, rsrp)
                            # 这里原来带 force_best=True，等于用「刚才 20 秒快测
                            # 当前这条不错」把「这个位置的最优」改写成当前频段。
                            # 实机 2026-10-31：10:11 一次就把最优从 BAND 3 改成
                            # BAND 40，12:31 再报「最优 BAND 40，直接锁定」——
                            # 用户要的「熟悉的地方锁记忆最优」就是这么丢的。
                            mark_site_provisional(key_now, band)
                        elif kind == "keep":
                            # 记忆里的最优可能是几天前、甚至换了 PCI 测的；
                            # 当前这条是几分钟前刚量的。差得不多就别切。
                            gap = int(round((1 - GUARD_QUICK_KEEP_RATIO) * 100))
                            print(f"      → 记忆里 BAND {memo} 是 {memo_best:.0f} Mbps，"
                                  f"但刚才实测当前 BAND {serving} 有 "
                                  f"{float(quick.get('mbps') or 0):.0f} Mbps"
                                  f"（差不到 {gap}%），锁当前这条，不切换", flush=True)
                            _guard_lock_band(dev, band, trace=trace)
                            note_band_rsrp_at(key_now, band, serving, rsrp)
                        elif kind == "pinned":
                            # 人已经替它做过决定了。这里不再比速度、不再看
                            # RSRP 差几 dB、不再问「当前够不够用」。
                            src = ("这个位置你指定过" if pinned_site
                                   else "你上次用 --lock-band 定的")
                            print(f"      → {src} BAND {band}，照它锁，不再比",
                                  flush=True)
                            _guard_lock_band(dev, band, trace=trace)
                            # 位置级命中时也补上设备级，并记下是「这个地方」说的
                            set_device_pin(band, st)
                            note_band_rsrp_at(key_now, band, serving, rsrp)
                        elif kind == "memo_bad_alt":
                            # 记忆说这条最优，可它刚刚被量到 3 Mbps。别锁它，
                            # 换记忆里另一条 —— 这一步几乎不断网，比跑体检快得多。
                            print(f"      → 记忆里 BAND {memo} 是最优，但刚才实测只有 "
                                  f"{float(quick.get('mbps') or 0):.0f} Mbps / "
                                  f"{quick['avg']}ms，不够用 —— "
                                  f"换到记忆里的 BAND {band}", flush=True)
                            _guard_lock_band(dev, band, trace=trace)
                            # 这里既不写 RSRP 也不写「测过」：RSRP 是当前那条
                            # （BAND 40）的，记到 BAND 3 名下就是造假；而这次
                            # 换过去本来就没测过 BAND 3，别把 last 刷新成「刚测过」。
                            # 换错了自有「连续 3 次不通 → 换备选/解锁」兜底。
                        elif kind == "memo":
                            print(f"      → {memo_why}，直接锁定（不重测）", flush=True)
                            _guard_lock_band(dev, band, trace=trace)
                            note_band_rsrp_at(key_now, band, serving, rsrp)
                        elif kind == "flap_avoid":
                            # 当前这条信号更强，但记忆里它会周期性掉包。
                            # 强信号 + 周期性掉包 = 用起来一直在卡，不如稳的那条。
                            print(f"      → 当前 BAND {serving} 信号更强，可它在记忆里"
                                  f"会周期性掉包 —— 锁稳的 BAND {band}", flush=True)
                            _guard_lock_band(dev, band, trace=trace)
                            note_band_rsrp_at(key_now, band, serving, rsrp)
                        elif kind == "quick":
                            # 「测不准」不能写成「够用」。同一组数字（9 Mbps，两次
                            # 2 / 9）在 18:21 印的是「测不准（先不动它）」，18:28
                            # 印的是「够用」，看日志的人只会以为程序在自相矛盾。
                            # 实情是：证据不足时按「先别动设备」处理，那就照实说。
                            if _quick_grade(quick) == "unknown":
                                sam = " / ".join(
                                    f"{x:.0f}" for x in (quick.get("samples") or []))
                                print(f"      → 当前 BAND {serving} 那次快测偏不准"
                                      f"（两次 {sam}），没有更硬的依据 —— 按其中较高的 "
                                      f"{float(quick.get('mbps') or 0):.0f} Mbps 先锁它，"
                                      f"锁上后每 20 分钟复核一次", flush=True)
                            else:
                                print(f"      → 当前 BAND {serving} 快测够用"
                                      f"（{float(quick.get('mbps') or 0):.0f} Mbps / "
                                      f"{quick['avg']}ms），直接锁它；其余频段下次来再比",
                                      flush=True)
                            _guard_lock_band(dev, band, trace=trace)
                            note_band_rsrp_at(key_now, band, serving, rsrp)
                            mark_site_provisional(key_now, band)
                        else:
                            why = ("记忆里的最优现在信号差太多、当前这条也不够用"
                                   if kind == "weak" else
                                   "记忆里没有信号记录、当前这条也不够用"
                                   if kind == "blind" else
                                   "记忆里这条标着最优，但刚才实测不够用，"
                                   "而记忆里也没有能换的备选"
                                   if kind == "memo_bad" else
                                   "当前这条在记忆里会周期性掉包，"
                                   "而记忆里也没有能换的备选"
                                   if kind == "flap_no_alt" else
                                   "当前这条以前锁过、锁完反倒更差，"
                                   "而记忆里也没有能换的备选"
                                   if kind == "regret_no_alt" else
                                   (memo_why or "没有可信依据"))
                            # 两条「不许体检」的闸门，放在这里而不是 guard_decide 里：
                            # 只有走到这个 else 才真的需要体检，别的分支压根不测。
                            cold = outage_left(st)
                            if bad_now:
                                # ① 链路整条不通 —— 体检六段全 0，见 _guard_measure。
                                band2, why2 = guard_outage_choice(
                                    st, serving=serving, pinned=pinned,
                                    tried=outage_tried)
                                print(f"      → {why}，但此刻整条链路不通 "
                                      f"（丢包 {ping.get('loss')}%）—— 体检只会测出 0，不跑",
                                      flush=True)
                                if band2:
                                    print(f"      → 改试 BAND {band2}（{why2}）；"
                                          f"不通就再换，不干等", flush=True)
                                    outage_tried.add(str(band2))
                                    _guard_lock_band(dev, band2, trace={
                                        "_key": _site_key(st),
                                        "t": datetime.now().isoformat(timespec="seconds"),
                                        "kind": "outage_try",
                                        "serving": serving,
                                        "rsrp": int(rsrp) if rsrp is not None else None,
                                        "hour": datetime.now().hour,
                                        "pre": {"loss": ping.get("loss"),
                                                "avg": ping.get("avg")},
                                    })
                                else:
                                    print("      → 没有可换的依据（这处没记录、也没指定过），"
                                          "先等网络自己恢复；这条真不通会再试别处用过的频段",
                                          flush=True)
                            elif cold > 0:
                                # ② 冷却中 —— 用户定的规则：一个地方反复没网就先别测，
                                # 隔一段时间「没再断」才允许重新全量测。
                                print(f"      → {why}，但这处在 {int(cold / 60)} 分钟前"
                                      f"刚断过网 —— 按你的设定先不测，"
                                      f"再等 {int(cold / 60) + 1} 分钟", flush=True)
                            else:
                                print(f"      → {why}，跑一次完整体检再锁", flush=True)
                                dev = None
                                pending_lock = (_guard_measure(args, prefer=serving)
                                                or serving)
                                # 体检要跑 3~6 分钟，这段时间本来就不采样。不把基线推过来，
                                # 下一拍会算出「隔了 300 多秒」，把自己刚跑的体检报成系统冻结。
                                last_beat = time.time()
                                if pending_lock:
                                    print(f"      → 体检跑了就得真的锁上：瞄准 BAND {pending_lock}"
                                          f"（体检结论「保持现状」不等于「别锁」）", flush=True)
                                else:
                                    # 连当前驻留都读不到。不吭声地跳过正是这次要修的病。
                                    print("      [!] 体检没留下结论、也读不到当前驻留频段，"
                                          "这次没锁上；下一轮重新判断", flush=True)
                    stable_since = time.time()
                    quick, quick_done = None, False
                else:  # measure：解锁后仍然不通，说明不是「锁错了」
                    dev = None
                    # 这条路径不是「选最优」，是「救火」：体检完照样得锁回去，
                    # 否则下一轮又会因为「没有记忆」再跑一次体检，无限断网。
                    # net_down 时它直接返回 None（体检在断网时毫无信息量）。
                    pending_lock = _guard_measure(args, prefer=serving,
                                                  net_down=bad_now) or None
                    last_beat = time.time()   # 同上：救护体检跑的几分钟不算被冻结
                    if not pending_lock:
                        # 断网时这里不该说「先保持解锁状态」—— 那会让人以为在等
                        # 设备自救，实际上什么都没发生。挑一条有依据的试。
                        band2, why2 = guard_outage_choice(
                            st, serving=serving, pinned=pinned, tried=outage_tried)
                        if band2:
                            print(f"      [!] 体检没跑（断网），改试 BAND {band2}"
                                  f"（{why2}）", flush=True)
                            outage_tried.add(str(band2))
                            _guard_lock_band(dev, band2, trace={
                                "_key": _site_key(st),
                                "t": datetime.now().isoformat(timespec="seconds"),
                                "kind": "outage_try",
                                "serving": serving,
                                "rsrp": int(rsrp) if rsrp is not None else None,
                                "hour": datetime.now().hour,
                                "pre": {"loss": ping.get("loss"),
                                        "avg": ping.get("avg")},
                            })
                        else:
                            print("      [!] 没有可换的依据，先保持解锁状态等网络恢复",
                                  flush=True)
                    stable_since = time.time()
                    quick, quick_done = None, False

                # 同上：断网时不挂冷却。锁上一条没解决的频段，下一拍就该换下一条，
                # 而不是原地等 5 分钟（outage_tried 保证不会来回跳）。
                next_allowed = 0.0 if bad_now else time.time() + GUARD_COOLDOWN
            # 用 Event.wait 而不是 time.sleep：关掉守护时线程要能立刻醒来退出。
            # 原来最坏要等满一个采样间隔（30 秒）—— 用户点了开关，界面上写着
            # 「正在停…」，那半分钟里进程已经不是前台了，正是最容易被杀的时候。
            #
            # 没网的时候把间隔压到 10 秒：链路恢复要尽快发现，别让人盯着
            # 「延迟 不通」多看半分钟（用户原话「检测到没网跳出太慢」）。
            # 探测本身已经从 20 秒压到 3 秒，一拍 10+3 秒，比原来 30+20 快得多。
            _GUARD_STOP.wait(GUARD_OUTAGE_FAST_SEC if bad_now else interval)
    except KeyboardInterrupt:
        _release_wakelock()
        print("\n[守护] 已停止（设备保持当前配置）")
        return 0

    # 从界面点「停止」走的是这条路：循环条件变假，正常退出
    _release_wakelock()
    print("\n[守护] 已停止（设备保持当前配置）")
    return 0


# ---------------- 主流程 ----------------

def _run_interactive(dev, caps, st0, args, cfg, speed_url, ping_host):
    """终端里一条命令走完「体检 → 给结论 → 问一句 → 应用」。

    为什么要有这个模式：手机上跑面板需要常驻一个网页服务，安卓会为省电把它杀掉，
    用户看到的就是「网页打不开」。终端里跑没这个问题——进程在前台，系统不会静默杀，
    也不需要记地址、不需要浏览器。
    """
    print("\n接下来会逐个切换频段实测：中途断网 2~4 次（每次约 30 秒），")
    print("结束时自动切回现在的配置。全程约 3~6 分钟，中途 Ctrl+C 可随时中止。\n")

    rc = run_preview(dev, caps, st0, args, cfg, speed_url, ping_host)
    if rc != 0:
        return rc

    try:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("\n[!] 没能读到本次结论，请重跑一次。")
        return 1

    # 默认走自动裁决；只有用户想强制某一条时才需要输入字母。
    options = plan.get("options") or {}
    avail = [k for k in ("auto", "speed", "stability") if options.get(k)]
    changes = plan.get("changes") or []

    if not avail and not changes:
        # 频段和信道都没什么可改的：上面的结论已经说清原因，这里不再重复。
        return 0

    print("\n" + "=" * 56)
    if not avail:
        # 只有信道建议（频段那边保持现状）。
        # 原来这个分支直接 return 0 —— 用户看到「信道 11 → 7，干扰降 74%」，
        # 却没有任何「要不要应用」的提示，只能自己去敲 --apply-plan
        # （2026-10-30 实机反馈）。
        print("要现在就应用吗？回车 = 只改信道；n = 不改")
        # 断网这句话必须说实话：这台设备的 WiFi 就是当前唯一通道，
        # 改信道 = AP 重启 = 本机先断 5~20 秒。写成「不会断网」会让用户
        # 以为卡死了（2026-10-30 实机就是先看到报错再看到这句话）。
        print("（设备会重启 WiFi，本机断 5~20 秒；Termux 里不用管，会自动重连）")
    elif options.get("auto"):
        a = options["auto"]
        print(f"要现在就应用吗？回车 = 按自动建议切到 BAND {a['band']}（{a['reason']}）")
    else:
        print("要现在就应用吗？回车 = 应用上面的建议")
    if avail:
        if options.get("speed"):
            print("  a = 只看速度那条线")
        if options.get("stability"):
            print("  b = 只看稳定那条线")
        print("  n = 不改，保持原样（应用只切一次，约 30~60 秒，会短暂断网）")
    try:
        ans = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n已跳过，设备保持原样。")
        return 0

    if ans in ("n", "no", "q", "quit", "不", "否", "取消"):
        print(f"已跳过，设备保持原样。下次想应用直接跑：{_cli()} --apply-plan")
        return 0
    if ans.startswith("a") and options.get("speed"):
        args.strategy = "speed"
    elif ans.startswith("b") and options.get("stability"):
        args.strategy = "stability"
    elif avail:
        args.strategy = "auto" if options.get("auto") else avail[0]
    # 只有信道建议时不动 strategy：run_apply_plan 会沿用它自己解析出的策略，
    # 而那时目标本来就只含信道。原来这里直接取 avail[0]，avail 空会 IndexError。

    try:
        cur = dev.get_state()
    except Exception as e:  # noqa: BLE001
        print(f"[×] 读取设备状态失败：{type(e).__name__}: {e}")
        return 1
    return run_apply_plan(dev, caps, cur, args, cfg, ping_host)


def pin_site_best(key, band):
    """把「这个位置的最优」钉成用户指定的那条。

    只有 --lock-band 会调它：那是用户明确说「就要这条」，不是程序在猜。
    同时盖上 verified_at —— 已经照人说的锁了，就别再拿「没记信号」去实测一遍。
    """
    if not key or not band:
        return
    try:
        data = load_sites()
        real = find_site(data, key)
        rec = data.get(real)
        if not rec:
            return
        rec["best"] = int(band)
        rec["verified_at"] = datetime.now().isoformat(timespec="seconds")
        rec["pinned"] = int(band)
        # 记下「哪天钉的」：没有这个时间戳，这条 pin 会永远压在所有判断之上
        rec["pinned_at"] = datetime.now().isoformat(timespec="seconds")
        save_sites(data)
    except (TypeError, ValueError):
        return


def run_here(dev, args):
    """给现在这个位置起个名字：`mifi --here 住处`。

    为什么需要一条「人说了算」的命令：位置靠 `PCI + 运营商` 认，而锁哪条频段
    就换哪个服务小区（住处锁 BAND 3 是 PCI 350、驻留 BAND 40 是 236）。工具
    尽量靠「这一处见过哪些小区」把它们绑成同一处，但那要么得体检遍历过所有
    频段、要么得亲眼看到一次频段切换。第一次到某地、或者手动锁 3 之后再没
    切过频段，手上就只有一张脸 —— 这时唯一可靠的锚点是人自己的一句话。

    名字只是标签，不参与选带：认位置照旧看小区。所以命名不会把两处混成一个，
    最多是「认不出来时什么都没发生」。
    """
    name = str(args.here or "").strip()
    if not name:
        print("[!] 用法：mifi --here 住处   （给当前位置起个名字）")
        return 2
    try:
        st = dev.get_state()
    except Exception as e:  # noqa: BLE001
        print(f"[×] 读不到设备状态：{type(e).__name__}: {e}")
        return 1
    key = _site_key(st)
    if not key:
        print("[!] 读不到 PCI / 运营商，现在没法给位置命名（等设备注册上再试）")
        return 1
    data = load_sites()
    real = find_site(data, key)
    _absorb_site(data, real, key)
    rec = data.setdefault(real, {"seen": 0, "bands": {}})
    old = str(rec.get("name") or "").strip()
    rec["name"] = name
    if not rec.get("last"):
        # 命名不等于「测过」：只留一行「我来过」，别让 site_memory_state 误判新鲜
        rec.setdefault("observed", datetime.now().isoformat(timespec="seconds"))
    band = st.get("band") or _serving_band(st)
    _remember_cell(rec, key, band)
    save_sites(data)

    pci, op = _split_key(key)
    others = [str(c.get("pci")) for c in (rec.get("cells") or [])
              if str(c.get("pci")) != pci]
    print(f"\n[命名] 这里叫「{name}」")
    print(f"  当前 PCI {pci}｜{op}｜驻留 {_band_label(st.get('band') or 0, band)}")
    print(f"  记在 {real} 这条记忆上"
          + (f"（同一处还见过 PCI {'、'.join(others[:6])}）" if others else ""))
    if old and old != name:
        print(f"  原来叫「{old}」，已改名")
    print("  以后回到这里、认得出这组小区时，日志里就会带上这个名字")
    return 0


def run_lock_band(dev, args, st0=None):
    """直接锁到指定频段：不测速、不裁决，只切一次。

    为什么需要这条：用户的场景是「BAND 40 丢包严重，我知道 BAND 3 才是首选」。
    他要的是把设备锁过去，而不是再花 6 分钟体检、然后被「当前这条更快」说服
    又锁回 BAND 40。体检仍是最好的默认，但「我已经知道答案」时要有一条不啰嗦的路。
    """
    band = int(args.lock_band)
    # band=0 是「解除锁定」，不是「锁定到 0 号频段」—— 界面上那个「自动」选项
    # 走的就是这条。措辞必须分开，否则用户看到「切到 BAND 0」只会以为出错了。
    if band == 0:
        print("\n[解锁] 解除频段锁定，交回设备自己选（只切一次，不测量）")
    else:
        print(f"\n[锁定] 直接切到 BAND {band}（不测量、不裁决，只切一次）")
    # main() 进门就读过一次状态了，别为了这一行再跑一趟设备 ——
    # 每次往返都要重新认证，手机上就是「点一下先卡两秒」。
    try:
        print(f"  当前：{(st0 or {}).get('band') or '自动'}")
    except Exception:  # noqa: BLE001
        pass
    try:
        dev.set_band(band)
    except DeviceUnreachable:
        pass                        # 射频重启把连接掐了，指令通常已经送达
    except Unsupported as e:
        print(f"[跳过] {e}")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"[×] 锁定失败：{type(e).__name__}: {e}")
        return 1
    if not wait_device_back(dev, timeout=60, poll=3):
        print("[!] 设备 60 秒没回来；指令多半已生效，稍后用 --status 确认")
        return 1
    st, sig = {}, {}
    try:
        st = dev.get_state()
        sig = st.get("signal") or {}
    except Exception:  # noqa: BLE001
        pass
    cur = st.get("band") or 0

    if band == 0:
        # 「解锁」要核对的是**锁定值**变成 0；而 st 里的服务频段一直都在，
        # 拿它去比对会永远「对不上」，所以这条分支不套下面那套校验。
        if cur:
            print(f"[!] 读回来的锁定值还是 BAND {cur}，可能没解除；稍后用 --status 再看一次")
            return 1
        _clear_device_pin()
        print(f"[√] 已解除锁定（设备此刻驻留在 BAND {serving_band(st) or '—'}）")
        print("  之后它会自己按信号强弱选频段，不再听这边的。")
        return 0

    print(f"[√] 已锁定 BAND {cur or band}"
          f"（RSRP {sig.get('rsrp') or '—'}，PCI {sig.get('pci') or '—'}）")
    try:
        pin_site_best(_site_key(st), band)
    except Exception:  # noqa: BLE001
        pass
    # 位置级的 pinned 只在「当前 PCI」那条记录上有效，设备换频段就失联；
    # 再存一份设备级的，断电/还原后守护才认得出「他说过要这条」。
    # 带上 st：这条设备级记录要记住它是**在哪儿**说的，别跟着人到处跑。
    set_device_pin(band, st)
    if cur and str(cur) != str(band):
        print(f"[!] 读回来是 BAND {cur}，不是 {band} —— 这台设备可能不支持这条")
        return 1
    print("  出门前记得：mifi --restore")
    return 0


def run_set_channel(dev, args, st0=None):
    """直接把 WiFi 信道设成指定值：不扫描、不评分、只写一次。

    和 run_lock_band 是同一类命令 —— 用户已经知道答案（邻居全挤在 6，
    他就要 13），不该逼他再走一遍「扫描 → 给建议 → 点应用」。
    channel=0 表示交回设备自动。

    注意这台设备的 WiFi 就是操作者唯一的通道：改信道 = 自己的网先断。
    所以「发完指令读不回状态」是必然现象，不能当成失败（那正是 10-30
    那次把成功的操作报成失败的原因）。
    """
    ch = int(args.set_channel)
    label = "自动" if ch == 0 else f"信道 {ch}"
    print(f"\n[设置] 直接改到{label}（不扫描、不评分，只写一次）")
    try:
        before = (st0 or {}).get("channel") or 0
        print(f"  当前：{'自动' if not before else f'信道 {before}'}")
    except Exception:  # noqa: BLE001
        pass
    try:
        dev.set_channel(ch)
    except DeviceUnreachable:
        pass                        # 改信道必断 AP，指令通常已经送达
    except Unsupported as e:
        print(f"[跳过] {e}")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"[×] 设置失败：{type(e).__name__}: {e}")
        return 1
    # 改信道比换频段回来得快，但手机自己也要重连一次，60 秒是宽裕值。
    if not wait_device_back(dev, timeout=60, poll=1.5):
        print("[!] 设备 60 秒没回来；指令多半已生效，稍后用 --status 确认")
        return 1
    try:
        cur = dev.get_state().get("channel") or 0
    except Exception:  # noqa: BLE001
        print("[!] 改完读不回设备状态，稍后用 --status 确认")
        return 0
    if cur != ch:
        have = "自动" if not cur else f"信道 {cur}"
        print(f"[!] 读回来是{have}，不是{label} —— 这台设备可能不支持这条")
        return 1
    print(f"[√] 已切到{label}")
    return 0


def main():
    cfg = load_config()

    ap = argparse.ArgumentParser(description="随身WiFi 自动调优（跨平台）")
    ap.add_argument("--apply", action="store_true", help="实际应用最优设置（默认只检测）")
    ap.add_argument("--wifi-only", action="store_true", help="只优化 WiFi 信道")
    ap.add_argument("--lte-only", action="store_true", help="只优化 LTE 频段")
    ap.add_argument("--bands", default="default",
                    help="default=设备声明的全部频段 / preferred=配置里的子集 / 逗号分隔如 3,40")
    ap.add_argument("--restore", action="store_true", help="还原为不锁频段 + 自动信道")
    ap.add_argument("--export-memory", nargs="?", const="", metavar="文件",
                    help="把位置记忆导出成一个 JSON（默认 mifi-memory.json）；"
                         "含真实 PCI/速度，只能在本机之间搬，别上传")
    ap.add_argument("--decisions", nargs="?", const="5", metavar="条数",
                    help="看最近几次「为什么锁这条频段」，以及锁完的回评结果"
                         "（默认 5 条；本机记忆，不连设备）")
    ap.add_argument("--import-memory", metavar="文件或网址",
                    help="把别处导出的记忆并进来（只补空缺，不覆盖本机实测）")
    ap.add_argument("--memory-origin", metavar="名字",
                    help="给导出的记忆标个来源（如 住处电脑），导入时会显示出来")
    ap.add_argument("--lock-band", type=int, metavar="N",
                    help="直接锁到指定频段（只切一次，不测量）；0 = 解除锁定交回自动")
    ap.add_argument("--set-channel", type=int, metavar="N",
                    help="直接设 WiFi 信道（只切一次，不扫描）；0 = 自动")
    ap.add_argument("--here", metavar="名字",
                    help="给当前位置起个名字（如 mifi --here 住处），日志里会带上它")
    ap.add_argument("--observe", type=int, default=OBSERVE_SEC, metavar="秒",
                    help=f"应用后盯多少秒确认稳定（0 = 不盯，默认 {OBSERVE_SEC}）")
    ap.add_argument("--fresh", action="store_true",
                    help=f"忽略位置记忆，把全部频段重测一遍"
                         f"（隔 {SITE_STALE_DAYS} 天以上没来会自动这样，一般不用手动给）")
    ap.add_argument("--forget", action="store_true",
                    help="清空位置记忆（搬过家、换过城市之后用）")
    ap.add_argument("--clean", action="store_true",
                    help="清掉历史运行记录与缓存（保留 config.json 和面板令牌）")
    ap.add_argument("--mtu", action="store_true",
                    help="探测这条线路的真实路径 MTU（只读，不改设备；手机端做不到）")
    ap.add_argument("--host", default="192.168.0.1", help="设备管理地址")
    ap.add_argument("--user", default="admin", help="后台账号")
    ap.add_argument("--password", default="admin", help="后台密码")
    ap.add_argument("--driver", default=None, help="指定驱动名（默认自动探测）")
    ap.add_argument("--list-drivers", action="store_true", help="列出已登记的驱动")
    ap.add_argument("--ssid", default=None,
                    help="自己设备的 WiFi 名称，扫描时排除（默认自动识别，识别不到则不过滤）")
    ap.add_argument("--speed-url", default=None, help="测速下载地址（默认 Cloudflare，国内可换国内镜像）")
    ap.add_argument("--ping-host", default=None, help="延迟探测目标（默认 223.5.5.5）")
    ap.add_argument("--rounds", type=int, default=None,
                    help="每个频段的测量轮数（体检默认 1，应用默认 3，均取中位数）")
    ap.add_argument("--scan-rounds", type=int, default=5, help="信道扫描轮数（默认 5，合并并滤除幽灵 AP）")
    ap.add_argument("--preview", action="store_true",
                    help="体检并给出「预期提升」，结束时恢复原配置（推荐的第一步）")
    ap.add_argument("--apply-plan", action="store_true",
                    help="按上一次 --preview 的结论应用，不再重测（只切一次，最快）")
    ap.add_argument("--apply-channel", action="store_true",
                    help="按上一次 --scan 的信道建议把信道锁上（不动频段）")
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="体检 → 给结论 → 问一句是否应用（手机上的 mifi 用的就是这个）")
    ap.add_argument("--status", action="store_true",
                    help="只看当前状态（频段/信道/信号），不测量、不改动")
    ap.add_argument("--scan", action="store_true",
                    help="只扫周边 2.4G 信道（几秒，不碰设备）——用来验证手机能不能扫")
    ap.add_argument("--strategy", "-s", choices=("auto", "speed", "stability"), default=None,
                    help="换频段的裁决方式：auto=自动（默认，速度强太多选快的、速度差不多选稳的、"
                         "稳定性差不多选快的）/ speed=只按速度 / stability=只按稳定")
    ap.add_argument("--dry-run", action="store_true",
                    help="演练：把要做的改动打出来但不碰设备（配合 --apply / --apply-plan）")
    ap.add_argument("--version", action="version",
                    version=f"mifi-autotune {__version__}")
    ap.add_argument("--guard", action="store_true",
                    help="全自动守护：在动就解锁频段、久坐就锁住，避免锁死导致没网")
    ap.add_argument("--guard-interval", type=int, default=GUARD_INTERVAL,
                    help=f"守护采样间隔秒数（默认 {GUARD_INTERVAL}）")
    ap.add_argument("--guard-settle", type=int, default=GUARD_SETTLE_SEC,
                    help=f"连续多少秒 PCI 不变算「久坐」，久坐才锁定（默认 {GUARD_SETTLE_SEC}）")
    ap.add_argument("--guard-fast", action="store_true",
                    help="久坐时直接锁当前驻留的频段、不做选优（省几分钟，但不保证最优）")
    ap.add_argument("--guard-dry-run", action="store_true",
                    help="守护演练：只报告会做什么，不改设备")
    ap.add_argument("--watch", action="store_true",
                    help="进阶：长期挂在后台，明显劣化时才自动重测并切换")
    ap.add_argument("--watch-interval", type=int, default=300,
                    help="守护模式探测间隔秒数（默认 300）")
    ap.add_argument("--watch-cooldown", type=int, default=900,
                    help="两次自动重测之间的最小间隔秒数（默认 900）")
    ap.add_argument("--watch-dry-run", action="store_true",
                    help="守护模式演练：只报告会触发什么，不改设备")
    args = ap.parse_args()

    if args.scan:
        # 只读扫描，不需要连上设备，也不改任何设置。
        # 单独做成一个命令，是因为手机上「能不能扫」需要几十秒就能验证，
        # 不该为了验证它去跑一次 5 分钟的频段体检。
        own = args.ssid or cfg.get("own_ssid") or scanner.connected_ssid()
        print(f"扫描周边 2.4G（{args.scan_rounds} 轮合并）…")
        print("      （手机上第一次调用要先唤起 Termux:API，慢的话可能要等半分钟；"
              "卡住会在超时后直接告诉你是哪一步坏了）")
        aps = scanner.scan_2g(rounds=args.scan_rounds, own_ssid=own)
        if not aps:
            print(f"[×] 没扫到邻居 AP：{scanner.scan_failure_hint()}")
            print("      原始信息（排查用，复制这一段就能定位问题）：")
            print(scanner.scan_diagnose())
            return 1
        if own:
            print(f"（已排除自己的 AP：{own}）")
        rows = scanner.score_channels(aps)
        best = scanner.pick_best_channel(rows)
        std = scanner.STANDARD_2G_CHANNELS

        # 列出**全部** 13 条信道。原来只印前 5 条，用户以为「没印出来的 = 没检测到
        # = 最干净」，实际上每条都算过、0 分的就在里面（2026-10-30 用户提问）。
        print(f"\n扫到 {len(aps)} 个 AP。全部 {len(rows)} 条信道的干扰分"
              f"（0 最好，越低越干净）：")
        for r in sorted(rows, key=lambda x: x["ch"]):
            note = "标准信道" if r["ch"] in std else ""
            if r["ch"] == best["ch"]:
                note = (note + "  ← 推荐").strip()
            print(f"  信道 {r['ch']:>2}   {r['score']:>5}   {note}")

        zeros = [r["ch"] for r in rows if r["score"] == 0]
        if zeros:
            print(f"\n  {zeros} 号一条邻居都没听到（0 分）。")
            print("  但「没听到」不等于「没人用」：扫描在每个信道只停几十毫秒，")
            print("  信号弱的 AP 和隐藏网络（不广播名字）很容易漏；而且 2.4G 只有")
            print("  1/6/11 三条互不重叠，其余信道都和邻居部分重叠 —— 邻居一活跃，")
            print("  它的分数就跟着涨。所以 0 分是「现在没人用」，不是「永远干净」。")
        else:
            print(f"\n  这次 {len(rows)} 条信道全都有邻居压着，一条 0 分的都没有 ——")
            print("  「挑一条没人用的」这个选项其实不存在，只能在被占用的里面选相对干净的。")

        if best["ch"] != rows[0]["ch"]:
            # 被「标准信道优先」改选过，得说清为什么不是分最低的那个。
            # 注意分数可能并列（都是 0.0），这时说「更低」就是错话 —— 实机出现过。
            low = rows[0]
            if abs(low["score"] - best["score"]) < 0.01:
                print(f"\n  {low['ch']} 号与 {best['ch']} 号分数并列（{low['score']}）；"
                      f"同等条件下优先 1/6/11，所以选 {best['ch']}。")
            else:
                print(f"\n  {low['ch']} 号分更低（{low['score']}），但它是夹缝信道：")
                ov = low.get("max_overlap") or 0
                print(f"  它和邻居只重叠 {ov}MHz（一条 20MHz 信道的大半是空的），"
                      f"分数低是因为「错开了几 MHz」，不是因为「真的空」。")
                print(f"  那条邻居一换信道、或者只是流量变忙，它的优势立刻消失 ——")
                print(f"  而 1/6/11 是结构性优势：邻居在标准信道上就是完全重叠，")
                print(f"  分数如实反映。差距不到 {scanner.CHANNEL_PREF_MARGIN} 时优先标准信道。")

        print(f"\n推荐：信道 {best['ch']}（干扰分 {best['score']}）")
        print("这里只扫描，不会改设备。想连频段一起优化就跑 `mifi`。")
        return 0

    if args.list_drivers:
        print("可用驱动：")
        for d in list_drivers():
            print(f"  {d['name']:<12} {d['description']}")
        print("\n新增机型：参考 drivers/template.py，在 drivers/__init__.py 里登记一行。")
        return 0

    if args.forget:
        data = load_sites()
        if data:
            save_sites({})
            print(f"位置记忆已清空（原有 {len(data)} 个位置）。")
        else:
            print("还没有位置记忆，不用清。")
        return 0

    if args.clean:
        removed = clean_run_files()
        if removed:
            print("已清理：")
            for name in removed:
                print(f"  · {name}")
        else:
            print("没有需要清理的文件。")
        print(f"\n保留：config.json（配置）、.panel-token（面板令牌）")
        print(f"运行记录：每跑一次只留最近 {_log_keep()} 条"
              f"（改 config.json 的 log_keep 可调整，0 = 不保存）")
        return 0

    if args.export_memory is not None:
        out = Path(args.export_memory or "mifi-memory.json")
        payload = export_memory(getattr(args, "memory_origin", None))
        try:
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        except OSError as e:
            print(f"[×] 写不进去：{e}")
            return 1
        n = len(payload["sites"])
        print(f"[导出] {n} 处位置 → {out}")
        if not n:
            print("  记忆是空的，导出的文件没什么用（先去跑一次体检）。")
        else:
            print("  这份文件含真实 PCI / 实测速度 / 到访时间，和 sites.json 同级，"
                  "别提交、别上传。")
            print(f"  搬到另一台机器上用：mifi --import-memory <文件或网址>")
        return 0

    if args.decisions is not None:
        try:
            cnt = max(1, int(args.decisions))
        except (TypeError, ValueError):
            cnt = 5
        print("\n".join(decisions_report(cnt)))
        return 0

    if args.import_memory:
        src = args.import_memory
        try:
            if src.startswith(("http://", "https://")):
                import urllib.request
                print(f"[导入] 正在从 {src} 取…")
                with urllib.request.urlopen(src, timeout=20) as r:
                    raw = r.read().decode("utf-8")
            else:
                raw = Path(src).read_text(encoding="utf-8")
            payload = json.loads(raw)
            stat = import_memory(payload)
        except (OSError, ValueError) as e:
            print(f"[×] 读不了这份记忆：{type(e).__name__}: {e}")
            print("    要的是 `mifi --export-memory` 导出的那个 JSON。")
            return 1
        print(f"[导入] 新增 {stat['added']} 处 ｜ 补齐 {stat['merged']} 处 ｜ "
              f"动了 {stat['bands']} 条频段数据 ｜ 跳过 {stat['skipped']} 条")
        if stat["added"]:
            print("  新增的位置用的是导出方的时间戳，不是「刚刚测过」——"
                  "该过期还是会过期。")
        print("  本机已有的实测记录不会被子文件覆盖（名字、次数、最好速度都保留）。")
        return 0

    speed_url = args.speed_url or cfg["speed_url"]
    ping_host = args.ping_host or cfg["ping_host"]
    if args.speed_url:
        cfg["speed_url"] = args.speed_url
        save_config(cfg)
    if args.ping_host:
        cfg["ping_host"] = args.ping_host
        save_config(cfg)

    # 动手测之前先确认「测速源真的下得动」。
    # 实测踩过：整条链路 HTTPS 到 CDN 全失败（HTTP 正常），于是每个频段都测出
    # 0 Mbps，工具据此推荐了错误的频段。先选源能把这类假结论挡在源头。
    # 这几条都不测速（只写一次就完事），没有测速源可用与否的问题。
    # lock-band / set-channel 用 `is not None` 判：0 是「自动」，是合法值。
    if not (args.watch or args.restore or args.apply_plan or args.list_drivers
            or args.lock_band is not None or args.set_channel is not None
            or args.here):
        speed_url = probe.resolve_speed_url(speed_url)

    if args.guard:
        return run_guard(args, cfg)

    if args.watch:
        return watch(args, cfg)

    try:
        dev = detect(args.host, args.user, args.password, name=args.driver)
    except LookupError as e:
        print(f"[×] {e}")
        return 1
    except DeviceBusy as e:
        print("[×] 设备在线，但当前拒绝提供服务：")
        print(f"    {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[×] 连不上设备：{type(e).__name__}: {e}")
        print("    请确认已连上这台随身WiFi（USB 或 WiFi 均可）。")
        return 1

    caps = dev.capabilities()
    try:
        st0 = dev.get_state()
    except Exception as e:  # noqa: BLE001
        print(f"[×] 读取状态失败：{type(e).__name__}: {e}")
        return 1

    cur_band = st0["band"]
    cur_ch = st0["channel"]
    print("=" * 56)
    print(f"设备：{getattr(dev, 'description', dev.name)}")
    band_txt, band_warn = describe_band_state(cur_band, _serving_band(st0))
    print(f"当前状态：频段 {band_txt} | "
          f"信道 {cur_ch or '自动'} | 带宽 {st0.get('bandwidth') or '—'} | MTU {st0.get('mtu') or '—'}")
    if band_warn:
        print("[!] 锁定的频段和设备实际在用的不是同一个 —— 锁定没生效，"
              "多半是这个频段信号太差、设备自己回落了。")
    sig = st0.get("signal") or {}
    if sig.get("rsrp"):
        print(f"信号：RSRP {sig.get('rsrp')} | SINR {sig.get('sinr')} | PCI {sig.get('pci')}")
    print("=" * 56)

    if args.status:
        # 已经打印过设备/频段/信道/信号了，这里补一条实时延迟就够了。
        p = probe.ping_stats(6, ping_host)
        if p["avg"] is None:
            print("网络：测不到回应（可能正在注册或没有数据通路）")
        else:
            print(f"网络：延迟 {p['avg']}ms ｜ 抖动 {p['jitter']}ms ｜ 丢包 {p['loss']}%")
        for line in describe_site_memory(st0):
            print(line)
        diag = diagnose_signal(st0.get("signal") or {})
        if diag:
            print(f"\n信号判读：{diag['text']}")
            print(f"          {diag['advice']}")
        print("\n想看「能提升多少」就跑：mifi        （体检约 3~6 分钟）")
        if not probe.is_termux():
            # 只在电脑上提示：手机端没有 ICMP 权限，这项在手机上做不了。
            print("遇到「网页能开、大文件下载卡死」可查 MTU：mifi --mtu（只读，20 秒）")
        return 0

    if args.mtu:
        return run_mtu_check(st0, ping_host)

    if args.interactive:
        return _run_interactive(dev, caps, st0, args, cfg, speed_url, ping_host)

    if args.preview:
        return run_preview(dev, caps, st0, args, cfg, speed_url, ping_host)

    if args.apply_plan:
        return run_apply_plan(dev, caps, st0, args, cfg, ping_host)

    if args.apply_channel:
        return run_apply_channel(dev, caps, st0, args, ping_host)

    # 0 是合法输入（解除锁定 / 交回自动），所以只判 None —— 写成 `if args.lock_band:`
    # 会把「自动」这条最常用的选项直接吞掉，界面上点了没反应。
    if args.lock_band is not None:
        return run_lock_band(dev, args, st0)

    if args.set_channel is not None:
        return run_set_channel(dev, args, st0)

    if args.here:
        return run_here(dev, args)

    if args.restore:
        # 还原是「断网时救命」的命令，绝不能崩。切频段会让射频重启，紧接着
        # 改信道必然打不进去：原来这里直接抛 DeviceUnreachable、带一屏 traceback
        # 退出；更糟的是「[还原] 信道 -> 自动」是先打印再调用，于是屏幕显示成功、
        # 实际没做（实机 2026-10-30 19:06 就这么崩的，信道一直停在 1）。
        # 现在：先做后说，等设备回来再动下一步，失败也只给一句人话。
        print("\n[还原] 频段 -> 未锁定")
        try:
            dev.set_band(0)
            time.sleep(SETTLE_SEC)
        except Unsupported as e:
            print(f"[跳过] {e}")
        except DeviceUnreachable:
            pass                    # 射频正在重启，本来就该断一下，下面统一等它
        back = wait_device_back(dev, timeout=60, poll=3)
        if not back:
            print("[!] 设备 60 秒没回来，信道这一步先跳过 —— 等它恢复后再跑一次 --restore")
        else:
            try:
                dev.set_channel(0)
                print("[还原] 信道 -> 自动")
                time.sleep(4)
            except Unsupported as e:
                print(f"[跳过] {e}")
            except DeviceUnreachable:
                # 改信道会把手机自己的 WiFi 掐掉，这条几乎必现，不算失败。
                print("[!] 改信道时连接断了一下；多半已生效，稍后用 --status 确认")
        if back:
            print("[√] 已还原。建议等待 30 秒重新注册。")
        else:
            print("[!] 只做了一半：频段已解开，信道没动（设备当时不可达）")
        # 人主动 --restore 就是「我要 auto」：顺手清掉 pin，否则下次守护一起
        # 会照旧锁回去，和这条命令的意图正好相反。两级都要清 —— 位置级挂在
        # PCI 键上，restore 后设备换小区，不清就会在另一条记录里继续生效。
        site_hits = clear_site_pins()
        if device_pin() or site_hits:
            clear_device_pin("已取消之前指定的频段（--restore 就是回到自动的意思）")
        # 保留改动前的原始记录，只补一条「什么时候还原的」。
        # 直接覆盖会把「我原来是哪个频段」抹掉，之后想查都查不到。
        saved = {}
        try:
            saved = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            saved = {}
        saved.update({"restored_at": datetime.now().isoformat(),
                      "restored_to": {"band": 0, "channel": 0}})
        save_state(saved)
        return 0 if back else 1

    report = {"ts": datetime.now().isoformat(), "before": {"band": cur_band, "channel": cur_ch}}

    # --- WiFi ---
    best_ch = None
    if not args.lte_only:
        own = args.ssid or cfg.get("own_ssid") or scanner.connected_ssid() or st0.get("ssid")
        if own and own != cfg.get("own_ssid"):
            cfg["own_ssid"] = own
            save_config(cfg)
        print(f"\n[1/2] 扫描周边 2.4G 信道（{args.scan_rounds} 轮合并，"
              f"出现不足 2 轮的 AP 视为幽灵丢弃）…")
        aps = scanner.scan_2g(rounds=args.scan_rounds, own_ssid=own)
        if own:
            print(f"      已排除自己的 AP「{own}」")
        elif aps:
            # 没连自己的 WiFi（例如走 USB）时无法直接识别，退化为「信号最强的那台大概率是自己」
            guess = aps.pop(0)
            print(f"      未指定自己的 SSID，按信号最强猜为「{guess['ssid']}」并已排除")
            print(f"      猜错会明显影响结论，建议固定：--ssid {guess['ssid']}")

        if not aps:
            print("      [!] 没有扫到任何邻居 AP。")
            print("          可能是系统语言不在支持列表里，或无线网卡被占用/未启用。")
            print("          本次跳过信道优化（宁可不改，也不能拿空数据乱改）。")
            report["wifi"] = {"supported": False, "reason": "no_ap_found"}
        else:
            print(f"      发现 {len(aps)} 个邻居 AP")
            chs = caps.get("channels") or list(range(1, 14))
            rows = scanner.score_channels(aps, chs)
            cur_score = next((r["score"] for r in rows if r["ch"] == cur_ch), None)
            best = scanner.pick_best_channel(rows)
            best_ch = best["ch"]
            print(f"      当前信道 {cur_ch} 评分 {cur_score} ｜ 最优信道 {best_ch} 评分 {best['score']}")
            if best["detail"]:
                print("      最优信道的重叠源：" + "；".join(
                    f"{n}(ch{c},{s}%,重叠{o}MHz)" for n, c, s, o in best["detail"]))
            else:
                print("      最优信道无重叠")
            report["wifi"] = {"current": cur_ch, "cur_score": cur_score,
                              "best": best_ch, "best_score": best["score"],
                              "neighbors": aps}

    # --- LTE ---
    best_band = None
    caveats = []
    serving = _serving_band(st0)
    cmp_band = cur_band or serving          # 对比基准（未锁频段时用实际驻留频段）
    cur_label = _band_label(cur_band, serving)
    if not args.wifi_only:
        if not args.apply:
            st = probe.ping_stats(8, ping_host)
            print(f"\n[2/2] LTE 当前频段：{cur_label}"
                  f"  延迟 {st['avg']}ms  抖动 {st['jitter']}ms  丢包 {st['loss']}%")
            print("      （频段实测需要逐个切换，会断网，因此只在 --apply 时执行）")
            report["lte"] = {"current": cur_band, "probed": False}
        elif not caps.get("can_lock_band"):
            print("\n[2/2] 该机型不支持锁定频段，跳过（只优化 WiFi）。")
            report["lte"] = {"supported": False}
        else:
            bands = resolve_bands(caps, args, cfg)
            rounds_n = args.rounds or 3
            per = SETTLE_SEC + rounds_n * 18 + 5
            print(f"\n[2/2] 实测 LTE 频段 {bands}（每频段 {rounds_n} 轮取中位数，"
                  f"约 {per} 秒/个；没信号的会提前跳过，期间会短暂断网）…")
            # 与 --preview 共用同一份「测量 + 保证还原」实现。
            # 以前两处各写一遍，--apply 那份漏了 try/finally —— 而 --watch 触发的
            # 正是 --apply，一次崩溃就把设备留在没人覆盖的频段上。
            results = measure_all_bands(dev, bands, rounds_n, cur_band, cur_label,
                                        speed_url, ping_host)

            report["lte"] = results
            # 和 --preview 用同一套判定：丢包 90% 的频段即使带宽最高也不能当最优。
            # 之前这里只看 r["ok"]，等于两套标准，--watch 可能把设备锁到打不开网页的频段。
            usable = bands_worth_comparing(results)
            usable.sort(key=lambda r: (-r["mbps"], r["avg"] or 999))
            cur_res = next((r for r in results if r["band"] == cmp_band), None)
            best_res = usable[0] if usable else None
            caveats = assess_reliability(results, cur_res, best_res)
            for r in results:
                if r.get("ok") and quality_issue(r):
                    print(f"      [!] BAND {r['band']} {quality_issue(r)}"
                          f" —— 质量不可接受，不会被选为最优")
            if usable:
                print(f"\n      最优频段：BAND {best_res['band']}"
                      f"（中位 {best_res['mbps']} Mbps，单轮 {best_res['mbps_all']}）")
            # 与 --preview 同一套裁决（自动 + 两条单线），否则 --watch 和手动跑会给出不同结论
            shaky, hard = split_caveats(caveats)
            trusted = ([r for r in usable if str(r.get("band")) not in shaky]
                       if shaky else usable)
            best_trusted = (max(trusted, key=lambda r: r.get("mbps") or 0)
                            if trusted else None)
            opts = {"auto": decide_band(cur_res, usable, cmp_band, caveats=caveats)[0],
                    "speed": speed_option(cur_res, best_trusted, cmp_band, hard),
                    "stability": stability_option(cur_res, usable, cmp_band)}
            strat = getattr(args, "strategy", None) or "auto"
            report["options"] = opts
            report["strategy"] = strat
            picked = opts.get(strat)
            if picked:
                best_band = picked["band"]
                label = {"auto": "自动", "speed": "速度", "stability": "稳定"}[strat]
                print(f"      按「{label}」建议：BAND {cur_label} → BAND {best_band}"
                      f"（{picked.get('reason')}）")
            elif usable:
                print(f"      按「{strat}」这次不给建议，保持现状")
                best_band = cur_band
            else:
                print("\n      [!] 所有频段都不通，回滚到未锁定")
                best_band = 0

    # --- 决策 ---
    print("\n" + "-" * 56)
    do_ch = bool(
        caps.get("can_set_channel", True) and best_ch is not None and best_ch != cur_ch
        and report.get("wifi", {}).get("cur_score") is not None
        and report["wifi"]["cur_score"] - report["wifi"]["best_score"] >= MIN_CHANNEL_GAIN
    )
    do_band = (best_band is not None and str(best_band) != str(cur_band))
    print(f"建议：信道 {cur_ch} -> {best_ch if do_ch else '保持'} ｜ "
          f"频段 {cur_label} -> "
          f"{('BAND ' + str(best_band)) if do_band else '保持'}")

    if not args.apply:
        print("\n（只检测模式，未改动设备。加 --apply 才会应用）")
        append_log(report)
        return 0

    # --- 应用 ---
    save_state({"saved_at": datetime.now().isoformat(),
                "band": cur_band, "channel": cur_ch})

    if do_band:
        print(f"\n[应用] 锁定 BAND {best_band} …")
        dev.set_band(best_band)
        time.sleep(SETTLE_SEC)
        chk = probe.ping_stats(6, ping_host)
        if chk["avg"] is None:
            print("[!] 切换后无法上网，回滚到原频段")
            # 回滚也要耐心等设备重新注册：发一条 POST 然后睡 26 秒就宣布失败，
            # 会把「还在恢复」当成「回滚失败」，让用户误以为设备废了。
            ok_r, _ = restore_band(dev, cur_band)
            if not ok_r:
                print(f"    [!] 回滚没成功，请手动在设备后台把频段改回 {cur_label}")
            best_band = cur_band
        else:
            print(f"[√] 已生效，延迟 {chk['avg']}ms")

    if do_ch:
        print(f"\n[应用] 切换 WiFi 信道到 {best_ch} …")
        dev.set_channel(best_ch)
        time.sleep(5)
        ch_now = dev.get_state()["channel"]
        if ch_now != best_ch:
            print(f"[!] 信道未生效（当前 {ch_now}），可能被设备自动接管，保持原样")
            dev.set_channel(cur_ch)
        else:
            print("[√] 已生效")

    st_end = dev.get_state()
    after = {"band": st_end["band"], "channel": st_end["channel"]}
    report["after"] = after
    append_log(report)
    print(f"\n完成。最终：频段 {'未锁定' if after['band'] == 0 else 'BAND ' + str(after['band'])}"
          f" | 信道 {after['channel']}")
    print(f"原始设置已存 {STATE}，可用 --restore 还原")
    return 0


if __name__ == "__main__":
    sys.exit(main())
