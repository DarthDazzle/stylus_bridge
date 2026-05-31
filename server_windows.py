#!/usr/bin/env python3
"""Windows stylus_bridge server.

Receives UDP packets from the Ubuntu client and injects them as a synthetic
pen pointer via InjectSyntheticPointerInput (Win10 1809+).
Stdlib only.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import socket
import struct
import sys
import threading

import protocol

# ---------------------------------------------------------------------------
# Win32 bindings
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
shcore = ctypes.WinDLL("shcore", use_last_error=True)

PT_PEN = 3
POINTER_FEEDBACK_DEFAULT = 1
MDT_EFFECTIVE_DPI = 0
HIMETRIC_PER_INCH = 2540  # 1 inch = 2540 HIMETRIC units (0.01 mm)

POINTER_FLAG_NONE         = 0x00000000
POINTER_FLAG_NEW          = 0x00000001
POINTER_FLAG_INRANGE      = 0x00000002
POINTER_FLAG_INCONTACT    = 0x00000004
POINTER_FLAG_FIRSTBUTTON  = 0x00000010
POINTER_FLAG_SECONDBUTTON = 0x00000020
POINTER_FLAG_THIRDBUTTON  = 0x00000040
POINTER_FLAG_DOWN         = 0x00010000
POINTER_FLAG_UPDATE       = 0x00020000
POINTER_FLAG_UP           = 0x00040000

PEN_MASK_PRESSURE = 0x00000001
PEN_MASK_TILT_X   = 0x00000004
PEN_MASK_TILT_Y   = 0x00000008

PEN_FLAG_NONE     = 0x00000000
PEN_FLAG_BARREL   = 0x00000001
PEN_FLAG_INVERTED = 0x00000002
PEN_FLAG_ERASER   = 0x00000004

# SendInput
INPUT_MOUSE    = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_LEFTDOWN   = 0x0002
MOUSEEVENTF_LEFTUP     = 0x0004
MOUSEEVENTF_RIGHTDOWN  = 0x0008
MOUSEEVENTF_RIGHTUP    = 0x0010
MOUSEEVENTF_MOVE       = 0x0001
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP   = 0x0040
MOUSEEVENTF_WHEEL      = 0x0800

VK_SHIFT = 0x10

KEYEVENTF_KEYUP = 0x0002

MONITORINFOF_PRIMARY = 0x00000001
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", ctypes.c_uint32),
        ("pointerId", ctypes.c_uint32),
        ("frameId", ctypes.c_uint32),
        ("pointerFlags", ctypes.c_uint32),
        ("sourceDevice", wt.HANDLE),
        ("hwndTarget", wt.HWND),
        ("ptPixelLocation", POINT),
        ("ptHimetricLocation", POINT),
        ("ptPixelLocationRaw", POINT),
        ("ptHimetricLocationRaw", POINT),
        ("dwTime", wt.DWORD),
        ("historyCount", ctypes.c_uint32),
        ("InputData", ctypes.c_int32),
        ("dwKeyStates", wt.DWORD),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", ctypes.c_int),
    ]


class POINTER_PEN_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("penFlags", ctypes.c_uint32),
        ("penMask", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32),
        ("tiltX", ctypes.c_int32),
        ("tiltY", ctypes.c_int32),
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("touchFlags", ctypes.c_uint32),
        ("touchMask", ctypes.c_uint32),
        ("rcContact", RECT),
        ("rcContactRaw", RECT),
        ("orientation", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
    ]


class _PTI_UNION(ctypes.Union):
    _fields_ = [("touchInfo", POINTER_TOUCH_INFO),
                ("penInfo", POINTER_PEN_INFO)]


class POINTER_TYPE_INFO(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _PTI_UNION)]


user32.CreateSyntheticPointerDevice.argtypes = [
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
user32.CreateSyntheticPointerDevice.restype = wt.HANDLE

user32.InjectSyntheticPointerInput.argtypes = [
    wt.HANDLE, ctypes.POINTER(POINTER_TYPE_INFO), ctypes.c_uint32]
user32.InjectSyntheticPointerInput.restype = wt.BOOL

user32.DestroySyntheticPointerDevice.argtypes = [wt.HANDLE]
user32.DestroySyntheticPointerDevice.restype = None


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", wt.WPARAM),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", wt.WPARAM),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUT_UNION)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT


shcore.GetDpiForMonitor.argtypes = [
    wt.HMONITOR, ctypes.c_int,
    ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
shcore.GetDpiForMonitor.restype = ctypes.c_long  # HRESULT

try:
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetProcessDpiAwarenessContext.restype = wt.BOOL
    user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
except (AttributeError, OSError):
    pass


# ---------------------------------------------------------------------------
# Monitor enumeration
# ---------------------------------------------------------------------------
class MONITORINFOEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


MonitorEnumProc = ctypes.WINFUNCTYPE(
    wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(RECT), wt.LPARAM)


def enumerate_monitors():
    monitors = []

    def cb(hmon, hdc, lprect, lparam):
        info = MONITORINFOEX()
        info.cbSize = ctypes.sizeof(MONITORINFOEX)
        user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        r = info.rcMonitor
        monitors.append({
            "hmon": int(hmon) if hmon else 0,
            "device": info.szDevice,
            "rect": (r.left, r.top, r.right, r.bottom),
            "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
        })
        return True

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    return monitors


def compute_fit_box(tablet_aspect, mon_rect):
    """FIT: preserve tablet aspect, letterbox inside monitor.

    Returns (left, top, width, height) in virtual-desktop pixel coords.
    """
    ml, mt, mr, mb = mon_rect
    mw = mr - ml
    mh = mb - mt
    mon_aspect = mw / mh
    if tablet_aspect > mon_aspect:
        box_w = mw
        box_h = mw / tablet_aspect
        box_l = ml
        box_t = mt + (mh - box_h) / 2
    else:
        box_h = mh
        box_w = mh * tablet_aspect
        box_l = ml + (mw - box_w) / 2
        box_t = mt
    return (box_l, box_t, box_w, box_h)


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------
def make_pen_info(pointer_id, x_px, y_px, x_him, y_him, pressure_1024,
                  tilt_x_deg, tilt_y_deg, flags, pen_flags):
    pi = POINTER_INFO()
    pi.pointerType = PT_PEN
    pi.pointerId = pointer_id
    pi.pointerFlags = flags
    pi.ptPixelLocation = POINT(int(round(x_px)), int(round(y_px)))
    pi.ptHimetricLocation = POINT(int(round(x_him)), int(round(y_him)))
    pi.ptPixelLocationRaw = pi.ptPixelLocation
    pi.ptHimetricLocationRaw = pi.ptHimetricLocation

    pen = POINTER_PEN_INFO()
    pen.pointerInfo = pi
    pen.penFlags = pen_flags
    pen.penMask = PEN_MASK_PRESSURE | PEN_MASK_TILT_X | PEN_MASK_TILT_Y
    pen.pressure = max(0, min(1024, int(pressure_1024)))
    pen.tiltX = max(-90, min(90, int(tilt_x_deg)))
    pen.tiltY = max(-90, min(90, int(tilt_y_deg)))

    info = POINTER_TYPE_INFO()
    info.type = PT_PEN
    info.penInfo = pen
    return info


def derive_flags(in_range, in_contact, was_in_range, was_in_contact,
                 barrel, barrel_uses_pen_flag):
    flags = 0
    pen_flags = 0
    if in_range:
        flags |= POINTER_FLAG_INRANGE
    if in_contact:
        flags |= POINTER_FLAG_INCONTACT | POINTER_FLAG_FIRSTBUTTON
    if barrel and barrel_uses_pen_flag:
        flags |= POINTER_FLAG_SECONDBUTTON
        pen_flags |= PEN_FLAG_BARREL

    # Edge detection
    if in_contact and not was_in_contact:
        flags |= POINTER_FLAG_DOWN
    elif not in_contact and was_in_contact:
        flags |= POINTER_FLAG_UP
    else:
        flags |= POINTER_FLAG_UPDATE
    return flags, pen_flags


# ---------------------------------------------------------------------------
# Barrel-button action layer
# ---------------------------------------------------------------------------
def _send_inputs(*inputs):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        err = ctypes.get_last_error()
        print(f"SendInput sent {sent}/{n} (GetLastError={err})", file=sys.stderr)


def _mouse_input(flag):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dwFlags = flag
    return inp


def _wheel_input(delta):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dwFlags = MOUSEEVENTF_WHEEL
    # mouseData is DWORD but wheel delta is signed; wrap negative values
    inp.mi.mouseData = ctypes.c_uint32(int(delta)).value
    return inp


def _move_input(dx, dy):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dwFlags = MOUSEEVENTF_MOVE
    inp.mi.dx = int(dx)
    inp.mi.dy = int(dy)
    return inp


def _key_input(vk, up=False):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    return inp


class BarrelAction:
    uses_pen_barrel_flag = False
    label = "none"
    def press(self):  pass
    def release(self): pass


class NoneAction(BarrelAction):
    label = "none"


class PenBarrelAction(BarrelAction):
    uses_pen_barrel_flag = True
    label = "barrel (POINTER_FLAG_SECONDBUTTON + PEN_FLAG_BARREL)"


class MouseAction(BarrelAction):
    def __init__(self, name, down_flag, up_flag):
        self.label = f"mouse-{name}"
        self._down = down_flag
        self._up = up_flag
    def press(self):   _send_inputs(_mouse_input(self._down))
    def release(self): _send_inputs(_mouse_input(self._up))


class KeyAction(BarrelAction):
    def __init__(self, vk, name):
        self.label = f"key {name} (VK=0x{vk:02X})"
        self._vk = vk
    def press(self):   _send_inputs(_key_input(self._vk, up=False))
    def release(self): _send_inputs(_key_input(self._vk, up=True))


_VK_TABLE = {
    "SPACE": 0x20, "TAB": 0x09, "ENTER": 0x0D, "ESC": 0x1B, "ESCAPE": 0x1B,
    "BACKSPACE": 0x08, "DELETE": 0x2E, "INSERT": 0x2D,
    "SHIFT": 0xA0, "LSHIFT": 0xA0, "RSHIFT": 0xA1,
    "CTRL": 0xA2, "LCTRL": 0xA2, "RCTRL": 0xA3,
    "ALT": 0xA4, "LALT": 0xA4, "RALT": 0xA5,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
}
for _c in range(ord("A"), ord("Z") + 1):
    _VK_TABLE[chr(_c)] = _c
for _c in range(ord("0"), ord("9") + 1):
    _VK_TABLE[chr(_c)] = _c
for _i in range(1, 13):
    _VK_TABLE[f"F{_i}"] = 0x6F + _i  # VK_F1 = 0x70


def parse_barrel_action(spec):
    s = spec.strip().lower()
    if s == "barrel":
        return PenBarrelAction()
    if s == "none":
        return NoneAction()
    if s == "left":
        return MouseAction("left", MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
    if s == "middle":
        return MouseAction("middle", MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)
    if s == "right":
        return MouseAction("right", MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)
    if s.startswith("key:"):
        name = spec.split(":", 1)[1].strip()
        upper = name.upper()
        if upper in _VK_TABLE:
            return KeyAction(_VK_TABLE[upper], upper)
        if name.lower().startswith("0x"):
            return KeyAction(int(name, 16), name)
        raise argparse.ArgumentTypeError(
            f"unknown key name {name!r}; use letter/digit, "
            "SPACE/TAB/ENTER/ESC/SHIFT/CTRL/ALT/F1-F12/arrows, or 0xNN")
    raise argparse.ArgumentTypeError(
        f"unknown barrel action {spec!r}; expected one of: "
        "barrel, none, left, middle, right, key:NAME")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
def pick_monitor(monitors, preselected):
    print("Detected monitors:")
    for i, m in enumerate(monitors):
        l, t, r, b = m["rect"]
        prim = "  (primary)" if m["primary"] else ""
        print(f"  [{i}] {m['device']}  {r - l}x{b - t} @ ({l},{t}){prim}")
    if preselected is not None:
        if preselected < 0 or preselected >= len(monitors):
            print(f"Invalid --monitor index {preselected}.", file=sys.stderr)
            sys.exit(2)
        return preselected
    while True:
        try:
            idx = int(input("Select monitor index: ").strip())
            if 0 <= idx < len(monitors):
                return idx
        except (ValueError, EOFError):
            pass
        print("Invalid selection.")


def main():
    ap = argparse.ArgumentParser(description="Stylus bridge server (Windows)")
    ap.add_argument("--monitor", type=int, default=None,
                    help="monitor index (skips prompt)")
    ap.add_argument("--barrel-action", default="middle",
                    help="action for the pen barrel button while pen tip is "
                         "the active tool: barrel | none | left | middle | "
                         "right | key:NAME  (default: middle)")
    ap.add_argument("--pinch-scroll-scale", type=float, default=2000.0,
                    help="wheel units per unit of normalized finger separation "
                         "change (default: 2000; 120 = one scroll notch)")
    ap.add_argument("--pan-scale", type=float, default=1500.0,
                    help="pixels per unit of normalized centroid delta "
                         "for 2-finger pan (default: 1500)")
    ap.add_argument("--eraser-barrel-action", default=None,
                    help="action for the barrel button while the eraser end "
                         "is the active tool. Same grammar as --barrel-action. "
                         "Defaults to whatever --barrel-action is.")
    args = ap.parse_args()

    pinch_scroll_scale = args.pinch_scroll_scale
    pan_scale = args.pan_scale
    pen_action = parse_barrel_action(args.barrel_action)
    eraser_action_spec = args.eraser_barrel_action or args.barrel_action
    eraser_action = parse_barrel_action(eraser_action_spec)
    print(f"Barrel action (pen):    {pen_action.label}", flush=True)
    print(f"Barrel action (eraser): {eraser_action.label}", flush=True)

    monitors = enumerate_monitors()
    if not monitors:
        print("No monitors detected.", file=sys.stderr)
        sys.exit(1)

    idx = pick_monitor(monitors, args.monitor)
    mon = monitors[idx]
    ml, mt, mr, mb = mon["rect"]
    print(f"Using monitor [{idx}] {mon['device']} "
          f"{mr - ml}x{mb - mt} @ ({ml},{mt})", flush=True)

    dpix = ctypes.c_uint(96)
    dpiy = ctypes.c_uint(96)
    hr = shcore.GetDpiForMonitor(mon["hmon"], MDT_EFFECTIVE_DPI,
                                  ctypes.byref(dpix), ctypes.byref(dpiy))
    if hr != 0:
        print(f"GetDpiForMonitor failed: HRESULT=0x{hr & 0xFFFFFFFF:08x}; "
              "using 96 DPI fallback.", file=sys.stderr)
        dpix.value, dpiy.value = 96, 96
    him_per_px_x = HIMETRIC_PER_INCH / dpix.value
    him_per_px_y = HIMETRIC_PER_INCH / dpiy.value
    print(f"Monitor DPI: {dpix.value} x {dpiy.value}  "
          f"(HIMETRIC/px = {him_per_px_x:.3f} x {him_per_px_y:.3f})", flush=True)

    pen_device = user32.CreateSyntheticPointerDevice(
        PT_PEN, 1, POINTER_FEEDBACK_DEFAULT)
    if not pen_device:
        err = ctypes.get_last_error()
        print(f"CreateSyntheticPointerDevice failed (GetLastError={err}). "
              "Requires Windows 10 1809 or later.", file=sys.stderr)
        sys.exit(1)

    fit_box = {"value": (ml, mt, mr - ml, mb - mt)}  # default 1:1 until discovery

    def discovery_responder():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", protocol.DISCOVERY_PORT))
        print(f"Discovery responder on UDP/{protocol.DISCOVERY_PORT}", flush=True)
        reply = protocol.OFFER_MAGIC + struct.pack("<H", protocol.DATA_PORT)
        while True:
            try:
                data, addr = s.recvfrom(64)
            except OSError as e:
                print(f"discovery socket error: {e}", file=sys.stderr)
                continue
            if not data.startswith(protocol.DISCOVERY_MAGIC):
                continue
            rest = data[len(protocol.DISCOVERY_MAGIC):]
            if len(rest) >= 4:
                aspect = struct.unpack("<f", rest[:4])[0]
                if aspect > 0 and aspect == aspect:  # finite, positive
                    fit_box["value"] = compute_fit_box(aspect, mon["rect"])
                    bl, bt, bw, bh = fit_box["value"]
                    print(f"Client {addr[0]}: tablet aspect={aspect:.6f}, "
                          f"FIT box=({bl:.0f},{bt:.0f}) {bw:.0f}x{bh:.0f}",
                          flush=True)
            s.sendto(reply, addr)

    threading.Thread(target=discovery_responder, daemon=True).start()

    data_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    data_sock.bind(("0.0.0.0", protocol.DATA_PORT))
    data_sock.settimeout(0.5)  # let Python process Ctrl+C on Windows
    print(f"Listening for stylus data on UDP/{protocol.DATA_PORT}", flush=True)

    last_seq = -1
    was_in_range = False
    was_in_contact = False
    was_touch_contact = False
    scroll_accum = 0.0
    pan_active = False
    held_action = None              # action currently in pressed state
    introduced = set()              # pointer ids that have seen POINTER_FLAG_NEW
    prev_pointer_id = None          # last pointer id we injected for

    def select_action(t):
        return eraser_action if t == int(protocol.Tool.RUBBER) else pen_action

    try:
        while True:
            try:
                data, addr = data_sock.recvfrom(64)
            except TimeoutError:
                continue
            except OSError as e:
                print(f"recv error: {e}", file=sys.stderr)
                continue
            if len(data) != protocol.PACKET_SIZE:
                continue

            (seq, _t_ns, x_n, y_n, p_n, tx, ty, _d_n,
             buttons, tool, flags_b) = protocol.unpack(data)

            # Drop out-of-order (wrap-aware: treat large backward jump as wrap)
            if last_seq >= 0:
                delta = (seq - last_seq) & 0xFFFFFFFF
                if delta == 0 or delta > 0x80000000:
                    continue
            last_seq = seq

            bl, bt, bw, bh = fit_box["value"]
            x_px = bl + max(0.0, min(1.0, x_n)) * bw
            y_px = bt + max(0.0, min(1.0, y_n)) * bh
            x_him = x_px * him_per_px_x
            y_him = y_px * him_per_px_y

            in_range = bool(flags_b & int(protocol.Flag.IN_RANGE))
            in_contact = bool(flags_b & int(protocol.Flag.IN_CONTACT))
            barrel = bool(buttons & int(protocol.Button.STYLUS))

            if tool == int(protocol.Tool.TOUCH):
                if tx != 0.0 or x_n != 0.0 or y_n != 0.0:
                    # 2-finger gesture: scroll and/or pan
                    if tx != 0.0:
                        scroll_accum += tx * pinch_scroll_scale
                        notches = int(scroll_accum / 120) * 120
                        if notches != 0:
                            _send_inputs(_wheel_input(notches))
                            scroll_accum -= notches
                    if x_n != 0.0 or y_n != 0.0:
                        if not pan_active:
                            _send_inputs(_key_input(VK_SHIFT),
                                         _mouse_input(MOUSEEVENTF_MIDDLEDOWN))
                            pan_active = True
                        _send_inputs(_move_input(x_n * pan_scale, y_n * pan_scale))
                else:
                    # Pure state packet: end pan if active, then handle middle mouse
                    if pan_active:
                        _send_inputs(_mouse_input(MOUSEEVENTF_MIDDLEUP),
                                     _key_input(VK_SHIFT, up=True))
                        pan_active = False
                    if in_contact and not was_touch_contact:
                        _send_inputs(_mouse_input(MOUSEEVENTF_MIDDLEDOWN))
                    elif not in_contact and was_touch_contact:
                        _send_inputs(_mouse_input(MOUSEEVENTF_MIDDLEUP))
                    was_touch_contact = in_contact
                continue

            current_action = select_action(tool)

            # Barrel state machine: dispatch press/release on (barrel, action)
            # changes. Handles tool switch while barrel is held by releasing
            # the old action and pressing the new one.
            desired = current_action if barrel else None
            if desired is not held_action:
                if held_action is not None:
                    held_action.release()
                if desired is not None:
                    desired.press()
                held_action = desired

            if not in_range and not was_in_range:
                # Stylus left proximity earlier; nothing to inject
                continue

            pointer_id = 2 if tool == int(protocol.Tool.RUBBER) else 1

            # Tool switch mid-session: emit final out-of-range frame for the
            # previous pointer id so Windows closes its in-range session
            # cleanly before the new id appears.
            if (prev_pointer_id is not None
                    and prev_pointer_id != pointer_id
                    and prev_pointer_id in introduced):
                final = make_pen_info(
                    prev_pointer_id, x_px, y_px, x_him, y_him,
                    0, 0, 0,
                    POINTER_FLAG_UPDATE,  # no INRANGE -> leaves range
                    PEN_FLAG_NONE)
                user32.InjectSyntheticPointerInput(
                    pen_device, ctypes.byref(final), 1)
                introduced.discard(prev_pointer_id)
                was_in_range = False
                was_in_contact = False

            pressure = int(round(max(0.0, min(1.0, p_n)) * 1024))
            flags, pen_flags = derive_flags(
                in_range, in_contact, was_in_range, was_in_contact,
                barrel, current_action.uses_pen_barrel_flag)
            if tool == int(protocol.Tool.RUBBER):
                pen_flags |= PEN_FLAG_INVERTED | PEN_FLAG_ERASER

            if pointer_id not in introduced:
                flags |= POINTER_FLAG_NEW
                introduced.add(pointer_id)

            info = make_pen_info(pointer_id, x_px, y_px, x_him, y_him,
                                 pressure, tx, ty, flags, pen_flags)
            ok = user32.InjectSyntheticPointerInput(
                pen_device, ctypes.byref(info), 1)
            if not ok:
                err = ctypes.get_last_error()
                # Avoid flooding stderr: only log on first failure per frame
                print(f"InjectSyntheticPointerInput failed: {err}",
                      file=sys.stderr)

            prev_pointer_id = pointer_id
            was_in_range = in_range
            was_in_contact = in_contact
    except KeyboardInterrupt:
        pass
    finally:
        if held_action is not None:
            held_action.release()
        if pan_active:
            _send_inputs(_mouse_input(MOUSEEVENTF_MIDDLEUP),
                         _key_input(VK_SHIFT, up=True))
        elif was_touch_contact:
            _send_inputs(_mouse_input(MOUSEEVENTF_MIDDLEUP))
        user32.DestroySyntheticPointerDevice(pen_device)


if __name__ == "__main__":
    main()
