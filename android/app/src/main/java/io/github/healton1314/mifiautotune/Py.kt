package io.github.mifiautotune.app

import android.content.Context
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.util.concurrent.Executors

/**
 * 所有 Python 调用的唯一入口。
 *
 * 三件事必须收在一处：
 *  1. Chaquopy 的调用不能并发乱来，统一串到一个单线程执行器；
 *  2. 每次 status() 都要真去连一次设备（Digest 认证 + ping，1~3 秒），
 *     绝对不能放在主线程 —— 放上去界面就是一顿一顿的；
 *  3. 任何异常都转成可读文字回调，别让线程静默死掉（那样界面永远停在旧数据）。
 *
 * 回调统一在主线程执行，界面代码不用再自己切。
 */
object Py {

    private val io = Executors.newSingleThreadExecutor { r -> Thread(r, "mifi-py") }

    @Volatile
    private var module: PyObject? = null

    /** 启动解释器。可能耗时几秒，调用方应放在后台线程。 */
    @Synchronized
    fun start(ctx: Context) {
        if (module != null) return
        if (!Python.isStarted()) Python.start(AndroidPlatform(ctx))
        Python.getInstance().getModule("bridge").also {
            // 必须先把可写目录告诉 Python：原生扫描结果、位置记忆、原始设置
            // 都落在 filesDir，而 APK 里解压出来的源码目录不是久留之地。
            // 漏了这一步，扫描会一路 fallback 到 nmcli/iw，报一句用户看不懂的错。
            it.callAttr("init", ctx.filesDir.absolutePath)
            module = it
        }
    }

    val isReady: Boolean get() = module != null

    /**
     * 后台调 bridge 里的一个函数，结果（JSON 字符串）回主线程。
     * [args] 支持 String / Int / Boolean，够 bridge 用了。
     */
    fun call(
        ctx: Context,
        fn: String,
        vararg args: Any,
        onResult: (String) -> Unit
    ) {
        io.execute {
            val text = try {
                start(ctx)
                val m = module ?: error("Python 模块没起来")
                val r = m.callAttr(fn, *args)
                r?.toString() ?: ""
            } catch (t: Throwable) {
                """{"error":"${t.javaClass.simpleName}: ${t.message?.replace("\"", "'")}"}"""
            }
            Main.post { onResult(text) }
        }
    }
}

/** 主线程投递的薄封装，省得每处都写 Looper。 */
object Main {
    private val h = android.os.Handler(android.os.Looper.getMainLooper())

    /**
     * 接受 Runnable 而不是 () -> Unit：界面那边传的是 pollStatus / pollJob
     * 这种 Runnable 实例，函数类型接不住；而 lambda 会由 Kotlin 自动 SAM 成
     * Runnable，两种写法都能用，也不会出现重载歧义。
     */
    fun post(r: Runnable) {
        if (android.os.Looper.myLooper() == android.os.Looper.getMainLooper()) r.run()
        else h.post(r)
    }

    fun postDelayed(r: Runnable, ms: Long) {
        h.postDelayed(r, ms)
    }

    /** 退出时把挂着的轮询清干净，否则界面销毁后还在跑，白耗电。 */
    fun clear() {
        h.removeCallbacksAndMessages(null)
    }
}
