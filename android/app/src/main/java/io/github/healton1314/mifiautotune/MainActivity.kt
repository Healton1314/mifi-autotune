package io.github.mifiautotune.app

import android.annotation.SuppressLint
import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.os.Build
import android.os.Bundle
import android.text.TextUtils
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.CompoundButton
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import kotlin.concurrent.thread

/**
 * 原生界面。默认只回答三个问题：现在好不好、要不要优化、守护开没开。
 *
 * 设计上刻意克制：默认一屏只有「一句话状态 + 一个主按钮 + 守护开关」。
 * 频段、信号、信道、延迟这些数字全收进「详情」，点一下才展开 —— 主界面
 * 不是仪表盘，是给人做决定的。日志只在真的在跑东西时出现。
 *
 * 数据全部来自 Python（bridge.py），这里只负责显示；跨语言调用统一走
 * Py.call，它保证串行 + 后台线程 + 异常转文字。
 */
class MainActivity : Activity() {

    // 状态：一句话，可点开详情
    private lateinit var tvConn: TextView
    private lateinit var tvChevron: TextView
    private lateinit var detailBox: LinearLayout

    // 详情里的读数
    private lateinit var tvBand: TextView
    private lateinit var tvChan: TextView
    private lateinit var tvRsrp: TextView
    private lateinit var tvSinr: TextView
    private lateinit var tvLat: TextView

    // 主操作
    private lateinit var btnMain: TextView
    private lateinit var tvMainHint: TextView
    // 体检结论（测完才显示）：一句话 + 两个按钮
    private lateinit var verdictBox: LinearLayout
    private lateinit var tvVerdict: TextView
    private lateinit var tvVerdictSub: TextView
    private lateinit var btnApply: TextView
    private lateinit var btnDecline: TextView
    // 日志最新一屏缓存在这儿：点「查看运行日志」直接用，不用等 Python 那边
    private var logLines: List<String> = emptyList()
    private var jobBusy = false

    // 守护
    private lateinit var swGuard: Switch
    private lateinit var tvGuardHint: TextView

