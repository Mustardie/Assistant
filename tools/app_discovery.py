import os
import json
import win32com.client

from config.paths import get_nova_app_file

START_MENU_PATHS = [
    os.path.join(os.environ["ProgramData"], r"Microsoft\Windows\Start Menu\Programs"),
    os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs"),
]


def scan_start_menu():
    apps = []

    shell = win32com.client.Dispatch("WScript.Shell")

    for start_path in START_MENU_PATHS:
        if not os.path.exists(start_path):
            continue

        for root, dirs, files in os.walk(start_path):
            for file in files:
                if not file.lower().endswith(".lnk"):
                    continue

                shortcut_path = os.path.join(root, file)

                try:
                    shortcut = shell.CreateShortcut(shortcut_path)
                    target = shortcut.Targetpath

                    if target and target.lower().endswith(".exe"):
                        apps.append({
                            "name": os.path.splitext(file)[0],
                            "target": target
                        })

                except Exception:
                    pass

    return apps


def build_database():
    apps = scan_start_menu()

    database = {}

    for app in apps:
        database[app["name"].lower()] = {
            "name": app["name"],
            "target": app["target"],
            "source": "start_menu",
            "aliases": []
        }

    database_path = get_nova_app_file("apps.json")

    with open(database_path, "w", encoding="utf-8") as f:
        json.dump(database, f, indent=2)

    print(f"Saved {len(database)} apps.")