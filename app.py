"""Pinch (thumb + index) to scroll the PC, joystick-style.

The spot where you pinch becomes the neutral point. Hold your hand below it to
scroll down, above it to scroll up — the further from neutral, the faster
(accelerating curve). Release the pinch to stop. No need to reposition: to keep
scrolling, just keep holding the offset.

Pinch thumb + middle finger and twist like a knob to change the system volume:
clockwise = up, counterclockwise = down.

Cover the webcam with your hand for ~a second to mute/unmute the system
microphone (low beep = muted, high beep = live again).

Close your hand into a fist for a moment to play/pause media.

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
PINCH_FRAMES = 3  # frames the same finger must stay nearest before a pinch latches
VOL_STEP = 8  # degrees of knob twist per volume tick (Windows: 1 tick = 2%)
# Sweep = the motion centroid travelling SWIPE_DIST (fraction of frame width)
# mostly-horizontally within SWIPE_TIME seconds.
SWIPE_DIST = 0.4  # a hand approaching the camera drifts the centroid ~0.3; stay above
SWIPE_TIME = 0.4
SWIPE_COOLDOWN = 0.6    # min gap between sweeps in the same direction
REVERSE_COOLDOWN = 1.5  # min gap before the opposite direction fires: the fast
                        # return stroke after a sweep must not undo the switch
MOTION_PX = 25     # gray-level change for a pixel to count as moving
MOTION_MIN = 0.02  # moving-pixel fraction below this = noise, ignore
MOTION_MAX = 0.5   # above this = scene/lighting change, not a hand
IDLE_AFTER = 30    # s without motion -> standby: skip hand tracking until motion wakes it
COVER_DARK = 40    # mean gray level below this = webcam covered by a hand
COVER_TIME = 0.8   # s the cover must be held before the mic mute toggles
FIST_TIME = 0.4    # s a closed fist must be held to toggle play/pause

VK_ALT = 0x12
VK_VOL_UP, VK_VOL_DOWN = 0xAF, 0xAE
VK_MEDIA_PLAY = 0xB3

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


def pinch_ratio(lm, tip=8):
    """Thumb-tip gap to `tip` (8 = index: scroll, 12 = middle: volume knob)."""
    hand = math.hypot(lm[0].x - lm[9].x, lm[0].y - lm[9].y)  # wrist -> middle knuckle
    return math.hypot(lm[4].x - lm[tip].x, lm[4].y - lm[tip].y) / max(hand, 1e-6)


def hand_angle(lm):
    """Image-plane heading (degrees) of the wrist -> middle-knuckle vector."""
    return math.degrees(math.atan2(lm[9].y - lm[0].y, lm[9].x - lm[0].x))


def angle_step(prev, cur):
    """Signed smallest rotation from prev to cur, seam-safe (-180..180]."""
    return (cur - prev + 180) % 360 - 180


def toggle_mic():
    """Flip the default microphone's mute; return True if now muted."""
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    vol = ctypes.cast(AudioUtilities.GetMicrophone().Activate(
        IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
        ctypes.POINTER(IAudioEndpointVolume))
    muted = not vol.GetMute()
    vol.SetMute(muted, None)
    return muted


def hold_step(start, done, on, now, hold):
    """Held-condition tracker. Returns (start, done, fire): fire goes True
    exactly once per continuous stretch of `on` lasting `hold` seconds."""
    if not on:
        return None, False, False
    if start is None:
        return now, done, False
    if not done and now - start > hold:
        return start, True, True
    return start, done, False


def is_fist(lm):
    """All four fingertips curled in closer to the wrist than their knuckles."""
    def d(i):
        return math.hypot(lm[i].x - lm[0].x, lm[i].y - lm[0].y)
    return all(d(tip) < d(tip - 3) for tip in (8, 12, 16, 20))  # tip-3 = knuckle


def pinch_fsm(mode, streak, ri, rm):
    """One pinch gesture at a time, picked by which tip is nearest the thumb.

    Curling the middle finger drags the index along with it, so both ratios
    can be under PINCH_ON at once — nearest wins, but only after PINCH_FRAMES
    consecutive frames, because the index tip often passes closest while a
    middle pinch is still forming. PINCH_OFF releases (hysteresis). Returns
    (mode, streak): mode in (None, "scroll", "vol"); streak is the signed
    run length of the current candidate (+ = index/scroll, - = middle/vol).
    """
    if mode == "scroll":
        return (None if ri > PINCH_OFF else mode), 0
    if mode == "vol":
        return (None if rm > PINCH_OFF else mode), 0
    if min(ri, rm) >= PINCH_ON:
        return None, 0
    s = 1 if ri <= rm else -1
    streak = streak + s if s * streak > 0 else s
    if abs(streak) >= PINCH_FRAMES:
        return ("scroll" if s > 0 else "vol"), 0
    return None, streak


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
    mode = None  # None / "scroll" / "vol" — the currently latched pinch gesture
    streak = 0
    acc = 0.0  # fractional wheel units carried between frames
    vol_angle = None
    vol_acc = 0.0  # accumulated twist (degrees) not yet paid out as volume ticks
    trail = deque()  # recent (t, motion-centroid x, y) samples for sweep detection
    last_motion = time.time()
    last_swipe = 0.0
    last_step = 0
    prev_small = None
    idle = False
    cover_start = None
    mic_done = True  # pre-"fired": a launch in a dark room must not mute the mic
    fist_start = None
    fist_done = False
    try:
        while True:
            if idle:
                time.sleep(0.45)  # ~2 fps is plenty for spotting wake motion
            ok, frame = cap.read()
            if not ok:
                break
            now = time.time()
            idle = now - last_motion > IDLE_AFTER
            # Standby: skip MediaPipe (the CPU hog) and poll slowly; the cheap
            # frame-diff below keeps watching and any motion wakes us next frame.
            # ponytail: a sweep from cold standby may need a second try (first one wakes it)
            result = None if idle else hands.process(
                cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            ri = rm = 9.9
            fist = False
            if result and result.multi_hand_landmarks:
                lm = result.multi_hand_landmarks[0].landmark
                ri, rm = pinch_ratio(lm), pinch_ratio(lm, 12)
                fist = is_fist(lm)
                if fist:  # fingers pass near the thumb while a fist closes:
                    mode, streak = None, 0  # never read that as a pinch
                else:
                    mode, streak = pinch_fsm(mode, streak, ri, rm)
            else:
                mode, streak = None, 0
            pinching = mode == "scroll"
            vol_pinch = mode == "vol"
            # Sweep detection: centroid of changed pixels between consecutive
            # (downscaled) frames. Works at any hand speed — blur only makes
            # the diff stronger — and needs no landmark tracking at all.
            small = cv2.cvtColor(cv2.resize(frame, (80, 45)), cv2.COLOR_BGR2GRAY)
            lum = small.mean()
            # Covering the lens blacks the frame out; hold it to toggle the mic.
            # ponytail: a genuinely dark room reads as covered — add a "was
            # recently bright" check if that ever bites
            cover_start, mic_done, fire = hold_step(cover_start, mic_done,
                                                    lum < COVER_DARK, now, COVER_TIME)
            if fire:
                winsound.Beep(250 if toggle_mic() else 900, 180)
            fist_start, fist_done, fire = hold_step(fist_start, fist_done,
                                                    fist, now, FIST_TIME)
            if fire:  # open the hand to re-arm; holding the fist won't refire
                key(VK_MEDIA_PLAY)
                key(VK_MEDIA_PLAY, up=True)
                winsound.Beep(600, 80)
            frac = dx = dy = 0.0
            if prev_small is not None:
                moving = cv2.absdiff(small, prev_small) > MOTION_PX
                frac = moving.mean()
                if frac > MOTION_MIN:  # any motion (even a scene change) ends standby
                    last_motion = now
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
                last_motion = now  # a held pinch is motionless; don't standby mid-scroll
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
            if vol_pinch:
                last_motion = now  # a slow twist barely moves pixels; don't standby
                a = hand_angle(lm)
                if vol_angle is not None:
                    # Unmirrored cam: user-clockwise = image angle decreasing
                    vol_acc += angle_step(vol_angle, a)
                    while abs(vol_acc) >= VOL_STEP:
                        vk = VK_VOL_UP if vol_acc < 0 else VK_VOL_DOWN
                        key(vk)
                        key(vk, up=True)
                        vol_acc -= math.copysign(VOL_STEP, vol_acc)
                vol_angle = a
            else:
                vol_angle = None
                vol_acc = 0.0
            if debug:
                cv2.putText(frame,
                            f"hand {'Y' if result and result.multi_hand_landmarks else '-'}"
                            f"  ri {ri:.2f}  rm {rm:.2f}  mode {mode or '-'}"
                            f"  fist {int(fist)}",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.putText(frame,
                            f"mot {frac:.2f}  dx {dx:+.2f}  lum {lum:.0f}"
                            f"  idle {int(idle)}",
                            (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
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

    # self-check: volume pinch (thumb+middle) reads independently of the index
    knob = hand(1.0)
    knob[8], knob[12] = P(0.9, 0), P(0.1, 0)  # index extended, middle on thumb
    assert pinch_ratio(knob, 12) < PINCH_ON < PINCH_OFF < pinch_ratio(knob)

    # self-check: middle pinch drags the index under PINCH_ON too — the
    # nearest tip must still win, and only after PINCH_FRAMES steady frames
    m, s = None, 0
    for _ in range(PINCH_FRAMES - 1):
        m, s = pinch_fsm(m, s, 0.30, 0.10)  # both "pinched", middle nearer
        assert m is None                    # not latched yet
    m, s = pinch_fsm(m, s, 0.30, 0.10)
    assert m == "vol"
    m, s = pinch_fsm(m, s, 0.05, 0.40)      # index sneaks nearer: latch holds
    assert m == "vol"
    m, s = pinch_fsm(m, s, 0.05, 0.60)      # middle past PINCH_OFF: released
    assert m is None
    m, s = pinch_fsm(m, s, 0.30, 0.90)      # index flicker...
    m, s = pinch_fsm(m, s, 0.90, 0.20)      # ...resets the middle streak
    m, s = pinch_fsm(m, s, 0.90, 0.20)
    assert m is None
    m, s = pinch_fsm(m, s, 0.90, 0.20)
    assert m == "vol"

    # self-check: twist direction and seam-safe angle deltas
    assert angle_step(175, -175) == 10    # crossing the +/-180 seam
    assert angle_step(-175, 175) == -10
    assert angle_step(10, 5) == -5
    up = hand(1.0)                        # hand pointing up: wrist below knuckle
    up[0], up[9] = P(0.5, 0.5), P(0.5, 0.3)
    cw = hand(1.0)                        # user twists clockwise: knuckle drifts
    cw[0], cw[9] = P(0.5, 0.5), P(0.4, 0.3)  # image-left (unmirrored camera)
    assert angle_step(hand_angle(up), hand_angle(cw)) < 0  # negative = volume up

    # self-check: fast sweep crosses SWIPE_DIST inside the window, slow drift doesn't
    fast = deque()
    assert any(abs(update_trail(fast, t / 30, 0.2 + t / 30 * 2.5, 0.5)[0]) > SWIPE_DIST
               for t in range(14))           # 2.5 frame-widths/sec
    slow = deque()
    assert not any(abs(update_trail(slow, t / 30, 0.2 + t / 30 * 0.3, 0.5)[0]) > SWIPE_DIST
                   for t in range(60))       # repositioning-speed drift stays under
    assert len(slow) <= SWIPE_TIME * 30 + 1  # old samples really get pruned

    # self-check: hold fires once per sustained stretch; pre-done doesn't fire
    T = COVER_TIME
    s, d, f = hold_step(None, False, True, 10.0, T)  # cover begins
    assert not f
    s, d, f = hold_step(s, d, True, 10.5, T)         # too brief yet
    assert not f
    s, d, f = hold_step(s, d, True, 11.0, T)
    assert f and d                                   # fires, exactly once
    s, d, f = hold_step(s, d, True, 12.0, T)
    assert not f                                     # holding doesn't refire
    s, d, f = hold_step(s, d, False, 12.5, T)
    assert (s, d) == (None, False)                   # release re-arms
    s, d, f = hold_step(None, True, True, 0.0, T)    # launched in a dark room
    s, d, f = hold_step(s, d, True, 9.0, T)
    assert not f                                     # must see light first

    # self-check: fist = all four tips curled; pinch grips must not read as one
    def pose(*curled):
        lm = [P(0, 0)] * 21
        for tip in (8, 12, 16, 20):
            lm[tip - 3] = P(0.4, 0)                       # knuckle
            lm[tip] = P(0.2, 0) if tip in curled else P(0.9, 0)
        return lm
    assert is_fist(pose(8, 12, 16, 20))
    assert not is_fist(pose())              # open hand
    assert not is_fist(pose(12))            # volume-knob grip: middle curled
    assert not is_fist(pose(8))             # scroll pinch: index curled

    # self-check: rightward sweep = image x decreasing, and it reads as horizontal
    r = deque()
    update_trail(r, 0, 0.85, 0.50)
    dx, dy = update_trail(r, 0.1, 0.35, 0.45)
    assert dx < -SWIPE_DIST and abs(dx) > 2 * abs(dy)  # -> switch_window(+1)

    # self-check: the window ring is non-empty and stable across two scans
    ring = app_windows()
    assert ring and ring == app_windows() == sorted(ring)
    main()
