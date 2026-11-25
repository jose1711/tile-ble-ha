package io.github.jose1711.tile2ha

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationManager
import android.os.Build
import android.os.SystemClock
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Network location only (Wi-Fi / cell towers via the platform NETWORK_PROVIDER): no GPS, no Google
 * Play Services. Returns null when unavailable; the caller then relies on source_tracker.
 */
object Locator {
    private const val MAX_AGE_MS = 10 * 60_000L

    @SuppressLint("MissingPermission")
    fun networkLocation(ctx: Context, timeoutS: Long = 15): Location? {
        val granted = listOf(Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.ACCESS_FINE_LOCATION)
            .any { ctx.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }
        if (!granted) return null
        val lm = ctx.getSystemService(LocationManager::class.java) ?: return null
        if (!lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) return null

        val last = runCatching { lm.getLastKnownLocation(LocationManager.NETWORK_PROVIDER) }.getOrNull()
        if (last != null && ageMs(last) < MAX_AGE_MS) return last
        if (Build.VERSION.SDK_INT < 30) return last

        var fresh: Location? = null
        val latch = CountDownLatch(1)
        val executor = Executors.newSingleThreadExecutor()
        try {
            lm.getCurrentLocation(LocationManager.NETWORK_PROVIDER, null, executor) { loc ->
                fresh = loc
                latch.countDown()
            }
            latch.await(timeoutS, TimeUnit.SECONDS)
        } catch (_: Exception) {
        } finally {
            executor.shutdown()
        }
        return fresh ?: last
    }

    fun ageMs(l: Location): Long =
        (SystemClock.elapsedRealtimeNanos() - l.elapsedRealtimeNanos) / 1_000_000
}
