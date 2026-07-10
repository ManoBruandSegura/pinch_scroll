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

Point with your index finger (other fingers curled) to drive the mouse
pointer — the central area of the frame maps to the whole screen. Extend your
thumb away from the fist and fold it back in to left-click.

Sweep a hand quickly sideways to switch app windows. Open apps form a fixed
ring (stable for as long as they stay open — no Alt+Tab recency reshuffling):
sweep right = next app, sweep left = previous app, instantly. Sweep detection
is motion-based (frame differencing), not landmark-based: the tracker loses a
fast hand to motion blur, but blur only makes the motion signal stronger.

Gestures only work while your face is in view of the camera (with a couple of
seconds of grace), so a stray hand with nobody at the desk moves nothing.

Runs headless with a tray icon: green = armed, gray = standby/paused, red
corner dot = mic muted. The menu pauses gestures (double-click the icon does
too), toggles the air mouse, registers the app to start with Windows, and
quits. Launching it again also stops the running instance (high beep =
started, low beep = stopped).
"""
import ctypes
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
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
FACE_GRACE = 2.0   # s after the last face sighting that gestures stay armed
FACE_EVERY = 0.3   # s between face checks (a face doesn't move fast)
FIST_TIME = 0.4    # s a closed fist must be held to toggle play/pause
AIR_MARGIN = 0.25       # frame-edge fraction outside the air-mouse box, sideways
AIR_BOX_Y = (0.25, 0.6)  # fingertip y range spanning screen top..bottom: the hand
                         # hangs below the fingertip, so pointing lower than ~0.6
                         # pushes the hand out of frame and tracking drops it
AIR_SMOOTH = 0.35  # 0..1 EMA for the cursor; lower = steadier but laggier
POINT_GRACE = 0.4  # s the pointer pose survives flickered reads: the tracker
                   # regularly drops the camera-aimed index for a frame or two
# Click trigger = thumb tip vs thumb joint, each as distance from the pinky
# knuckle: below THUMB_ON = thumb folded onto the fist (fire), above
# THUMB_OFF = extended (re-arm). Ratio of the hand's own proportions, so no
# hand-size or morphology constant is involved.
THUMB_ON = 1.05   # measured on the actual hand: folded reads ~0.96
THUMB_OFF = 1.15  # extended reads ~1.2+
# A "hand" popping up inside the face box from nowhere is the face misread as
# a hand. A real hand keeps its trust by tracking in from outside: samples no
# older than HAND_TRAIL s and no farther apart than HAND_JUMP frame-fractions.
HAND_TRAIL = 0.5
HAND_JUMP = 0.3

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
# ponytail: primary monitor only; switch to virtual-screen metrics if multi-monitor matters
SCREEN = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def scroll(amount):
    user32.mouse_event(0x0800, 0, 0, int(amount), 0)  # MOUSEEVENTF_WHEEL


def click():
    user32.mouse_event(2, 0, 0, 0, 0)  # LEFTDOWN
    user32.mouse_event(4, 0, 0, 0, 0)  # LEFTUP


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


_mic = None


def mic_vol():
    """The default microphone's volume endpoint (cached: letting the COM
    pointer get garbage-collected trips an access violation in comtypes)."""
    global _mic
    if _mic is None:
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        _mic = ctypes.cast(AudioUtilities.GetMicrophone().Activate(
            IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
            ctypes.POINTER(IAudioEndpointVolume))
    return _mic


def toggle_mic():
    """Flip the default microphone's mute; return True if now muted."""
    vol = mic_vol()
    muted = not vol.GetMute()
    vol.SetMute(muted, None)
    return muted


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_on():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "PinchScroll")
        return True
    except OSError:
        return False


def startup_toggle():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_ALL_ACCESS) as k:
        if startup_on():
            winreg.DeleteValue(k, "PinchScroll")
        elif getattr(sys, "frozen", False):  # the PyInstaller build
            winreg.SetValueEx(k, "PinchScroll", 0, winreg.REG_SZ,
                              f'"{sys.executable}"')
        else:  # script run: pythonw = no console window at login
            pyw = sys.executable.replace("python.exe", "pythonw.exe")
            winreg.SetValueEx(k, "PinchScroll", 0, winreg.REG_SZ,
                              f'"{pyw}" "{os.path.abspath(__file__)}"')


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


