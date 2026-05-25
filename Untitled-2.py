
import evdev
from evdev import ecodes
for p in evdev.list_devices():
    d = evdev.InputDevice(p)
    caps = d.capabilities()
    abs_map = {c: i for c, i in caps.get(ecodes.EV_ABS, [])}
    if ecodes.ABS_TILT_X in abs_map:
        tx = abs_map[ecodes.ABS_TILT_X]
        ty = abs_map.get(ecodes.ABS_TILT_Y)
        print(f"{p}  {d.name!r}")
        print(f"  ABS_TILT_X: min={tx.min} max={tx.max} res={tx.resolution}")
        if ty is not None:
            print(f"  ABS_TILT_Y: min={ty.min} max={ty.max} res={ty.resolution}")
    d.close()