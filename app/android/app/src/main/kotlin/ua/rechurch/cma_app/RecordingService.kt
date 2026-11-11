package ua.rechurch.cma_app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat

// Keeps the microphone open while the screen is off or another app is in
// front. Android (9 and up) cuts a background app off from the microphone
// within a minute; a foreground service with the `microphone` type is the
// sanctioned exception, and its price is the notification «Іде запис» that
// stays in the shade for as long as the recording runs. The service records
// nothing itself — lib/recorder.dart does, through the `record` plugin — it
// only holds the process in the foreground and a partial wake lock so the
// CPU does not doze between audio buffers.
class RecordingService : Service() {
    private var lock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val text = intent?.getStringExtra(EXTRA_TEXT) ?: "Іде запис зустрічі"
        val notification = build(text)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(ID, notification)
        }
        if (lock == null) {
            lock = (getSystemService(POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "cma:recording")
                .also { it.acquire(12 * 60 * 60 * 1000L) }   // a meeting is not twelve hours
        }
        // Not sticky: if Android kills the process the recorder is gone too, and a
        // notification without a recording behind it would be a lie.
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        lock?.let { if (it.isHeld) it.release() }
        lock = null
        super.onDestroy()
    }

    private fun build(text: String): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL, "Запис зустрічі", NotificationManager.IMPORTANCE_LOW).apply {
                    description = "Показується, поки CMA записує зустріч"
                    setShowBadge(false)
                }
            )
        }
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("CMA")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    companion object {
        const val ID = 41
        const val CHANNEL = "recording"
        const val EXTRA_TEXT = "text"
    }
}
