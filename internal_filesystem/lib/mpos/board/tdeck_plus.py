import logging

logger = logging.getLogger(__name__)

if __debug__: logger.debug("tdeck_plus.py initialization")
"""
Hardware initialization for the LilyGo T-Deck Plus.

The T-Deck Plus is an ESP32-S3 handheld that is a revision of the original
LilyGo T-Deck. It shares the same core peripheral set:

  - 2.3" 240x320 ST7789 IPS display over SPI
  - GT911 capacitive touch panel over I2C (address latches to 0x14/0x5D)
  - 9-key matrix numpad keyboard over I2C (device 0x55)
  - 5-way trackball on direct GPIOs
  - microSD slot on the shared SPI bus
  - SX1262 LoRa radio on the shared SPI bus
  - Buzzer, RTC and power button around the ESP32-S3

Reference pin mapping (from lvgl_micropython display_configs/LilyGo-TDeck):
  SPI  : host=1, mosi=41, miso=38, sck=40
  LCD  : dc=11, cs=12, backlight=42
  I2C  : host=0, scl=8, sda=18, 100 kHz
  Touch: GT911 (0x14/0x5D), interrupt on GPIO 16
  KB   : numpad at device 0x55
  SD   : cs=39
  LoRa : cs=9, gpio=13, irq=45, rst=17
  Trackball: up=3, down=2, left=15, right=1, press=0
  Power enum: GPIO 10 (drive HIGH to power the peripherals)
"""

from micropython import const

import i2c
import lcd_bus
import lvgl as lv
import machine
import mpos.ui
from mpos import InputManager, SDCardManager

# --- Display SPI bus (shared with SD card and LoRa) ---
SPI_HOST = const(1)
SPI_FREQ = const(40_000_000)
LCD_DC = const(11)
LCD_CS = const(12)
LCD_BACKLIGHT = const(42)

# --- Touch I2C bus ---
I2C_HOST = const(0)
I2C_FREQ = const(100_000)
TP_SDA = const(18)
TP_SCL = const(8)
TP_INT = const(16)
TP_ADDR_ALT = const(0x5D)
TP_ADDR_MAIN = const(0x14)

# --- Numpad keyboard ---
KB_ADDR = const(0x55)

# --- Trackball GPIO ---
TB_UP = const(3)
TB_DOWN = const(2)
TB_LEFT = const(15)
TB_RIGHT = const(1)
TB_PRESS = const(0)

# --- SD card ---
SD_CS = const(39)

# --- Power enable ---
# GPIO10 drives the power-enable transistor that routes power to the on-board
# peripherals (keyboard MCU, GT911 touch, LoRa, SD, ...). It must be driven
# HIGH or the keyboard/touch will not appear on the I2C bus at all.
POWER_EN = const(10)

# --- Display geometry ---
TFT_WIDTH = const(240)
TFT_HEIGHT = const(320)


def _enable_peripherals():
    """Drive GPIO10 (power enable) HIGH so the on-board peripherals
    (keyboard, touch, LoRa, SD) actually receive power. Without this the
    numpad does not appear at I2C 0x55 and the GT911 touch does not latch a
    stable address. Must run before any peripheral is configured."""
    machine.Pin(POWER_EN, machine.Pin.OUT, value=1)
    import time
    time.sleep_ms(200)  # let the peripherals power up and settle


def _init_display():
    global _spi_bus
    _spi_bus = machine.SPI.Bus(
        host=SPI_HOST,
        mosi=41,
        miso=38,
        sck=40,
    )

    display_bus = lcd_bus.SPIBus(
        spi_bus=_spi_bus,
        freq=SPI_FREQ,
        dc=LCD_DC,
        cs=LCD_CS,
    )

    _BUFFER_SIZE = const(240 * 170 * 2 + 1)
    fb1 = display_bus.allocate_framebuffer(_BUFFER_SIZE, lcd_bus.MEMORY_INTERNAL | lcd_bus.MEMORY_DMA)

    import drivers.display.st7789 as st7789
    mpos.ui.main_display = st7789.ST7789(
        data_bus=display_bus,
        frame_buffer1=fb1,
        display_width=TFT_WIDTH,
        display_height=TFT_HEIGHT,
        color_space=lv.COLOR_FORMAT.RGB565,
        color_byte_order=st7789.BYTE_ORDER_BGR,
        rgb565_byte_swap=True,
        backlight_pin=LCD_BACKLIGHT,
        backlight_on_state=st7789.STATE_PWM,
    )  # this will trigger lv.init()

    mpos.ui.main_display.init()
    mpos.ui.main_display.set_power(True)
    mpos.ui.main_display.set_backlight(100)


