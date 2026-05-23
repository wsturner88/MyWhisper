import subprocess
import time

from pynput.keyboard import Controller, Key


def paste_text(text):
    """Place text on the clipboard and paste it into the focused app."""
    if not text:
        return
    subprocess.run(["pbcopy"], input=text, text=True, check=False)
    time.sleep(0.15)
    keyboard = Controller()
    with keyboard.pressed(Key.cmd):
        keyboard.press("v")
        keyboard.release("v")
