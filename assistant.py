import sys

from config.settings import settings


class Assistant:
    def __init__(self):
        self.name = settings.assistant_name

        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    def speak(self, message):
        try:
            print(f"{self.name}: {message}")
        except UnicodeEncodeError:
            safe_message = str(message).encode("utf-8", errors="replace").decode("utf-8")
            print(f"{self.name}: {safe_message}")