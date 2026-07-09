package com.pinchscroll

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Handler
import android.view.accessibility.AccessibilityEvent
import kotlin.math.abs

/**
 * Injects a continuous vertical drag, system-wide. While the user holds the
 * pinch, a virtual finger stays pressed and keeps dragging at the requested
 * rate (chained via StrokeDescription.continueStroke), so content glides like
 * a real touch instead of stepping swipe-by-swipe. Near the screen edge it
 * lifts and re-presses from the center (invisible clutch).
 */
class ScrollService : AccessibilityService() {

    companion object {
        var instance: ScrollService? = null
        const val SEG_MS = 100L    // drag is extended in segments this long
        const val MIN_RATE = 50f   // px/sec below which the finger lifts (also avoids long-press)
        const val EDGE = 0.15f     // lift & re-center when the finger gets this close to an edge
    }

    // All gesture state is touched only on the main thread.
    private var stroke: GestureDescription.StrokeDescription? = null
    private var fingerY = 0f
    @Volatile private var rate = 0f  // px/sec; positive = finger moves down = content scrolls up

    override fun onServiceConnected() {
        instance = this
    }

    override fun onDestroy() {
        if (instance == this) instance = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    /** Called every camera frame while pinching. Starts the drag if needed. */
    fun setRate(pxPerSec: Float) {
        rate = pxPerSec
        Handler(mainLooper).post {
            if (stroke == null && abs(rate) >= MIN_RATE) {
                fingerY = resources.displayMetrics.heightPixels / 2f
                segment(first = true)
            }
        }
    }

    /** Called when the pinch releases. The drag lifts at the next segment boundary. */
    fun stop() {
        rate = 0f
    }

    private fun segment(first: Boolean) {
        val dm = resources.displayMetrics
        val x = dm.widthPixels / 2f
        val h = dm.heightPixels.toFloat()
        val r = rate
        if (abs(r) < MIN_RATE) {
            lift(x)
            return
        }
        val newY = (fingerY + r * SEG_MS / 1000f).coerceIn(h * EDGE, h * (1 - EDGE))
        if (newY == fingerY) {  // pinned against the edge: clutch (lift, re-press from center)
            lift(x)
            return
        }
        val path = Path().apply { moveTo(x, fingerY); lineTo(x, newY) }
        stroke = if (first) {
            GestureDescription.StrokeDescription(path, 0, SEG_MS, true)
        } else {
            stroke!!.continueStroke(path, 0, SEG_MS, true)
        }
        fingerY = newY
        dispatchGesture(
            GestureDescription.Builder().addStroke(stroke!!).build(),
            object : GestureResultCallback() {
                override fun onCompleted(g: GestureDescription?) = segment(first = false)
                override fun onCancelled(g: GestureDescription?) { stroke = null }
            }, null
        )
    }

    /** End the drag with a slow 1 px settle so nothing reads it as a fling. */
    private fun lift(x: Float) {
        val s = stroke ?: return
        stroke = null
        val path = Path().apply { moveTo(x, fingerY); lineTo(x, fingerY + 1f) }
        dispatchGesture(
            GestureDescription.Builder().addStroke(s.continueStroke(path, 0, 40, false)).build(),
            null, null
        )
    }
}
