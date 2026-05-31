#!/usr/bin/env python3
"""Ubuntu stylus capture -> UDP send.

Requires: python-evdev (`pip install evdev`), user in `input` group.
"""
import argparse
import asyncio
import math
import socket
import struct
import sys
import time

import evdev
from evdev import ecodes

import protocol


def find_tablet_device():
    """First device with ABS_X, ABS_Y, ABS_PRESSURE and BTN_TOOL_PEN."""
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        caps = dev.capabilities()
        abs_codes = {c for c, _ in caps.get(ecodes.EV_ABS, [])}
        key_codes = set(caps.get(ecodes.EV_KEY, []))
        if (ecodes.ABS_X in abs_codes
                and ecodes.ABS_Y in abs_codes
                and ecodes.ABS_PRESSURE in abs_codes
                and ecodes.BTN_TOOL_PEN in key_codes):
            return dev
        dev.close()
    return None


def find_touch_device():
    """First device with ABS_MT_POSITION_X/Y and BTN_TOUCH but not BTN_TOOL_PEN."""
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        caps = dev.capabilities()
        abs_codes = {c for c, _ in caps.get(ecodes.EV_ABS, [])}
        key_codes = set(caps.get(ecodes.EV_KEY, []))
        if (ecodes.ABS_MT_POSITION_X in abs_codes
                and ecodes.ABS_MT_POSITION_Y in abs_codes
                and ecodes.BTN_TOUCH in key_codes
                and ecodes.BTN_TOOL_PEN not in key_codes):
            return dev
        dev.close()
    return None


def get_abs(dev, code):
    for c, info in dev.capabilities().get(ecodes.EV_ABS, []):
        if c == code:
            return info
    return None


def physical_mm(axinfo):
    """Returns axis physical extent in mm, or None if resolution unavailable.

    evdev convention: ABS_X / ABS_Y resolution is units/mm.
    """
    if axinfo is None:
        return None
    res = getattr(axinfo, "resolution", 0) or 0
    if res > 0:
        return (axinfo.max - axinfo.min) / res
    return None


def tilt_deg_per_unit(axinfo):
    """Convert raw evdev tilt-axis units to degrees.

    evdev convention: ABS_TILT_{X,Y} resolution is units/radian.
    Fall back to assuming axis max maps to 90 deg if resolution unset.
    """
    if axinfo is None:
        return 0.0
    if getattr(axinfo, "resolution", 0) and axinfo.resolution > 0:
        return 180.0 / (math.pi * axinfo.resolution)
    if axinfo.max > 0:
        return 90.0 / axinfo.max
    return 1.0


def discover_server(tablet_aspect, timeout_total=10.0, retry_interval=0.5):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(retry_interval)
    hello = protocol.DISCOVERY_MAGIC + struct.pack("<f", float(tablet_aspect))
    deadline = time.monotonic() + timeout_total
    try:
        while time.monotonic() < deadline:
            try:
                sock.sendto(hello, ("255.255.255.255", protocol.DISCOVERY_PORT))
                data, addr = sock.recvfrom(64)
                if data.startswith(protocol.OFFER_MAGIC):
                    rest = data[len(protocol.OFFER_MAGIC):]
                    if len(rest) >= 2:
                        port = struct.unpack("<H", rest[:2])[0]
                        return (addr[0], port)
            except socket.timeout:
                continue
        raise TimeoutError("no server responded to discovery within timeout")
    finally:
        sock.close()


