package io.github.jose1711.tile2ha

import android.os.Build
import android.view.View
import android.view.WindowInsets

/**
 * Android 15+ draws apps targeting SDK 35 edge-to-edge: without this the top of the screen (status line,
 * webhook field) hides under the status bar / camera cutout and the bottom under the navigation bar.
 */
fun View.padForSystemBars(): View {
    val base = intArrayOf(paddingLeft, paddingTop, paddingRight, paddingBottom)
    setOnApplyWindowInsetsListener { v, insets ->
        if (Build.VERSION.SDK_INT >= 30) {
            val i = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout() or
                WindowInsets.Type.ime())
            v.setPadding(base[0] + i.left, base[1] + i.top, base[2] + i.right, base[3] + i.bottom)
        } else {
            @Suppress("DEPRECATION")
            v.setPadding(base[0] + insets.systemWindowInsetLeft, base[1] + insets.systemWindowInsetTop,
                base[2] + insets.systemWindowInsetRight, base[3] + insets.systemWindowInsetBottom)
        }
        insets
    }
    return this
}