def is_point(lm):
    """Index extended, the other three fingertips curled: the air-mouse pose.
    3D distances: pointing at the screen aims the index at the camera, which
    foreshortens it to nothing in 2D — only depth still sees it extended."""
    def d(i):
        return math.hypot(lm[i].x - lm[0].x, lm[i].y - lm[0].y, lm[i].z - lm[0].z)
    return d(8) > d(5) and all(d(tip) < d(tip - 3) for tip in (12, 16, 20))


def thumb_out(lm):
    """Thumb extension, self-normalized: tip (4) vs its own IP joint (3),
    each measured from the pinky knuckle (17). Extended = the tip reaches
    beyond the joint (> 1); folded across the fist = it dives past the
    joint toward the pinky (< 1). 3D, so the ratio survives the hand
    rotating when pointing at the screen edges."""
    def d(i):
        return math.hypot(lm[i].x - lm[17].x, lm[i].y - lm[17].y,
                          lm[i].z - lm[17].z)
    return d(4) / max(d(3), 1e-6)


def air_map(nx, ny):
    """Fingertip (normalized, unmirrored image coords) -> screen pixel. A box
    in the upper-middle of the frame spans the whole screen, so every corner
    stays reachable while the hand stays fully in view."""
    def span(v, lo, hi):
        return min(max((v - lo) / (hi - lo), 0.0), 1.0)
    return (int(span(1 - nx, AIR_MARGIN, 1 - AIR_MARGIN) * (SCREEN[0] - 1)),
            int(span(ny, *AIR_BOX_Y) * (SCREEN[1] - 1)))  # x mirrored: user-right = image -x


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
                         capture_output=True, text=True).stdout.lower()
    # script runs show as python*.exe, the PyInstaller build as its own name
    me = os.path.basename(sys.executable).lower()
    if "python" not in out and me not in out:
        return False  # stale pidfile from a crashed/killed run
    os.kill(pid, signal.SIGTERM)
    os.remove(PIDFILE)
    return True


