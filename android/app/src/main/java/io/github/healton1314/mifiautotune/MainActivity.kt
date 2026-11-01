package io.github.mifiautotune.app

import android.annotation.SuppressLint
import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlin.concurrent.thread

/**
 * 把仓库里那个 Web 面板原样装进 APK。
 *
 * 思路是「不重写界面」：Python 侧的 server.py 本来就把面板做成网页，APK 只要
 * 在本地起一个只监听 127.0.0.1 的实例，再用 WebView 指过去，界面、逻辑、样式
 * 跟电脑上完全同一套。以后改面板只改 web/，不用再动 Kotlin。
 *
 * 起不来的时候不黑屏：把 Python 的自检报告摊在屏幕上。Python 打进 APK 之后，
 * 「黑屏 + 一闪就退」是最难查的故障，文字比 logcat 快。
 */
class MainActivity : Activity() {

    private lateinit var web: WebView
    private lateinit var cover: ScrollView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.useWideViewPort = true
            settings.loadWithOverviewMode = true
            visibility = View.INVISIBLE
            webViewClient = object : WebViewClient() {
                // 面板里没有外链。只放行本机地址，免得误触跳去浏览器。
                override fun shouldOverrideUrlLoading(
                    view: WebView?,
                    url: String?
                ): Boolean = url == null || !url.startsWith("http://127.0.0.1")
            }
        }

        cover = ScrollView(this).apply {
            addView(TextView(this@MainActivity).apply {
                textSize = 12f
                setPadding(48, 120, 48, 48)
                setTextIsSelectable(true)
                text = "正在启动本地面板…"
            })
        }

        setContentView(FrameLayout(this).apply {
            addView(web, matchParent())
            addView(cover, matchParent())
        })

        bootPanel()
    }

    private fun bootPanel() {
        // 扫描桥要定位 / 附近的设备权限，不加这一步「扫描信道」永远是空的。
        // 权限弹窗是异步的，所以不等着它 —— 桥那边拿不到结果就先写空数组，
        // 用户点允许之后下一轮自动就有数据了。
        requestScanPermissions()
        WifiScanBridge.start(this)

        thread(name = "py-boot") {
            val started = runCatching {
                if (!Python.isStarted()) Python.start(AndroidPlatform(this))
                val boot = Python.getInstance().getModule("boot")
                // 先把可写目录告诉 Python：扫描结果、位置记忆、原始设置都落在
                // 这里，而不是 APK 里那个解压出来的源码目录。
                boot.callAttr("set_files_dir", filesDir.absolutePath)
                boot.callAttr("start")
                boot.callAttr("url").toString()
            }

            runOnUiThread {
                started.onSuccess { pageUrl ->
                    web.loadUrl(pageUrl)
                    web.visibility = View.VISIBLE
                    cover.visibility = View.GONE
                }.onFailure { t ->
                    Log.e(TAG, "面板启动失败", t)
                    showFailure(t)
                }
            }
        }
    }

    /** 安卓 13 起扫周边 WiFi 要「附近的设备」，10~12 要「定位」。多问的那个系统会忽略。 */
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
        // 权限刚给上，启动时写的那份空结果该重写一次，否则用户得等下一轮 60 秒
        if (requestCode == REQ_SCAN) WifiScanBridge.refresh()
    }

    /** 起不来时给两条出路：重试一次，或者把自检报告摊开看是哪一步坏了。 */
    private fun showFailure(t: Throwable) {
        val report = runCatching {
            Python.getInstance().getModule("boot")
                .callAttr("probe", filesDir.absolutePath).toString()
        }.getOrElse { Log.getStackTraceString(t) }

        val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        box.addView(TextView(this).apply {
            text = "面板没起来。下面这份自检能看出是哪一步坏的："
            textSize = 14f
            setPadding(48, 96, 48, 24)
        })
        box.addView(Button(this).apply {
            text = "重试"
            setOnClickListener { recreate() }
        })
        box.addView(TextView(this).apply {
            text = report + "\n\n" + Log.getStackTraceString(t)
            textSize = 11f
            setTextIsSelectable(true)
            setPadding(48, 24, 48, 48)
        })

        cover.removeAllViews()
        cover.addView(box)
        cover.visibility = View.VISIBLE
        web.visibility = View.INVISIBLE
    }

    /** 返回键先给网页处理（面板里可能进了二级页面），到头了再退出。 */
    @Deprecated("Activity.onBackPressed")
    override fun onBackPressed() {
        if (::web.isInitialized && web.canGoBack()) {
            web.goBack()
        } else {
            super.onBackPressed()
        }
    }

    private fun matchParent() = FrameLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        ViewGroup.LayoutParams.MATCH_PARENT
    )

    companion object {
        private const val TAG = "MifiApk"
        private const val REQ_SCAN = 1001
    }
}
