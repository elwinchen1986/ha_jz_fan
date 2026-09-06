"""Constants and protocol definitions for the XD Smart Fan integration.

Control packet (write):
    [0xAA, 0x55, 0x10, 0x00, 0x0A, <10 payload bytes>]
    Header: AA 55 10 00 0A  (fixed)
    Byte 5  power       0x01=off, 0x02=on
    Byte 6  gear        0x01..0x0C (1..12)
    Byte 7  lr_swing    0x00=off .. 0x05=on (angle steps)
    Byte 8  ud_swing    0x00=off, 1=30°, 2=60°, 3=90°
    Byte 9  manual      0x00 default, 1=up 2=down 3=left 4=right
    Byte 10 mode        1=sleep, 2=natural, 3=custom, 4=cycle_3d
    Byte 11 timing      0x00..0x0F (0..15 hours)
    Byte 12 light       0x01=off, 0x02=on
    Byte 14 trumpet     0x01=off, 0x02=on
    Any byte == 0xFF means "do not change".

Notify packet (device -> app): same 15-byte layout, values start at index 5.
A value of 255 (0xFF) in a notify byte means "unchanged / keep current".
"""

DOMAIN = "jz_fan"

# Config entry keys
CONF_ADDRESS = "address"
CONF_NAME = "name"

# BLE write pacing (seconds) - the mini-program delayed each write by 666ms.
WRITE_DELAY = 0.666

# Protocol header for control packets
CTRL_HEADER = [0xAA, 0x55, 0x10, 0x00, 0x0A]

# Init / handshake packets sent right after connecting.
#
# The mini-program sends three packets on connect. The third packet
# (0xAA 0x55 0x80 0x00 0x01 <code>) carries a "bind code" parsed from the
# device QR code (TLV field 0x03). Empirically the device only starts
# pushing status frames over notify AFTER it receives this bind handshake,
# which is why a persistent HA connection that only sent the first two
# packets never received any echo/state notifications.
#
# The bind code is the same (0x04) across all of these devices (they share
# a single QR code), so it is a fixed constant rather than a config option.
BIND_CODE = 0x04
INIT_PACKETS = [
    [0xAA, 0x55, 0x21, 0x00, 0x01],
    [0xAA, 0x55, 0x10, 0x00, 0x01, 0x00],
    [0xAA, 0x55, 0x80, 0x00, 0x01, BIND_CODE],
]

# Placeholder meaning "no change"
NO_CHANGE = 0xFF

# On/off encoding for toggle fields
OFF_VALUE = 0x01
ON_VALUE = 0x02

# Fan gear limits
MIN_GEAR = 1
MAX_GEAR = 12

# Payload byte offsets within the full packet (index into notify/control array)
IDX_POWER = 5
IDX_GEAR = 6
IDX_LR_SWING = 7
IDX_UD_SWING = 8
IDX_MANUAL = 9
IDX_MODE = 10
IDX_TIMING = 11
IDX_LIGHT = 12
IDX_TRUMPET = 14

# Preset modes (mode field)
MODE_SLEEP = 1
MODE_NATURAL = 2
MODE_CUSTOM = 3
MODE_CYCLE_3D = 4

PRESET_MODES = {
    "sleep": MODE_SLEEP,
    "natural": MODE_NATURAL,
    "custom": MODE_CUSTOM,
    "cycle_3d": MODE_CYCLE_3D,
}
PRESET_MODES_REVERSE = {v: k for k, v in PRESET_MODES.items()}

# Left/right swing angle steps (byte 7).
# 0=off, 1=30°, 2=60°, 4=120°.
LR_SWING_OPTIONS = {
    "off": 0,
    "30": 1,
    "60": 2,
    "120": 4,
}
LR_SWING_OPTIONS_REVERSE = {v: k for k, v in LR_SWING_OPTIONS.items()}
# The fan.oscillate on/off maps to the strongest angle when turned on.
LR_SWING_ON_VALUE = 4

# Up/down swing angle steps (byte 8).
# 0=off, 1=30°, 2=60°, 3=90°.
UD_SWING_OPTIONS = {
    "off": 0,
    "30": 1,
    "60": 2,
    "90": 3,
}
UD_SWING_OPTIONS_REVERSE = {v: k for k, v in UD_SWING_OPTIONS.items()}

# Manual direction (byte 9): momentary nudge of the fan head.
# Mini-program: 1=up, 2=down, 3=left, 4=right (0=default/none).
MANUAL_UP = 1
MANUAL_DOWN = 2
MANUAL_LEFT = 3
MANUAL_RIGHT = 4

# Timing / sleep timer in hours (byte 11): 0=cancel, 1..15 hours.
MIN_TIMING = 0
MAX_TIMING = 15