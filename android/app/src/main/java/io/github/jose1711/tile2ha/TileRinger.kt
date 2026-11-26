package io.github.jose1711.tile2ha

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.os.Build
import android.util.Base64
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.SecureRandom
import java.util.UUID
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Connects to a Tile and rings it. Port of custom_components/tile_ble/ring_ble.py (TOA/MEP protocol),
 * except that the channel key is computed by HA (webhook "session_key"): the auth_key stays in HA and
 * the phone only gets a key valid for this one connection.
 *
 * Blocking; call from a worker thread. [progress] gets short human-readable steps.
 */
class TileRinger(
    private val ctx: Context,
    private val address: String,
    private val tileKey: String,
    private val ha: HaClient,
    private val progress: (String) -> Unit,
) {
    class RingError(msg: String) : Exception(msg)

    private class Frame(val prefix: Int, val data: ByteArray)

    private var gatt: BluetoothGatt? = null
    private var cmdChar: BluetoothGattCharacteristic? = null
    private val connected = Semaphore(0)
    private val opDone = Semaphore(0)
    private val mtuDone = Semaphore(0)
    @Volatile private var opStatus = 0
    @Volatile private var disconnected = false
    private val frames = LinkedBlockingQueue<Frame>()

    private val rnd = SecureRandom()
    private val localToken = ByteArray(4).also { rnd.nextBytes(it) }
    private var channelPrefix: Int? = null
    private var sessionKey: ByteArray? = null
    private var nonceA = 0L
    private var features: ByteArray? = null
    private var tdgDone = false

    fun ring(volume: Int = 3, seconds: Int = 5) {
        try {
            connect()
            authenticate()
            progress("Zvoním…")
            val payload = if (supported("TSD")) byteArrayOf(2, 1, volume.toByte(), seconds.toByte())
            else byteArrayOf(2, 1, volume.toByte())
            sendCid(5, payload, waitPrefix = 7, timeoutMs = 10_000)
            Thread.sleep(seconds * 1000L + 2000)
        } finally {
            close()
        }
    }

    // --- GATT --------------------------------------------------------------------------------

    private val callback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) connected.release()
            else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                disconnected = true
                connected.release(); opDone.release()
                frames.offer(Frame(-1, ByteArray(0)))  // wakes a waiting reader
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) { opStatus = status; opDone.release() }
        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            opStatus = status; opDone.release()
        }
        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            opStatus = status; opDone.release()
        }
        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) { mtuDone.release() }

        @Deprecated("API < 33")
        @Suppress("DEPRECATION")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) =
            onNotify(c.value ?: ByteArray(0))

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) =
            onNotify(value)
    }

    private fun onNotify(raw: ByteArray) {
        if (raw.isEmpty()) return
        val prefix = raw[0].toInt() and 0xFF
        if (prefix == 0) {
            if (raw.size < 6) return
            val tok = raw.copyOfRange(1, 5)
            if (!(tok.all { it == 0xFF.toByte() } || tok.contentEquals(localToken))) return
            frames.offer(Frame(raw[5].toInt() and 0xFF, raw.copyOfRange(6, raw.size)))
        } else if (prefix == channelPrefix && raw.size >= 2) {
            val data = raw.copyOfRange(2, raw.size)
            // channel frames end with a 4 B MIC
            frames.offer(Frame(raw[1].toInt() and 0xFF, if (data.size >= 4) data.copyOf(data.size - 4) else data))
        }
    }

    private fun await(sem: Semaphore, ms: Long, what: String) {
        if (!sem.tryAcquire(ms, TimeUnit.MILLISECONDS)) throw RingError("$what: časový limit")
        if (disconnected) throw RingError("Tile sa odpojil ($what)")
    }

    @SuppressLint("MissingPermission")
    private fun connect() {
        progress("Pripájam sa k $address…")
        val adapter = ctx.getSystemService(BluetoothManager::class.java)?.adapter
            ?: throw RingError("Bluetooth nie je k dispozícii")
        val dev: BluetoothDevice = adapter.getRemoteDevice(address)
        gatt = dev.connectGatt(ctx, false, callback, BluetoothDevice.TRANSPORT_LE)
        await(connected, 15_000, "pripojenie")
        val g = gatt!!
        // default MTU fits 20 B writes (TCU/TIME/preAuth); a longer TDT config needs more
        if (g.requestMtu(64)) mtuDone.tryAcquire(3, TimeUnit.SECONDS)
        if (!g.discoverServices()) throw RingError("discoverServices zlyhalo")
        await(opDone, 15_000, "služby")
        val svc = g.getService(FEED) ?: throw RingError("Tile služba 0xFEED nenájdená")
        cmdChar = svc.getCharacteristic(MEP_COMMAND) ?: throw RingError("chýba MEP command")
        val rsp = svc.getCharacteristic(MEP_RESPONSE) ?: throw RingError("chýba MEP response")
        g.setCharacteristicNotification(rsp, true)
        val cccd = rsp.getDescriptor(CCCD) ?: throw RingError("chýba CCCD")
        val enable = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
        if (Build.VERSION.SDK_INT >= 33) g.writeDescriptor(cccd, enable)
        else @Suppress("DEPRECATION") run { cccd.value = enable; g.writeDescriptor(cccd) }
        await(opDone, 5_000, "notifikácie")
    }

    @SuppressLint("MissingPermission")
    private fun write(frame: ByteArray) {
        val g = gatt ?: throw RingError("nepripojené")
        val c = cmdChar!!
        val type = BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
        val ok = if (Build.VERSION.SDK_INT >= 33) g.writeCharacteristic(c, frame, type) == android.bluetooth.BluetoothStatusCodes.SUCCESS
        else @Suppress("DEPRECATION") run { c.writeType = type; c.value = frame; g.writeCharacteristic(c) }
        if (!ok) throw RingError("zápis zlyhal")
        await(opDone, 5_000, "zápis")
    }

    @SuppressLint("MissingPermission")
    private fun close() {
        runCatching { gatt?.disconnect() }
        runCatching { gatt?.close() }
        gatt = null
    }

    // --- TOA frames -----------------------------------------------------------------------------

    /** Next frame with [prefix]; other frames (diagnostics, keep-alive) are handled on the way. */
    private fun waitFor(prefix: Int, timeoutMs: Long): ByteArray {
        val end = System.currentTimeMillis() + timeoutMs
        while (true) {
            val left = end - System.currentTimeMillis()
            if (left <= 0) throw RingError("Tile neodpovedal (TOA $prefix)")
            val f = frames.poll(left, TimeUnit.MILLISECONDS) ?: continue
            if (f.prefix == -1) throw RingError("Tile sa odpojil")
            if (f.prefix == prefix) return f.data
            unsolicited(f)
        }
    }

    private fun unsolicited(f: Frame) {
        when (f.prefix) {
            12 -> if (f.data.isNotEmpty() && f.data[0] == 1.toByte()) tdgDone = true   // last TDG chunk
            10 -> if (f.data.isNotEmpty() && f.data[0] == 3.toByte()) sendCid(8, byteArrayOf(3))  // TKA check
        }
    }

    private fun sendPreAuth(prefix: Int, data: ByteArray, waitPrefix: Int?): ByteArray? {
        write(byteArrayOf(0) + localToken + byteArrayOf(prefix.toByte()) + data)
        return waitPrefix?.let { waitFor(it, 10_000) }
    }

    private fun sendCid(prefix: Int, data: ByteArray, waitPrefix: Int? = null, timeoutMs: Long = 10_000): ByteArray? {
        nonceA++
        val payload = byteArrayOf(prefix.toByte()) + data
        val mic = hmac(sessionKey!!, longLe(nonceA) + byteArrayOf(1, payload.size.toByte()) + payload).copyOf(4)
        write(byteArrayOf(channelPrefix!!.toByte()) + payload + mic)
        return waitPrefix?.let { waitFor(it, timeoutMs) }
    }

    private fun authenticate() {
        progress("Overujem…")
        // TDI (identification), as the official app does before authenticating
        val avail = sendPreAuth(19, byteArrayOf(1), 20)?.firstOrNull()?.toInt() ?: 0
        for ((bit, req) in listOf(0 to 2, 1 to 3, 2 to 4, 3 to 5))
            if (avail and (1 shl bit) != 0) sendPreAuth(19, byteArrayOf(req.toByte()), 20)

        val randA = ByteArray(14).also { rnd.nextBytes(it) }
        sendPreAuth(20, randA, 21)
        val open = sendPreAuth(16, randA, 18) ?: throw RingError("kanál sa neotvoril")
        if (open.isEmpty()) throw RingError("prázdna odpoveď OPEN_CHANNEL")
        val chPrefix = open[0].toInt() and 0xFF
        val chData = open.copyOfRange(1, open.size)

        progress("Kľúč relácie z HA…")
        val sk = ha.sessionKey(tileKey, randA, chPrefix, chData, localToken)
            ?: throw RingError("HA nevydal kľúč: ${ha.lastError ?: "?"}")
        sessionKey = hex(sk.getString("session_key"))
        val tdt = sk.optString("expected_tdt_cmd_config", "").takeIf { it.isNotEmpty() && it != "null" }
        channelPrefix = chPrefix

        val ready = sendCid(18, byteArrayOf(19), waitPrefix = 1)!!
        parseReady(ready)
        when {
            supported("TFC") -> sendCid(15, byteArrayOf(1))
            supported("TDG") -> sendCid(10, byteArrayOf(1))
            else -> throw RingError("Tile nepodporuje TFC ani TDG")
        }
        // the handshake continues after the last diagnostics (TDG) chunk
        val end = System.currentTimeMillis() + 25_000
        while (!tdgDone) {
            val left = end - System.currentTimeMillis()
            if (left <= 0) throw RingError("Tile neposlal diagnostiku (TDG)")
            val f = frames.poll(left, TimeUnit.MILLISECONDS) ?: continue
            if (f.prefix == -1) throw RingError("Tile sa odpojil")
            unsolicited(f)
        }
        if (supported("PPM")) sendCid(6, ByteArray(0))
        if (supported("ADV_INT")) sendCid(7, byteArrayOf(2), waitPrefix = 9)
        if (supported("TCU")) {
            val tcu = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN)
                .putShort(288).putShort(304).putShort(4).putShort(600).array()
            sendCid(12, byteArrayOf(3) + tcu, waitPrefix = 14, timeoutMs = 15_000)
        }
        if (supported("TDT") && tdt != null) sendCid(4, byteArrayOf(1) + Base64.decode(tdt, Base64.DEFAULT))
        if (supported("TIME")) sendCid(13, byteArrayOf(3) + longLe(System.currentTimeMillis() / 1000 - 1451606400L))
        if (supported("TPFS")) runCatching { sendCid(5, byteArrayOf(6), waitPrefix = 7, timeoutMs = 5_000) }
    }

    /** TOA_RSP_READY: [maxPayload, info(3), nonceB(4), features...] (see ring_ble._parse_ready). */
    private fun parseReady(data: ByteArray) {
        val nonce = if (data.isNotEmpty()) data.copyOfRange(1, data.size) else ByteArray(0)
        features = if (nonce.size >= 7) {
            val rest = nonce.copyOfRange(7, nonce.size)
            nonce.copyOf(3) + rest
        } else nonce
    }

    private fun supported(name: String): Boolean {
        val f = features ?: return false
        val (byteLoc0, bit) = FEATURES[name] ?: return false
        val byteLoc = if (byteLoc0 > 2) byteLoc0 + 1 else byteLoc0
        return byteLoc < f.size && (f[byteLoc].toInt() and (1 shl bit)) != 0
    }

    companion object {
        val FEED: UUID = UUID.fromString("0000feed-0000-1000-8000-00805f9b34fb")
        val MEP_COMMAND: UUID = UUID.fromString("9d410018-35d6-f4dd-ba60-e7bd8dc491c0")
        val MEP_RESPONSE: UUID = UUID.fromString("9d410019-35d6-f4dd-ba60-e7bd8dc491c0")
        val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        private val FEATURES = mapOf(
            "TDT" to (0 to 2), "PPM" to (0 to 4), "ADV_INT" to (0 to 5), "TDG" to (1 to 2),
            "TCU" to (1 to 4), "TIME" to (1 to 5), "TFC" to (2 to 0), "TPFS" to (2 to 1), "TSD" to (3 to 1),
        )

        /** HMAC-SHA256 over [msg] zero-padded to 32 B (toa_protocol.generate_hmac). */
        fun hmac(key: ByteArray, msg: ByteArray): ByteArray {
            require(msg.size <= 32) { "msg > 32 B" }
            val mac = Mac.getInstance("HmacSHA256")
            mac.init(SecretKeySpec(key, "HmacSHA256"))
            return mac.doFinal(msg.copyOf(32))
        }

        fun longLe(n: Long): ByteArray = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN).putLong(n).array()

        fun hex(s: String): ByteArray = ByteArray(s.length / 2) { s.substring(it * 2, it * 2 + 2).toInt(16).toByte() }

        fun toHex(b: ByteArray): String = b.joinToString("") { String.format(java.util.Locale.ROOT, "%02x", it) }
    }
}
