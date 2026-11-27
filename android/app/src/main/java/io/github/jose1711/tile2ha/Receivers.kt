package io.github.jose1711.tile2ha

import android.app.job.JobParameters
import android.app.job.JobService
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * A Wi-Fi network connected: apply its rule (stop on an excluded SSID, resume on any other one).
 * Disconnecting does not wake us; the watchdog resumes the scan then.
 *
 * The callback also fires on every capability/link change of the network (signal strength, ...),
 * many times a minute: act only when the SSID changed, and never re-register the callback here.
 */
class NetworkReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val s = Settings(context)
        if (!s.enabled) return
        val ssid = Wifi.currentSsid(context) ?: ""
        if (ssid == s.lastSsid) return
        ScanController.refresh(context, "wifi")
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED &&
            intent.action != Intent.ACTION_MY_PACKAGE_REPLACED
        ) return
        if (Settings(context).enabled) ScanController.refresh(context, "štart", network = true)
    }
}

/**
 * Every ~15 min: re-registers the scan (after leaving an excluded Wi-Fi, a Bluetooth toggle, or the
 * system dropping the scan). This is also the worst-case delay before scanning resumes.
 */
class WatchdogJob : JobService() {
    override fun onStartJob(params: JobParameters): Boolean {
        ScanController.refresh(this, "watchdog")
        return false
    }

    override fun onStopJob(params: JobParameters): Boolean = true
}