async def capture_touch_and_send(dev, server_addr, grab,
                                 mt_x_min, mt_x_range, mt_y_min, mt_y_range):
    if grab:
        dev.grab()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x10)

    seq = 0
    current_slot = 0
    slots = {}          # slot_id -> {"tid": int, "x": int, "y": int}
    middle_held = False
    prev_sep = None
    prev_cx = None
    prev_cy = None
    prev_n = 0

    def get_slot(sid):
        if sid not in slots:
            slots[sid] = {"tid": -1, "x": 0, "y": 0}
        return slots[sid]

    def send_packet(in_contact, pinch_delta=0.0, cx_delta=0.0, cy_delta=0.0):
        nonlocal seq
        buf = protocol.pack(
            seq, time.monotonic_ns(),
            cx_delta, cy_delta, float(in_contact),
            pinch_delta, 0.0, 0.0,
            int(protocol.Button.TOUCH) if in_contact else 0,
            int(protocol.Tool.TOUCH),
            int(protocol.Flag.IN_CONTACT) if in_contact else 0,
        )
        try:
            sock.sendto(buf, server_addr)
        except OSError as e:
            print(f"touch send error: {e}", file=sys.stderr)
        seq = (seq + 1) & 0xFFFFFFFF

    async for ev in dev.async_read_loop():
        et, ec, val = ev.type, ev.code, ev.value
        if et == ecodes.EV_ABS:
            if ec == ecodes.ABS_MT_SLOT:
                current_slot = val
            elif ec == ecodes.ABS_MT_TRACKING_ID:
                get_slot(current_slot)["tid"] = val
            elif ec == ecodes.ABS_MT_POSITION_X:
                get_slot(current_slot)["x"] = val
            elif ec == ecodes.ABS_MT_POSITION_Y:
                get_slot(current_slot)["y"] = val
        elif et == ecodes.EV_SYN and ec == ecodes.SYN_REPORT:
            n = sum(1 for s in slots.values() if s["tid"] >= 0)

            # Leaving 2-finger: signal server to end pan before any other packet
            if n != 2 and prev_n == 2:
                send_packet(in_contact=False)

            if n != 1 and middle_held:
                send_packet(in_contact=False)
                middle_held = False

            if n == 1 and not middle_held:
                send_packet(in_contact=True)
                middle_held = True

            if n == 2:
                active = [s for s in slots.values() if s["tid"] >= 0]
                a, b = active[0], active[1]
                cx = ((a["x"] + b["x"]) / 2 - mt_x_min) / mt_x_range
                cy = ((a["y"] + b["y"]) / 2 - mt_y_min) / mt_y_range
                dx = (a["x"] - b["x"]) / mt_x_range
                dy = (a["y"] - b["y"]) / mt_y_range
                cur_sep = math.sqrt(dx * dx + dy * dy)

                sep_delta = (cur_sep - prev_sep) if prev_sep is not None else 0.0
                cx_delta = (cx - prev_cx) if prev_cx is not None else 0.0
                cy_delta = (cy - prev_cy) if prev_cy is not None else 0.0

                if sep_delta != 0.0 or cx_delta != 0.0 or cy_delta != 0.0:
                    send_packet(in_contact=False,
                                pinch_delta=sep_delta,
                                cx_delta=cx_delta, cy_delta=cy_delta)

                prev_sep = cur_sep
                prev_cx = cx
                prev_cy = cy
            else:
                prev_sep = None
                prev_cx = None
                prev_cy = None

            prev_n = n


