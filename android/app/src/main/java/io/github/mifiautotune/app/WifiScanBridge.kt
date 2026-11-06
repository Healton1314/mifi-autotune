package io.github.mifiautotune.app

import android.annotation.SuppressLint
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.wifi.WifiManager
import android.os.Handler
import android.os.Looper
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * 把系统扫到的周边 Wi-Fi 写成 JSON，给 Python 侧的 scanner.py 读。
 *
 * 为什么用文件而不是让 Python 直接调 WifiManager：扫描是异步的（请求 → 等系统
 * 回调 → 取结果）。在 Kotlin 里做完再落盘，Python 那边就只剩「读文件 + 解析」，
 * 而且跟 Termux 那条路的输入格式完全一致 —— 解析代码只有一份。
 *
 * 安卓的限流绕不过去：Android 10 起对前台应用是「两分钟最多 4 次」，超了
 * startScan 照样返回 true 但结果不更新。所以这里 60 秒扫一次，并且不假设每
 * 次都真扫到了 —— 广播里拿到什么就写什么，结果上带文件时间戳，Python 侧
 * 据此判断数据是不是过期了。
 */
object WifiScanBridge {

    private const val TAG = "MifiApk"
    private const val FILE_NAME = "wifi_scan.json"
    private const val INTERVAL_MS = 60_000L

    private var appCtx: Context? = null
    private var wifi: WifiManager? = null
    private var handler: Handler? = null
    private var started = false

    private val tick = object : Runnable {
        override fun run() {
            requestScan()
            handler?.postDelayed(this, INTERVAL_MS)
        }
    }

    fun start(ctx: Context) {
        if (started) return
        val app = ctx.applicationContext
        val wm = app.getSystemService(Context.WIFI_SERVICE) as? WifiManager
        if (wm == null) {
            Log.w(TAG, "拿不到 WifiManager，信道扫描不可用")
            return
        }
        appCtx = app
        wifi = wm

        // 不管 EXTRA_RESULTS_UPDATED 是 true 还是 false 都写一次：系统限流时它
        // 常常是 false，但缓存里的结果依然有效，只是不新鲜。写下去比什么都不写强。
        app.registerReceiver(
            object : BroadcastReceiver() {
                override fun onReceive(c: Context?, intent: Intent?) = dump()
            },
            IntentFilter(WifiManager.SCAN_RESULTS_AVAILABLE_ACTION)
        )
        started = true

        dump()          // 先把当前缓存写一份，用户第一次点「扫描信道」就有数据
        requestScan()

        handler = Handler(Looper.getMainLooper())
        handler?.postDelayed(tick, INTERVAL_MS)
    }

    /**
     * 权限刚给上时调一次：立即请求扫描并落盘。
     * 只靠 [start] 的话，启动时那次权限还没批，写下去的是空数组。
     */
    fun refresh() {
        if (!started) return
        requestScan()
        dump()
    }

    @SuppressLint("MissingPermission")
    private fun requestScan() {
        val wm = wifi ?: return
        try {
            wm.startScan()
        } catch (t: Throwable) {
            // 没权限或系统限流都会走到这里。不抛给用户 —— 上一次的结果还在文件里
            Log.w(TAG, "startScan 被拒：${t.javaClass.simpleName}", t)
        }
    }

    @SuppressLint("MissingPermission")
    private fun dump() {
        val ctx = appCtx ?: return
        val wm = wifi ?: return
        val arr = JSONArray()
        try {
            for (r in wm.scanResults) {
                // 个别 AP 的 SSID 带奇怪字符，一条坏不能带倒整份
                try {
                    arr.put(
                        JSONObject().apply {
                            put("ssid", r.SSID ?: "")
                            put("bssid", r.BSSID ?: "")
                            put("frequency", r.frequency)
                            put("rssi", r.level)
                        }
                    )
                } catch (t: Throwable) {
                    Log.w(TAG, "跳过一条扫不到名字的 AP", t)
                }
            }
        } catch (t: Throwable) {
            Log.w(TAG, "读取扫描结果失败（权限没给？）", t)
            return          // 读都读不到，别拿旧数据骗 Python
        }
        try {
            File(ctx.filesDir, FILE_NAME).writeText(arr.toString())
        } catch (t: Throwable) {
            Log.e(TAG, "写扫描结果失败", t)
        }
    }
}
