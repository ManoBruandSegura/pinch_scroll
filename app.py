"""Pinch (thumb + index) to scroll the PC, joystick-style.

The spot where you pinch becomes the neutral point. Hold your hand below it to
scroll down, above it to scroll up — the further from neutral, the faster
(accelerating curve). Release the pinch to stop. No need to reposition: to keep
scrolling, just keep holding the offset.

Sweep an open (unpinched) hand quickly sideways to switch windows (Alt+Tab).

Runs headless. Launching it again stops the running instance (high beep = started,
low beep = stopped).
"""
import ctypes
import math
import os
import signal
import subprocess
import tempfile
import time
import winsound
from collections import deque

PIDFILE = os.path.join(tempfile.gettempdir(), "pinch_scroll.pid")

# --- tuning knobs ---
SCROLL_SPEED = 800  # wheel units/sec when the hand is OFFSET_REF from the anchor
OFFSET_REF = 0.15   # offset (fraction of frame height) that gets SCROLL_SPEED
ACCEL = 1.7         # 1 = linear, higher = far offsets scroll disproportionately faster
DEADZONE = 0.02     # offsets smaller than this don't scroll (rest zone around anchor)
SMOOTH = 0.5        # 0..1, higher = snappier but jitterier position tracking
# Pinch = thumb-tip/index-tip gap relative to hand size (works at any distance
# from the camera). Hysteresis: easier to hold than to start, so one noisy
# frame doesn't drop the pinch.
PINCH_ON = 0.35
PINCH_OFF = 0.55
# Sweep = unpinched wrist travelling SWIPE_DIST (fraction of frame width)
# within SWIPE_TIME seconds. Cooldown so one sweep fires one Alt+Tab.
SWIPE_DIST = 0.35
SWIPE_TIME = 0.25
SWIPE_COOLDOWN = 0.8


def scroll(amount):
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, int(amount), 0)  # MOUSEEVENTF_WHEEL


def alt_tab():
    """Quick Alt+Tab tap: switches to the previous window."""
    for vk, flag in ((0x12, 0), (0x09, 0), (0x09, 2), (0x12, 2)):  # alt/tab down, up
        ctypes.windll.user32.keybd_event(vk, 0, flag, 0)


def update_trail(trail, t, x):
    """Add a wrist sample, drop ones older than SWIPE_TIME, return travel."""
    trail.append((t, x))
    while t - trail[0][0] > SWIPE_TIME:
        trail.popleft()
    return abs(x - trail[0][1])


def pinch_ratio(lm):
    hand = math.hypot(lm[0].x - lm[9].x, lm[0].y - lm[9].y)  # wrist -> middle knuckle
    return math.hypot(lm[4].x - lm[8].x, lm[4].y - lm[8].y) / max(hand, 1e-6)


def scroll_rate(offset):
    """Wheel units/sec for a hand held `offset` above (+) or below (-) the anchor."""
    if abs(offset) < DEADZONE:
        return 0.0
    return math.copysign(SCROLL_SPEED * (abs(offset) / OFFSET_REF) ** ACCEL, offset)


def stop_existing():
    """If another instance is running, kill it and return True (toggle behavior)."""
    try:
        pid = int(open(PIDFILE).read())
    except (OSError, ValueError):
        return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                         capture_output=True, text=True).stdout
    if "python" not in out.lower():
        return False  # stale pidfile from a crashed/killed run
    os.kill(pid, signal.SIGTERM)
    os.remove(PIDFILE)
    return True


def main():
    if stop_existing():
        winsound.Beep(400, 200)  # low beep: stopped
        return
    import cv2  # heavy imports (~2s) deferred past the toggle check: stopping is instant
    import mediapipe as mp
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    hands = mp.solutions.hands.Hands(max_num_hands=1,
                                     model_complexity=1,
                                     min_detection_confidence=0.6,
                                     min_tracking_confidence=0.3)
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)  # MSMF backend takes ~16s to open here
    if not cap.isOpened():
        raise SystemExit("No camera found.")

    print("Pinch to set an anchor, hold hand above/below it to scroll. Ctrl+C to quit.")
    winsound.Beep(800, 200)  # high beep: started
    anchor_y = None
    smooth_y = None
    prev_t = None
    pinching = False
    acc = 0.0  # fractional wheel units carried between frames
    trail = deque()  # recent (t, wrist x) samples for sweep detection
    last_swipe = 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            now = time.time()
            result = hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            if result.multi_hand_landmarks:
                lm = result.multi_hand_landmarks[0].landmark
                pinching = pinch_ratio(lm) < (PINCH_OFF if pinching else PINCH_ON)
            else:
                pinching = False
            if pinching:
                trail.clear()  # scroll mode: hand motion is not a sweep
                y = lm[0].y  # wrist: steadier than fingertips
                if anchor_y is None:
                    anchor_y = smooth_y = y  # pinch start = neutral point
                else:
                    smooth_y += SMOOTH * (y - smooth_y)  # EMA against jitter
                    acc += scroll_rate(anchor_y - smooth_y) * (now - prev_t)
                    if abs(acc) >= 1:
                        scroll(int(acc))
                        acc -= int(acc)
                prev_t = now
            else:
                anchor_y = None
                acc = 0.0
                if result.multi_hand_landmarks:
                    if (update_trail(trail, now, lm[0].x) > SWIPE_DIST
                            and now - last_swipe > SWIPE_COOLDOWN):
                        alt_tab()
                        last_swipe = now
                        trail.clear()
                else:
                    trail.clear()  # hand lost: don't bridge across reappearance
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        try:
            os.remove(PIDFILE)  # killed-via-toggle leaves this to the tasklist check
        except OSError:
            pass


if __name__ == "__main__":
    # self-check: direction, deadzone, acceleration curve
    assert scroll_rate(0.1) > 0       # hand above anchor -> scroll up
    assert scroll_rate(-0.1) < 0      # hand below anchor -> scroll down
    assert scroll_rate(0.01) == 0.0   # inside rest zone -> no scroll
    assert scroll_rate(0.3) > 2 * scroll_rate(0.15)  # 2x offset -> more than 2x speed

    # self-check: pinch ratio is scale-invariant
    class P:
        def __init__(self, x, y): self.x, self.y = x, y
    def hand(scale):
        lm = [P(0, 0)] * 21
        lm[0], lm[9] = P(0, 0), P(0, scale)          # hand size
        lm[4], lm[8] = P(0, 0), P(0.1 * scale, 0)    # small pinch gap
        return lm
    assert pinch_ratio(hand(1.0)) == pinch_ratio(hand(0.3)) < PINCH_ON
    far = hand(1.0); far[4] = P(0.9, 0)
    assert pinch_ratio(far) > PINCH_OFF

    # self-check: fast sweep crosses SWIPE_DIST inside the window, slow drift doesn't
    fast = deque()
    assert any(update_trail(fast, t / 30, 0.2 + t / 30 * 2.5) > SWIPE_DIST
               for t in range(8))            # 2.5 frame-widths/sec for ~0.25s
    slow = deque()
    assert not any(update_trail(slow, t / 30, 0.2 + t / 30 * 0.5) > SWIPE_DIST
                   for t in range(60))       # same 1s distance, spread over 2s
    assert len(slow) <= SWIPE_TIME * 30 + 1  # old samples really get pruned
    main()
