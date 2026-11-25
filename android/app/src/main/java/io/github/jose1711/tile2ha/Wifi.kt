package io.github.jose1711.tile2ha

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager

object Wifi {
    /**
     * SSID of the current Wi-Fi network without quotes, or null (not on Wi-Fi, or unknown because
     * location permission / location services are off - then the phone is treated as "not excluded").
     */
    @Suppress("DEPRECATION")
    fun currentSsid(context: Context): String? {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return null
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return null
        if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return null
        val wm = context.applicationContext.getSystemService(WifiManager::class.java) ?: return null
        val ssid = wm.connectionInfo?.ssid ?: return null
        if (ssid == WifiManager.UNKNOWN_SSID || ssid.isBlank()) return null
        return ssid.removeSurrounding("\"")
    }

    /** The rule for the Wi-Fi network the phone is connected to, if any. */
    fun currentRule(context: Context, settings: Settings): WifiRule? {
        val rules = settings.wifiRules
        if (rules.isEmpty()) return null
        val ssid = currentSsid(context) ?: return null
        return rules.firstOrNull { it.ssid == ssid }
    }
}