    private var busy = false
    // 正在跑的是哪种任务。只用来决定「要不要冻住读数」：体检要冻，其余不冻。
    private var busyKind = ""
    private var statusBusy = false
    private var guardOn = false
    // 体检跑完、plan.json 里确实有可改的项 → 主按钮切换成「应用结论」
    private var planReady = false
    // 这条建议来自哪：体检出来的频段（band）还是扫描出来的信道（channel）。
    // 两者落地路径不同 —— 信道只动信道，绝不能顺手把频段也切了。
    private var applyKind = "band"
    // 手动选频段/信道要用的一组事实：设备认哪些值、现在锁的是什么、支不支持改。
    // 一律用设备声明的集合，不让手填 —— 填个不支持的值，设备会先静默忽略、
    // 再花 26 秒重新注册，最后失败，用户只会以为工具坏了。
    private var devBands: List<Int> = emptyList()
    private var devChannels: List<Int> = emptyList()
    private var capLockBand = false
    private var capSetChannel = false
    private var curBandLock = 0
    private var curChannel = 0
    // 详情默认展开：那几个数就是用户打开 app 最想先看到的，藏起来反而多一步。
    // 「收起」还留着 —— 想只看按钮和开关的人可以收掉。
    private var detailOpen = true

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildUi())

        // 扫描信道要权限：13+ 是「附近的设备」，10~12 是「定位」。
        // 弹窗是异步的，不等它 —— 桥那边先写一份空结果，允许之后 refresh 一次即可。
        requestScanPermissions()
        WifiScanBridge.start(this)

        thread(name = "py-init") {
            runCatching { Py.start(this) }
                .onFailure { Log.e(TAG, "Python 启动失败", it) }
            Main.post {
                refreshStatus()
                // 守护有可能是「上次开着、App 被系统回收、服务又被 START_STICKY
                // 拉起来」的状态。这时开关默认是关的，界面会说「关着」，可它其实
                // 在跑 —— 用户会以为没生效，再点一次开关反而重复启动。开机对一次。
                syncGuardOnce()
            }
        }

        Main.post(pollStatus)
        Main.post(pollJob)
        Main.post(pollGuard)
    }

    // ---------------- 界面 ----------------

    private fun buildUi(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(BG)
            setPadding(dp(20), dp(52), dp(20), dp(28))
        }

        root.addView(TextView(this).apply {
            text = "随身WiFi 调优"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 26f)
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(INK)
        })

        // ---- 一句话状态，点一下展开详情 ----
        val statusRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(10), 0, 0)
        }
        tvConn = TextView(this).apply {
            text = "正在连接…"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
            setTextColor(MUTED)
            // 一行封顶：状态行一长就会折行，整页跟着长，一屏就装不下了
            setSingleLine(true)
            ellipsize = TextUtils.TruncateAt.END
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        tvChevron = TextView(this).apply {
            text = if (detailOpen) "收起" else "详情"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            setTextColor(ACCENT)
            setPadding(dp(8), dp(2), 0, dp(2))
        }
        statusRow.addView(tvConn)
        statusRow.addView(tvChevron)
        statusRow.setOnClickListener { toggleDetail() }
        root.addView(statusRow)

        // ---- 详情：默认展开，可收起 ----
        detailBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = if (detailOpen) View.VISIBLE else View.GONE
            setPadding(0, dp(14), 0, 0)
        }
        detailBox.addView(detailCard())
        root.addView(detailBox)

        root.addView(primaryCard())
        root.addView(guardCard())

        return ScrollView(this).apply {
            isFillViewport = true
            addView(root)
        }
    }

    /** 详情：四个读数 + 底部的两个低频操作。全部藏在「详情」后面。 */
    private fun detailCard(): View {
        val box = card()
        val row1 = row()
        val row2 = row()

        tvBand = metric(row1, "频段", "—", big = true)
        tvChan = metric(row1, "信道", "—", big = true)
        tvRsrp = metric(row2, "信号", "—", big = false)
        tvSinr = metric(row2, "信噪比", "—", big = false)

        box.addView(row1)
        box.addView(spacer(10))
        box.addView(row2)

        tvLat = TextView(this).apply {
            text = "延迟 —"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12.5f)
            setTextColor(MUTED)
            setPadding(0, dp(12), 0, 0)
        }
        box.addView(tvLat)

        val tools = row()
        val scan = actionButton("扫描信道", false)
        scan.setOnClickListener { doScan(scan) }
        val restore = actionButton("恢复默认", false)
        // 这条路会把频段锁、信道这些改回出厂状态，中间必然断一次网。
        // 放在详情里已经不容易误触，再问一句 —— 点错了要重新体检一遍才能回来。
        restore.setOnClickListener { confirmRestore() }
        tools.addView(scan, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        tools.addView(spacerW(10))
        tools.addView(restore, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        box.addView(spacer(12))
        box.addView(tools)

        // 手动选择：和上面「扫描信道」不是一回事 —— 那条是「我不知道哪个好，你
        // 扫给我看」，这条是「我已经知道答案」。体检要 3~6 分钟、中途断网，还可能
        // 因为波动大给不出结论；用户说「BAND 40 晚上丢包，我就要 BAND 3」的时候，
        // 他要的是把设备锁过去，不是再被说服一遍。
        val manual = row()
        val pickBand = actionButton("手动设频段", false)
        pickBand.setOnClickListener { showBandDialog() }
        val pickChan = actionButton("手动设信道", false)
        pickChan.setOnClickListener { showChannelDialog() }
        manual.addView(pickBand, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        manual.addView(spacerW(10))
        manual.addView(pickChan, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        box.addView(spacer(10))
        box.addView(manual)

        // 日志平时不摆出来（守护的刷屏尤其吵），想看的时候从这儿弹出来
        val logBtn = actionButton("查看运行日志", false)
        logBtn.setOnClickListener { showLogDialog() }
        box.addView(spacer(10))
        box.addView(logBtn)
        return box
    }

    /** 主操作：一个按钮，一行状态。状态平时是时长说明，跑起来就换成最新一行日志。 */
    private fun primaryCard(): View {
        val box = card()

        // 体检结论：只给两句，一句「建议什么」，一句「好在哪」。
        // 平时完全不占地方；测完了才出现，就摆在按钮上面 —— 看到结论的下一
        // 眼就是能按的那个键，不用去翻日志猜「到底该不该点」。
        verdictBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            setPadding(0, 0, 0, dp(12))
        }
        tvVerdict = TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 15.5f)
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(INK)
        }
        tvVerdictSub = TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12.5f)
            setTextColor(MUTED)
            setPadding(0, dp(4), 0, 0)
            setMaxLines(2)
        }
        verdictBox.addView(tvVerdict)
        verdictBox.addView(tvVerdictSub)

        // 两个按钮并排，但**不是**一样大：应用占两份宽、不用了占一份。
        // 实测中 的原话是「应用建议的按钮不好看，换一个」—— 原来两个
        // 键各占一半、都是实心色块，看着像两个同等分量的选项；可「应用」和
        // 「不用了」从来不等价，一个是执行结论，一个是让它过去。
        // 拒绝的入口仍然是一个正经按钮，不缩成一行小字（那样等于没有）。
        val acts = row()
        btnApply = verdictButton("应用建议", true)
        btnApply.setOnClickListener { applyNow() }
        btnDecline = verdictButton("不用了", false)
        btnDecline.setOnClickListener { declineVerdict() }
        acts.addView(btnApply, LinearLayout.LayoutParams(
            0, ViewGroup.LayoutParams.WRAP_CONTENT, 2f).apply { topMargin = dp(14) })
        acts.addView(spacerW(10))
        acts.addView(btnDecline, LinearLayout.LayoutParams(
            0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply { topMargin = dp(14) })
        verdictBox.addView(acts)

        box.addView(verdictBox)

        btnMain = actionButton("开始体检并优化", true)
        btnMain.setOnClickListener { startPreview() }
        box.addView(btnMain)

        tvMainHint = TextView(this).apply {
            text = "约 3~6 分钟，中途会断网"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setTextColor(MUTED)
            setGravity(Gravity.CENTER)
            setMaxLines(2)
            setPadding(0, dp(10), 0, 0)
        }
        box.addView(tvMainHint)
        return box
    }

    /** 守护：一行标题 + 开关 + 四个字的状态。说明只在真开着的时候给一句。 */
    @SuppressLint("UseSwitchCompatOrMaterialCode")
    private fun guardCard(): View {
        val box = card()
        val head = row()

        head.addView(TextView(this).apply {
            text = "守护模式"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 17f)
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(INK)
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        })

        swGuard = Switch(this).apply {
            isChecked = false
            // 整行都可点：用户要开守护，多半是去点「守护模式」那四个字，而不是
            // 右下角那个小滑块。只让 Switch 响应的话，点文字等于没反应。
            isClickable = false
            isFocusable = false
            setOnCheckedChangeListener { _: CompoundButton, on: Boolean -> toggleGuard(on) }
        }
        head.addView(swGuard)
        head.isClickable = true
        head.setOnClickListener { swGuard.isChecked = !swGuard.isChecked }
        box.addView(head)

        tvGuardHint = TextView(this).apply {
            text = "关着"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12.5f)
            setTextColor(MUTED)
            setPadding(0, dp(6), 0, 0)
            // 守护每次采样都会带一串读数，不封顶的话这一格能长到占半屏，
            // 想看的按钮就被顶出去了
            setMaxLines(2)
            ellipsize = TextUtils.TruncateAt.END
        }
        box.addView(tvGuardHint)
        return box
    }

    // ---------------- 刷新 ----------------

    private val pollStatus = object : Runnable {
        override fun run() {
            refreshStatus()
            Main.postDelayed(this, 5000)
        }
    }

    private val pollJob = object : Runnable {
        override fun run() {
            // 上一次还没回来就别再投。job() 本身是读内存，很快，但体检这类
            // 长任务一旦把线程占住，不设这道闸请求就会越积越多 —— 等它跑完，
            // 队列里还压着几百个旧请求，点「查看运行日志」自然半天没反应。
            if (jobBusy || !Py.isReady) {
                Main.postDelayed(this, 1200)
                return
            }
            jobBusy = true
            Py.callFast(this@MainActivity, "job") { txt ->
                jobBusy = false
                val o = json(txt) ?: return@callFast
                val running = o.optBoolean("running")
                // org.json 的 optString 对 JSON null 会返回字符串 "null"，
                // 直接拿它判空会让日志区一直挂着。这里显式问 isNull。
                val kind = if (o.isNull("kind")) "" else o.optString("kind")
                val exit = if (o.isNull("exit")) 0 else o.optInt("exit", 0)

                // 行缓存在这儿顺手存一份：日志弹窗要用，指过去就能立刻弹出来，
                // 不必再等一次 Python 往返。
                val arr = o.optJSONArray("lines")
                if (arr != null) logLines = (0 until arr.length()).map { arr.optString(it) }

                // 只有真在跑的时候才让日志占着提示行。跑完就交还给结论 ——
                // 否则「任务结束」会一直盖在「有 2 项可以改」上面，让人以为没结论。
                if (running) showJob(o)

                if (!running && busy) {
                    busy = false
                    busyKind = ""
                    btnMain.isEnabled = true
                    btnMain.alpha = 1f
                    // 体检成功不代表没事可做，也不代表该「优化」—— plan.json 里
                    // changes 为空就是「现在这设置已经最好，别折腾」。让按钮去说实话。
                    if (kind == "preview" && exit == 0) afterPreview() else resetMain()
                    // 立刻刷一次读数，别等下一个 5 秒的轮询。改完设置最想看的就是
                    // 「变了没有」，这几秒的空窗会被读成「没生效」。
                    refreshStatus()
                }
            }
            Main.postDelayed(this, 1200)
        }
    }

    /**
     * 盯着守护还活着没有。
     *
     * 守护是常驻线程，连不上设备、异常退出都会让它悄悄停下，而开关还亮着 ——
     * 用户以为它在守，其实早就不干了。这里每 5 秒问一次，发现没了就把开关
     * 如实地拨回去，并告诉他去哪儿看原因。
     */
    private val pollGuard = object : Runnable {
        override fun run() {
            if (guardOn) {
                Py.callFast(this@MainActivity, "guard_state") { txt ->
                    val o = json(txt) ?: return@callFast
                    if (!o.optBoolean("running")) {
                        if (guardOn) {
                            guardOn = false
                            // 线程没了，前台服务还挂着只会留一条骗人的通知
                            GuardService.stop(this@MainActivity)
                            swGuard.setOnCheckedChangeListener(null)
                            swGuard.isChecked = false
                            swGuard.setOnCheckedChangeListener { _: CompoundButton, x: Boolean ->
                                toggleGuard(x)
                            }
                            tvGuardHint.text = "已经停了。点「查看运行日志」看原因"
                        }
                        return@callFast
                    }
                    // 把守护最近一条输出摆出来：不然开关亮着、底下只有一句
                    // 「运行中」，用户根本不知道它到底在干活没有。
                    val tip = if (o.isNull("tip")) "" else o.optString("tip")
                    tvGuardHint.text = if (tip.isNotEmpty()) "运行中 · $tip"
                    else "运行中 · 正在等第一轮采样…"
                }
            }
            Main.postDelayed(this, 5000)
        }
    }

    private fun refreshStatus() {
        // 上一次还没回来就别再发：每次 status 都要真连设备，堆起来只会越来越慢
        if (statusBusy || !Py.isReady) return
        // 体检时设备会反复重启射频，这时候去查必然「连不上」—— 那不是故障，是过程。
        //
        // 但**只有体检**该冻住数字：它要挨个切十几个频段，读数一路乱跳，看着像坏了。
        // 手动设频段/信道只切一次、全程十几秒，冻住反而像卡死 —— 用户的原话是
        // 「运行日志里面都改了，但是显示界面一直没有改」。所以这里按 kind 区分，
        // 别再一刀切。
        if (busy && busyKind == KIND_PREVIEW) {
            tvConn.text = "体检中，中途会断网"
            tvConn.setTextColor(MUTED)
            return
        }
        statusBusy = true
        Py.call(this, "status") { txt ->
            statusBusy = false
            val o = json(txt)
            if (o == null || o.has("error")) {
                tvConn.text = "没连上随身WiFi"
                tvConn.setTextColor(ERR)
                return@call
            }

            val lte = o.optJSONObject("lte")
            val ping = o.optJSONObject("ping")
            val loss = if (ping == null || ping.isNull("loss")) -1.0 else ping.optDouble("loss", -1.0)
            val lat = if (ping == null || ping.isNull("avg")) -1.0 else ping.optDouble("avg", -1.0)
            val band = o.optInt("band", 0)
            // 「频段」有两个：band 是锁定值（0 = 自动），serving_band 是此刻实际
            // 驻留的。恢复默认之后锁定被解除，band 就是 0 —— 只认 band 的话，
            // 界面上那一格会变成一个「—」，看着像设备坏了，其实它只是变回自动了。
            val serving = if (o.isNull("serving_band")) 0 else o.optInt("serving_band", 0)
            val bandWarn = o.optBoolean("band_warn", false)

            // 手动选择要用的那份事实，跟着每次状态刷新一起更新 —— 设备换了、
            // 固件不同，支持的值就不一样，缓存一次是不行的。
            curBandLock = band
            curChannel = o.optInt("channel", 0)
            capLockBand = o.optBoolean("can_lock_band", false)
            capSetChannel = o.optBoolean("can_set_channel", false)
            devBands = intList(o.optJSONArray("bands"))
            devChannels = intList(o.optJSONArray("channels"))

            // 状态行只说一句人话：好不好。数字都放详情里。
            val slow = (loss >= 8) || (lat >= 200)
            // 锁 39 实际驻留 3 这种事必须说，不然用户以为自己锁住了，白等。
            val bandTag = when {
                bandWarn && serving > 0 -> " · 锁 $band 实际 $serving"
                band > 0 -> " · BAND $band"
                serving > 0 -> " · 自动 BAND $serving"
                else -> ""
            }
            tvConn.text = (if (slow) "网络偏慢" else "网络正常") + bandTag
            tvConn.setTextColor(if (slow) WARN else OK)

            // 详情里的数：频段这一格永远显示**实际驻留**的那个。锁定值 0（自动）
            // 时以前会显示成一个「—」，看着像设备坏了 —— 那是最误导人的一种空。
            tvBand.text = when {
                serving > 0 -> "BAND $serving"
                band > 0 -> "BAND $band"
                else -> "—"
            }
            tvBand.setTextColor(if (bandWarn) WARN else INK)
            tvChan.text = o.optInt("channel", 0).takeIf { it > 0 }?.toString() ?: "自动"
            // 设备给的是带单位的字符串（"-99dBm" / "3dB"）。浏览器里 parseInt 会
            // 自动截出数字，Kotlin 的 optInt 直接失败返回 0 —— 这就是前一版这两格
            // 一直是「—」的原因。这里显式剥掉单位。
            val rsrp = num(lte?.optString("rsrp"))
            val sinr = num(lte?.optString("sinr"))
            tvRsrp.text = if (rsrp != null) "$rsrp dBm" else "—"
            tvSinr.text = if (sinr != null) "$sinr dB" else "—"
            tvLat.text = "延迟 " + (if (lat >= 0) "${lat.toInt()} ms" else "—") +
                " · 丢包 " + (if (loss >= 0) "${loss.toInt()}%" else "—")
        }
    }

    // ---------------- 动作 ----------------

    private fun toggleDetail() {
        detailOpen = !detailOpen
        detailBox.visibility = if (detailOpen) View.VISIBLE else View.GONE
        tvChevron.text = if (detailOpen) "收起" else "详情"
    }

    private fun startPreview() {
        if (busy) return
        // 守护开着的时候它自己也在连设备、也在切频段。这时候再跑体检，两边会
        // 抢同一台路由器的登录和射频状态，结果是谁都不准。先说清楚，别硬跑。
        if (guardOn) {
            showLog("守护在跑，先关掉它再体检")
            return
        }
        busy = true
        busyKind = KIND_PREVIEW
        hideVerdict()   // 重新测就别让上一条结论还挂在那儿
        btnMain.text = "体检中…约 3~6 分钟"
        btnMain.isEnabled = false
        btnMain.alpha = 0.6f
        showLog("正在启动…")
        runTask("preview") { "已开始体检" }
    }

    /** 体检跑完了：去 plan.json 里看看有没有值得改的，再决定按钮说什么。 */
    private fun afterPreview() {
        Py.callFast(this, "plan") { txt ->
            val p = json(txt)?.optJSONObject("plan")
            val changes = p?.optJSONArray("changes")
            val n = changes?.length() ?: 0
            if (p == null) {
                hideVerdict()
                resetMain()
                tvMainHint.text = "体检没留下结论，看日志找原因"
            } else if (n == 0) {
                // 结论是「别动」的时候，硬塞一个「立即优化」按钮就是骗人
                hideVerdict()
                resetMain()
                tvMainHint.text = "当前设置已经合适，不用动"
            } else {
                planReady = true
                val list = (0 until n).map { changes!!.optString(it) }
                showVerdict(list.joinToString("，"), verdictSub(p))
                // 两件事说清楚：会断网、可以不改
                tvMainHint.text = "应用会断一次网 · 不想改就点「不用了」"
            }
        }
    }

    /**
     * 结论正文：只说实测对比，不给没把握的承诺。
     *
     * 引擎在「数据不足以断定更快」时会把 mbps_gain 置空（换的是稳定性，不是速度）,
     * 这里也就不编一个提速百分比出来。
     */
    private fun verdictSub(p: JSONObject): String {
        val e = p.optJSONObject("expected") ?: return ""
        if (e.isNull("mbps_now") || e.isNull("mbps_best")) return ""
        val now = e.optDouble("mbps_now", 0.0)
        val best = e.optDouble("mbps_best", 0.0)
        if (now <= 0 || best <= 0) return ""
        // 固定用 US 格式：某些区域的小数点是逗号，"19,7 Mbps" 看着像千分位
        return String.format(java.util.Locale.US, "实测 %.1f Mbps，现在 %.1f Mbps", best, now)
    }

    private fun showVerdict(title: String, sub: String, kind: String = "band") {
        tvVerdict.text = if (title.startsWith("建议")) title else "建议：$title"
        tvVerdictSub.text = sub
        tvVerdictSub.visibility = if (sub.isEmpty()) View.GONE else View.VISIBLE
        verdictBox.visibility = View.VISIBLE
        applyKind = kind
        // 主按钮让位：结论已经自带「应用建议 / 不用了」，再摆一个只会让人选错
        btnMain.visibility = View.GONE
    }

    private fun hideVerdict() {
        verdictBox.visibility = View.GONE
        btnMain.visibility = View.VISIBLE
    }

    /** 拒绝建议：界面回到「还没测」的样子，不再追着问。 */
    private fun declineVerdict() {
        hideVerdict()
        resetMain()
        tvMainHint.text = "已保持现状 · 想重测再点上面"
    }

    private fun resetMain() {
        planReady = false
        btnMain.visibility = View.VISIBLE
        btnMain.text = "开始体检并优化"
        tvMainHint.text = "约 3~6 分钟，中途会断网"
    }

    /** 按上次体检的结论改设置。只切一次，比重新体检快得多。 */
    private fun applyNow() {
        if (busy) return
        if (guardOn) {
            showLog("守护在跑，先关掉它再应用")
            return
        }
        planReady = false
        hideVerdict()
        busy = true
        busyKind = "apply"
        btnMain.text = "正在应用…"
        btnMain.isEnabled = false
        btnMain.alpha = 0.6f
        // 信道的建议只改信道：那条建议是刚扫出来的，跟体检时的频段结论没有关系，
        // 用 apply_plan 会顺手把频段也切了 —— 那等于替用户多做一次决定。
        val fn = if (applyKind == "channel") "apply_channel" else "apply_plan"
        runTask(fn) { "正在应用，中途会断网" }
    }

    private fun confirmRestore() {
        if (guardOn) {
            showLog("守护在跑，先关掉它再恢复")
            return
        }
        AlertDialog.Builder(this)
            .setTitle("恢复默认设置？")
            .setMessage("会把频段锁定、信道这些改回设备原来的状态，中间会断一次网。")
            .setNegativeButton("算了", null)
            .setPositiveButton("恢复") { _, _ ->
                busy = true
                busyKind = "restore"
                btnMain.text = "正在恢复…"
                btnMain.isEnabled = false
                btnMain.alpha = 0.6f
                runTask("restore") { "正在恢复，完成后会自动重连" }
            }
            .show()
    }

    // ---------------- 手动选频段 / 信道 ----------------

    private fun showBandDialog() {
        if (guardOn) {
            showLog("守护在跑，先关掉它再手动设")
            return
        }
        if (!capLockBand) {
            showLog("这台设备不支持锁定频段")
            return
        }
        if (devBands.isEmpty()) {
            showLog("还没读到设备支持哪些频段，等状态刷新一下再点")
            return
        }
        // current 传的是**锁定值**（0 = 自动）。界面上显示的实际驻留频段是另一个
        // 概念，不能拿来当选中项 —— 否则「自动但此刻在 BAND 40」会被画成「锁了 40」。
        pickDialog(
            title = "手动选频段",
            sub = "只切一次，不测速、不裁决。换频段会断网 20~40 秒。",
            values = devBands,
            current = curBandLock,
            label = { "BAND $it" },
            autoLabel = "自动",
            allowAuto = true
        ) { band ->
            val what = if (band == 0) "解除频段锁定" else "锁定到 BAND $band"
            AlertDialog.Builder(this)
                .setTitle("$what？")
                .setMessage("会断一次网，20~40 秒后自动重连。")
                .setNegativeButton("算了", null)
                .setPositiveButton("确定") { _, _ -> startManual("lock_band", band, "$what…") }
                .show()
        }
    }

    private fun showChannelDialog() {
        if (guardOn) {
            showLog("守护在跑，先关掉它再手动设")
            return
        }
        if (!capSetChannel) {
            showLog("这台设备不支持改信道")
            return
        }
        if (devChannels.isEmpty()) {
            showLog("还没读到设备支持哪些信道，等状态刷新一下再点")
            return
        }
        pickDialog(
            title = "手动设信道",
            sub = "只写一次，不扫描、不评分。改信道会断网十几秒。",
            values = devChannels,
            current = curChannel,
            label = { "信道 $it" },
            autoLabel = "自动",
            allowAuto = true
        ) { ch ->
            val what = if (ch == 0) "交回自动选信道" else "切到信道 $ch"
            AlertDialog.Builder(this)
                .setTitle("$what？")
                // 这台设备的 WiFi 就是手机唯一的通道，改信道等于先把自己掐断。
                // 不说清楚，用户会以为「怎么一点就断网了，是不是坏了」。
                .setMessage("手机现在就是通过这台设备的 WiFi 上网的，改信道会先断一下再自动重连。")
                .setNegativeButton("算了", null)
                .setPositiveButton("确定") { _, _ -> startManual("set_channel", ch, "$what…") }
                .show()
        }
    }

    /** 手动设置的落地：只跑一条命令，不碰体检结论、不碰守护。 */
    private fun startManual(fn: String, value: Int, busyText: String) {
        if (busy) return
        busy = true
        busyKind = fn
        // 手动改过之后，屏幕上那条体检结论就过期了。留着它等于骗人 ——
        // 用户会以为「建议里写的是 BAND 3」，而设备已经被他自己改成别的了。
        hideVerdict()
        btnMain.text = busyText
        btnMain.isEnabled = false
        btnMain.alpha = 0.6f
        showLog(busyText)
        Py.callLong(this, fn, value) { txt ->
            val o = json(txt)
            if (o == null || !o.optBoolean("ok", false)) {
                showLog("没启动起来：" + (o?.optString("msg") ?: txt.take(200)))
                busy = false
                resetMain()
                btnMain.isEnabled = true
                btnMain.alpha = 1f
            }
            // 起来之后 busy 交给 pollJob 在任务结束时复位，和体检/恢复走同一条路
        }
    }

    /**
     * 一组可选值 +「自动」。选中项自己画高亮 —— 整个界面是手写的 LinearLayout，
     * 没有 Material 的 Chip 可用，选中态只能自己涂。
     */
    private fun pickDialog(
        title: String,
        sub: String,
        values: List<Int>,
        current: Int,
        label: (Int) -> String,
        autoLabel: String,
        allowAuto: Boolean,
        onPick: (Int) -> Unit
    ) {
        val all = ArrayList<Int>()
        if (allowAuto) all.add(0)
        all.addAll(values)

        // 设备当前值不在候选里（比如固件报了个 14）就落到「自动」，别显示成没选
        var picked = if (all.contains(current)) current else 0
        val chips = LinkedHashMap<Int, TextView>()

        fun paint() {
            for ((v, tv) in chips) {
                val on = v == picked
                tv.setTextColor(if (on) Color.WHITE else INK)
                (tv.background as? GradientDrawable)?.setColor(
                    if (on) ACCENT else Color.parseColor("#EFEFF3")
                )
            }
        }

        val grid = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        // 一行 4 个：13 个信道排 4 行，弹窗不会顶出屏幕
        for (group in all.chunked(4)) {
            val r = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(0, dp(8), 0, 0)
            }
            for (i in 0 until 4) {
                if (i > 0) r.addView(spacerW(8))
                val v = group.getOrNull(i)
                if (v == null) {
                    // 末行补空位，否则剩下的一个会拉满整行，看着像主按钮
                    r.addView(View(this), LinearLayout.LayoutParams(0, dp(1), 1f))
                    continue
                }
                val tv = TextView(this).apply {
                    text = if (v == 0) autoLabel else label(v)
                    gravity = Gravity.CENTER
                    setTextSize(TypedValue.COMPLEX_UNIT_SP, 13.5f)
                    setTypeface(typeface, Typeface.BOLD)
                    background = GradientDrawable().apply { cornerRadius = dp(12).toFloat() }
                    setPadding(dp(2), dp(11), dp(2), dp(11))
                    isClickable = true
                    isFocusable = true
                }
                tv.setOnClickListener {
                    picked = v
                    paint()
                }
                chips[v] = tv
                r.addView(tv, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            }
            grid.addView(r)
        }
        paint()

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.WHITE)
            setPadding(dp(16), dp(14), dp(16), dp(12))
        }
        root.addView(TextView(this).apply {
            text = title
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(INK)
        })
        root.addView(TextView(this).apply {
            text = sub
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12.5f)
            setTextColor(MUTED)
            setPadding(0, dp(6), 0, 0)
        })
        root.addView(grid)

        val dlg = Dialog(this)
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        val cancel = actionButton("取消", false)
        val okBtn = actionButton("应用", true)
        cancel.setOnClickListener { dlg.dismiss() }
        okBtn.setOnClickListener {
            dlg.dismiss()
            onPick(picked)
        }
        bar.addView(cancel, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        bar.addView(spacerW(10))
        bar.addView(okBtn, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        root.addView(bar, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
        ).apply { topMargin = dp(14) })

        dlg.setContentView(root)
        dlg.window?.setLayout(
            (resources.displayMetrics.widthPixels * 0.92f).toInt(),
            ViewGroup.LayoutParams.WRAP_CONTENT
        )
        dlg.show()
    }

    private fun runTask(fn: String, okMsg: () -> String) {
        // 用长任务通道：它在 APK 里是同步跑完的，挤在查询线程上会让状态、
        // 日志、结论全部排队，界面上就是「按钮半天不变」。
        Py.callLong(this, fn) { txt ->
            val o = json(txt)
            if (o != null && o.optBoolean("ok", false)) {
                showLog(okMsg())
            } else {
                showLog("启动失败：" + (o?.optString("msg") ?: txt.take(200)))
                busy = false
                btnMain.isEnabled = true
                btnMain.alpha = 1f
                btnMain.text = "开始体检并优化"
            }
        }
    }

    private fun doScan(btn: TextView) {
        if (guardOn) {
            showLog("守护在跑，先关掉它再扫信道")
            return
        }
        val old = btn.text
        btn.isEnabled = false
        btn.text = "扫描中…"
        Py.call(this, "scan") { txt ->
            btn.isEnabled = true
            btn.text = old
            val o = json(txt)
            // isNull 对「键不存在」和「值是 null」都返回 true，两种都得算没有。
            // 不能直接 optString 后判空：那次踩过的坑是拿回一个 "null" 字面量。
            val err = if (o == null || o.isNull("error")) "" else o.optString("error")
            val best = if (o == null || o.isNull("best")) 0 else o.optInt("best", 0)
            val cur = if (o == null || o.isNull("current")) 0 else o.optInt("current", 0)
            when {
                o == null -> showLog("扫描失败：${txt.take(200)}")
                err.isNotEmpty() -> showLog("扫描失败：$err")
                best <= 0 -> showLog("扫到了，但没有可推荐的信道")
                // 信道设成「自动」时设备报回来的是 0，直接拼进句子就成了
                // 「现在信道 0」—— 用户会以为自己看错了。
                best == cur -> showLog("信道 $cur 已经是周围最干净的")
                else -> {
                    // 信道建议也要能落地：2026-11-01 用户原话「信道也是一样的
                    // 问题，我都点击了，你还给我建议了，但是没有应用建议和
                    // 不用了的按钮」。所以结论卡片对频段和信道是同一套。
                    showVerdict(
                        "把信道固定在 $best",
                        if (cur <= 0) "现在是自动的，设备自己挑的未必最干净"
                        else "现在是 $cur，$best 号周围最空",
                        "channel"
                    )
                    tvMainHint.text = "换信道会断一次网 · 不想改就点「不用了」"
                }
            }
        }
    }

    /**
     * 打开 App 时对一次守护的真实状态。
     *
     * 两种情况会不一致：
     *   · 守护还在跑、但界面是新起的（开关默认关着）→ 要拨成开；
     *   · 守护已经没了、通知还挂着 → 要把服务收掉。
     * 不和的话，用户会看到「关着」却一直在跑，点开关等于又启动一次。
     */
    private fun syncGuardOnce() {
        Py.callFast(this, "guard_state") { txt ->
            val o = json(txt) ?: return@callFast
            val running = o.optBoolean("running")
            if (running) {
                guardOn = true
                swGuard.setOnCheckedChangeListener(null)
                swGuard.isChecked = true
                swGuard.setOnCheckedChangeListener { _: CompoundButton, x: Boolean ->
                    toggleGuard(x)
                }
                // 服务不一定还在（进程被杀过），补起来
                GuardService.start(this@MainActivity)
            } else {
                GuardService.stop(this@MainActivity)
            }
        }
    }

    private fun toggleGuard(on: Boolean) {
        if (on) {
            tvGuardHint.text = "正在启动…"
            // 顺序很讲究：前台服务必须从**前台**的 Activity 起来（Android 12+
            // 不许后台启动前台服务），所以先起服务、再起 Python 线程。
            // 起反了的话，guard_start 会在别的线程跑，那会儿 Activity 可能已经
            // 退到后面，服务就起不来 —— 表现是「开关亮着，一按 Home 就停」。
            GuardService.start(this)
            askNotificationPermission()
            Py.call(this, "guard_start") { txt ->
                val o = json(txt)
                if (o?.optBoolean("ok", false) != true) {
                    // 没起来就把服务收掉，别留一条「守护在跑」的通知骗人
                    GuardService.stop(this@MainActivity)
                    swGuard.setOnCheckedChangeListener(null)
                    swGuard.isChecked = false
                    swGuard.setOnCheckedChangeListener { _: CompoundButton, x: Boolean -> toggleGuard(x) }
                    tvGuardHint.text = "没起来：" + (o?.optString("msg") ?: txt.take(120))
                } else {
                    guardOn = true
                    tvGuardHint.text = "运行中 · 你走动时它会松开频段锁"
                    // 这里不主动把日志区拉出来：守护第一行输出要等它连上设备，
                    // 先摆一个空黑框只会让人以为又卡住了。日志一到 showJob 会显示。
                    suggestBatteryUnrestricted()
                }
            }
        } else {
            guardOn = false
            tvGuardHint.text = "正在停…"
            // 先撤服务，再停线程：反过来会有一小段时间「线程还在跑，但已经不算
            // 前台」，那几秒正好是最容易被系统抓住杀掉的时候。
            GuardService.stop(this)
            Py.call(this, "guard_stop") { _ ->
                tvGuardHint.text = "关着"
            }
        }
    }

    /**
     * Android 13+ 没给通知权限的话，前台服务的通知不显示，但服务仍然在跑。
     * 不显示等于用户没法从通知栏看到「它还在」，也没法一眼确认它还活着 ——
     * 所以请求一次，拒了也不影响功能。
     */
    private fun askNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
            == PackageManager.PERMISSION_GRANTED) return
        requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 12)
    }

    /**
     * 问一次「要不要把它加进电池优化白名单」。
     *
     * 前台服务能挡住 AOSP 的回收，但挡不住 MIUI 自己那层省电策略 —— 实机日志里
     * 那次 238 秒的采样空档就是它干的。这一步能明显减少那种情况，所以值得多问
     * 一次；用户拒绝也不影响其他功能，只是后台被冻的概率高一些。
     *
     * 只问一次：记在 SharedPreferences 里，别每次开守护都弹，那会变成骚扰。
     */
    private fun suggestBatteryUnrestricted() {
        val pm = getSystemService(POWER_SERVICE) as android.os.PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) return
        val sp = getSharedPreferences("mifi", MODE_PRIVATE)
        if (sp.getBoolean("asked_battery", false)) return
        sp.edit().putBoolean("asked_battery", true).apply()

        AlertDialog.Builder(this)
            .setTitle("让它一直在后台跑")
            .setMessage(
                "守护要靠每 30 秒采一次样才知道你有没有在走动。\n\n" +
                "系统默认会把这类的后台活动冻住——日志里已经出现过一次" +
                "「隔了 238 秒才醒来」的记录，那段时间它在睡觉，你走了它也不知道。\n\n" +
                "把本应用设为「不受电池优化限制」能明显减少这种情况。")
            .setPositiveButton("去设置") { _, _ ->
                runCatching {
                    startActivity(android.content.Intent(
                        android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        android.net.Uri.parse("package:$packageName")))
                }
            }
            .setNegativeButton("先不用", null)
            .show()
    }

    // ---------------- 小工具 ----------------

    /**
     * 一行状态，不再往界面上堆日志。
     *
     * 主界面要保证「详情展开也在一个屏幕里」，就不能有会越长越高的区域。
     * 所以进度只取最新一行，全文走 [showLogDialog] 弹出来看。
     */
    private fun showLog(text: String) {
        tvMainHint.text = text.lineSequence().lastOrNull()?.take(80) ?: ""
    }

    private fun showJob(o: JSONObject) {
        val lines = o.optJSONArray("lines") ?: return
        if (lines.length() == 0) return
        showLog(lines.optString(lines.length() - 1))
    }

    /**
     * 完整日志：独立弹窗 + 独立滚动。
     *
     * 这里必须是 Dialog 自己搭布局，不能用 AlertDialog.setView —— 它会把内容
     * 塞进一个自带的 ScrollView，于是滑动落到外层，日志那一块自己滚不动，
     * 手指在日志上划却在拖动整个页面。给 ScrollView 一个固定的、按屏幕算出来的
     * 高度，滚动就锁死在框里了。
     */
    private fun showLogDialog() {
        val tv = TextView(this).apply {
            text = renderLog(logLines)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
            setTextColor(LOG_INK)
            setPadding(dp(12), dp(12), dp(12), dp(12))
        }
        // 底色给 ScrollView，不给 TextView：日志只有两三行的时候，TextView
        // 只有那么高，黑底就只剩顶上一条，下面一大片白 —— 看着像框破了。
        val sv = ScrollView(this).apply {
            setBackgroundColor(Color.parseColor("#14161A"))
            addView(tv, ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
            isVerticalScrollBarEnabled = true
            minimumHeight = dp(96)
        }

        val dlg = Dialog(this)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.WHITE)
            setPadding(dp(16), dp(14), dp(16), dp(12))
        }
        root.addView(TextView(this).apply {
            text = "运行日志"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(INK)
        })
        root.addView(sv, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, logHeight(logLines.size)).apply {
            topMargin = dp(10)
        })

        val close = actionButton("关闭", false)
        close.setOnClickListener { dlg.dismiss() }
        root.addView(close, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
        ).apply { topMargin = dp(10) })

        dlg.setContentView(root)
        dlg.window?.setLayout(
            (resources.displayMetrics.widthPixels * 0.92f).toInt(),
            ViewGroup.LayoutParams.WRAP_CONTENT
        )
        dlg.show()
        // 日志是往下追加的，打开就该停在最新一行，不用每次手划到底
        sv.post { sv.fullScroll(View.FOCUS_DOWN) }

        // 上面那屏是缓存 —— 点下去立刻就有东西，不依赖 Python 那边。这里再补
        // 一次最新的：长任务在另一条线程上跑，这次往返不会被它堵住，几百毫秒就回。
        Py.callFast(this, "job") { txt ->
            val arr = json(txt)?.optJSONArray("lines") ?: return@callFast
            logLines = (0 until arr.length()).map { arr.optString(it) }
            if (!dlg.isShowing) return@callFast   // 用户已经关掉了，白改
            tv.text = renderLog(logLines)
            sv.layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, logHeight(logLines.size)
            ).apply { topMargin = dp(10) }
            sv.post { sv.fullScroll(View.FOCUS_DOWN) }
        }
    }

    private fun renderLog(lines: List<String>): String =
        if (lines.isEmpty()) "还没有日志。\n跑一次体检，或把守护打开一会儿再看。"
        else lines.joinToString("\n").trimEnd()

    /** 行数少就贴着内容长，行数多才给固定的半屏高 —— 滚动锁在框里。 */
    private fun logHeight(n: Int): Int =
        if (n <= 12) ViewGroup.LayoutParams.WRAP_CONTENT
        else (resources.displayMetrics.heightPixels * 0.52f).toInt()

    private fun json(s: String): JSONObject? = try {
        JSONObject(s)
    } catch (t: Throwable) {
        null
    }

    /**
     * 从 "-99dBm" / "3dB" / "-14.5dB" 这种带单位的串里取数字。
     * 设备返回的就是这样，别指望它是纯数字。
     */
    private fun num(s: String?): Int? {
        if (s.isNullOrBlank() || s == "null") return null
        val m = Regex("-?\\d+(?:\\.\\d+)?").find(s) ?: return null
        return m.value.toDoubleOrNull()?.toInt()
    }

    /** JSON 数组 → Int 列表。设备能力里这些值一定是整数，取不出来就跳过。 */
    private fun intList(a: JSONArray?): List<Int> {
        if (a == null) return emptyList()
        val out = ArrayList<Int>(a.length())
        for (i in 0 until a.length()) out.add(a.optInt(i, 0))
        return out.filter { it > 0 }
    }

    private fun card() = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        background = GradientDrawable().apply {
            setColor(Color.WHITE)
            cornerRadius = dp(18).toFloat()
        }
        setPadding(dp(16), dp(16), dp(16), dp(16))
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
        ).apply { bottomMargin = dp(12) }
    }

    private fun row() = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
    }

    /** 一个读数：小标题 + 大数字。不设可点击属性，让它看起来就不像按钮。 */
    private fun metric(parent: LinearLayout, label: String, value: String, big: Boolean): TextView {
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        col.addView(TextView(this).apply {
            text = label
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setTextColor(MUTED)
        })
        val v = TextView(this).apply {
            text = value
            setTextSize(TypedValue.COMPLEX_UNIT_SP, if (big) 20f else 16f)
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(INK)
            setPadding(0, dp(4), 0, 0)
        }
        col.addView(v)
        parent.addView(col)
        return v
    }

    /**
     * 体检结论下面那两个键。
     *
     * 和主按钮（[actionButton]）分开写，因为这两个是「对一句话表态」，
     * 需要一眼看出主次：
     *   · 主键：渐变蓝 + 轻微上浮 + 按下去有水波纹，占两份宽；
     *   · 次键：白底细描边，占一份宽 —— 看得见、点得到，但不抢注意力。
     */
    private fun verdictButton(text: String, primary: Boolean) = TextView(this).apply {
        this.text = text
        gravity = Gravity.CENTER
        setTextSize(TypedValue.COMPLEX_UNIT_SP, if (primary) 16f else 14f)
        setTypeface(typeface, Typeface.BOLD)
        setTextColor(if (primary) Color.WHITE else INK)
        val shape = GradientDrawable().apply {
            cornerRadius = dp(16).toFloat()
            if (primary) {
                // 斜向渐变比纯色更有「按一下会发生事情」的感觉，也不至于太花。
                orientation = GradientDrawable.Orientation.TL_BR
                colors = intArrayOf(Color.parseColor("#4C8DFF"),
                                    Color.parseColor("#1657E0"))
            } else {
                setColor(Color.WHITE)
                setStroke(dp(1), Color.parseColor("#DFE2E8"))
            }
        }
        background = RippleDrawable(
            ColorStateList.valueOf(
                if (primary) Color.parseColor("#40FFFFFF")
                else Color.parseColor("#14000000")), shape, null)
        if (primary) elevation = dp(6).toFloat()
        setPadding(dp(16), dp(15), dp(16), dp(15))
        isClickable = true
        isFocusable = true
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
        )
    }

    private fun actionButton(text: String, primary: Boolean) = TextView(this).apply {
        this.text = text
        gravity = Gravity.CENTER
        setTextSize(TypedValue.COMPLEX_UNIT_SP, if (primary) 16f else 14f)
        setTypeface(typeface, Typeface.BOLD)
        setTextColor(if (primary) Color.WHITE else INK)
        background = GradientDrawable().apply {
            cornerRadius = dp(14).toFloat()
            setColor(if (primary) ACCENT else Color.parseColor("#EFEFF3"))
        }
        setPadding(dp(16), if (primary) dp(16) else dp(12), dp(16), if (primary) dp(16) else dp(12))
        isClickable = true
        isFocusable = true
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
        )
    }

    private fun spacer(h: Int) = View(this).apply {
        layoutParams = LinearLayout.LayoutParams(1, dp(h))
    }

    private fun spacerW(w: Int) = View(this).apply {
        layoutParams = LinearLayout.LayoutParams(dp(w), 1)
    }

    private fun dp(v: Int): Int =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()

    // ---------------- 权限 ----------------

    private fun requestScanPermissions() {
        val need = mutableListOf<String>()
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
            != PackageManager.PERMISSION_GRANTED
        ) {
            need += Manifest.permission.ACCESS_FINE_LOCATION
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.NEARBY_WIFI_DEVICES)
            != PackageManager.PERMISSION_GRANTED
        ) {
            need += Manifest.permission.NEARBY_WIFI_DEVICES
        }
        if (need.isNotEmpty()) requestPermissions(need.toTypedArray(), REQ_SCAN)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        // 权限刚给上，启动时写的空结果该重写一次，不用等下一轮 60 秒
        if (requestCode == REQ_SCAN) WifiScanBridge.refresh()
    }

    override fun onDestroy() {
        Main.clear()   // 轮询是自我重投递的，必须显式清掉，否则界面销毁后还在跑
        super.onDestroy()
    }

    companion object {
        private const val TAG = "MifiApk"
        private const val REQ_SCAN = 1001

        // 任务种类。目前只有体检需要冻住读数（它要挨个切频段，数字会乱跳）。
        private const val KIND_PREVIEW = "preview"

        private val BG = Color.parseColor("#F7F7F5")
        private val INK = Color.parseColor("#16181D")
        private val MUTED = Color.parseColor("#8A8F98")
        private val ACCENT = Color.parseColor("#1B6EF3")
        private val OK = Color.parseColor("#1FA971")
        private val WARN = Color.parseColor("#E8890C")
        private val ERR = Color.parseColor("#D94A3D")
        private val LOG_INK = Color.parseColor("#C9D1C9")
    }
}
