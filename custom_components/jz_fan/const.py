"""Constants and protocol definitions for the XD Smart Fan integration.

Protocol reverse-engineered from the original WeChat mini-program.

Control packet (write):
    [0xAA, 0x55, 0x10, 0x00, 0x0A, <10 payload bytes>]
    Header: AA 55 10 00 0A  (fixed)
    Byte 5  power       0x01=off, 0x02=on
    Byte 6  gear        0x01..0x0C (1..12)
    Byte 7  lr_swing    0x00=off .. 0x05=on (angle steps)
    Byte 8  ud_swing    0x00..0x03
    Byte 9  manual      0x00 default, 1=up 2=down 3=left 4=right
    Byte 10 mode        1=sleep, 3=custom, 5=storm
    Byte 11 timing      0x00..0x0C (0..12 hours)
    Byte 12 light       0x01=off, 0x02=on
    Byte 13 voice       0x01=off, 0x02=on
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

# Init packets sent right after connecting
INIT_PACKETS = [
    [0xAA, 0x55, 0x21, 0x00, 0x01],
    [0xAA, 0x55, 0x10, 0x00, 0x01, 0x00],
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
IDX_VOICE = 13
IDX_TRUMPET = 14

# Preset modes (mode field)
MODE_SLEEP = 1
MODE_CUSTOM = 3
MODE_STORM = 5

PRESET_MODES = {
    "sleep": MODE_SLEEP,
    "custom": MODE_CUSTOM,
    "storm": MODE_STORM,
}
PRESET_MODES_REVERSE = {v: k for k, v in PRESET_MODES.items()}

# Left/right swing angle steps (byte 7).
# Mini-program: 0=off, 1=30°, 2=60°, 3=90°, 4=120°.
LR_SWING_OPTIONS = {
    "off": 0,
    "30": 1,
    "60": 2,
    "90": 3,
    "120": 4,
}
LR_SWING_OPTIONS_REVERSE = {v: k for k, v in LR_SWING_OPTIONS.items()}
# The fan.oscillate on/off maps to the strongest angle when turned on.
LR_SWING_ON_VALUE = 5

# Up/down swing angle steps (byte 8).
# Mini-program: 0=off, 1=30°, 2=60°, 3=120° (note: no 90°).
UD_SWING_OPTIONS = {
    "off": 0,
    "30": 1,
    "60": 2,
    "120": 3,
}
UD_SWING_OPTIONS_REVERSE = {v: k for k, v in UD_SWING_OPTIONS.items()}

# Manual direction (byte 9): momentary nudge of the fan head.
# Mini-program: 1=up, 2=down, 3=left, 4=right (0=default/none).
MANUAL_UP = 1
MANUAL_DOWN = 2
MANUAL_LEFT = 3
MANUAL_RIGHT = 4

# Timing / sleep timer in hours (byte 11): 0=cancel, 1..12 hours.
MIN_TIMING = 0
MAX_TIMING = 12