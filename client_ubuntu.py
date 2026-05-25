#!/usr/bin/env python3
"""Ubuntu stylus capture -> UDP send.

Requires: python-evdev (`pip install evdev`), user in `input` group.
"""
import argparse
import asyncio
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


def get_abs(dev, code):
    for c, info in dev.capabilities().get(ecodes.EV_ABS, []):
        if c == code:
            return info
    return None


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


async def capture_and_send(dev, server_addr,
                           x_min, x_max, y_min, y_max,
                           p_max, dist_max, grab):
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
            # BTN_TOOL_RUBBER: reserved, ignored.
        elif et == ecodes.EV_SYN and ec == ecodes.SYN_REPORT:
            x_norm = (st["x"] - x_min) / x_range
            y_norm = (st["y"] - y_min) / y_range
            p_norm = st["p"] / p_range
            d_norm = st["dist"] / d_range
            buf = protocol.pack(
                seq, time.monotonic_ns(),
                x_norm, y_norm,
                p_norm,
                float(st["tx"]), float(st["ty"]),
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
    if ax is None or ay is None or ap_ is None:
        print("Device missing required ABS axes.", file=sys.stderr)
        sys.exit(1)

    x_range = max(1, ax.max - ax.min)
    y_range = max(1, ay.max - ay.min)
    tablet_aspect = x_range / y_range
    dist_max = ad.max if ad is not None else 0

    print(f"Device: {dev.name}  ({dev.path})", flush=True)
    print(f"  ABS_X: [{ax.min}, {ax.max}]", flush=True)
    print(f"  ABS_Y: [{ay.min}, {ay.max}]", flush=True)
    print(f"  ABS_PRESSURE: [0, {ap_.max}]", flush=True)
    if ad is not None:
        print(f"  ABS_DISTANCE: [0, {ad.max}]", flush=True)
    print(f"  tablet aspect = {tablet_aspect:.6f}", flush=True)

    if args.server:
        host, port = args.server.split(":")
        server_addr = (host, int(port))
    else:
        print("Discovering server via UDP broadcast...", flush=True)
        server_addr = discover_server(tablet_aspect)
        print(f"Server: {server_addr[0]}:{server_addr[1]}", flush=True)

    try:
        asyncio.run(capture_and_send(
            dev, server_addr,
            ax.min, ax.max, ay.min, ay.max,
            ap_.max, dist_max, grab=args.grab,
        ))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