def main():
    if stop_existing():
        winsound.Beep(400, 200)  # low beep: stopped
        return
    import cv2  # heavy imports (~2s) deferred past the toggle check: stopping is instant
    cam = []  # the camera takes ~1s to open: overlap it with the mediapipe import
    opener = threading.Thread(  # DSHOW: the MSMF backend takes ~16s to open here
        target=lambda: cam.append(cv2.VideoCapture(0, cv2.CAP_DSHOW)))
    opener.start()
    import mediapipe as mp
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    hands = mp.solutions.hands.Hands(max_num_hands=1,
                                     model_complexity=0,  # lighter = faster = re-locks onto fast hands sooner
                                     min_detection_confidence=0.4,  # catch blurred fast hands
                                     min_tracking_confidence=0.2)
    # Gestures only arm while a face is in view: a hand without you behind it
    # (pet, passer-by, chair back) must not drive the PC.
    face = mp.solutions.face_detection.FaceDetection(
        model_selection=0, min_detection_confidence=0.5)
    opener.join()
    cap = cam[0]
    if not cap.isOpened():
        raise SystemExit("No camera found.")

    # Tray icon: a status LED (green = armed, gray = standby/paused, red
    # corner dot = mic muted) with flag-flipping menu items the loop reads.
    ui = {"paused": False, "air": True, "quit": False, "status": "starting"}
    try:
        mic_muted = bool(mic_vol().GetMute())
    except Exception:  # no mic: the red dot just never shows
        mic_muted = False
    tray = None
    try:
        import pystray
        from PIL import Image, ImageDraw

        def led(armed, muted):
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((8, 8, 56, 56),
                      fill=(0, 200, 80) if armed else (130, 130, 130))
            if muted:
                d.ellipse((34, 34, 62, 62), fill=(220, 40, 40))
            return img

        def flip(key):
            return lambda icon, item: ui.update({key: not ui[key]})

        tray = pystray.Icon(
            "pinch_scroll", led(False, mic_muted), "Pinch Scroll",
            pystray.Menu(
                pystray.MenuItem(lambda item: f"Pinch Scroll — {ui['status']}",
                                 None, enabled=False),
                pystray.MenuItem("Pause gestures", flip("paused"),
                                 checked=lambda item: ui["paused"],
                                 default=True),  # default: double-click = pause
                pystray.MenuItem("Air mouse", flip("air"),
                                 checked=lambda item: ui["air"]),
                pystray.MenuItem("Start with Windows",
                                 lambda icon, item: startup_toggle(),
                                 checked=lambda item: startup_on()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", flip("quit"))))
        tray.run_detached()
    except ImportError:
        pass  # ponytail: no pystray = no tray; the beeps still tell the story
    tray_state = None

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
    last_face = face_check = last_point = last_hand = 0.0
    hand_x = hand_y = 0.0
    face_box = None
    cover_start = None
    mic_done = True  # pre-"fired": a launch in a dark room must not mute the mic
    fist_start = None
    fist_done = False
    air_x = air_y = None
    click_down = False
    try:
        while not ui["quit"]:
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
            rgb = None if idle else cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            if rgb is not None and now - face_check >= FACE_EVERY:
                face_check = now
                det = face.process(rgb).detections
                if det:
                    last_face = now
                    b = det[0].location_data.relative_bounding_box
                    face_box = (b.xmin, b.ymin,
                                b.xmin + b.width, b.ymin + b.height)
            armed = not ui["paused"] and now - last_face < FACE_GRACE
            state = "paused" if ui["paused"] else "armed" if armed else "standby"
            if tray and (state, mic_muted) != tray_state:
                tray_state = (state, mic_muted)
                ui["status"] = state
                tray.icon = led(armed, mic_muted)
            result = hands.process(rgb) if armed and rgb is not None else None
            ri = rm = rt = 9.9
            fist = point = veto = False
            lm = (result.multi_hand_landmarks[0].landmark
                  if result and result.multi_hand_landmarks else None)
            # The low-confidence detector loves finding "hands" in faces: veto
            # a hand inside the face box unless it earned trust by tracking in
            # continuously — a real hand slides in, a phantom just appears.
            if (lm and face_box and (face_box[0] < lm[9].x < face_box[2]
                                     and face_box[1] < lm[9].y < face_box[3])
                    and (now - last_hand > HAND_TRAIL
                         or math.hypot(lm[9].x - hand_x,
                                       lm[9].y - hand_y) > HAND_JUMP)):
                lm, veto = None, True
            if lm:
                last_hand, hand_x, hand_y = now, lm[9].x, lm[9].y
                ri, rm = pinch_ratio(lm), pinch_ratio(lm, 12)
                rt = thumb_out(lm)  # thumb clearance from the fist: the trigger
                fist = is_fist(lm)
                # A latched pinch keeps priority over the pointer pose.
                # ponytail: a vol-knob grip with ring+pinky curled reads as
                # point+click — keep them extended for the knob, or disentangle later
                if mode is None and is_point(lm):
                    last_point = now
                # A real fist ends the pose at once (it's the play/pause
                # gesture, and its low thumb must not fire the trigger).
                point = (ui["air"] and mode is None and not fist
                         and now - last_point < POINT_GRACE)
                if fist or point:  # fingers pass near the thumb while a fist
                    mode, streak = None, 0  # closes: never read that as a pinch
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
            # The cover hides your face, but COVER_TIME < FACE_GRACE: the fire
            # lands inside the grace window from the face seen just before.
            # ponytail: a genuinely dark room reads as covered — add a "was
            # recently bright" check if that ever bites
            cover_start, mic_done, fire = hold_step(cover_start, mic_done,
                                                    armed and lum < COVER_DARK,
                                                    now, COVER_TIME)
            if fire:
                mic_muted = toggle_mic()
                winsound.Beep(250 if mic_muted else 900, 180)
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
                    if (armed and not point  # a pointing hand is mousing, not sweeping
                            and abs(dx) > SWIPE_DIST and abs(dx) > 2 * abs(dy)  # horizontal
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
            if point:
                last_motion = now  # a hovering pointer barely moves pixels
                if air_x is None:  # entering the pose: a thumb that starts
                    air_x, air_y = lm[8].x, lm[8].y  # down must not fire
                    click_down = rt < THUMB_OFF
                else:
                    air_x += AIR_SMOOTH * (lm[8].x - air_x)  # EMA against jitter
                    air_y += AIR_SMOOTH * (lm[8].y - air_y)
                user32.SetCursorPos(*air_map(air_x, air_y))
                # Click = folding the extended thumb back onto the fist.
                # Both thumb states are unoccluded — unlike the old
                # thumb-to-curled-middle pinch, whose hidden fingertip the
                # tracker kept guessing wrong.
                down = rt < (THUMB_OFF if click_down else THUMB_ON)
                if down and not click_down:
                    click()
                click_down = down
            else:
                air_x = None
                click_down = False
            if debug:
                if lm:
                    mp.solutions.drawing_utils.draw_landmarks(
                        frame, result.multi_hand_landmarks[0],
                        mp.solutions.hands.HAND_CONNECTIONS)
                if face_box:
                    fh, fw = frame.shape[:2]
                    cv2.rectangle(frame,
                                  (int(face_box[0] * fw), int(face_box[1] * fh)),
                                  (int(face_box[2] * fw), int(face_box[3] * fh)),
                                  (255, 200, 0), 1)
                cv2.putText(frame,
                            f"hand {'X' if veto else 'Y' if lm else '-'}"
                            f"  ri {ri:.2f}  rm {rm:.2f}  rt {rt:.2f}",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.putText(frame,
                            f"mot {frac:.2f}  dx {dx:+.2f}  lum {lum:.0f}"
                            f"  idle {int(idle)}  armed {int(armed)}",
                            (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.putText(frame,
                            f"mode {mode or '-'}  fist {int(fist)}"
                            f"  pt {int(point)}{'*' * click_down}",
                            (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.imshow("pinch_scroll debug", frame)
                cv2.waitKey(1)
    except KeyboardInterrupt:
        pass
    finally:
        if tray:
            tray.stop()
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
        def __init__(self, x, y, z=0.0): self.x, self.y, self.z = x, y, z
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

    # self-check: covering the lens hides the face, so the mute can only fire
    # off the grace window — the knobs must keep that window wide enough
    assert COVER_TIME < FACE_GRACE
    assert FACE_EVERY < FACE_GRACE  # a check gap must not outlive the grace

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

    # self-check: air-mouse pose = index out, others curled; fist/open are not
    assert is_point(pose(12, 16, 20))
    assert not is_point(pose(8, 12, 16, 20))  # fist
    assert not is_point(pose())               # open hand
    assert not is_point(pose(8))              # scroll pinch
    cam = pose(12, 16, 20)                    # index aimed at the camera:
    cam[8] = P(0.4, 0, -0.5)                  # flat in 2D, extended in depth
    assert is_point(cam)

    # self-check: trigger thumb — folded onto the fist reads pulled, extended reads armed
    gun = pose(12, 16, 20)
    gun[3] = P(0.2, -0.15)                       # thumb IP joint, out to the side
    gun[4] = P(0.35, 0.1)                        # tip folded in past the joint
    assert thumb_out(gun) < THUMB_ON
    gun[4] = P(0.1, -0.3)                        # tip extended beyond the joint
    assert thumb_out(gun) > THUMB_OFF

    # self-check: air map mirrors x and pins the margin box to the screen edges
    assert air_map(1.0, 0.0) == (0, 0)
    assert air_map(0.0, 1.0) == (SCREEN[0] - 1, SCREEN[1] - 1)
    assert air_map(0.5, AIR_BOX_Y[0])[1] == 0
    assert air_map(0.5, AIR_BOX_Y[1])[1] == SCREEN[1] - 1
    assert air_map(0.3, 0.5)[0] > air_map(0.7, 0.5)[0]  # user-right -> screen-right

    # self-check: rightward sweep = image x decreasing, and it reads as horizontal
    r = deque()
    update_trail(r, 0, 0.85, 0.50)
    dx, dy = update_trail(r, 0.1, 0.35, 0.45)
    assert dx < -SWIPE_DIST and abs(dx) > 2 * abs(dy)  # -> switch_window(+1)

    # self-check: the window ring is non-empty and stable across two scans
    ring = app_windows()
    assert ring and ring == app_windows() == sorted(ring)
    main()
