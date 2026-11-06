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

    /**
     * 长任务专用线程。
     *
     * APK 里没有独立的 python 进程，体检/应用/恢复都是在当前进程里同步跑完的
     * —— 一次三到六分钟。要是它也排在 [io] 上，这几分钟里所有状态查询、日志
     * 拉取全得在它后面排队，界面看着像死了一样：点「查看运行日志」要等几分钟
     * 才弹出来。所以长任务必须和查询分开两条线。
     *
     * 两条线同时调 Python 是安全的：CPython 有 GIL，而且长任务大部分时间卡在
     * sleep 和 socket 上，会主动让出 GIL，查询能正常插进去。
     */
    private val longRunner = Executors.newSingleThreadExecutor { r -> Thread(r, "mifi-py-long") }

    /**
     * 轻量查询专用线程：job / guard_state / plan 这类「只读内存、几毫秒」的调用。
     *
     * 为什么非得单独一条：[io] 上的 status 每次都要真连设备（认证 + 测延迟），
     * 一趟一两秒，而界面每 5 秒就来一次 —— 那条线几乎永远是满的。进度查询和
     * 「任务结束该复位按钮了」的判断都排在它后面，用户看到的就是
     * 「点了没反应、跑完了按钮还挂着『正在…』」。
     * 读内存的调用不该等网络。
     */
    private val fast = Executors.newSingleThreadExecutor { r -> Thread(r, "mifi-py-fast") }

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
    ) = submit(io, ctx, fn, args, onResult)

    /**
     * 跑会占住线程几分钟的那种活：体检、应用结论、恢复默认。
     *
     * 只在这条线上跑，查询才不会被堵在后面。回调依然是任务**结束后**才来
     * （in-process 模式没有可分离的进程），界面靠轮询 job() 看进度。
     */
    fun callLong(
        ctx: Context,
        fn: String,
        vararg args: Any,
        onResult: (String) -> Unit
    ) = submit(longRunner, ctx, fn, args, onResult)

    /**
     * 读内存的轻量查询走这条：不会被正在跑的状态查询挡住。
     * 只给 job / guard_state / plan 这类调用用 —— 它们不碰设备。
     *
     * 注意：lambda 里的隐式标签跟着**函数名**走，所以这条线上要提前返回得写
     * `return@callFast`，写成 `return@call` 会编译不过（'return' is prohibited
     * here）—— 那个标签在 [call] 那个 lambda 里才成立。
     */
    fun callFast(
        ctx: Context,
        fn: String,
        vararg args: Any,
        onResult: (String) -> Unit
    ) = submit(fast, ctx, fn, args, onResult)

    private fun submit(
        pool: java.util.concurrent.Executor,
        ctx: Context,
        fn: String,
        args: Array<out Any>,
        onResult: (String) -> Unit
    ) {
        pool.execute {
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
