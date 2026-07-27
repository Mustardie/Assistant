print("========== LAUNCHER V2 LOADED ==========")
import os
import subprocess

from rapidfuzz import process, fuzz

from tools.app_indexer import load_index
from tools.cache import load_cache, save_cache

app_index = load_index()

def find_candidates(query):
    print("find_candidates() called")
    """
    Returns a list of possible executables matching the query.
    """

    query = query.lower().strip()

    # Exact match
    if query in app_index:
        paths = app_index[query]

        if isinstance(paths, str):
            return [paths]

        return paths

    # Fuzzy match
    match = process.extractOne(
        query,
        app_index.keys(),
        scorer=fuzz.WRatio
    )

    if match:
        name, score, _ = match

        if score >= 70:
            paths = app_index[name]

            if isinstance(paths, str):
                return [paths]

            return paths

    return []

def score_candidate(path):
    """
    Gives each executable a score.
    Higher = more likely to be the main app.
    """

    score = 0
    p = path.lower()

    # Prefer Program Files
    if "program files" in p:
        score += 40

    # Prefer 64-bit executables
    if "64" in p or "64bit" in p:
        score += 20

    # Common "real app" folders
    if "bin" in p:
        score += 10

    # Penalize things that are usually NOT the main app
    bad_words = [
        "plugin",
        "plugins",
        "sdk",
        "test",
        "debug",
        "helper",
        "updater",
        "crash",
        "uninstall"
    ]

    for word in bad_words:
        if word in p:
            score -= 40

    return score

def choose_best(candidates):
    if not candidates:
        return None

    scored = []

    print("\n========== CANDIDATES ==========")

    for path in candidates:
        score = score_candidate(path)
        filename = os.path.basename(path)

        print(f"{score:3} | {filename}")
        print(f"      {path}")

        scored.append((score, path))

    scored.sort(reverse=True)

    print("\n========== BEST ==========")
    print(scored[0][1])

    return scored[0][1]

def launch(path):
    """
    Launches an executable.
    """

    try:
        subprocess.Popen(path)
        return True
    except Exception:
        return False
    
def launch_app(query):
    print("launch_app() called")
    query = query.lower().strip()

    # 1. Cache
    cache = load_cache()

    if query in cache:
        path = cache[query]

        if os.path.exists(path):
            if launch(path):
                return True, path

        # Remove invalid cache entry
        del cache[query]
        save_cache(cache)

    # 2. Find possible executables
    candidates = find_candidates(query)

    if not candidates:
        return False, None

    # 3. Pick the best one
    best = choose_best(candidates)

    if best is None:
        return False, None

    # 4. Launch it
    if launch(best):
        cache[query] = best
        save_cache(cache)
        return True, best

    return False, None