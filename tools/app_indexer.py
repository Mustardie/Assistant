import os
import json

from config.paths import get_nova_app_file

INDEX_PATH = str(get_nova_app_file("app_index.json"))


def build_index():
    print("BUILDING INDEX...")

    index = {}

    program_files = os.environ.get("ProgramFiles", r"C:\\Program Files")
    for root, dirs, files in os.walk(program_files):
        for file in files:
            if file.lower().endswith(".exe"):
                name = file.lower().replace(".exe", "")
                path = os.path.join(root, file)
                if name not in index:
                    index[name] = path

    print(f"FOUND {len(index)} APPS")

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)

    print("INDEX SAVED")

    return index


def load_index():
    if not os.path.exists(INDEX_PATH):
        return build_index()

    with open(INDEX_PATH, "r") as f:
        data = json.load(f)

    if not data:
        return build_index()

    return data