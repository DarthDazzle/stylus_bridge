"""Shared wire format and discovery constants for stylus_bridge.

Copy this file to both the Ubuntu client host and the Windows server host.
"""
import struct
from enum import IntEnum, IntFlag

DISCOVERY_PORT = 41234
DATA_PORT = 41235

DISCOVERY_MAGIC = b"STYLUS_DISC_V1"   # 14 bytes
OFFER_MAGIC = b"STYLUS_OFFER_V1"      # 15 bytes

# Discovery hello   = DISCOVERY_MAGIC + <f tablet_aspect>           (14 + 4 = 18 B)
# Discovery offer   = OFFER_MAGIC     + <H data_port>               (15 + 2 = 17 B)

# Data packet (little-endian, packed, 40 B):
#   u32 seq          monotonic per-sender sequence
#   u64 t_ns         CLOCK_MONOTONIC ns at SYN_REPORT
#   f32 x_norm       normalized [0, 1] from ABS_X
#   f32 y_norm       normalized [0, 1] from ABS_Y
#   f32 pressure     normalized [0, 1]
#   f32 tilt_x_deg   signed degrees
#   f32 tilt_y_deg   signed degrees
#   f32 distance     normalized [0, 1] hover; 0 if unsupported
#   u16 buttons      Button flags
#   u8  tool         Tool enum
#   u8  flags        Flag bits
PACKET_FORMAT = "<IQffffffHBB"
PACKET_SIZE = struct.calcsize(PACKET_FORMAT)
assert PACKET_SIZE == 40, PACKET_SIZE


class Button(IntFlag):
    TOUCH = 1 << 0     # BTN_TOUCH (tip contact)
    STYLUS = 1 << 1    # BTN_STYLUS  (barrel button 1)
    STYLUS2 = 1 << 2   # BTN_STYLUS2 (barrel button 2)


class Tool(IntEnum):
    NONE = 0
    PEN = 1
    RUBBER = 2  # reserved; deferred


class Flag(IntFlag):
    IN_RANGE = 1 << 0
    IN_CONTACT = 1 << 1


def pack(seq, t_ns, x_norm, y_norm, pressure, tilt_x_deg, tilt_y_deg,
         distance, buttons, tool, flags):
    return struct.pack(
        PACKET_FORMAT,
        seq & 0xFFFFFFFF, t_ns & 0xFFFFFFFFFFFFFFFF,
        float(x_norm), float(y_norm), float(pressure),
        float(tilt_x_deg), float(tilt_y_deg), float(distance),
        int(buttons) & 0xFFFF, int(tool) & 0xFF, int(flags) & 0xFF,
    )


def unpack(buf):
    return struct.unpack(PACKET_FORMAT, buf)