def _probe_touch_address(i2c_bus):
    """GT911 latches its I2C address to 0x14 or 0x5D depending on the reset
    /INT pin sequencing at power-up, and this is flaky on T-Deck hardware.
    Probe for whichever address actually responds and return it."""
    dev = i2c.I2C.Device(bus=i2c_bus, dev_id=TP_ADDR_MAIN, reg_bits=16)
    try:
        buf = bytearray(1)
        dev.read(buf=buf)  # NOQA
        return TP_ADDR_MAIN
    except Exception:
        pass
    return TP_ADDR_ALT


def _init_touch():
    try:
        i2c_bus = i2c.I2C.Bus(host=I2C_HOST, scl=TP_SCL, sda=TP_SDA, freq=I2C_FREQ, use_locks=False)
        addr = _probe_touch_address(i2c_bus)
        if __debug__: logger.debug("GT911 touch address: 0x%02X", addr)
        import drivers.indev.gt911 as gt911
        touch_dev = i2c.I2C.Device(bus=i2c_bus, dev_id=addr, reg_bits=gt911.BITS)
        indev = gt911.GT911(
            touch_dev,
            reset_pin=None,
            interrupt_pin=TP_INT,
            startup_rotation=lv.DISPLAY_ROTATION._90,
            debug=False,
        )
        InputManager.register_indev(indev)
    except Exception as e:
        logger.error("Touch init got exception: %s" % (e))


def _init_keyboard():
    # The T-Deck numpad is an I2C keyboard controller at 0x55 that reports a
    # single byte per read: 0x00 = no key, otherwise an ASCII code (with a few
    # control codes such as 0x08 backspace and 0x0D enter). Expose it to LVGL
    # as a keypad indev.
    try:
        i2c_bus = i2c.I2C.Bus(host=I2C_HOST, scl=TP_SCL, sda=TP_SDA, freq=I2C_FREQ, use_locks=False)
        kb_dev = i2c.I2C.Device(bus=i2c_bus, dev_id=KB_ADDR, reg_bits=8)
        buf = bytearray(1)
        mv = memoryview(buf)

        last_key = [None]

        def keypad_read_cb(indev, data):
            current_key = None
            try:
                kb_dev.read(buf=mv)
                key = buf[0]
                if key == 0x00:
                    current_key = None
                elif key == 0x08:
                    current_key = lv.KEY.BACKSPACE
                elif key == 0x0D:
                    current_key = lv.KEY.ENTER
                else:
                    current_key = key
            except Exception:
                current_key = None

            data.continue_reading = False
            if current_key is not None:
                data.key = current_key
                data.state = lv.INDEV_STATE.PRESSED
                last_key[0] = current_key
            else:
                data.key = last_key[0] if last_key[0] is not None else lv.KEY.ENTER
                data.state = lv.INDEV_STATE.RELEASED
                last_key[0] = None

        indev = lv.indev_create()
        indev.set_type(lv.INDEV_TYPE.KEYPAD)
        indev.set_read_cb(keypad_read_cb)
        indev.set_group(lv.group_get_default())
        indev.set_display(lv.display_get_default())
        indev.set_long_press_time(400)
        indev.set_long_press_repeat_time(100)
        indev.enable(True)
        InputManager.register_indev(indev)
    except Exception as e:
        logger.error("Keyboard init got exception: %s" % (e))


def _init_trackball():
    try:
        from drivers.indev.trackball import Trackball
        tb = Trackball(
            up_pin=TB_UP,
            down_pin=TB_DOWN,
            left_pin=TB_LEFT,
            right_pin=TB_RIGHT,
            press_pin=TB_PRESS,
        )
        InputManager.register_indev(tb.indev)
    except Exception as e:
        logger.error("Trackball init got exception: %s" % (e))


def _init_sd():
    try:
        SDCardManager.init(spi_bus=_spi_bus, cs_pin=SD_CS)
        SDCardManager.mount()
    except Exception as e:
        logger.error("SD card init got exception: %s" % (e))


_enable_peripherals()
_init_display()


def _after_display_rotation():
    try:
        mpos.ui.main_display.set_rotation(lv.DISPLAY_ROTATION._90)  # rotate 90° clockwise to landscape
        mpos.ui.main_display.set_color_inversion(True)
    except Exception as e:
        logger.error("display rotation init got exception: %s" % (e))


# NOTE: _init_touch() must run BEFORE _after_display_rotation(). The GT911
# PointerDriver captures _orig_width/_orig_height from the display resolution at
# construction time; if the display has already been rotated those snap the
# swapped 320x240 dims and _calc_coords applies the rotation a second time.
_init_touch()
_after_display_rotation()
_init_keyboard()
_init_trackball()
_init_sd()


if __debug__: logger.debug("tdeck_plus.py finished")
