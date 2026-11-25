package io.github.jose1711.tile2ha

import android.content.Context
import android.content.SharedPreferences
import android.os.Build
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

enum class LocationMode { NONE, NETWORK }

/** A Wi-Fi rule: zone == null -> do not scan on this network; otherwise scan and report this HA zone. */
data class WifiRule(val ssid: String, val zone: String?)

/** All configuration and small state (throttling, log) in one SharedPreferences file. */
class Settings(context: Context) {
    private val p: SharedPreferences =
        context.applicationContext.getSharedPreferences("tile2ha", Context.MODE_PRIVATE)
    private val sent: SharedPreferences =
        context.applicationContext.getSharedPreferences("tile2ha_sent", Context.MODE_PRIVATE)

    var enabled: Boolean
        get() = p.getBoolean("enabled", false)
        set(v) = p.edit().putBoolean("enabled", v).apply()

    var haUrl: String
        get() = p.getString("ha_url", "") ?: ""
        set(v) = p.edit().putString("ha_url", v.trim().trimEnd('/')).apply()

    var token: String
        get() = p.getString("token", "") ?: ""
        set(v) = p.edit().putString("token", v.trim()).apply()

    var source: String
        get() = p.getString("source", null) ?: Build.MODEL
        set(v) = p.edit().putString("source", v.trim()).apply()

    /** e.g. device_tracker.pixel_8 (HA Companion app of this phone); HA takes the coordinates from it. */
    var sourceTracker: String
        get() = p.getString("source_tracker", "") ?: ""
        set(v) = p.edit().putString("source_tracker", v.trim()).apply()

    var locationMode: LocationMode
        get() = runCatching { LocationMode.valueOf(p.getString("location_mode", null) ?: "") }
            .getOrDefault(LocationMode.NONE)
        set(v) = p.edit().putString("location_mode", v.name).apply()

    /** Minimum minutes between two reports of the same tracker/beacon. */
    var intervalMin: Int
        get() = p.getInt("interval_min", 5)
        set(v) = p.edit().putInt("interval_min", v.coerceIn(1, 1440)).apply()

    /** iBeacon proximity UUIDs to scan for (filtered in the Bluetooth controller). */
    var ibeaconUuids: List<UUID>
        get() = lines("ibeacon_uuids").mapNotNull { runCatching { UUID.fromString(it) }.getOrNull() }
        set(v) = p.edit().putString("ibeacon_uuids", v.joinToString("\n")).apply()

    var ibeaconUuidsText: String
        get() = p.getString("ibeacon_uuids", "") ?: ""
        set(v) = p.edit().putString("ibeacon_uuids", v).apply()

    /**
     * Wi-Fi rules, one per line: `SSID = zone.home` (keep scanning, report that HA zone instead of a
     * location; `zone.` may be omitted) or `SSID = none` (nothing is scanned or sent while connected;
     * a bare `SSID` means the same).
     */
    val wifiRules: List<WifiRule>
        get() = lines("excluded_ssids").map { line ->
            val i = line.lastIndexOf('=')
            if (i <= 0) return@map WifiRule(line, null)
            val ssid = line.substring(0, i).trim()
            val value = line.substring(i + 1).trim()
            when {
                value.isEmpty() || value.equals("none", ignoreCase = true) -> WifiRule(ssid, null)
                value.startsWith("zone.") -> WifiRule(ssid, value)
                else -> WifiRule(ssid, "zone.$value")
            }
        }

    var excludedSsidsText: String
        get() = p.getString("excluded_ssids", "") ?: ""
        set(v) = p.edit().putString("excluded_ssids", v).apply()

    private fun lines(key: String): List<String> =
        (p.getString(key, "") ?: "").lines().map { it.substringBefore('#').trim() }.filter { it.isNotEmpty() }

    var lastScanState: String
        get() = p.getString("last_scan_state", "") ?: ""
        set(v) = p.edit().putString("last_scan_state", v).apply()

    // --- throttling: key -> last successful report (epoch ms) ---------------------------------

    fun shouldSend(key: String, now: Long): Boolean =
        now - sent.getLong(key, 0L) >= intervalMin * 60_000L

    fun markSent(key: String, now: Long) {
        val e = sent.edit().putLong(key, now)
        // forget old keys (Tile privateIds rotate, strangers' Tiles come and go)
        for ((k, v) in sent.all) if (v is Long && now - v > 24 * 3600_000L) e.remove(k)
        e.apply()
    }

    // --- small rolling log shown in the app ------------------------------------------------------

    fun log(msg: String) {
        val ts = SimpleDateFormat("dd.MM. HH:mm:ss", Locale.ROOT).format(Date())
        val lines = (listOf("$ts $msg") + (p.getString("log", "") ?: "").lines()).filter { it.isNotBlank() }
        p.edit().putString("log", lines.take(40).joinToString("\n")).apply()
    }

    val logText: String get() = p.getString("log", "") ?: ""
}
