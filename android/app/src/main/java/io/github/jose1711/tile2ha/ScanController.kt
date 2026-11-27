package io.github.jose1711.tile2ha

import android.Manifest
import android.annotation.SuppressLint
import android.app.PendingIntent
import android.app.job.JobInfo
import android.app.job.JobScheduler
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanSettings
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Build
import android.os.ParcelUuid
import java.nio.ByteBuffer
import java.util.UUID

/**
 * Starts/stops the hardware-filtered BLE scan. The scan is registered with a PendingIntent, so the
 * app process does not have to run: the Bluetooth controller filters advertisements (Tile 0xFEED
 * service, configured iBeacon UUIDs) and the system wakes [ScanReceiver] only for matches, batched
 * when the controller supports it.
 */
object ScanController {
    val TILE_SERVICE: ParcelUuid = ParcelUuid.fromString("0000feed-0000-1000-8000-00805f9b34fb")
    const val APPLE_ID = 0x004C
    private const val BATCH_MS = 60_000L
    private const val JOB_ID = 1711

    fun hasScanPermissions(ctx: Context): Boolean {
        val perms = if (Build.VERSION.SDK_INT >= 31)
            listOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.ACCESS_FINE_LOCATION)
        else listOf(Manifest.permission.ACCESS_FINE_LOCATION)
        return perms.all { ctx.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }
    }

    private fun scanIntent(ctx: Context): PendingIntent {
        val i = Intent(ctx, ScanReceiver::class.java).setAction(ScanReceiver.ACTION)
        // must be mutable: the system adds the scan results as extras
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
        return PendingIntent.getBroadcast(ctx, 0, i, flags)
    }

    private fun networkIntent(ctx: Context): PendingIntent {
        val i = Intent(ctx, NetworkReceiver::class.java)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
        return PendingIntent.getBroadcast(ctx, 1, i, flags)
    }

    fun filters(settings: Settings): List<ScanFilter> {
        val list = mutableListOf(ScanFilter.Builder().setServiceUuid(TILE_SERVICE).build())
        for (uuid in settings.ibeaconUuids) list += ibeaconFilter(uuid)
        return list
    }

    /** iBeacon: 02 15 <uuid:16> <major:2> <minor:2> <tx:1>; match type+length and the UUID. */
    fun ibeaconFilter(uuid: UUID): ScanFilter {
        val data = ByteBuffer.allocate(23).put(0x02.toByte()).put(0x15.toByte())
            .putLong(uuid.mostSignificantBits).putLong(uuid.leastSignificantBits).array()
        val mask = ByteArray(23) { i -> if (i < 18) 0xFF.toByte() else 0 }
        return ScanFilter.Builder().setManufacturerData(APPLE_ID, data, mask).build()
    }

    /**
     * Bring the scan into the state the settings ask for. Safe to call any time, from anywhere -
     * except from [NetworkReceiver] with [network] = true: (re)registering the network callback makes
     * the system deliver the current network at once, which would wake [NetworkReceiver] again (a loop
     * that leaked PendingIntent cancel listeners in system_server until it crashed).
     *
     * [network]: re-register the network callback (start, settings changed). Otherwise it is only
     * registered when its desired state differs from the last registration.
     */
    @SuppressLint("MissingPermission")
    fun refresh(ctx: Context, reason: String, network: Boolean = false) {
        val s = Settings(ctx)
        scheduleWatchdog(ctx, s.enabled)
        val netOn = s.enabled && s.wifiRules.isNotEmpty()
        if (network || netOn != s.networkCallbackOn) registerNetworkCallback(ctx, netOn)
        s.lastSsid = Wifi.currentSsid(ctx) ?: ""

        val adapter = ctx.getSystemService(BluetoothManager::class.java)?.adapter
        val scanner = adapter?.takeIf { it.isEnabled }?.bluetoothLeScanner
        val pi = scanIntent(ctx)
        if (scanner != null && hasScanPermissions(ctx)) {
            runCatching { scanner.stopScan(pi) }  // restart: avoids the system downgrading long scans
        }

        val rule = Wifi.currentRule(ctx, s)
        val why = when {
            !s.enabled -> "vypnuté"
            s.webhookUrl.isEmpty() -> "chýba URL webhooku"
            !hasScanPermissions(ctx) -> "chýbajú oprávnenia"
            scanner == null -> "Bluetooth je vypnutý"
            rule != null && rule.zone == null -> "wifi ${rule.ssid} je vo výnimkách"
            else -> null
        }
        if (why != null) {
            logState(s, reason, "sken zastavený: $why")
            return
        }
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_POWER)
            .setCallbackType(ScanSettings.CALLBACK_TYPE_ALL_MATCHES)
            .apply { if (adapter?.isOffloadedScanBatchingSupported == true) setReportDelay(BATCH_MS) }
            .build()
        val f = filters(s)
        val rc = scanner!!.startScan(f, settings, pi)
        val mode = rule?.let { " (wifi ${it.ssid} → ${it.zone})" } ?: ""
        logState(s, reason, if (rc == 0) "sken beží$mode, filtre: ${f.size}" else "startScan zlyhal: $rc")
    }

    /** The watchdog runs every 15 min: log only changes, so the log is not flooded. */
    private fun logState(s: Settings, reason: String, state: String) {
        if (reason != "watchdog" || state != s.lastScanState) s.log("$state ($reason)")
        s.lastScanState = state
    }

    /** Periodic watchdog: restarts the scan after Bluetooth toggles, leaving an excluded Wi-Fi, ... */
    private fun scheduleWatchdog(ctx: Context, on: Boolean) {
        val js = ctx.getSystemService(JobScheduler::class.java) ?: return
        if (!on) {
            js.cancel(JOB_ID); return
        }
        if (js.getPendingJob(JOB_ID) != null) return
        js.schedule(
            JobInfo.Builder(JOB_ID, ComponentName(ctx, WatchdogJob::class.java))
                .setPeriodic(15 * 60_000L)
                .setPersisted(true)
                .build()
        )
    }

    /** Wake [NetworkReceiver] when a Wi-Fi network connects, to apply its rule (stop scanning / zone). */
    private fun registerNetworkCallback(ctx: Context, on: Boolean) {
        val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return
        val pi = networkIntent(ctx)
        runCatching { cm.unregisterNetworkCallback(pi) }
        Settings(ctx).networkCallbackOn = false
        if (!on) return
        val req = NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI).build()
        runCatching { cm.registerNetworkCallback(req, pi) }
            .onSuccess { Settings(ctx).networkCallbackOn = true }
            .onFailure { Settings(ctx).networkCallbackOn = false; Settings(ctx).log("network callback: ${it.message}") }
    }

    fun uuidFromBytes(b: ByteArray, off: Int): UUID {
        val bb = ByteBuffer.wrap(b, off, 16)
        return UUID(bb.long, bb.long)
    }
}
