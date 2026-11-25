package io.github.jose1711.tile2ha

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings as AndroidSettings
import android.text.InputType
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.TextView
import java.util.UUID
import kotlin.concurrent.thread

/** Settings screen, built in code (no resources, no extra libraries). */
class MainActivity : Activity() {
    private lateinit var s: Settings
    private lateinit var url: EditText
    private lateinit var token: EditText
    private lateinit var source: EditText
    private lateinit var tracker: EditText
    private lateinit var interval: EditText
    private lateinit var locNone: RadioButton
    private lateinit var locNetwork: RadioButton
    private lateinit var beacons: EditText
    private lateinit var ssids: EditText
    private lateinit var status: TextView
    private lateinit var log: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        s = Settings(this)
        val pad = (16 * resources.displayMetrics.density).toInt()
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        fun label(t: String) = col.addView(TextView(this).apply {
            text = t; setTypeface(typeface, Typeface.BOLD); setPadding(0, pad / 2, 0, 0)
        })
        fun hint(t: String) = col.addView(TextView(this).apply { text = t; alpha = 0.7f })
        fun field(value: String, type: Int = InputType.TYPE_CLASS_TEXT, lines: Int = 1): EditText =
            EditText(this).apply {
                setText(value); inputType = type
                if (lines > 1) { minLines = lines; isSingleLine = false }
                col.addView(this, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
            }
        fun button(t: String, onClick: () -> Unit) = col.addView(Button(this).apply {
            text = t; setOnClickListener { onClick() }
        }, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))

        status = TextView(this).apply { setTypeface(typeface, Typeface.BOLD) }
        col.addView(status)

