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
 * 原生界面。上半屏是「现在怎么样 + 一键体检」，下半屏是「守护模式」。
 *
 * 为什么从 WebView 换成原生：网页那套是给电脑浏览器设计的，搬到手机上
 * 一堆只读卡片长得像按钮、点不动；而且面板里还塞着「装 Termux」这种
 * 跟 app 无关的内容。原生的好处是每个可点的东西就是一个真按钮，不可点的
 * 就明确不长得像按钮。
 *
 * 数据全部来自 Python（bridge.py），这里只负责显示；所有跨语言调用都走
 * Py.call，它保证串行 + 后台 + 异常转文字。
 */
class MainActivity : Activity() {

    // 状态区
    private lateinit var tvConn: TextView
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
    }

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

    // ---------------- 界面搭建 ----------------

    private fun buildUi(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(BG)
            setPadding(dp(18), dp(44), dp(18), dp(28))
        }

        root.addView(TextView(this).apply {
            text = "随身WiFi 调优"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 24f)
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(INK)
        })

        tvConn = TextView(this).apply {
            text = "正在连接…"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            setTextColor(MUTED)
            setPadding(0, dp(6), 0, dp(16))
        }
        root.addView(tvConn)

        root.addView(statusCard())
        root.addView(primaryCard())
        root.addView(guardCard())
        root.addView(toolsCard())

        return ScrollView(this).apply {
            isFillViewport = true
            addView(root)
        }
    }

    /** 上半屏：四个读数。明确做成「不可点」的样子 —— 没有按钮边框、没有按压反馈。 */
    private fun statusCard(): View {
        val box = card()
        val row1 = row()
        val row2 = row()

        tvBand = metric(row1, "LTE 频段", "—", big = true)
        tvChan = metric(row1, "WiFi 信道", "—", big = true)
        tvRsrp = metric(row2, "信号强度", "—", big = false)
        tvSinr = metric(row2, "信噪比", "—", big = false)

        box.addView(row1)
        box.addView(spacer(10))
        box.addView(row2)

        tvLat = TextView(this).apply {
            text = "延迟 — · 丢包 —"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            setTextColor(MUTED)
            setPadding(0, dp(12), 0, 0)
        }
        box.addView(tvLat)
        return box
    }

    /** 主操作：一个大按钮 + 展开后才出现的进度区。 */
    private fun primaryCard(): View {
        val box = card()

        btnMain = actionButton("开始体检并优化", primary = true)
        btnMain.setOnClickListener { startPreview() }
        box.addView(btnMain)

        box.addView(TextView(this).apply {
            text = "会逐个切换频段实测，约 3~6 分钟，中途断网几次；跑完给结论，不点应用就不改设备"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setTextColor(MUTED)
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
        // 必须是可滚动的：体检和守护都能刷出上百行，直接塞进固定高度的
        // TextView 只会把超出的部分裁掉，用户以为卡住了。
        jobScroll = ScrollView(this).apply { addView(tvJob) }
        jobBox.addView(jobScroll, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, dp(200)
        ))
        box.addView(jobBox)
        return box
    }

    /** 下半屏：守护模式。开关是真开关，旁边写清楚它现在在干什么。 */
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
            text = "关着。打开后它自己盯着：你一走动就松开频段锁，坐满 10 分钟才锁住，换个地方会自己重选。"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12.5f)
            setTextColor(MUTED)
            setLineSpacing(dp(3).toFloat(), 1f)
            setPadding(0, dp(8), 0, 0)
        }
        box.addView(tvGuardHint)
        return box
    }

    private fun toolsCard(): View {
        val box = card()
        val r = row()

        val scan = actionButton("扫描信道", primary = false)
        scan.setOnClickListener { doScan(scan) }
        val restore = actionButton("恢复默认", primary = false)
        restore.setOnClickListener { runTask("restore") { "已请求恢复默认设置" } }

        val lp = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        r.addView(scan, lp)
        r.addView(spacerW(10))
        r.addView(restore, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        box.addView(r)
        return box
    }

    // ---------------- 数据刷新 ----------------

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
                val hasLines = (o.optJSONArray("lines")?.length() ?: 0) > 0
                // hasLines 这一条是给守护用的：守护不走 JOB 的 running/kind，
                // 只往日志缓冲里写，不加这行它一整套输出都会被判成「没事可显示」。
                if (running || kind.isNotEmpty() || exit != 0 || hasLines) showJob(o)
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

    private fun refreshStatus() {
        // 上一次还没回来就别再发：每次 status 都要真连设备，堆起来只会越来越慢
        if (statusBusy || !Py.isReady) return
        statusBusy = true
        Py.call(this, "status") { txt ->
            statusBusy = false
            val o = json(txt) ?: run {
                tvConn.text = "● 连不上设备"
                tvConn.setTextColor(ERR)
                return@call
            }
            if (o.has("error")) {
                tvConn.text = "● 连不上：${o.optString("error").take(60)}"
                tvConn.setTextColor(ERR)
                return@call
            }
            val ping = o.optJSONObject("ping") ?: JSONObject()
            val loss = ping.optDouble("loss", -1.0)
            val lat = ping.optDouble("avg", -1.0)
            tvConn.text = "● 已连接" +
                (if (lat >= 0) " · 延迟 ${lat.toInt()}ms" else "") +
                (if (loss >= 0) " · 丢包 ${loss.toInt()}%" else "")
            tvConn.setTextColor(OK)

            tvBand.text = "BAND ${o.optInt("band", 0).takeIf { it > 0 } ?: "—"}"
            tvChan.text = o.optInt("channel", 0).takeIf { it > 0 }?.toString() ?: "自动"
            // 设备给的是带单位的字符串（"-99dBm" / "3dB"）。浏览器里 parseInt 会
            // 自动截出数字，Kotlin 的 optInt 直接失败返回 0 —— 这就是前一版这两格
            // 一直是「—」的原因。这里显式剥掉单位。
            val lte = o.optJSONObject("lte")
            val rsrp = num(lte?.optString("rsrp"))
            val sinr = num(lte?.optString("sinr"))
            tvRsrp.text = if (rsrp != null) "$rsrp dBm" else "—"
            tvSinr.text = if (sinr != null) "$sinr dB" else "—"
            tvLat.text = "延迟 " + (if (lat >= 0) "${lat.toInt()} ms" else "—") +
                " · 丢包 " + (if (loss >= 0) "${loss.toInt()}%" else "—")
        }
    }

    // ---------------- 动作 ----------------

    private fun startPreview() {
        if (busy) return
        // 守护开着的时候它自己也在连设备、也在切频段。这时候再跑体检，两边会
        // 抢同一台路由器的登录和射频状态，结果是谁都不准。先说清楚，别硬跑。
        if (guardOn) {
            jobBox.visibility = View.VISIBLE
            tvJob.text = "守护模式正在运行，体检会和它抢设备。\n先在上面把守护关掉，再体检。"
            return
        }
        busy = true
        btnMain.text = "体检中…（约 3~6 分钟）"
        btnMain.isEnabled = false
        btnMain.alpha = 0.6f
        jobBox.visibility = View.VISIBLE
        tvJob.text = "正在启动…"
        runTask("preview") { "已开始体检" }
    }

    private fun runTask(fn: String, okMsg: () -> String) {
        Py.call(this, fn) { txt ->
            val o = json(txt)
            if (o != null && o.optBoolean("ok", false)) {
                jobBox.visibility = View.VISIBLE
                tvJob.text = okMsg()
            } else {
                jobBox.visibility = View.VISIBLE
                tvJob.text = "启动失败：" + (o?.optString("msg") ?: txt.take(200))
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
            jobBox.visibility = View.VISIBLE
            tvJob.text = when {
                o == null -> "扫描失败：${txt.take(200)}"
                o.optString("error").isNotEmpty() -> "扫描失败：${o.optString("error")}"
                o.optInt("best", 0) > 0 ->
                    "当前信道 ${o.optInt("current")}，建议换到 ${o.optInt("best")}\n" +
                    "周边 ${o.optJSONArray("aps")?.length() ?: 0} 个 AP"
                else -> "扫到了，但没有可推荐的信道"
            }
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
                    tvGuardHint.text = "正在运行。它每 30 秒看一眼：在动就松开频段锁，坐满 10 分钟才锁住。"
                    // 这里不主动把日志区拉出来：守护第一行输出要等它连上设备，
                    // 先摆一个空黑框只会让人以为又卡住了。日志一到 showJob 会显示。
                }
            }
        } else {
            guardOn = false
            tvGuardHint.text = "正在停止（最多等 30 秒，它会先把手头这一轮跑完）…"
            Py.call(this, "guard_stop") { _ ->
                tvGuardHint.text = "已请求停止，设备保持当前设置。"
            }
        }
    }

    // ---------------- 小工具 ----------------

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
            setTextSize(TypedValue.COMPLEX_UNIT_SP, if (big) 22f else 17f)
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
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
        setTypeface(typeface, Typeface.BOLD)
        setTextColor(if (primary) Color.WHITE else INK)
        background = GradientDrawable().apply {
            cornerRadius = dp(14).toFloat()
            setColor(if (primary) ACCENT else Color.parseColor("#EFEFF3"))
        }
        setPadding(dp(16), dp(14), dp(16), dp(14))
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
        private val ERR = Color.parseColor("#D94A3D")
        private val LOG_INK = Color.parseColor("#C9D1C9")
    }
}
