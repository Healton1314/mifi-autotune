package io.github.mifiautotune.app

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.widget.ScrollView
import android.widget.TextView
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlin.concurrent.thread

/**
 * 第一阶段：只做自检屏。
 *
 * 目的不是好用，是把「Python 到底有没有在 APK 里跑起来」这件事变成看得见的文字。
 * 这一屏绿了，才换成 WebView 加载真正的面板。
 */
class MainActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val tv = TextView(this).apply {
            textSize = 12f
            setPadding(48, 120, 48, 48)
            setTextIsSelectable(true)
        }
        setContentView(ScrollView(this).apply { addView(tv) })

        tv.text = "正在启动 Python 运行时…"

        thread(name = "py-probe") {
            val report = try {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(this))
                }
                Python.getInstance()
                    .getModule("android_probe")
                    .callAttr("report", filesDir.absolutePath)
                    .toString()
            } catch (t: Throwable) {
                Log.e(TAG, "Python 启动失败", t)
                "Python 启动失败：\n\n" + Log.getStackTraceString(t)
            }
            runOnUiThread { tv.text = report }
        }
    }

    companion object {
        private const val TAG = "MifiApk"
    }
}
