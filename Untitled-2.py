python3 - <<'PY'
import evdev
from evdev import ecodes
for p in evdev.list_devices():
    d = evdev.InputDevice(p)
    caps = d.capabilities(verbose=False)
    abs_codes = [c for c, _ in caps.get(ecodes.EV_ABS, [])]
    key_codes = caps.get(ecodes.EV_KEY, [])
    tool_keys = [k for k in key_codes if 0x140 <= k <= 0x14f]
    def name(table, code):
        v = table.get(code)
        return v if isinstance(v, str) else (v[0] if v else str(code))
    print(f"{p}  {d.name!r}")
    print(f"  ABS  : {[name(ecodes.ABS, c) for c in abs_codes]}")
    print(f"  BTN_*: {[name(ecodes.KEY, k) for k in tool_keys]}")
    d.close()
PY