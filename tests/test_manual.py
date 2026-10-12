"""Manual mode turns the emulator's raw input events into taps, swipes and Back presses at screen coordinates."""

import threading

from droid_monkey.manual_explore import ManualRecorder


def recorder():
    r = ManualRecorder.__new__(ManualRecorder)
    r.events, r.lock, r.stop, r.last_screen = [], threading.Lock(), threading.Event(), "SCREEN"
    r.scale = (1080 / 32768, 2424 / 32768)
    return r


def frame(tid, x, y):
    return [f"/dev/input/event10: EV_ABS       ABS_MT_TRACKING_ID   {tid}",
            f"/dev/input/event10: EV_ABS       ABS_MT_POSITION_X    {x:08x}",
            f"/dev/input/event10: EV_ABS       ABS_MT_POSITION_Y    {y:08x}",
            "/dev/input/event10: EV_KEY       BTN_TOUCH            DOWN",
            "/dev/input/event10: EV_SYN       SYN_REPORT           00000000"]


UP = ["/dev/input/event10: EV_ABS       ABS_MT_TRACKING_ID   ffffffff",
      "/dev/input/event10: EV_KEY       BTN_TOUCH            UP",
      "/dev/input/event10: EV_SYN       SYN_REPORT           00000000"]


def test_a_tap_lands_on_screen_pixels():
    r = recorder()
    r.consume(frame("00000001", 16384, 16384) + UP)
    (kind, (down, up, screen)), = r.events
    assert kind == "touch" and screen == "SCREEN"
    assert (round(down[0]), round(down[1])) == (540, 1212) and down == up


def test_a_swipe_keeps_start_and_end_and_back_is_a_key():
    r = recorder()
    move = ["/dev/input/event10: EV_ABS       ABS_MT_POSITION_Y    00002000",
            "/dev/input/event10: EV_SYN       SYN_REPORT           00000000"]
    r.consume(frame("00000002", 16384, 24000) + move + UP + ["/dev/input/event2: EV_KEY       KEY_BACK             DOWN"])
    (_, (down, up, _)), back = r.events
    assert down[1] > up[1] + 500          # finger moved up the screen: a scroll down
    assert back == ("back", None)