        label("Home Assistant URL")
        url = field(s.haUrl, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        hint("napr. https://ha.example.sk alebo http://192.168.1.10:8123 (cez VPN)")
        label("Long-lived access token")
        token = field(s.token, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD)
        label("Názov skenera (source)")
        source = field(s.source)

        label("Poloha")
        val group = RadioGroup(this)
        locNone = RadioButton(this).apply { id = View.generateViewId(); text = "Len source_tracker (apka polohu nezisťuje)" }
        locNetwork = RadioButton(this).apply { id = View.generateViewId(); text = "Sieťová poloha telefónu (wifi/BTS, bez GPS)" }
        group.addView(locNone); group.addView(locNetwork)
        group.check(if (s.locationMode == LocationMode.NETWORK) locNetwork.id else locNone.id)
        col.addView(group)
        label("source_tracker (voliteľné)")
        tracker = field(s.sourceTracker)
        hint("napr. device_tracker.pixel_8 z HA Companion apky. HA z neho vezme súradnice; pri sieťovej polohe slúži ako záloha.")

        label("Interval hlásenia (minúty na tracker)")
        interval = field(s.intervalMin.toString(), InputType.TYPE_CLASS_NUMBER)

        label("iBeacon UUID (jeden na riadok)")
        beacons = field(s.ibeaconUuidsText, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE, 2)
        hint("Filtruje sa v Bluetooth čipe. Názvy a major/minor sa nastavujú v HA (tile_ble → Možnosti).")

        label("Pravidlá pre wifi (SSID = zóna, jedno na riadok)")
        ssids = field(s.excludedSsidsText, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE, 2)
        hint("„MojaWifi = zone.home“ = skenovať ďalej, ale hlásiť túto zónu namiesto polohy. " +
            "„MojaWifi = none“ = na tejto sieti vôbec neskenovať (napr. doma s BT proxy v HA).")
        button("Pridať aktuálnu wifi") {
            val ssid = Wifi.currentSsid(this)
            if (ssid == null) toastStatus("SSID neznáme (wifi vypnutá alebo chýba oprávnenie na polohu)")
            else if (ssid !in ssids.text.lines().map { it.substringBefore('#').substringBeforeLast('=').trim() })
                ssids.setText((ssids.text.toString().trimEnd() + "\n" + ssid + " = zone.home").trim())
        }

        button("Uložiť a spustiť") { save(); s.enabled = true; ScanController.refresh(this, "uložené"); render() }
        button("Zastaviť") { s.enabled = false; ScanController.refresh(this, "používateľ"); render() }
        button("Otestovať spojenie s HA") { save(); testConnection() }
        button("1. Oprávnenia (Bluetooth, poloha)") { requestBasePermissions() }
        button("2. Poloha na pozadí („Povoliť vždy“)") { requestBackgroundLocation() }
        button("3. Vypnúť optimalizáciu batérie") { requestIgnoreBatteryOptimizations() }

        label("Záznam")
        log = TextView(this).apply { typeface = Typeface.MONOSPACE; textSize = 11f }
        col.addView(log)
        button("Obnoviť záznam") { render() }

        setContentView(ScrollView(this).apply { addView(col) })
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    private fun save() {
        s.haUrl = url.text.toString()
        s.token = token.text.toString()
        s.source = source.text.toString().ifBlank { Build.MODEL }
        s.sourceTracker = tracker.text.toString()
        s.locationMode = if (locNetwork.isChecked) LocationMode.NETWORK else LocationMode.NONE
        s.intervalMin = interval.text.toString().toIntOrNull() ?: 5
        val bad = beacons.text.lines().map { it.substringBefore('#').trim() }.filter { it.isNotEmpty() }
            .filter { runCatching { UUID.fromString(it) }.isFailure }
        s.ibeaconUuidsText = beacons.text.toString()
        s.excludedSsidsText = ssids.text.toString()
        if (bad.isNotEmpty()) toastStatus("Neplatné UUID (ignorované): ${bad.joinToString()}")
    }

    private fun render() {
        val missing = buildList {
            if (!ScanController.hasScanPermissions(this@MainActivity)) add("oprávnenia")
            if (Build.VERSION.SDK_INT >= 29 &&
                checkSelfPermission(Manifest.permission.ACCESS_BACKGROUND_LOCATION) != PackageManager.PERMISSION_GRANTED
            ) add("poloha na pozadí")
        }
        status.text = (if (s.enabled) "Zapnuté" else "Vypnuté") +
            (if (missing.isNotEmpty()) " — chýba: ${missing.joinToString()}" else "")
        log.text = s.logText
    }

    private fun toastStatus(msg: String) {
        status.text = msg
    }

    private fun testConnection() {
        val ha = HaClient(s.haUrl, s.token)
        status.text = "Testujem…"
        thread {
            val code = ha.ping()
            val msg = when (code) {
                200 -> "HA OK (200)"
                401 -> "Zlý token (401)"
                -1 -> "Nedostupné: ${ha.lastError}"
                else -> "HTTP $code ${ha.lastError ?: ""}"
            }
            runOnUiThread { status.text = msg; s.log("test: $msg"); log.text = s.logText }
        }
    }

    private fun requestBasePermissions() {
        val perms = mutableListOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)
        if (Build.VERSION.SDK_INT >= 31) perms += Manifest.permission.BLUETOOTH_SCAN
        requestPermissions(perms.toTypedArray(), 1)
    }

    private fun requestBackgroundLocation() {
        if (Build.VERSION.SDK_INT < 29) return
        // Android 11+ shows a settings page; the user must pick "Allow all the time"
        requestPermissions(arrayOf(Manifest.permission.ACCESS_BACKGROUND_LOCATION), 2)
    }

    @SuppressLint("BatteryLife")
    private fun requestIgnoreBatteryOptimizations() {
        val pm = getSystemService(PowerManager::class.java)
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            status.text = "Optimalizácia batérie je už vypnutá"
            return
        }
        startActivity(Intent(AndroidSettings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
            Uri.parse("package:$packageName")))
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (s.enabled) ScanController.refresh(this, "oprávnenia")
        render()
    }
}