async def capture_and_send(dev, server_addr,
                           x_min, x_max, y_min, y_max,
                           p_max, dist_max,
                           tilt_x_scale, tilt_y_scale, grab):
    if grab:
        dev.grab()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x10)  # IPTOS_LOWDELAY

    x_range = max(1, x_max - x_min)
    y_range = max(1, y_max - y_min)
    p_range = max(1, p_max)
    d_range = max(1, dist_max) if dist_max > 0 else 1

    seq = 0
    st = {
        "x": x_min, "y": y_min, "p": 0, "tx": 0, "ty": 0, "dist": 0,
        "buttons": 0, "tool": int(protocol.Tool.NONE), "flags": 0,
    }

    print(f"Streaming to {server_addr[0]}:{server_addr[1]}", flush=True)

    async for ev in dev.async_read_loop():
        et, ec, val = ev.type, ev.code, ev.value
        if et == ecodes.EV_ABS:
            if ec == ecodes.ABS_X:
                st["x"] = val
            elif ec == ecodes.ABS_Y:
                st["y"] = val
            elif ec == ecodes.ABS_PRESSURE:
                st["p"] = val
            elif ec == ecodes.ABS_TILT_X:
                st["tx"] = val
            elif ec == ecodes.ABS_TILT_Y:
                st["ty"] = val
            elif ec == ecodes.ABS_DISTANCE:
                st["dist"] = val
        elif et == ecodes.EV_KEY:
            pressed = val != 0
            if ec == ecodes.BTN_TOUCH:
                if pressed:
                    st["buttons"] |= int(protocol.Button.TOUCH)
                    st["flags"] |= int(protocol.Flag.IN_CONTACT)
                else:
                    st["buttons"] &= ~int(protocol.Button.TOUCH)
                    st["flags"] &= ~int(protocol.Flag.IN_CONTACT)
            elif ec == ecodes.BTN_STYLUS:
                if pressed:
                    st["buttons"] |= int(protocol.Button.STYLUS)
                else:
                    st["buttons"] &= ~int(protocol.Button.STYLUS)
            elif ec == ecodes.BTN_STYLUS2:
                if pressed:
                    st["buttons"] |= int(protocol.Button.STYLUS2)
                else:
                    st["buttons"] &= ~int(protocol.Button.STYLUS2)
            elif ec == ecodes.BTN_TOOL_PEN:
                if pressed:
                    st["tool"] = int(protocol.Tool.PEN)
                    st["flags"] |= int(protocol.Flag.IN_RANGE)
                else:
                    st["tool"] = int(protocol.Tool.NONE)
                    st["flags"] &= ~int(protocol.Flag.IN_RANGE)
            elif ec == ecodes.BTN_TOOL_RUBBER:
                if pressed:
                    st["tool"] = int(protocol.Tool.RUBBER)
                    st["flags"] |= int(protocol.Flag.IN_RANGE)
                else:
                    st["tool"] = int(protocol.Tool.NONE)
                    st["flags"] &= ~int(protocol.Flag.IN_RANGE)
        elif et == ecodes.EV_SYN and ec == ecodes.SYN_REPORT:
            x_norm = (st["x"] - x_min) / x_range
            y_norm = (st["y"] - y_min) / y_range
            p_norm = st["p"] / p_range
            d_norm = st["dist"] / d_range
            buf = protocol.pack(
                seq, time.monotonic_ns(),
                x_norm, y_norm,
                p_norm,
                st["tx"] * tilt_x_scale, st["ty"] * tilt_y_scale,
                d_norm,
                st["buttons"], st["tool"], st["flags"],
            )
            try:
                sock.sendto(buf, server_addr)
            except OSError as e:
                print(f"send error: {e}", file=sys.stderr)
            seq = (seq + 1) & 0xFFFFFFFF


