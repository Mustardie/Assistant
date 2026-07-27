import time


class _PyAutoGUI:
    _instance = None

    def __getattr__(self, name):
        if self._instance is None:
            import pyautogui as _pg
            _pg.FAILSAFE = True
            _pg.PAUSE = 0.2
            type(self)._instance = _pg
        return getattr(self._instance, name)


pyautogui = _PyAutoGUI()


def move_mouse(x, y):
    pyautogui.moveTo(x, y, duration=0.25)


def left_click():
    pyautogui.click()


def double_click():
    pyautogui.doubleClick()


def right_click():
    pyautogui.rightClick()


def type_text(text):
    pyautogui.write(text, interval=0.02)


def press_key(key):
    pyautogui.press(key)


def hotkey(*keys):
    pyautogui.hotkey(*keys)


def scroll(amount):
    """Positive scrolls up, negative scrolls down (pyautogui convention)."""
    pyautogui.scroll(int(amount))


def wait(seconds):
    time.sleep(seconds)
