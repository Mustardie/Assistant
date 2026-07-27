import json
import os

from config.paths import get_nova_app_file

APP_DB = str(get_nova_app_file("apps.json"))


def load_database():
    with open(APP_DB, "r", encoding="utf-8") as f:
        return json.load(f)


def save_database(database):
    with open(APP_DB, "w", encoding="utf-8") as f:
        json.dump(database, f, indent=2)


def learn_app(name, path):
    database = load_database()

    database[name.lower()] = {
        "name": name,
        "target": path,
        "source": "user",
        "aliases": []
    }

    save_database(database)