def main():
    ap = argparse.ArgumentParser(description="Ubuntu stylus -> UDP forwarder")
    ap.add_argument("--device", help="/dev/input/eventN override (else auto-pick)")
    ap.add_argument("--server", help="HOST:PORT to skip discovery")
    ap.add_argument("--grab", action="store_true",
                    help="EVIOCGRAB the device (exclusive; local cursor will not move)")
    args = ap.parse_args()

    if args.device:
        dev = evdev.InputDevice(args.device)
    else:
        dev = find_tablet_device()
        if dev is None:
            print("No tablet found (need ABS_X, ABS_Y, ABS_PRESSURE, BTN_TOOL_PEN).",
                  file=sys.stderr)
            sys.exit(1)

    ax = get_abs(dev, ecodes.ABS_X)
    ay = get_abs(dev, ecodes.ABS_Y)
    ap_ = get_abs(dev, ecodes.ABS_PRESSURE)
    ad = get_abs(dev, ecodes.ABS_DISTANCE)
    atx = get_abs(dev, ecodes.ABS_TILT_X)
    aty = get_abs(dev, ecodes.ABS_TILT_Y)
    if ax is None or ay is None or ap_ is None:
        print("Device missing required ABS axes.", file=sys.stderr)
        sys.exit(1)

    x_range = max(1, ax.max - ax.min)
    y_range = max(1, ay.max - ay.min)
    raw_aspect = x_range / y_range
    phys_x = physical_mm(ax)
    phys_y = physical_mm(ay)
    if phys_x and phys_y:
        tablet_aspect = phys_x / phys_y
    else:
        tablet_aspect = raw_aspect
    dist_max = ad.max if ad is not None else 0
    tilt_x_scale = tilt_deg_per_unit(atx)
    tilt_y_scale = tilt_deg_per_unit(aty)

    print(f"Device: {dev.name}  ({dev.path})", flush=True)
    print(f"  ABS_X: [{ax.min}, {ax.max}]  res={ax.resolution}", flush=True)
    print(f"  ABS_Y: [{ay.min}, {ay.max}]  res={ay.resolution}", flush=True)
    print(f"  ABS_PRESSURE: [0, {ap_.max}]", flush=True)
    if ad is not None:
        print(f"  ABS_DISTANCE: [0, {ad.max}]", flush=True)
    if atx is not None:
        print(f"  ABS_TILT_X: [{atx.min}, {atx.max}]  res={atx.resolution}  "
              f"-> {tilt_x_scale:.6f} deg/unit", flush=True)
    if aty is not None:
        print(f"  ABS_TILT_Y: [{aty.min}, {aty.max}]  res={aty.resolution}  "
              f"-> {tilt_y_scale:.6f} deg/unit", flush=True)
    if phys_x and phys_y:
        print(f"  physical area: {phys_x:.2f} x {phys_y:.2f} mm", flush=True)
        print(f"  tablet aspect: physical={tablet_aspect:.6f}  "
              f"raw={raw_aspect:.6f}", flush=True)
    else:
        print(f"  tablet aspect: raw={raw_aspect:.6f}  "
              "(no resolution; cannot correct)", flush=True)

    if args.server:
        host, port = args.server.split(":")
        server_addr = (host, int(port))
    else:
        print("Discovering server via UDP broadcast...", flush=True)
        server_addr = discover_server(tablet_aspect)
        print(f"Server: {server_addr[0]}:{server_addr[1]}", flush=True)

    touch_dev = find_touch_device()
    mt_x_min = mt_y_min = 0
    mt_x_range = mt_y_range = 1
    if touch_dev is not None:
        mt_ax = get_abs(touch_dev, ecodes.ABS_MT_POSITION_X)
        mt_ay = get_abs(touch_dev, ecodes.ABS_MT_POSITION_Y)
        mt_x_min = mt_ax.min if mt_ax else 0
        mt_y_min = mt_ay.min if mt_ay else 0
        mt_x_range = max(1, mt_ax.max - mt_ax.min) if mt_ax else 1
        mt_y_range = max(1, mt_ay.max - mt_ay.min) if mt_ay else 1
        print(f"Touch device: {touch_dev.name}  ({touch_dev.path})", flush=True)
        print(f"  ABS_MT_POSITION_X: [{mt_x_min}, {mt_x_min + mt_x_range}]  "
              f"ABS_MT_POSITION_Y: [{mt_y_min}, {mt_y_min + mt_y_range}]", flush=True)
    else:
        print("No touch device found; finger touch disabled.", flush=True)

    async def run_all():
        tasks = [capture_and_send(
            dev, server_addr,
            ax.min, ax.max, ay.min, ay.max,
            ap_.max, dist_max,
            tilt_x_scale, tilt_y_scale, grab=args.grab,
        )]
        if touch_dev is not None:
            tasks.append(capture_touch_and_send(
                touch_dev, server_addr, args.grab,
                mt_x_min, mt_x_range, mt_y_min, mt_y_range))
        await asyncio.gather(*tasks)

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
