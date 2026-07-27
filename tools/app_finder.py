import subprocess
from tools.app_indexer import load_index

from rapidfuzz import process, fuzz

from tools.cache import load_cache, save_cache
import os

app_index = load_index()


def launch_app(query):
    query = query.lower().strip()

    cache = load_cache()

    # 1. Cache (fastest)
    if query in cache:
        path = cache[query]

        if os.path.exists(path):
            subprocess.Popen(path)
            return True, path

        del cache[query]
        save_cache(cache)

    # 2. Exact match
    if query in app_index:
        path = app_index[query]

        cache[query] = path
        save_cache(cache)

        subprocess.Popen(path)
        return True, path

    # 3. Fuzzy match
    match = process.extractOne(
        query,
        app_index.keys(),
        scorer=fuzz.WRatio
    )

    if match:
        name, score, _ = match

        if score >= 70:
            path = app_index[name]

            cache[query] = path
            save_cache(cache)

            subprocess.Popen(path)
            return True, path

    # 4. Windows fallback
    try:
        subprocess.Popen(query)
        return True, query
    except Exception:
        pass

    return False, None