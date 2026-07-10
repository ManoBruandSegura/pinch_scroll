# Pinch Scroll 

A lightweight, headless Python utility that lets you scroll your PC using a pinch gesture with your webcam.

Instead of scrolling by pointing up and down continuously, **Pinch Scroll** uses a joystick-style interaction:
1. **Pinch** (bring your thumb and index finger together) to set a neutral anchor point.
2. **Hold** your hand below the anchor to scroll down, or above it to scroll up.
3. The further your hand moves from the anchor point, the faster it scrolls (with an accelerating curve).
4. **Release** the pinch to stop.

No need to reposition your hand; to keep scrolling, just keep holding the offset!

## Features

- **Joystick-style scrolling**: Effortlessly scroll long documents without repetitive gestures.
- **Volume knob**: Pinch your thumb and **middle** finger together and twist your hand like a knob — clockwise raises the volume, counterclockwise lowers it (Windows shows its volume overlay as feedback). The index-finger pinch still scrolls: whichever fingertip is nearest the thumb claims the gesture (after a few steady frames), so curling your middle finger — which drags the index along — can't be mistaken for a scroll pinch.
- **Fist to play/pause**: Close your hand into a fist for a moment to send the media play/pause key (a short beep confirms). Open your hand before making another fist to fire again.
- **Mic mute toggle**: Cover the webcam with your hand for about a second to mute the system microphone; cover it again to unmute. A low beep means muted, a high beep means live. Works even from idle standby.
- **Sweep to switch apps**: Sweep a hand quickly sideways to switch windows. Open apps form a fixed ring (no Alt+Tab recency reshuffling): sweep right = next app, sweep left = previous app, switched instantly. A short beep confirms each sweep. Detection is motion-based, so any speed works — the faster the sweep, the stronger the signal. The quick return stroke after a sweep is deliberately ignored; to go back the other way, pause ~1.5s first.
- **Headless and lightweight**: Runs silently in the background without opening any distracting windows.
- **Idle standby**: After 30s without motion in view, the hand tracker (the CPU-heavy part) is paused and the camera is polled at ~2 fps with only a tiny frame-diff watching. Any motion wakes it within half a second.
- **Toggle to Stop**: Run the application once to start it. Run it again to gracefully stop the existing instance.
- **Audible Cues**: Emits a high-pitched beep when started and a low-pitched beep when stopped.

## Requirements

- A webcam
- Python 3.7+
- Windows (uses `ctypes.windll.user32` for sending scroll events and `winsound` for beeps)

## Installation

1. Clone or download this repository.
2. Install the required Python packages:

```bash
pip install opencv-python mediapipe pycaw
```

## Usage

You can launch the program via a shortcut or from the terminal:

```bash
python app.py
```

- **To start**: Run `app.py` (or your shortcut). You will hear a **high beep**.
- **To stop**: Run `app.py` again. It will detect the running instance, kill it, and emit a **low beep**.

### Startup & shutdown speed

Starting takes a few seconds (~4s): the camera is opened through the **DirectShow** backend (`cv2.CAP_DSHOW`), which is far faster than OpenCV's default Media Foundation backend on Windows (~16s on some webcams). Stopping is near-instant (<1s): the heavy `opencv`/`mediapipe` imports are deferred until after the stop-toggle check, so the stop path never loads them.

### Configuration

You can easily adjust the sensitivity, deadzone, and other parameters directly in `app.py` by modifying these constants:

```python
SCROLL_SPEED = 800  # wheel units/sec when the hand is OFFSET_REF from the anchor
OFFSET_REF = 0.15   # offset (fraction of frame height) that gets SCROLL_SPEED
ACCEL = 1.7         # 1 = linear, higher = far offsets scroll disproportionately faster
DEADZONE = 0.02     # offsets smaller than this don't scroll (rest zone around anchor)
SMOOTH = 0.5        # 0..1, higher = snappier but jitterier position tracking
PINCH_ON = 0.35     # pinch detection threshold
PINCH_OFF = 0.55    # pinch release threshold (hysteresis)
PINCH_FRAMES = 3    # frames a finger must stay nearest the thumb to latch a pinch
VOL_STEP = 8        # degrees of knob twist per volume tick (1 tick = 2%)
SWIPE_DIST = 0.4    # fraction of frame width a sweep must cover
SWIPE_TIME = 0.4    # seconds the sweep must fit within
SWIPE_COOLDOWN = 0.6    # seconds before another same-direction sweep can fire
REVERSE_COOLDOWN = 1.5  # seconds before the opposite direction can fire
MOTION_PX = 25      # pixel-change threshold for the motion detector
MOTION_MIN = 0.02   # moving-pixel fraction below this = noise
MOTION_MAX = 0.5    # above this = lighting/scene change, ignored
IDLE_AFTER = 30     # seconds without motion before standby (motion wakes it)
COVER_DARK = 40     # mean gray level below this = webcam covered
COVER_TIME = 0.8    # seconds the cover must be held to toggle the mic
FIST_TIME = 0.4     # seconds a closed fist must be held to toggle play/pause
```

### Debugging

Run `python app.py --debug` to get a live camera preview with the tracker state
(hand detected, pinch state, sweep travel) overlaid — useful for tuning the
thresholds above.
