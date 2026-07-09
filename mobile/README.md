# Pinch Scroll — Android port

Same joystick model as the desktop `app.py`: pinch thumb+index in front of the
**front camera** to set an anchor, hold your hand above/below it to scroll
(further = faster), release to stop.

- **Hand tracking**: MediaPipe Tasks `HandLandmarker` (model already in
  `app/src/main/assets/hand_landmarker.task`), fed by CameraX from a foreground service.
- **Scrolling**: an AccessibilityService injects vertical swipe gestures system-wide.
  Scroll amounts accumulate and are batched into swipes.

## Build & run

1. Open the `mobile/` folder in Android Studio (it will generate the Gradle
   wrapper on first sync).
2. Run on a real device (needs a camera — the emulator won't cut it).
3. In the app: grant camera permission → enable "Pinch Scroll" in the
   accessibility settings it opens → tap Start.

## Caveats

- **Untested scaffold**: written blind, expect to iterate on device. Tuning
  knobs are constants at the top of `CameraService.kt`, mirroring `app.py`.
- The camera only runs while the screen is on (which is when you'd scroll
  anyway). Continuous camera use is a real battery cost, and Android shows the
  green camera-in-use indicator the whole time.
- Scrolls are injected as discrete swipes, so it steps rather than glides;
  lower `MIN_SWIPE_PX` for smoother/smaller steps.
