package io.github.mifiautotune.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager

/**
 * 守护模式的前台服务：只做一件事 —— 让进程活着。
 *
 * 为什么必须有这个：守护是一个跑在本进程里的 Python 线程（见 bridge.guard_start），
 * 它没有任何系统层的东西托着。用户一按 Home，进程就成了后台进程，MIUI 那套省电
 * 策略几个十秒内就会把它冻住 —— 实机日志里出现过这样的空档：
 *
 *     09:19:06  [!] 上一拍到这一拍隔了 238 秒（正常约 30 秒）—— 这段时间被系统挂起了
 *
 * 那四分钟里用户正在走路，守护却在睡觉，于是「走到新地方要解锁」这件事根本没机会
 * 发生。原先代码里甚至还找 Termux 的 wakelock 来顶（_acquire_wakelock），可在
 * 这个 APK 里根本没有 termux-api。
 *
 * 三件事一起做才够：
 *   ① startForeground —— 进程拿到前台优先级，不再是「随时可回收」；
 *   ② PARTIAL_WAKE_LOCK —— 屏幕熄灭后 CPU 也能继续跑那 30 秒一拍；
 *   ③ 常驻通知 —— 用户随时看得见「它还在跑」，也能从这里点回去。
 *
 * 剩下的靠用户给「无限制」后台权限（MIUI 自己那一层比 AOSP 更狠），
 * MainActivity 里会引导一次。
 */
class GuardService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        // PARTIAL：只要 CPU 醒着就行，不用点屏幕。任务是「每 30 秒跑一次 ping +
        // 读一次设备状态」，CPU 一睡就全停。
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "mifi:guard")
            .apply {
                setReferenceCounted(false)
                acquire()
            }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        startForeground(NOTIF_ID, buildNotification())
        // START_STICKY：万一还是被系统回收了，让它自己回来。守护的状态都在
        // Python 那边，重启一次只是重新采样，不会留下「锁了一半」的设备。
        return START_STICKY
    }

    override fun onDestroy() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        super.onDestroy()
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

        val b = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return b.setContentTitle("守护模式在跑")
            .setContentText("走动时释放频段锁，坐下久了再锁回来")
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val CHANNEL_ID = "mifi-guard"
        private const val NOTIF_ID = 1001
        const val ACTION_STOP = "io.github.mifiautotune.app.GUARD_STOP"

        /** 开守护时调。调用方必须已经在前台（Android 12+ 的后台启动限制）。 */
        fun start(ctx: Context) {
            createChannel(ctx)
            val i = Intent(ctx, GuardService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(i)
            } else {
                ctx.startService(i)
            }
        }

        /** 关守护时调。stopSelf 之外再 stopService，避免 START_STICKY 把它拉回来。 */
        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, GuardService::class.java))
        }

        private fun createChannel(ctx: Context) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
            val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE)
                    as NotificationManager
            if (nm.getNotificationChannel(CHANNEL_ID) != null) return
            // LOW：不响、不弹，只占一条常驻通知。守护是背景行为，不该打扰人。
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "守护模式",
                    NotificationManager.IMPORTANCE_LOW).apply {
                    description = "守护运行期间显示，关掉守护就消失"
                    setShowBadge(false)
                })
        }
    }
}
