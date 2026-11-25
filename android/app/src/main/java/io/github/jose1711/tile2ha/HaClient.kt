package io.github.jose1711.tile2ha

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/** Minimal Home Assistant REST client (long-lived access token). */
class HaClient(private val baseUrl: String, private val token: String) {

    /** Returns the HTTP status code, or -1 on a network error (message in [lastError]). */
    fun callService(domain: String, service: String, data: JSONObject): Int =
        request("POST", "/api/services/$domain/$service", data.toString())

    fun ping(): Int = request("GET", "/api/", null)

    var lastError: String? = null
        private set

    private fun request(method: String, path: String, body: String?): Int {
        lastError = null
        val conn = try {
            URL(baseUrl + path).openConnection() as HttpURLConnection
        } catch (e: Exception) {
            lastError = "zlá URL: ${e.message}"
            return -1
        }
        return try {
            conn.requestMethod = method
            conn.connectTimeout = 10_000
            conn.readTimeout = 10_000
            conn.setRequestProperty("Authorization", "Bearer $token")
            if (body != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body.toByteArray()) }
            }
            val code = conn.responseCode
            if (code >= 400) lastError = runCatching {
                conn.errorStream?.bufferedReader()?.readText()?.take(200)
            }.getOrNull()
            code
        } catch (e: Exception) {
            lastError = e.javaClass.simpleName + ": " + e.message
            -1
        } finally {
            conn.disconnect()
        }
    }
}
