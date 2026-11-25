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
 * Location without turning on GPS and without Google Play Services:
 * 1. the best location Android already has cached from any provider (gps, network, fused, passive) -
 *    whatever another app (HA Companion, maps, ...) recently requested; costs nothing,
 * 2. only in [LocationMode.NETWORK] and only if nothing cached is fresh enough: one network fix
 *    (Wi-Fi / cell towers).
 * Returns null when nothing usable is available; the caller then relies on source_tracker.
 */
object Locator {
    /** Assumed movement while a cached fix ages (~walking pace), used to compare old vs. coarse fixes. */
    private const val DRIFT_M_PER_MIN = 80f

    private fun granted(ctx: Context) =
        listOf(Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.ACCESS_FINE_LOCATION)
            .any { ctx.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }

    fun locate(ctx: Context, mode: LocationMode, maxAgeMin: Int): Location? {
        if (mode == LocationMode.NONE || !granted(ctx)) return null
        bestCached(ctx, maxAgeMin * 60_000L)?.let { return it }
        return if (mode == LocationMode.NETWORK) freshNetwork(ctx) else null
    }

    /** Cached fix from any provider not older than [maxAgeMs], best by accuracy + age drift. */
    @SuppressLint("MissingPermission")
    fun bestCached(ctx: Context, maxAgeMs: Long): Location? {
        if (!granted(ctx)) return null
        val lm = ctx.getSystemService(LocationManager::class.java) ?: return null
        return lm.allProviders
            .mapNotNull { p -> runCatching { lm.getLastKnownLocation(p) }.getOrNull() }
            .filter { ageMs(it) in 0..maxAgeMs }
            .minByOrNull { score(it) }
    }

    /** Effective uncertainty in meters: reported accuracy plus how far one could have moved since. */
    private fun score(l: Location): Float =
        (if (l.hasAccuracy()) l.accuracy else 5_000f) + ageMs(l) / 60_000f * DRIFT_M_PER_MIN

    @SuppressLint("MissingPermission")
    private fun freshNetwork(ctx: Context, timeoutS: Long = 15): Location? {
        val lm = ctx.getSystemService(LocationManager::class.java) ?: return null
        if (!lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) return null
        if (Build.VERSION.SDK_INT < 30) return null  // getCurrentLocation is API 30+

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
        return fresh
    }

    fun ageMs(l: Location): Long =
        (SystemClock.elapsedRealtimeNanos() - l.elapsedRealtimeNanos) / 1_000_000
}
