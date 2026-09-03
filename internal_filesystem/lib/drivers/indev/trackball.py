# T-Deck trackball input device.
#
# The LilyGo T-Deck / T-Deck Plus has a 5-way trackball wired directly to
# ESP32-S3 GPIOs. This module exposes it to LVGL as a keypad indev, mapping
# the four direction pins to LVGL UP/DOWN/LEFT/RIGHT navigation keys and the
# centre press to ENTER, following the pin layout from the LilyGo T-Deck
# board definition in lvgl_micropython's display_configs/LilyGo-TDeck toml:
#
#   up_pin=3, down_pin=2, left_pin=15, right_pin=1, press_pin=0
#
# The pins are pull-up inputs that go low when a direction is rolled/pressed.
# A simple read callback (like the other MPOS keypad indevs) is used rather
# than an interrupt-driven approach because MicroPython interrupts are
# unreliable under high LVGL load.

from micropython import const

import lvgl as lv
import machine

_DIR_UP = const(1)
_DIR_DOWN = const(2)
_DIR_LEFT = const(4)
_DIR_RIGHT = const(8)
_DIR_PRESS = const(16)


class Trackball:
    """LVGL keypad indev driving the T-Deck 5-way trackball via GPIO."""

    def __init__(self, up_pin=3, down_pin=2, left_pin=15, right_pin=1, press_pin=0):
        self._up = machine.Pin(up_pin, machine.Pin.IN, machine.Pin.PULL_UP)
        self._down = machine.Pin(down_pin, machine.Pin.IN, machine.Pin.PULL_UP)
        self._left = machine.Pin(left_pin, machine.Pin.IN, machine.Pin.PULL_UP)
        self._right = machine.Pin(right_pin, machine.Pin.IN, machine.Pin.PULL_UP)
        self._press = machine.Pin(press_pin, machine.Pin.IN, machine.Pin.PULL_UP)

        self._pending = 0
        self._last_key = None
        self._last_state = lv.INDEV_STATE.RELEASED

        self._indev = lv.indev_create()
        self._indev.set_type(lv.INDEV_TYPE.KEYPAD)
        self._indev.set_read_cb(self._read_cb)
        self._indev.set_group(lv.group_get_default())
        disp = lv.display_get_default()
        self._indev.set_display(disp)
        self._indev.set_long_press_time(400)
        self._indev.set_long_press_repeat_time(100)
        self._indev.enable(True)

    @property
    def indev(self):
        return self._indev

    def _read_cb(self, indev, data):
        # Read the current physical state of all five directions.
        dirs = 0
        if self._up.value() == 0:
            dirs |= _DIR_UP
        if self._down.value() == 0:
            dirs |= _DIR_DOWN
        if self._left.value() == 0:
            dirs |= _DIR_LEFT
        if self._right.value() == 0:
            dirs |= _DIR_RIGHT
        if self._press.value() == 0:
            dirs |= _DIR_PRESS

        key = None
        if dirs != 0:
            # Prioritise a single direction per report so navigation stays
            # smooth, turning each direction into a discrete LVGL key press.
            if dirs & _DIR_UP:
                key = lv.KEY.UP
            elif dirs & _DIR_DOWN:
                key = lv.KEY.DOWN
            elif dirs & _DIR_LEFT:
                key = lv.KEY.LEFT
            elif dirs & _DIR_RIGHT:
                key = lv.KEY.RIGHT
            elif dirs & _DIR_PRESS:
                key = lv.KEY.ENTER

        if key is not None:
            data.key = key
            data.state = lv.INDEV_STATE.PRESSED
            data.continue_reading = False
            self._last_key = key
            self._last_state = lv.INDEV_STATE.PRESSED
        else:
            data.key = self._last_key if self._last_key is not None else lv.KEY.ENTER
            data.state = lv.INDEV_STATE.RELEASED
            data.continue_reading = False
            self._last_key = None
            self._last_state = lv.INDEV_STATE.RELEASED
