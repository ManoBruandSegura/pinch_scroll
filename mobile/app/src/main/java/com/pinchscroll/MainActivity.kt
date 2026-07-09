package com.pinchscroll

import android.Manifest
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat

class MainActivity : AppCompatActivity() {

    private val permissions =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { refresh() }
    private lateinit var status: TextView
    private lateinit var live: TextView
    private val handler = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() {
            live.text = if (CameraService.running) CameraService.debug else ""
            handler.postDelayed(this, 150)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 96, 48, 48)
        }
        status = TextView(this).apply { textSize = 16f }
        root.addView(status)

        fun button(text: String, onClick: () -> Unit) {
            root.addView(Button(this).apply {
                setText(text)
                setOnClickListener { onClick(); refresh() }
            })
        }
        button("1. Grant camera permission") {
            permissions.launch(arrayOf(Manifest.permission.CAMERA, Manifest.permission.POST_NOTIFICATIONS))
        }
        button("2. Enable accessibility service") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        button("3. Start / stop scrolling") {
            if (CameraService.running) {
                stopService(Intent(this, CameraService::class.java))
            } else {
                ContextCompat.startForegroundService(this, Intent(this, CameraService::class.java))
            }
        }
        live = TextView(this).apply {
            textSize = 20f
            setPadding(0, 48, 0, 0)
        }
        root.addView(live)
        setContentView(root)
    }

    override fun onResume() {
        super.onResume()
        refresh()
        handler.post(tick)
    }

    override fun onPause() {
        handler.removeCallbacks(tick)
        super.onPause()
    }

    private fun refresh() {
        val cam = ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
                android.content.pm.PackageManager.PERMISSION_GRANTED
        status.text = """
            Camera permission: ${if (cam) "OK" else "missing"}
            Accessibility service: ${if (ScrollService.instance != null) "OK" else "not enabled"}
            Scrolling: ${if (CameraService.running) "RUNNING" else "stopped"}

            Pinch thumb+index in front of the front camera to set an anchor,
            then hold your hand above/below it to scroll.
        """.trimIndent()
    }
}
