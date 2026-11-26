package io.github.jose1711.tile2ha

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Posts to the tile_ble webhook (`https://…/api/webhook/<id>`, shown in the integration's options).
 * No access token: the webhook id is the secret and it only allows feeding sightings.
 */
class HaClient(private val webhookUrl: String) {

    var lastError: String? = null
        private set

    /** Response body of the last successful request (JSON from the integration, empty if unknown webhook). */
    var lastBody: String = ""
        private set

    /** type = "tile" | "ibeacon"; returns the HTTP status code, or -1 on a network error. */
    fun report(type: String, data: JSONObject): Int = post(JSONObject(data.toString()).put("type", type))

    /**
     * true when the webhook belongs to a loaded tile_ble entry. HA answers 200 with an empty body even
     * for an unknown webhook id, so the reply itself is checked.
     */
    fun ping(): Int {
        val code = post(JSONObject().put("type", "ping"))
        if (code == 200 && runCatching { JSONObject(lastBody).optBoolean("ok") }.getOrDefault(false).not()) {
            lastError = "neznámy webhook (skopíruj URL znova z HA)"
            return 404
        }
        return code
    }

    /** Tracker list with names and Tile privateIds (see tile_ble trackables_payload); null on error. */
    fun trackables(windowDays: Int): JSONObject? {
        val code = post(JSONObject().put("type", "trackables").put("window_days", windowDays))
        if (code != 200) return null
        return runCatching { JSONObject(lastBody).takeIf { it.has("tiles") } }.getOrNull()
            ?: null.also { lastError = "neznámy webhook alebo stará verzia tile_ble" }
    }

    /** Key of one authenticated Tile channel (tile_ble webhook "session_key"); null on error. */
    fun sessionKey(key: String, randA: ByteArray, channelPrefix: Int, channelData: ByteArray,
                   localToken: ByteArray): JSONObject? {
        val code = post(JSONObject().put("type", "session_key").put("key", key)
            .put("rand_a", TileRinger.toHex(randA)).put("channel_prefix", channelPrefix)
            .put("channel_data", TileRinger.toHex(channelData)).put("local_token", TileRinger.toHex(localToken)))
        if (code != 200) return null
        return runCatching { JSONObject(lastBody).takeIf { it.has("session_key") } }.getOrNull()
            ?: null.also { lastError = "neznámy webhook alebo stará verzia tile_ble" }
    }

    private fun post(body: JSONObject): Int {
        lastError = null
        lastBody = ""
        val conn = try {
            URL(webhookUrl).openConnection() as HttpURLConnection
        } catch (e: Exception) {
            lastError = "zlá URL: ${e.message}"
            return -1
        }
        return try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 10_000
            conn.readTimeout = 10_000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.outputStream.use { it.write(body.toString().toByteArray()) }
            val code = conn.responseCode
            if (code >= 400) lastError = runCatching {
                conn.errorStream?.bufferedReader()?.readText()?.take(200)
            }.getOrNull()
            else lastBody = runCatching { conn.inputStream.bufferedReader().readText() }.getOrDefault("")
            code
        } catch (e: Exception) {
            lastError = e.javaClass.simpleName + ": " + e.message
            -1
        } finally {
            conn.disconnect()
        }
    }
}
