# stylus_bridge

Stream pen/stylus input from a Linux host (evdev) to a Windows host as a
synthetic pen pointer, over UDP on a trusted LAN.

```
[Ubuntu laptop]  evdev  -->  UDP  -->  [Windows PC]  InjectSyntheticPointerInput
   client_ubuntu.py                       server_windows.py
```

Pressure, tilt (X/Y), hover distance, barrel button, and in-range/in-contact
state are preserved end-to-end. Apps that read `WM_POINTER` (Krita, Photoshop,
Clip Studio, OneNote, ...) receive true pen input with pressure.

## Requirements

| Host | OS | Python | Deps |
|---|---|---|---|
| Client | Linux (X11 or Wayland) | >= 3.10 | `evdev` (see `requirements.txt`) |
| Server | Windows 10 1809+ / Windows 11 | >= 3.10 | stdlib only |

The client user must belong to the `input` group:
```bash
sudo usermod -aG input "$USER"   # log out and back in
```

## Install

Client (Ubuntu):
```bash
git clone https://github.com/DarthDazzle/stylus_bridge.git
cd stylus_bridge
pip install -r requirements.txt
```

Server (Windows): clone the repo; no `pip install` needed.

## Run

1. **Server first** (so it is listening for discovery):
   ```
   python server_windows.py                # interactive monitor pick
   python server_windows.py --monitor 0    # non-interactive
   ```
2. **Client**:
   ```
   python3 client_ubuntu.py                            # auto device + discovery
   python3 client_ubuntu.py --server 192.168.1.50:41235   # skip discovery
   python3 client_ubuntu.py --device /dev/input/event8    # explicit device
   python3 client_ubuntu.py --grab                     # exclusive evdev capture
   ```

Default UDP ports: discovery `41234`, data `41235`. Both must be reachable
between hosts (open in Windows Firewall for the Python process).

## Coordinate mapping

- Tablet `ABS_X/Y` range -> normalized `[0, 1]` on the client.
- Server computes a **FIT** box inside the selected monitor (preserve tablet
  aspect ratio, letterbox the shorter axis).
- Pixel coordinates are in the **virtual desktop** space, so multi-monitor
  layouts with negative origins work correctly.

## Wire format

Discovery (Ubuntu -> 255.255.255.255:41234):
```
b"STYLUS_DISC_V1" + <f tablet_aspect>          (18 B)
```
Discovery reply (Windows -> client unicast):
```
b"STYLUS_OFFER_V1" + <H data_port>             (17 B)
```
Data packet (little-endian, packed, 40 B):

| Offset | Type | Field |
|---|---|---|
| 0  | u32 | seq |
| 4  | u64 | t_ns (CLOCK_MONOTONIC) |
| 12 | f32 | x_norm `[0, 1]` |
| 16 | f32 | y_norm `[0, 1]` |
| 20 | f32 | pressure `[0, 1]` |
| 24 | f32 | tilt_x (deg) |
| 28 | f32 | tilt_y (deg) |
| 32 | f32 | distance `[0, 1]` (hover; 0 if unsupported) |
| 36 | u16 | buttons (bit0 TOUCH, bit1 STYLUS, bit2 STYLUS2) |
| 38 | u8  | tool (0 none, 1 pen, 2 rubber [reserved]) |
| 39 | u8  | flags (bit0 IN_RANGE, bit1 IN_CONTACT) |

## Known limitations / deferred

- Eraser (`BTN_TOOL_RUBBER`) is parsed but not injected (tool code 2 reserved).
- No tablet rotation; mount in native orientation.
- Single client, single pen. Multi-client is not handled.
- No authentication or encryption -- trusted LAN only.
- UDP only; packets out of order or lost are dropped silently.
