package io.github.jose1711.tile2ha

import android.annotation.SuppressLint
import android.app.Activity
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONObject
import java.io.File
import java.util.Locale
import java.util.UUID
import kotlin.concurrent.thread

/**
 * "Find" screen: a full-power scan only while it is open, showing the signal of every tracker known to
 * HA. Names and Tile privateIds come from the tile_ble webhook (type "trackables"); the auth_key never
 * leaves HA. The list is cached in filesDir, so the screen works offline too.
 */
class FindActivity : Activity() {
    private class Row(val name: String, val view: TextView, val bar: ProgressBar) {
        var rssi = Double.NaN       // smoothed
        var last = 0L               // elapsedRealtime of the last advertisement
    }

    private data class Beacon(val key: String, val uuid: String, val major: Int?, val minor: Int?)

    private lateinit var s: Settings
    private lateinit var status: TextView
    private lateinit var list: LinearLayout
    private val rows = LinkedHashMap<String, Row>()      // key -> row
    private var tileIds = HashMap<String, String>()      // privateId hex -> key
    private var beacons = listOf<Beacon>()
    private val ui = Handler(Looper.getMainLooper())
    private var scanning = false

    private val cache get() = File(filesDir, "trackables.json")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        s = Settings(this)
        val pad = (16 * resources.displayMetrics.density).toInt()
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad) }
        status = TextView(this).apply { setTypeface(typeface, Typeface.BOLD) }
        col.addView(status)
        col.addView(Button(this).apply { text = "Obnoviť zoznam z HA"; setOnClickListener { download() } },
            LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        col.addView(TextView(this).apply {
            alpha = 0.7f
            text = "Sken beží naplno len kým je táto obrazovka otvorená. Silnejší signál (bližšie k 0 dBm) = bližšie."
        })
        list = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        col.addView(list)
        setContentView(ScrollView(this).apply { addView(col) })

        val cached = runCatching { JSONObject(cache.readText()) }.getOrNull()
        if (cached != null) load(cached)
        if (cached == null || expired(cached)) download()
    }

    override fun onResume() {
        super.onResume()
        startScan()
        ui.post(tick)
    }

    override fun onPause() {
        super.onPause()
        stopScan()
        ui.removeCallbacks(tick)
    }

    /** Refresh at half of the window (valid_until), or once a day when HA sent the whole cycle. */
    private fun expired(o: JSONObject): Boolean {
        val fetched = o.optLong("fetched", 0)
        if (System.currentTimeMillis() - fetched > 24 * 3600_000L) return true
        val tiles = o.optJSONArray("tiles") ?: return true
        for (i in 0 until tiles.length()) {
            val until = tiles.getJSONObject(i).optString("valid_until", "")
            if (until.isNotEmpty() && until != "null" &&
                runCatching { java.time.Instant.parse(until.replace("+00:00", "Z")).toEpochMilli() }
                    .getOrDefault(0L) < System.currentTimeMillis()
            ) return true
        }
        return false
    }

    private fun download() {
        if (s.webhookUrl.isEmpty()) { status.text = "Chýba URL webhooku (nastavenia)"; return }
        status.text = "Sťahujem zoznam z HA…"
        thread {
            val ha = HaClient(s.webhookUrl)
            val o = ha.trackables(WINDOW_DAYS)
            runOnUiThread {
                if (o == null) {
                    status.text = "Zoznam sa nepodarilo stiahnuť: ${ha.lastError ?: "?"}" +
                        (if (rows.isNotEmpty()) " (používam uložený)" else "")
                } else {
                    o.put("fetched", System.currentTimeMillis())
                    runCatching { cache.writeText(o.toString()) }
                    load(o)
                }
            }
        }
    }

    private fun load(o: JSONObject) {
        rows.clear(); list.removeAllViews()
        val ids = HashMap<String, String>()
        val tiles = o.optJSONArray("tiles")
        for (i in 0 until (tiles?.length() ?: 0)) {
            val t = tiles!!.getJSONObject(i)
            val key = t.getString("key")
            addRow(key, t.optString("name", key))
            val arr = t.getJSONArray("ids")
            for (j in 0 until arr.length()) ids[arr.getString(j)] = key
        }
        val bs = mutableListOf<Beacon>()
        val ba = o.optJSONArray("beacons")
        for (i in 0 until (ba?.length() ?: 0)) {
            val b = ba!!.getJSONObject(i)
            val key = b.getString("key")
            addRow(key, b.optString("name", key) + " (iBeacon)")
            bs += Beacon(key, b.getString("uuid").lowercase(Locale.ROOT),
                if (b.isNull("major")) null else b.getInt("major"),
                if (b.isNull("minor")) null else b.getInt("minor"))
        }
        tileIds = ids
        beacons = bs
        status.text = "${rows.size} trackerov" + if (rows.isEmpty()) " – v HA nie sú žiadne" else ""
        if (scanning) { stopScan(); startScan() }  // the beacon UUID filters may have changed
    }

    private fun addRow(key: String, name: String) {
        val tv = TextView(this).apply { textSize = 16f; setPadding(0, 24, 0, 0) }
        val bar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply { max = 60 }
        list.addView(tv)
        list.addView(bar, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        rows[key] = Row(name, tv, bar)
    }

    private val callback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) = handle(result)
        override fun onBatchScanResults(results: MutableList<ScanResult>) = results.forEach { handle(it) }
        override fun onScanFailed(errorCode: Int) {
            runOnUiThread { status.text = "Sken zlyhal (kód $errorCode)" }
        }
    }

    private fun handle(r: ScanResult) {
        val rec = r.scanRecord ?: return
        var key: String? = null
        rec.getServiceData(ScanController.TILE_SERVICE)?.let { sd ->
            if (sd.size >= 10 && sd[0] == 2.toByte())
                key = tileIds[sd.copyOfRange(2, 10).joinToString("") { b -> String.format(Locale.ROOT, "%02x", b) }]
        }
        rec.getManufacturerSpecificData(ScanController.APPLE_ID)?.let { md ->
            if (md.size >= 23 && md[0] == 0x02.toByte() && md[1] == 0x15.toByte()) {
                val uuid = ScanController.uuidFromBytes(md, 2).toString()
                val major = ((md[18].toInt() and 0xFF) shl 8) or (md[19].toInt() and 0xFF)
                val minor = ((md[20].toInt() and 0xFF) shl 8) or (md[21].toInt() and 0xFF)
                // the most specific configured line wins, as in HA
                key = beacons.filter { it.uuid == uuid && (it.major == null || it.major == major) &&
                    (it.minor == null || it.minor == minor) }
                    .maxByOrNull { (if (it.major != null) 1 else 0) + (if (it.minor != null) 1 else 0) }?.key ?: key
            }
        }
        val k = key ?: return
        val rssi = r.rssi
        runOnUiThread {
            val row = rows[k] ?: return@runOnUiThread
            row.rssi = if (row.rssi.isNaN()) rssi.toDouble() else row.rssi * 0.7 + rssi * 0.3
            row.last = SystemClock.elapsedRealtime()
        }
    }

    private val tick = object : Runnable {
        override fun run() {
            val now = SystemClock.elapsedRealtime()
            for (row in rows.values) {
                val age = (now - row.last) / 1000
                if (row.last == 0L || age > 30) {
                    row.view.text = "${row.name}: —" + if (row.last != 0L) "  (naposledy pred ${age}s)" else ""
                    row.bar.progress = 0
                } else {
                    val v = row.rssi.toInt()
                    row.view.text = "${row.name}: $v dBm  ${hint(v)}" + if (age >= 3) "  (pred ${age}s)" else ""
                    row.bar.progress = (v + 100).coerceIn(0, 60)  // -100 dBm .. -40 dBm
                }
            }
            ui.postDelayed(this, 500)
        }
    }

    private fun hint(rssi: Int) = when {
        rssi >= -55 -> "veľmi blízko"
        rssi >= -70 -> "blízko"
        rssi >= -85 -> "v dosahu"
        else -> "ďaleko"
    }

    @SuppressLint("MissingPermission")
    private fun startScan() {
        if (scanning) return
        if (!ScanController.hasScanPermissions(this)) { status.text = "Chýbajú oprávnenia (nastavenia → 1.)"; return }
        val scanner = getSystemService(BluetoothManager::class.java)?.adapter
            ?.takeIf { it.isEnabled }?.bluetoothLeScanner
        if (scanner == null) { status.text = "Bluetooth je vypnutý"; return }
        val filters = mutableListOf(
            android.bluetooth.le.ScanFilter.Builder().setServiceUuid(ScanController.TILE_SERVICE).build())
        for (u in beacons.map { it.uuid }.distinct())
            runCatching { filters += ScanController.ibeaconFilter(UUID.fromString(u)) }
        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        runCatching { scanner.startScan(filters, settings, callback) }
            .onSuccess { scanning = true }
            .onFailure { status.text = "Sken sa nespustil: ${it.message}" }
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        if (!scanning) return
        scanning = false
        runCatching {
            getSystemService(BluetoothManager::class.java)?.adapter?.bluetoothLeScanner?.stopScan(callback)
        }
    }

    companion object {
        /** privateIds for +-WINDOW_DAYS (+1 day drift) around the last sighting; 0 = whole 90-day cycle. */
        const val WINDOW_DAYS = 1
    }
}
