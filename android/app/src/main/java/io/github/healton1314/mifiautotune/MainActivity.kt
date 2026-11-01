package io.github.mifiautotune.app

import android.annotation.SuppressLint
import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
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
    private lateinit var jobBox: LinearLayout
    private lateinit var jobScroll: ScrollView
    private lateinit var tvJob: TextView

    // 守护
    private lateinit var swGuard: Switch
    private lateinit var tvGuardHint: TextView

    private var busy = false
    private var statusBusy = false
    private var guardOn = false
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
            Main.post { refreshStatus() }
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
        restore.setOnClickListener { runTask("restore") { "已请求恢复默认设置" } }
        tools.addView(scan, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        tools.addView(spacerW(10))
        tools.addView(restore, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        box.addView(spacer(12))
        box.addView(tools)

        // 日志平时不摆出来（守护的刷屏尤其吵），想看的时候从这儿调
        val logBtn = actionButton("查看运行日志", false)
        logBtn.setOnClickListener { showLogFromBuffer() }
        box.addView(spacer(10))
        box.addView(logBtn)
        return box
    }

    /** 主操作：一个按钮，一句时长说明。没有第二句。 */
    private fun primaryCard(): View {
        val box = card()

        btnMain = actionButton("开始体检并优化", true)
        btnMain.setOnClickListener { startPreview() }
        box.addView(btnMain)

        box.addView(TextView(this).apply {
            text = "约 3~6 分钟，中途会断网"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setTextColor(MUTED)
            setGravity(Gravity.CENTER)
            setPadding(0, dp(10), 0, 0)
        })

        jobBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            setPadding(0, dp(14), 0, 0)
        }
        tvJob = TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11.5f)
            setTextColor(LOG_INK)
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
            setBackgroundColor(Color.parseColor("#14161A"))
            setPadding(dp(12), dp(10), dp(12), dp(10))
        }
        // 必须是可滚动的：体检和守护都能刷出上百行，塞进固定高度的 TextView
        // 只会把超出的部分裁掉，用户以为卡住了。
        jobScroll = ScrollView(this).apply { addView(tvJob) }
        jobBox.addView(jobScroll, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, dp(200)
        ))
        box.addView(jobBox)
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
            setOnCheckedChangeListener { _: CompoundButton, on: Boolean -> toggleGuard(on) }
        }
        head.addView(swGuard)
        box.addView(head)

        tvGuardHint = TextView(this).apply {
            text = "关着"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12.5f)
            setTextColor(MUTED)
            setPadding(0, dp(6), 0, 0)
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
            Py.call(this@MainActivity, "job") { txt ->
                val o = json(txt) ?: return@call
                val running = o.optBoolean("running")
                // org.json 的 optString 对 JSON null 会返回字符串 "null"，
                // 直接拿它判空会让日志区一直挂着。这里显式问 isNull。
                val kind = if (o.isNull("kind")) "" else o.optString("kind")
                val exit = if (o.isNull("exit")) 0 else o.optInt("exit", 0)
                // 只有「体检/应用这类一次性任务」才把日志摆出来。
                // 守护是常驻的，每 30 秒刷一行，摆出来就是一大坨噪音 —— 它的
                // 状态放在开关下面那一行就够了；真要看日志去详情里点。
                if (running || kind.isNotEmpty() || exit != 0) showJob(o)
                if (!running && busy) {
                    busy = false
                    btnMain.text = "开始体检并优化"
                    btnMain.isEnabled = true
                    btnMain.alpha = 1f
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
                Py.call(this@MainActivity, "guard_state") { txt ->
                    val o = json(txt) ?: return@call
                    if (!o.optBoolean("running")) {
                        if (guardOn) {
                            guardOn = false
                            swGuard.setOnCheckedChangeListener(null)
                            swGuard.isChecked = false
                            swGuard.setOnCheckedChangeListener { _: CompoundButton, x: Boolean ->
                                toggleGuard(x)
                            }
                            tvGuardHint.text = "已经停了。点「查看运行日志」看原因"
                        }
                        return@call
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

            // 状态行只说一句人话：好不好。数字都放详情里。
            val slow = (loss >= 8) || (lat >= 200)
            tvConn.text = when {
                slow -> "网络偏慢" + if (band > 0) " · BAND $band" else ""
                else -> "网络正常" + if (band > 0) " · BAND $band" else ""
            }
            tvConn.setTextColor(if (slow) WARN else OK)

            // 详情里的数
            tvBand.text = if (band > 0) "BAND $band" else "—"
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
            showLog("守护模式正在运行，体检会和它抢设备。\n先把下面那个开关关掉，再体检。")
            return
        }
        busy = true
        btnMain.text = "体检中…约 3~6 分钟"
        btnMain.isEnabled = false
        btnMain.alpha = 0.6f
        showLog("正在启动…")
        runTask("preview") { "已开始体检" }
    }

    private fun runTask(fn: String, okMsg: () -> String) {
        Py.call(this, fn) { txt ->
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
        val old = btn.text
        btn.isEnabled = false
        btn.text = "扫描中…"
        Py.call(this, "scan") { txt ->
            btn.isEnabled = true
            btn.text = old
            val o = json(txt)
            showLog(when {
                o == null -> "扫描失败：${txt.take(200)}"
                o.optString("error").isNotEmpty() -> "扫描失败：${o.optString("error")}"
                o.optInt("best", 0) > 0 ->
                    "当前信道 ${o.optInt("current")}，建议换到 ${o.optInt("best")}"
                else -> "扫到了，但没有可推荐的信道"
            })
        }
    }

    private fun toggleGuard(on: Boolean) {
        if (on) {
            tvGuardHint.text = "正在启动…"
            Py.call(this, "guard_start") { txt ->
                val o = json(txt)
                if (o?.optBoolean("ok", false) != true) {
                    swGuard.setOnCheckedChangeListener(null)
                    swGuard.isChecked = false
                    swGuard.setOnCheckedChangeListener { _: CompoundButton, x: Boolean -> toggleGuard(x) }
                    tvGuardHint.text = "没起来：" + (o?.optString("msg") ?: txt.take(120))
                } else {
                    guardOn = true
                    tvGuardHint.text = "运行中 · 你走动时它会松开频段锁"
                    // 这里不主动把日志区拉出来：守护第一行输出要等它连上设备，
                    // 先摆一个空黑框只会让人以为又卡住了。日志一到 showJob 会显示。
                }
            }
        } else {
            guardOn = false
            tvGuardHint.text = "正在停…"
            Py.call(this, "guard_stop") { _ ->
                tvGuardHint.text = "关着"
            }
        }
    }

    // ---------------- 小工具 ----------------

    /** 日志区统一入口：有内容才露出来，避免空黑框。 */
    private fun showLog(text: String) {
        tvJob.text = text
        jobBox.visibility = View.VISIBLE
    }

    /**
     * 从「详情」里主动要日志。
     *
     * 平时不显示是有意的：守护每 30 秒刷一行，摆出来就是一屏噪音。但排障时
     * 又确实需要看到它，所以留这个入口 —— 点完把它滚到可见位置。
     */
    private fun showLogFromBuffer() {
        Py.call(this, "job") { txt ->
            val o = json(txt)
            val lines = o?.optJSONArray("lines")
            if (o == null || lines == null || lines.length() == 0) {
                showLog("还没有日志。跑一次体检，或把守护打开一会儿再看。")
            } else {
                showJob(o)
            }
            jobBox.post {
                jobBox.requestRectangleOnScreen(
                    android.graphics.Rect(0, 0, jobBox.width, jobBox.height), true
                )
            }
        }
    }

    private fun showJob(o: JSONObject) {
        val lines = o.optJSONArray("lines") ?: return
        if (lines.length() == 0) return
        val sb = StringBuilder()
        for (i in 0 until lines.length()) sb.append(lines.optString(i)).append('\n')
        tvJob.text = sb.toString().trimEnd()
        jobBox.visibility = View.VISIBLE
        // 跟着最新一行走：用户不需要手动拖到最底下才看见当前在干什么
        jobScroll.post { jobScroll.fullScroll(View.FOCUS_DOWN) }
    }

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
