"""Pinch (thumb + index) to scroll the PC, joystick-style.

The spot where you pinch becomes the neutral point. Hold your hand below it to
scroll down, above it to scroll up — the further from neutral, the faster
(accelerating curve). Release the pinch to stop. No need to reposition: to keep
scrolling, just keep holding the offset.

Sweep a hand quickly sideways to switch app windows. Open apps form a fixed
ring (stable for as long as they stay open — no Alt+Tab recency reshuffling):
sweep right = next app, sweep left = previous app, instantly. Sweep detection
is motion-based (frame differencing), not landmark-based: the tracker loses a
fast hand to motion blur, but blur only makes the motion signal stronger.

Runs headless. Launching it again stops the running instance (high beep = started,
low beep = stopped).
"""
import ctypes
import math
import os
import signal
import subprocess
import sys
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
# Sweep = the motion centroid travelling SWIPE_DIST (fraction of frame width)
# mostly-horizontally within SWIPE_TIME seconds.
SWIPE_DIST = 0.25
SWIPE_TIME = 0.4
SWIPE_COOLDOWN = 0.6    # min gap between sweeps in the same direction
REVERSE_COOLDOWN = 1.5  # min gap before the opposite direction fires: the fast
                        # return stroke after a sweep must not undo the switch
MOTION_PX = 25     # gray-level change for a pixel to count as moving
MOTION_MIN = 0.02  # moving-pixel fraction below this = noise, ignore
MOTION_MAX = 0.5   # above this = scene/lighting change, not a hand

VK_ALT = 0x12

user32 = ctypes.windll.user32
# HWNDs must round-trip as pointers, not default 32-bit ints
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetAncestor.restype = ctypes.c_void_p
user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetWindow.restype = ctypes.c_void_p
user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
user32.EnumWindows.argtypes = [_EnumProc, ctypes.c_void_p]


def scroll(amount):
    user32.mouse_event(0x0800, 0, 0, int(amount), 0)  # MOUSEEVENTF_WHEEL


def key(vk, up=False):
    user32.keybd_event(vk, 0, 2 * up, 0)  # 2 = KEYEVENTF_KEYUP


def app_windows():
    """Alt-Tab-eligible top-level windows, in a stable (hwnd) order."""
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
    wins = []

    @_EnumProc
    def cb(hwnd, _):
        pid = ctypes.c_uint()  # skip our own windows (the --debug preview)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return True
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        cloaked = ctypes.c_int(0)  # invisible UWP/virtual-desktop ghosts
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
        if (user32.IsWindowVisible(hwnd)
                and not user32.GetWindow(hwnd, 4)  # GW_OWNER: skip owned dialogs
                and (not ex & WS_EX_TOOLWINDOW or ex & WS_EX_APPWINDOW)
                and user32.GetWindowTextLengthW(hwnd)
                and not cloaked.value):
            wins.append(hwnd)
        return True

    user32.EnumWindows(cb, None)
    return sorted(wins)  # hwnd order: arbitrary but stable while windows live


def switch_window(step):
    """Activate the current app's neighbour (step = +1/-1) in the window ring."""
    wins = app_windows()
    if not wins:
        return
    cur = user32.GetAncestor(user32.GetForegroundWindow(), 2)  # GA_ROOT
    i = wins.index(cur) if cur in wins else 0
    target = wins[(i + step) % len(wins)]
    if user32.IsIconic(target):
        user32.ShowWindow(target, 9)  # SW_RESTORE
    key(VK_ALT)  # an injected keypress lets a background process take foreground
    user32.SetForegroundWindow(target)
    key(VK_ALT, up=True)


def update_trail(trail, t, x, y):
    """Add a motion-centroid sample, drop ones older than SWIPE_TIME,
    return signed (dx, dy) travel across the window."""
    trail.append((t, x, y))
    while t - trail[0][0] > SWIPE_TIME:
        trail.popleft()
    return x - trail[0][1], y - trail[0][2]


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
                                     model_complexity=0,  # lighter = faster = re-locks onto fast hands sooner
                                     min_detection_confidence=0.4,  # catch blurred fast hands
                                     min_tracking_confidence=0.2)
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)  # MSMF backend takes ~16s to open here
    if not cap.isOpened():
        raise SystemExit("No camera found.")

    debug = "--debug" in sys.argv  # shows the camera feed + tracker state
    print("Pinch to set an anchor, hold hand above/below it to scroll. Ctrl+C to quit.")
    winsound.Beep(800, 200)  # high beep: started
    anchor_y = None
    smooth_y = None
    prev_t = None
    pinching = False
    acc = 0.0  # fractional wheel units carried between frames
    trail = deque()  # recent (t, motion-centroid x, y) samples for sweep detection
    last_swipe = 0.0
    last_step = 0
    prev_small = None
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
            # Sweep detection: centroid of changed pixels between consecutive
            # (downscaled) frames. Works at any hand speed — blur only makes
            # the diff stronger — and needs no landmark tracking at all.
            small = cv2.cvtColor(cv2.resize(frame, (80, 45)), cv2.COLOR_BGR2GRAY)
            frac = dx = dy = 0.0
            if prev_small is not None:
                moving = cv2.absdiff(small, prev_small) > MOTION_PX
                frac = moving.mean()
                if MOTION_MIN < frac < MOTION_MAX:
                    ys, xs = moving.nonzero()
                    dx, dy = update_trail(trail, now, xs.mean() / moving.shape[1],
                                          ys.mean() / moving.shape[0])
                    step = 1 if dx < 0 else -1  # unmirrored cam: user-right = -x
                    gap = now - last_swipe
                    if (abs(dx) > SWIPE_DIST and abs(dx) > 2 * abs(dy)  # horizontal
                            and gap > SWIPE_COOLDOWN
                            and (step == last_step or gap > REVERSE_COOLDOWN)):
                        switch_window(step)
                        winsound.Beep(1200, 30)  # click: sweep registered
                        last_swipe, last_step = now, step
                        trail.clear()
                        anchor_y = None  # a sweep is not a scroll: drop any anchor
            prev_small = small
            if pinching:
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
            if debug:
                cv2.putText(frame,
                            f"hand {'Y' if result.multi_hand_landmarks else '-'}"
                            f"  pinch {int(pinching)}  mot {frac:.2f}  dx {dx:+.2f}",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.imshow("pinch_scroll debug", frame)
                cv2.waitKey(1)
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
    assert any(abs(update_trail(fast, t / 30, 0.2 + t / 30 * 2.5, 0.5)[0]) > SWIPE_DIST
               for t in range(14))           # 2.5 frame-widths/sec
    slow = deque()
    assert not any(abs(update_trail(slow, t / 30, 0.2 + t / 30 * 0.3, 0.5)[0]) > SWIPE_DIST
                   for t in range(60))       # repositioning-speed drift stays under
    assert len(slow) <= SWIPE_TIME * 30 + 1  # old samples really get pruned

    # self-check: rightward sweep = image x decreasing, and it reads as horizontal
    r = deque()
    update_trail(r, 0, 0.8, 0.50)
    dx, dy = update_trail(r, 0.1, 0.4, 0.45)
    assert dx < -SWIPE_DIST and abs(dx) > 2 * abs(dy)  # -> switch_window(+1)

    # self-check: the window ring is non-empty and stable across two scans
    ring = app_windows()
    assert ring and ring == app_windows() == sorted(ring)
    main()
