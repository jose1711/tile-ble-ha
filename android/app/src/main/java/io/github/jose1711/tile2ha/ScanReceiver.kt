package io.github.jose1711.tile2ha

import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanResult
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import org.json.JSONObject
import java.util.Locale
import kotlin.concurrent.thread

/** Woken by the system with batched, hardware-filtered scan results; reports them to HA. */
class ScanReceiver : BroadcastReceiver() {
    companion object {
        const val ACTION = "io.github.jose1711.tile2ha.SCAN_RESULT"
    }

    private sealed class Report(val key: String, val rssi: Int, val address: String) {
        class Tile(val serviceData: String, rssi: Int, address: String) :
            Report("tile:$serviceData", rssi, address)

        class IBeacon(val uuid: String, val major: Int, val minor: Int, rssi: Int, address: String) :
            Report("ib:$uuid:$major:$minor", rssi, address)
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION) return
        val err = intent.getIntExtra(BluetoothLeScanner.EXTRA_ERROR_CODE, 0)
        val results = scanResults(intent)

        val pending = goAsync()
        thread(name = "tile2ha-report") {
            try {
                val s = Settings(context)
                if (err != 0) s.log("chyba skenu: $err")
                handle(context, s, results)
            } catch (e: Exception) {
                Settings(context).log("chyba: ${e.javaClass.simpleName} ${e.message}")
            } finally {
                pending.finish()
            }
        }
    }

    private fun handle(ctx: Context, s: Settings, results: List<ScanResult>) {
        if (!s.enabled || results.isEmpty()) return
        val rule = Wifi.currentRule(ctx, s)
        if (rule != null && rule.zone == null) {
            ScanController.refresh(ctx, "wifi ${rule.ssid}")  // stops the scan
            return
        }

        val now = System.currentTimeMillis()
        // newest result per key, strongest signal wins within a batch
        val reports = LinkedHashMap<String, Report>()
        for (r in results) {
            val rec = r.scanRecord ?: continue
            val address = r.device?.address ?: ""
            rec.getServiceData(ScanController.TILE_SERVICE)?.let { sd ->
                put(reports, Report.Tile(sd.toHex(), r.rssi, address))
            }
            rec.getManufacturerSpecificData(ScanController.APPLE_ID)?.let { md ->
                if (md.size >= 23 && md[0] == 0x02.toByte() && md[1] == 0x15.toByte()) {
                    val uuid = ScanController.uuidFromBytes(md, 2).toString()
                    val major = ((md[18].toInt() and 0xFF) shl 8) or (md[19].toInt() and 0xFF)
                    val minor = ((md[20].toInt() and 0xFF) shl 8) or (md[21].toInt() and 0xFF)
                    put(reports, Report.IBeacon(uuid, major, minor, r.rssi, address))
                }
            }
        }
        val due = reports.values.filter { s.shouldSend(it.key, now) }
        if (due.isEmpty()) return

        val zone = rule?.zone  // on a "SSID = zone.x" network: report the zone, no location lookup
        val loc = if (zone == null && s.locationMode == LocationMode.NETWORK) Locator.networkLocation(ctx) else null
        if (zone == null && loc == null && s.sourceTracker.isEmpty()) {
            s.log("${due.size}× neodoslané: bez polohy a bez source_tracker")
            return
        }
        val ha = HaClient(s.haUrl, s.token)
        for (r in due) {
            val data = JSONObject().put("rssi", r.rssi).put("address", r.address).put("source", s.source)
            if (loc != null) {
                data.put("latitude", loc.latitude).put("longitude", loc.longitude)
                    .put("gps_accuracy", if (loc.hasAccuracy()) loc.accuracy.toDouble() else 0.0)
            }
            if (zone != null) data.put("zone", zone)
            else if (s.sourceTracker.isNotEmpty()) data.put("source_tracker", s.sourceTracker)
            val service = when (r) {
                is Report.Tile -> { data.put("service_data", r.serviceData); "report_advertisement" }
                is Report.IBeacon -> {
                    data.put("uuid", r.uuid).put("major", r.major).put("minor", r.minor); "report_ibeacon"
                }
            }
            val code = ha.callService("tile_ble", service, data)
            val what = when (r) {
                is Report.Tile -> "Tile ${r.serviceData.takeLast(8)}"
                is Report.IBeacon -> "iBeacon ${r.uuid.take(8)}…/${r.major}/${r.minor}"
            }
            val where = zone?.let { " @$it" } ?: loc?.let {
                String.format(Locale.ROOT, " @%.4f,%.4f ±%dm", it.latitude, it.longitude, it.accuracy.toInt())
            } ?: " @${s.sourceTracker}"
            if (code in 200..299) {
                s.markSent(r.key, now)
                s.log("$what ${r.rssi}dBm$where → $code")
            } else {
                // not marked as sent: retried with the next sighting
                s.log("$what → $code ${ha.lastError ?: ""}")
                if (code == -1) break  // offline, no point in trying the rest
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun scanResults(intent: Intent): List<ScanResult> =
        if (Build.VERSION.SDK_INT >= 33)
            intent.getParcelableArrayListExtra(BluetoothLeScanner.EXTRA_LIST_SCAN_RESULT, ScanResult::class.java)
                ?: emptyList()
        else
            intent.getParcelableArrayListExtra<ScanResult>(BluetoothLeScanner.EXTRA_LIST_SCAN_RESULT) ?: emptyList()

    private fun put(map: LinkedHashMap<String, Report>, r: Report) {
        val prev = map[r.key]
        if (prev == null || r.rssi > prev.rssi) map[r.key] = r
    }

    private fun ByteArray.toHex(): String = joinToString("") { String.format(Locale.ROOT, "%02x", it) }
}
