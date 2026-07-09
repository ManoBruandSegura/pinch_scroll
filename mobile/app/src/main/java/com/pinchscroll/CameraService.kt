package com.pinchscroll

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Matrix
import android.os.SystemClock
import android.os.VibrationEffect
import android.os.Vibrator
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleService
import com.google.mediapipe.framework.image.BitmapImageBuilder
import com.google.mediapipe.tasks.components.containers.NormalizedLandmark
import com.google.mediapipe.tasks.core.BaseOptions
import com.google.mediapipe.tasks.vision.core.RunningMode
import com.google.mediapipe.tasks.vision.handlandmarker.HandLandmarker
import com.google.mediapipe.tasks.vision.handlandmarker.HandLandmarkerResult
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.pow
import kotlin.math.sign

/**
 * Foreground service: front camera -> MediaPipe hand landmarks -> joystick scroll.
 * The spot where you pinch is the neutral anchor; hold your hand above/below it
 * to scroll, further = faster. Short vibration = pinch engaged, shorter = released.
 */
class CameraService : LifecycleService() {

    companion object {
        @Volatile var running = false
        @Volatile var debug = ""  // live state shown by MainActivity

        // --- tuning knobs ---
        // Pinch = thumb-tip/index-tip gap relative to hand size (validated on desktop).
        const val PINCH_ON = 0.35f
        const val PINCH_OFF = 0.55f
        // Offsets are in hand-lengths (wrist -> middle knuckle), so the feel is the
        // same whether the hand is near or far from the camera.
        const val DEADZONE = 0.10f      // ~1 cm of real hand travel before scrolling starts
        const val OFFSET_REF = 0.5f     // offset (beyond deadzone) that gets SCROLL_SPEED
        const val SCROLL_SPEED = 2.5f   // screen-heights/sec at OFFSET_REF
        const val ACCEL = 1.7f          // 1 = linear, higher = far offsets disproportionately faster
        const val SMOOTH = 0.3f         // 0..1, lower = smoother but laggier tracking
        const val MAX_SPEED = 5f        // cap, screen-heights/sec
    }

    private lateinit var landmarker: HandLandmarker
    private lateinit var analysisExecutor: ExecutorService
    private var pinching = false
    private var anchorY: Float? = null
    private var anchorHand = 1f  // hand size at pinch time, normalized units
    private var smoothY = 0f

    override fun onCreate() {
        super.onCreate()
        running = true
        startForeground(1, notification())
        landmarker = HandLandmarker.createFromOptions(
            this,
            HandLandmarker.HandLandmarkerOptions.builder()
                .setBaseOptions(BaseOptions.builder().setModelAssetPath("hand_landmarker.task").build())
                .setRunningMode(RunningMode.LIVE_STREAM)
                .setNumHands(1)
                .setMinHandDetectionConfidence(0.6f)
                .setMinTrackingConfidence(0.3f)
                .setResultListener { result, _ -> onHands(result) }
                .build()
        )
        analysisExecutor = Executors.newSingleThreadExecutor()
        startCamera()
    }

    private fun startCamera() {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            val provider = future.get()
            val analysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
                .build()
            var lastTs = 0L
            analysis.setAnalyzer(analysisExecutor) { proxy ->
                val ts = max(SystemClock.uptimeMillis(), lastTs + 1)  // must be monotonic
                lastTs = ts
                // Rotate the frame upright ourselves (like the official mediapipe
                // sample) — detection is unreliable on sideways hands.
                var bmp = proxy.toBitmap()
                val degrees = proxy.imageInfo.rotationDegrees
                proxy.close()
                if (degrees != 0) {
                    val m = Matrix().apply { postRotate(degrees.toFloat()) }
                    bmp = Bitmap.createBitmap(bmp, 0, 0, bmp.width, bmp.height, m, true)
                }
                landmarker.detectAsync(BitmapImageBuilder(bmp).build(), ts)
            }
            provider.unbindAll()
            provider.bindToLifecycle(this, CameraSelector.DEFAULT_FRONT_CAMERA, analysis)
        }, ContextCompat.getMainExecutor(this))
    }

    /** Thumb-tip/index-tip gap relative to hand size (wrist -> middle knuckle). */
    private fun pinchRatio(lm: List<NormalizedLandmark>): Float {
        val hand = hypot(lm[0].x() - lm[9].x(), lm[0].y() - lm[9].y())
        return hypot(lm[4].x() - lm[8].x(), lm[4].y() - lm[8].y()) / max(hand, 1e-6f)
    }

    /**
     * Screen-heights/sec for a hand held `offset` hand-lengths above (+) or
     * below (-) the anchor. Deadzone is subtracted, not gated, so the rate ramps
     * from zero at the deadzone edge instead of jumping.
     */
    private fun scrollRate(offset: Float): Float {
        val effective = abs(offset) - DEADZONE
        if (effective <= 0) return 0f
        return sign(offset) * (SCROLL_SPEED * (effective / OFFSET_REF).pow(ACCEL)).coerceAtMost(MAX_SPEED)
    }

    private fun onHands(result: HandLandmarkerResult) {
        val screen = result.landmarks().firstOrNull()
        val ratio = screen?.let { pinchRatio(it) }
        val wasPinching = pinching
        pinching = ratio != null && ratio < if (pinching) PINCH_OFF else PINCH_ON
        if (pinching != wasPinching) {
            vibrate(if (pinching) 40L else 15L)  // feel the pinch engage/release
        }
        debug = if (screen == null) "hand: NOT DETECTED" else
            "hand: detected\npinch ratio: %.2f (pinch < %.2f)\npinching: %s".format(
                ratio, if (pinching) PINCH_OFF else PINCH_ON, pinching)
        if (!pinching) {
            anchorY = null
            ScrollService.instance?.stop()
            return
        }

        val y = screen!![0].y()  // wrist: steadier than fingertips
        val anchor = anchorY
        if (anchor == null) {  // pinch start = neutral point
            anchorY = y
            smoothY = y
            anchorHand = max(
                hypot(screen[0].x() - screen[9].x(), screen[0].y() - screen[9].y()),
                0.05f  // guard against a degenerate hand-size reading
            )
            return
        }
        smoothY += SMOOTH * (y - smoothY)

        val offset = (anchor - smoothY) / anchorHand
        val rate = scrollRate(offset)
        debug += "\noffset: %.2f hand-lengths (deadzone %.2f)\nscroll rate: %.2f screens/sec"
            .format(offset, DEADZONE, rate)
        ScrollService.instance?.setRate(rate * resources.displayMetrics.heightPixels)
    }

    private fun vibrate(ms: Long) {
        getSystemService(Vibrator::class.java)
            ?.vibrate(VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE))
    }

    private fun notification(): Notification {
        val channel = NotificationChannel("scroll", "Pinch Scroll", NotificationManager.IMPORTANCE_LOW)
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        return Notification.Builder(this, "scroll")
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentTitle("Pinch Scroll is watching for gestures")
            .build()
    }

    override fun onDestroy() {
        running = false
        ScrollService.instance?.stop()
        landmarker.close()
        analysisExecutor.shutdown()
        super.onDestroy()
    }

    override fun onBind(intent: Intent) = super.onBind(intent)
}
