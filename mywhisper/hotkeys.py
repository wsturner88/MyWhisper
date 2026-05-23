from pynput import keyboard

# Keys offered for push-to-talk. All are safe to hold: a bare modifier key
# does not type anything on its own.
_KEYS = {
    "right_option": keyboard.Key.alt_r,
    "right_command": keyboard.Key.cmd_r,
    "right_control": keyboard.Key.ctrl_r,
    "right_shift": keyboard.Key.shift_r,
    "left_option": keyboard.Key.alt_l,
    "left_command": keyboard.Key.cmd_l,
}


def resolve(name):
    return _KEYS.get(name, keyboard.Key.alt_r)


class PushToTalk:
    """Calls on_down when the chosen key is pressed, on_up when released."""

    def __init__(self, key, on_down, on_up):
        self._key = key
        self._on_down = on_down
        self._on_up = on_up
        self._held = False
        self._listener = keyboard.Listener(
            on_press=self._press, on_release=self._release)

    def _press(self, key):
        if key == self._key and not self._held:
            self._held = True
            self._on_down()

    def _release(self, key):
        if key == self._key and self._held:
            self._held = False
            self._on_up()

    def start(self):
        self._listener.start()

    def stop(self):
        try:
            self._listener.stop()
        except Exception:
            pass
