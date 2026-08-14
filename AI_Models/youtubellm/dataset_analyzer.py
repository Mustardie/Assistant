"""
Dataset Analyzer — V0.4

Read-only report on the reviewed training dataset.

Scans every .json / .jsonl file in the reviewed folder, DISCOVERS the
actual schema from the data (it does not assume one blindly), and prints
a terminal report: review-status counts, score statistics, field coverage,
inconsistencies, duplicates, malformed files, and a final readiness verdict.

It NEVER writes, moves, deletes, or rewrites any dataset file.

Usage:
  python dataset_analyzer.py                -> analyze dataset/reviewed/
  python dataset_analyzer.py <folder>       -> analyze a different folder
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Windows fix: make the terminal show Unicode correctly
# (em-dashes, curly quotes, etc.).
sys.stdout.reconfigure(encoding="utf-8")

PROJECT_DIR = Path(__file__).parent
REVIEWED_DIR = PROJECT_DIR / "dataset" / "reviewed"
RAW_DIR = PROJECT_DIR / "dataset" / "raw"

# Fields the dataset generator is expected to write. The analyzer REPORTS
# examples missing any of these — it never crashes when the data differs.
CORE_TOP_LEVEL_FIELDS = (
    "id", "task", "input", "analysis", "judgment",
    "reasoning", "improved_version", "improvement_reasoning", "status",
)
ALLOWED_JUDGMENTS = ("MAKE IT", "REWORK IT", "KILL IT")
ALLOWED_TASKS = (
    "video_idea_evaluation", "video_idea_generation",
    "title_evaluation", "hook_evaluation",
)
MIN_SCORE, MAX_SCORE = 1, 10

# "READY TO SCALE" additionally requires enough examples to train on.
MIN_EXAMPLES_FOR_READY = 50

# Top-level keys that (if present in a reviewed example) mark it as
# human-corrected. The generator stores no such marker, so by default the
# analyzer compares reviewed content against dataset/raw/ instead.
CORRECTION_MARKER_KEYS = ("edited", "edit", "correction", "corrected",
                          "accepted", "verdict")


def section(title):
    """Print a visible report section header."""
    print("\n" + "=" * 60)
    print("  " + title)
    print("=" * 60)


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------

def collect_files(folder):
    """Split folder contents into JSON files and ignored (unrelated) files."""
    json_files, ignored = [], []
    for path in sorted(folder.iterdir()):
        if path.is_dir():
            continue
        if path.suffix.lower() in (".json", ".jsonl"):
            json_files.append(path)
        else:
            ignored.append(path)
    return json_files, ignored


def parse_file(path):
    """Parse one .json / .jsonl file.

    Returns (objects, errors).
    objects: list of (line_number_or_None, parsed_data).
    errors:  list of (line_number_or_None, message).
    """
    objects, errors = [], []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        return objects, [(None, f"cannot read file: {error}")]

    if path.suffix.lower() == ".json":
        try:
            objects.append((None, json.loads(text)))
        except json.JSONDecodeError as error:
            errors.append((None, f"invalid JSON: {error}"))
        return objects, errors

    # .jsonl: one JSON object per line.
    for number, line_text in enumerate(text.splitlines(), start=1):
        if not line_text.strip():
            continue
        try:
            objects.append((number, json.loads(line_text)))
        except json.JSONDecodeError as error:
            errors.append((number, f"invalid JSON line: {error}"))

    # Fallback: the whole file might be one pretty-printed object.
    if not objects and not errors:
        try:
            objects.append((None, json.loads(text)))
        except json.JSONDecodeError:
            pass

    if not objects and not errors:
        errors.append((None, "file contains no parseable JSON"))
    return objects, errors


def to_examples(file_path, objects):
    """Turn parsed objects into example records.

    An example record: {"source": Path, "line": int|None, "data": dict}.
    Objects that parse as JSON but are not examples are returned in
    not_examples as (path, line, reason) — reported, never crashed on.
    """
    examples, not_examples = [], []
    for line_no, data in objects:
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            examples.append({"source": file_path, "line": line_no,
                             "data": data})
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    examples.append({"source": file_path, "line": line_no,
                                     "data": item})
                else:
                    not_examples.append(
                        (file_path, line_no, "list item is not an example"))
        else:
            not_examples.append(
                (file_path, line_no, "not a JSON object with an 'id' field"))
    return examples, not_examples


def load_folder(folder):
    """Load every example from a folder.

    Returns (examples, problems, not_examples, ignored, json_file_count).
    problems: list of (path, line, message) for malformed content.
    """
    json_files, ignored = collect_files(folder)
    examples, problems, not_examples = [], [], []
    for path in json_files:
        objects, errors = parse_file(path)
        for line_no, message in errors:
            problems.append((path, line_no, message))
        found, rejected = to_examples(path, objects)
        examples.extend(found)
        not_examples.extend(rejected)
    return examples, problems, not_examples, ignored, len(json_files)


# ----------------------------------------------------------------------
# Schema discovery
# ----------------------------------------------------------------------

def walk_numeric_leaves(data, prefix=""):
    """Yield (dotted_path, value) for every int/float (non-bool) leaf."""
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from walk_numeric_leaves(value, path)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            yield path, value


def field_presence(examples):
    """Count how many examples contain each top-level / nested field."""
    top = Counter()
    analysis_keys = Counter()
    input_keys = Counter()
    for example in examples:
        data = example["data"]
        top.update(data.keys())
        analysis = data.get("analysis")
        if isinstance(analysis, dict):
            analysis_keys.update(analysis.keys())
        input_obj = data.get("input")
        if isinstance(input_obj, dict):
            input_keys.update(input_obj.keys())
    return top, analysis_keys, input_keys


# ----------------------------------------------------------------------
# Review status (accepted / edited / rejected)
# ----------------------------------------------------------------------

def review_status(examples, raw_dir):
    """Estimate accepted / edited / unknown counts.

    Method 1 (if used): a correction marker key inside the example.
    Method 2 (default): compare each reviewed example with its raw
    counterpart — identical content means accepted, differences mean
    the human edited it. Read-only.
    Returns (accepted, edited, unknown, method_description).
    """
    marker_count = 0
    for example in examples:
        lowered = {key.lower() for key in example["data"]}
        if any(marker in lowered for marker in CORRECTION_MARKER_KEYS):
            marker_count += 1

    if marker_count > 0:
        return (0, marker_count, len(examples) - marker_count,
                "explicit correction marker found in examples")

    accepted = edited = unknown = compared = 0
    for example in examples:
        raw_path = raw_dir / example["source"].name
        if not raw_path.exists():
            unknown += 1
            continue
        try:
            raw_data = json.loads(raw_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            unknown += 1
            continue
        compared += 1
        if raw_data == example["data"]:
            accepted += 1
        else:
            edited += 1

    if compared == 0:
        method = ("no markers and no dataset/raw/ counterparts to compare — "
                  "cannot distinguish accepted vs edited")
    else:
        method = (f"content compared against dataset/raw/ "
                  f"({compared} counterpart(s) found)")
    return accepted, edited, unknown, method


# ----------------------------------------------------------------------
# Validation & consistency
# ----------------------------------------------------------------------

def validate_example(example):
    """Return a list of issue strings for one example."""
    issues = []
    data = example["data"]

    if data.get("task") not in ALLOWED_TASKS:
        issues.append(f"task type not in expected set: {data.get('task')!r}")
    if data.get("judgment") not in ALLOWED_JUDGMENTS:
        issues.append(f"judgment not in {ALLOWED_JUDGMENTS}: "
                      f"{data.get('judgment')!r}")

    analysis = data.get("analysis")
    if isinstance(analysis, dict):
        for key, value in analysis.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                issues.append(f"analysis.{key} is not numeric: {value!r}")
            elif not MIN_SCORE <= value <= MAX_SCORE:
                issues.append(f"analysis.{key} outside range "
                              f"{MIN_SCORE}-{MAX_SCORE}: {value}")
    else:
        issues.append("'analysis' is missing or not an object")

    if not data.get("status"):
        issues.append("'status' is missing or empty")

    input_obj = data.get("input")
    if not isinstance(input_obj, dict) or not input_obj:
        issues.append("'input' is missing or empty")
    return issues


def find_duplicates(examples):
    """Return (exact_duplicates, id_collisions).

    exact_duplicates: [(id, file_count)] — same id AND identical content.
    id_collisions:    [(id, file_count, content_variants)] — same id,
                      different content (worse: two different examples
                      sharing one id).
    """
    id_map = defaultdict(list)
    for example in examples:
        id_map[example["data"]["id"]].append(example)

    exact_duplicates, collisions = [], []
    for example_id, entries in id_map.items():
        contents = defaultdict(list)
        for entry in entries:
            content = json.dumps(entry["data"], sort_keys=True,
                                 ensure_ascii=False)
            contents[content].append(entry)
        if len(entries) > 1:
            if len(contents) == 1:
                exact_duplicates.append((example_id, len(entries)))
            else:
                collisions.append((example_id, len(entries),
                                   len(contents)))
    return exact_duplicates, collisions


def majority_analysis_set(examples):
    """Return (key_set, share) of the most common analysis key layout."""
    layouts = Counter()
    for example in examples:
        analysis = example["data"].get("analysis")
        if isinstance(analysis, dict):
            layouts[frozenset(analysis.keys())] += 1
    if not layouts:
        return None, 0.0
    best_set, count = layouts.most_common(1)[0]
    return best_set, count / sum(layouts.values())


# ----------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------

def print_score_table(numeric_stats):
    """Print average / min / max for every numeric field found."""
    if not numeric_stats:
        print("  (no numeric fields found)")
        return
    print(f"  {'field':<30}{'count':>7}{'min':>6}{'max':>6}{'avg':>8}")
    print("  " + "-" * 57)
    for path in sorted(numeric_stats):
        values = numeric_stats[path]
        count = len(values)
        average = sum(values) / count
        print(f"  {path:<30}{count:>7}{min(values):>6}"
              f"{max(values):>6}{average:>8.2f}")


def print_assessment(items):
    """Print the internal-consistency bullet list."""
    if not items:
        print("  (no issues found)")
    for item in items:
        print(f"  - {item}")


def classify(total, problems, not_examples, missing_count, issue_count,
             out_of_range_count, accepted, edited, unknown, correction_method,
             uniform_top, uniform_analysis, task_count):
    """Return (verdict, reasons) based on the documented criteria."""
    reasons = []

    if problems:
        return "NOT READY", [f"{len(problems)} malformed file(s) found"]
    if total == 0:
        return "NOT READY", ["no valid examples found"]
    if uniform_top < 0.7 or uniform_analysis < 0.7:
        return "NOT READY", [
            f"major schema inconsistency: only {uniform_top * 100:.0f}% share "
            f"the top-level layout, {uniform_analysis * 100:.0f}% share the "
            "analysis layout"]
    if missing_count > 0.2 * total:
        return "NOT READY", [
            f"{missing_count}/{total} examples are missing important fields"]
    if out_of_range_count:
        return "NOT READY", [
            f"{out_of_range_count} example(s) contain score(s) outside 1-10"]
    if edited + accepted == 0 and correction_method != "none":
        return "NOT READY", ["no evidence of human review in the data"]

    if (uniform_top >= 0.9 and uniform_analysis >= 0.9
            and issue_count == 0 and not not_examples
            and (edited > 0 or accepted > 0)
            and total >= MIN_EXAMPLES_FOR_READY
            and task_count >= 1):
        return "READY TO SCALE", [
            "consistent schema across all examples",
            "all examples structurally valid",
            "human review/corrections are preserved",
            f"{total} examples >= {MIN_EXAMPLES_FOR_READY} threshold"]

    reasons.append(
        "schema is usable and review is present, but the dataset is small "
        f"({total} example(s) vs {MIN_EXAMPLES_FOR_READY} required for "
        "READY TO SCALE)")
    if uniform_top < 0.9 or uniform_analysis < 0.9:
        reasons.append("some examples deviate from the majority schema")
    if issue_count:
        reasons.append(f"{issue_count} example(s) have value issues "
                       "(unknown tasks, bad judgments, non-numeric scores)")
    return "PROMISING", reasons


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else REVIEWED_DIR
    if not folder.exists():
        print(f"[ERROR] folder not found: {folder}")
        return

    examples, problems, not_examples, ignored, json_file_count = (
        load_folder(folder))
    total = len(examples)
    n = max(total, 1)

    print("=" * 60)
    print("  DATASET ANALYZER  (V0.4)")
    print("  Folder: " + str(folder))
    print("=" * 60)

    # 1. Total
    section("1. TOTAL REVIEWED EXAMPLES")
    print(f"  Examples found:     {total}")
    print(f"  JSON files scanned: {json_file_count}")
    if ignored:
        print(f"  Unrelated files ignored: {len(ignored)} "
              f"({', '.join(p.name for p in ignored)})")

    # 2-4. Review status
    section("2-4. REVIEW STATUS  (accepted / edited / rejected)")
    accepted, edited, unknown, method = review_status(examples, RAW_DIR)
    print(f"  Accepted:  {accepted}")
    print(f"  Edited:    {edited}")
    print(f"  Unknown:   {unknown}")
    print("  Rejected examples stored in this folder: 0 "
          "(by design, rejections stay in dataset/raw/ and never enter "
          "dataset/reviewed/)")
    print(f"  How detected: {method}")

    # 5-6. Score statistics
    section("5-6. SCORE STATISTICS  (average / min / max per field)")
    numeric_stats = defaultdict(list)
    for example in examples:
        for path, value in walk_numeric_leaves(example["data"]):
            numeric_stats[path].append(value)
    print_score_table(numeric_stats)

    # 7. Human corrections
    section("7. EXAMPLES WITH HUMAN CORRECTIONS")
    if method.startswith("explicit"):
        print(f"  {edited} example(s) carry a correction marker")
    elif method.startswith("content compared"):
        print(f"  {edited} example(s) differ from their raw draft "
              "(human-edited during review)")
        print(f"  {accepted} example(s) are identical to their raw draft")
    else:
        print("  Undetectable: no marker is stored and no raw counterpart "
              "exists to compare against")

    # 8. Missing fields
    section("8. MISSING FIELDS")
    top_counts, analysis_counts, input_counts = field_presence(examples)
    majority_analysis, _ = majority_analysis_set(examples)
    missing_report = []  # (example_id, [field, ...])
    for example in examples:
        data = example["data"]
        missing = [field for field in CORE_TOP_LEVEL_FIELDS
                   if field not in data]
        if isinstance(data.get("analysis"), dict) and majority_analysis:
            missing.extend(
                f"analysis.{key}" for key in majority_analysis
                if key not in data["analysis"])
        if missing:
            missing_report.append((data["id"], missing))
    if missing_report:
        for example_id, missing in missing_report:
            print(f"  {example_id}: missing {', '.join(missing)}")
        print(f"  Total examples with missing fields: "
              f"{len(missing_report)}/{total}")
    else:
        print("  None — every example contains the expected fields")

    # 9. Malformed / invalid files
    section("9. MALFORMED / INVALID FILES")
    if problems:
        for path, line_no, message in problems:
            where = f"line {line_no}" if line_no else "whole file"
            print(f"  {path.name} ({where}): {message}")
    else:
        print("  None")
    if not_examples:
        print(f"  Parseable but NOT examples ({len(not_examples)}):")
        for path, line_no, reason in not_examples:
            print(f"    {path.name}: {reason}")

    # 10. Duplicates
    section("10. DUPLICATES")
    exact_duplicates, collisions = find_duplicates(examples)
    if exact_duplicates:
        for example_id, count in exact_duplicates:
            print(f"  Exact duplicate: '{example_id}' appears {count} time(s) "
                  "with identical content")
    if collisions:
        for example_id, count, variants in collisions:
            print(f"  ID collision: '{example_id}' appears {count} time(s) "
                  f"with {variants} different content(s)")
    if not exact_duplicates and not collisions:
        print("  None — every id is unique")

    # 11. Task types
    section("11. TASK TYPES REPRESENTED")
    task_counts = Counter(e["data"].get("task") for e in examples)
    for task, count in task_counts.most_common():
        print(f"  {count:>3}  {task}")
    unknown_tasks = [task for task in task_counts
                     if task not in ALLOWED_TASKS]
    if unknown_tasks:
        print(f"  WARNING: unexpected task types: {unknown_tasks}")

    # 12. All fields found
    section("12. ALL FIELDS FOUND ACROSS THE DATASET")
    all_fields = set(top_counts)
    all_fields.update(f"analysis.{key}" for key in analysis_counts)
    all_fields.update(f"input.{key}" for key in input_counts)
    for field in sorted(all_fields):
        if "." in field:
            parent, key = field.split(".", 1)
            count = analysis_counts[key] if parent == "analysis" \
                else input_counts[key]
        else:
            count = top_counts[field]
        print(f"  {field:<30} present in {count}/{total}")

    # 13. Internal consistency
    section("13. INTERNAL CONSISTENCY ASSESSMENT")
    top_layouts = Counter(
        frozenset(e["data"].keys()) for e in examples)
    majority_top, _ = top_layouts.most_common(1)[0]
    uniform_top = top_layouts[majority_top] / n
    uniform_analysis = majority_analysis_set(examples)[1]

    issues, score_issues, out_of_range = [], 0, 0
    for example in examples:
        for issue in validate_example(example):
            issues.append(f"{example['data'].get('id')}: {issue}")
            if "outside range" in issue:
                out_of_range += 1
            if "not numeric" in issue:
                score_issues += 1

    assessment = []
    assessment.append(f"{uniform_top * 100:.0f}% of examples share the "
                      "same top-level field layout")
    assessment.append(f"{uniform_analysis * 100:.0f}% of examples share "
                      "the same analysis score layout")
    if top_layouts.most_common(1)[0][0] == majority_top and \
            len(top_layouts) > 1:
        assessment.append(f"{len(top_layouts) - 1} other top-level "
                          "layout(s) exist")
    if issues:
        assessment.extend(f"issue: {issue}" for issue in issues[:10])
    print_assessment(assessment)

    # Final verdict
    verdict, verdict_reasons = classify(
        total, problems, not_examples, len(missing_report), len(issues),
        out_of_range, accepted, edited, unknown, method,
        uniform_top, uniform_analysis, len(task_counts))

    section("FINAL VERDICT")
    print(f"  >>> {verdict} <<<")
    for reason in verdict_reasons:
        print(f"      - {reason}")


if __name__ == "__main__":
    main()
