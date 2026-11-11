package ua.rechurch.cma_app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File

// local_auth needs a FragmentActivity for the biometric prompt.
class MainActivity : FlutterFragmentActivity() {
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        // Self-update (lib/update.dart): Dart downloads the APK into cacheDir/apk
        // and asks us to hand it to the system installer.
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "ua.rechurch.cma_app/update")
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "cacheDir" -> result.success(cacheDir.absolutePath)
                    "install" -> result.success(install(call.argument<String>("path")!!))
                    else -> result.notImplemented()
                }
            }
        // Recording (lib/recorder.dart): where the pieces are kept on the phone,
        // and the foreground service that keeps the microphone alive with the
        // screen off (RecordingService.kt).
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "ua.rechurch.cma_app/recording")
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "filesDir" -> result.success(filesDir.absolutePath)
                    "start" -> { startRecordingService(call.argument<String>("text")); result.success(null) }
                    "update" -> { updateRecordingService(call.argument<String>("text")); result.success(null) }
                    "stop" -> { stopService(Intent(this, RecordingService::class.java)); result.success(null) }
                    else -> result.notImplemented()
                }
            }
    }

    private fun startRecordingService(text: String?) {
        // Android 13+: the notification only shows with this permission; the
        // service runs either way, so a refusal is not an error.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 41)
        }
        val intent = Intent(this, RecordingService::class.java).putExtra(RecordingService.EXTRA_TEXT, text)
        ContextCompat.startForegroundService(this, intent)
    }

    private fun updateRecordingService(text: String?) {
        // Re-delivers the intent to the running service, which re-posts its
        // notification. Throws when the app is in the background and the service
        // is not running — then there is nothing to update anyway.
        try {
            startService(Intent(this, RecordingService::class.java).putExtra(RecordingService.EXTRA_TEXT, text))
        } catch (_: IllegalStateException) {}
    }

    // "ok" — the installer is open. "permission" — this app may not yet install
    // unknown apps; the settings page for exactly that toggle is opened, and
    // the user taps «Встановити» again when back (the file is kept).
    private fun install(path: String): String {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !packageManager.canRequestPackageInstalls()) {
            startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
            return "permission"
        }
        val uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", File(path))
        startActivity(Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        })
        return "ok"
    }
}
