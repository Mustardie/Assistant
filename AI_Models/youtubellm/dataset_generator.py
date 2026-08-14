"""
Dataset Generator — V0.5

Builds training examples for the future fine-tuning of the YouTube AI.

Flow:
  user input
  -> Qwen3 8B (with the user's YouTube rules in context)
  -> structured JSON draft
  -> saved to dataset/raw/
  -> human review (Accept / Edit / Reject)
  -> accepted examples go to dataset/reviewed/

The model's draft is NEVER automatically treated as final training data.
Only examples you accept or correct become part of the reviewed dataset.

Modes:
  python dataset_generator.py           -> interactive generation + review
  python dataset_generator.py --review  -> review raw examples generated earlier
  python dataset_generator.py --batch   -> batch-generate many different raw
                                           candidates (duplicate-checked),
                                           then optionally review them

Exit with: quit, or Ctrl+C
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import knowledge
import ollama_client
import prompts

# Windows fix: make the terminal show Unicode correctly.
sys.stdout.reconfigure(encoding="utf-8")

PROJECT_DIR = Path(__file__).parent
RAW_DIR = PROJECT_DIR / "dataset" / "raw"
REVIEWED_DIR = PROJECT_DIR / "dataset" / "reviewed"
FINAL_DIR = PROJECT_DIR / "dataset" / "final"

# The four tasks supported in V0.3.
TASKS = {
    "1": {"task": "video_idea_evaluation", "input_key": "idea",
          "what": "video idea"},
    "2": {"task": "video_idea_generation", "input_key": "request",
          "what": "generation request (niche, style, constraints)"},
    "3": {"task": "title_evaluation", "input_key": "title",
          "what": "title"},
    "4": {"task": "hook_evaluation", "input_key": "hook",
          "what": "hook (opening lines of a video)"},
}
TASKS_BY_NAME = {info["task"]: info for info in TASKS.values()}

ALLOWED_JUDGMENTS = ("MAKE IT", "REWORK IT", "KILL IT")
SCORE_FIELDS = (
    "clickability",
    "curiosity",
    "originality",
    "story_potential",
    "retention_potential",
    "visual_potential",
    "payoff",
    "execution_difficulty",
)

# Batch mode: how many attempts per candidate slot (Stage 1 generation +
# Stage 2 similarity check) before giving up and letting the user decide.
BATCH_MAX_ATTEMPTS = 5


# ----------------------------------------------------------------------
# IDs and saving
# ----------------------------------------------------------------------

def ask(prompt):
    """input() that exits gracefully on EOF or Ctrl+C instead of
    dumping a Python traceback."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\nGoodbye!")
        raise SystemExit(0)


def next_example_id():
    """Return the next unused example ID, e.g. 'example_0004'.

    Scans both raw/ and reviewed/ so IDs never collide, even if an
    example was already reviewed.
    """
    highest = 0
    for folder in (RAW_DIR, REVIEWED_DIR):
        if not folder.exists():
            continue
        for file in folder.glob("example_*.jsonl"):
            try:
                number = int(file.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            highest = max(highest, number)
    return f"example_{highest + 1:04d}"


def save_example(example, folder):
    """Save one example as a single-line JSONL file. Returns the path.

    Uses exclusive mode ('x') so an existing file is never overwritten.
    """
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{example['id']}.jsonl"
    with open(path, "x", encoding="utf-8") as file:
        file.write(json.dumps(example, ensure_ascii=False) + "\n")
    return path


# ----------------------------------------------------------------------
# Generation
# ----------------------------------------------------------------------

def build_prompt(task_name, user_input, knowledge_text):
    """Combine instructions + task + user input + knowledge into one prompt."""
    task_instructions = prompts.DATASET_TASKS[task_name]
    return f"""{prompts.DATASET_JSON_INSTRUCTION}

TASK: {task_name}
{task_instructions}

USER'S INPUT TO WORK WITH:
{user_input}

THE USER'S YOUTUBE RULES AND EXAMPLES:
{knowledge_text}
"""


def extract_json(reply):
    """Parse the model's reply as JSON, tolerating markdown fences."""
    text = reply.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


def validate_example(data):
    """Check that the model's JSON has every required field.

    Raises ValueError with a specific message if something is wrong.
    """
    if not isinstance(data, dict):
        raise ValueError("Model output is not a JSON object.")

    analysis = data.get("analysis")
    if not isinstance(analysis, dict):
        raise ValueError("Missing 'analysis' object.")

    for field in SCORE_FIELDS:
        value = analysis.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"Score '{field}' must be an integer from 1 to 10.")
        if not 1 <= value <= 10:
            raise ValueError(f"Score '{field}' must be between 1 and 10.")

    if data.get("judgment") not in ALLOWED_JUDGMENTS:
        raise ValueError(
            f"'judgment' must be exactly one of {ALLOWED_JUDGMENTS}."
        )

    for field in ("reasoning", "improved_version", "improvement_reasoning"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"'{field}' must be a non-empty string.")


def generate_example(example_id, task_name, user_input, knowledge_text):
    """Ask the model for one structured example. Retries once if the
    JSON is invalid. Returns a complete example dict.
    """
    prompt = build_prompt(task_name, user_input, knowledge_text)
    messages = [{"role": "system", "content": prompt}]

    last_error = None
    for attempt in range(1, 3):
        try:
            reply = ollama_client.send_messages(messages)
        except Exception as error:
            raise RuntimeError(f"Ollama request failed: {error}") from error

        try:
            data = extract_json(reply)
            validate_example(data)
            break
        except (json.JSONDecodeError, ValueError) as error:
            last_error = error
            if attempt == 2:
                raise RuntimeError(
                    "Could not parse the model's JSON after 2 attempts.\n"
                    f"Last error: {last_error}\n\n"
                    f"Model's raw output:\n{reply}"
                ) from error

    return {
        "id": example_id,
        "task": task_name,
        "input": {TASKS_BY_NAME[task_name]["input_key"]: user_input},
        "analysis": data["analysis"],
        "judgment": data["judgment"],
        "reasoning": data["reasoning"],
        "improved_version": data["improved_version"],
        "improvement_reasoning": data["improvement_reasoning"],
        "status": "raw",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ----------------------------------------------------------------------
# Review
# ----------------------------------------------------------------------

def show_example(example):
    print("\n" + "-" * 56)
    print(json.dumps(example, ensure_ascii=False, indent=2))
    print("-" * 56)


def edit_score(label, current):
    """Let the user change one 1-10 score. Enter alone keeps the value.

    After every invalid attempt the full question (field name, current
    value, accepted range) is reprinted with a visible prompt, so the
    loop always shows which field is being edited and how to escape it.
    """
    question = (f"{label} (current: {current}) — number 1-10, "
                "Enter to keep")
    print(f"\n{question}:")
    while True:
        line = ask("  -> ").strip()
        if line == "":
            return current
        try:
            value = int(line)
        except ValueError:
            print(f"'{line}' is not a number. {question}:")
            continue
        if 1 <= value <= 10:
            return value
        print(f"'{line}' is not between 1 and 10. {question}:")


def edit_judgment(current):
    """Let the user change the judgment. Enter alone keeps the value."""
    question = (f"judgment (current: {current}) — "
                "MAKE IT / REWORK IT / KILL IT")
    print(f"\n{question}:")
    while True:
        line = ask("  -> ").strip()
        if line == "":
            return current
        if line in ALLOWED_JUDGMENTS:
            return line
        print(f"'{line}' is not a valid judgment. {question}:")


def edit_text(label, current):
    """Let the user replace one text field with a single line.

    Enter alone keeps the current value. This is a one-shot prompt —
    no hidden multi-line loop — so every field is processed exactly
    once and can never get stuck re-asking.
    """
    print(f"\n{label}")
    print(f"CURRENT: {current}")
    print("Type the new value, or press Enter alone to keep it:")
    line = ask("  -> ").strip()
    if line == "":
        return current
    return line


def edit_example(example):
    """Step through every editable field and return a corrected copy."""
    edited = dict(example)

    analysis = dict(edited["analysis"])
    print("\n--- EDIT SCORES ---")
    for field in SCORE_FIELDS:
        analysis[field] = edit_score(field, analysis[field])
    edited["analysis"] = analysis

    print("\n--- EDIT TEXT FIELDS ---")
    edited["judgment"] = edit_judgment(edited["judgment"])
    edited["reasoning"] = edit_text("reasoning", edited["reasoning"])
    edited["improved_version"] = edit_text(
        "improved_version", edited["improved_version"])
    edited["improvement_reasoning"] = edit_text(
        "improvement_reasoning", edited["improvement_reasoning"])
    return edited


def review_one(example):
    """Ask the user what to do with one example.

    Returns: "accepted" (copy saved to reviewed/), "rejected", "skipped".
    The raw file is never modified.
    """
    while True:
        choice = ask("\n[A]ccept  [E]dit  [R]eject  [S]kip  -> ").strip().upper()
        if choice in ("A", "ACCEPT"):
            example["status"] = "reviewed"
            path = save_example(example, REVIEWED_DIR)
            print(f"Accepted -> saved to {path}")
            return "accepted"
        if choice in ("E", "EDIT"):
            corrected = edit_example(example)
            corrected["status"] = "reviewed"
            path = save_example(corrected, REVIEWED_DIR)
            print(f"Corrected -> saved to {path}")
            return "accepted"
        if choice in ("R", "REJECT"):
            print("Rejected. It stays in dataset/raw/ only and will NOT "
                  "become training data.")
            return "rejected"
        if choice in ("S", "SKIP"):
            print("Skipped. It stays in dataset/raw/ — review it later "
                  "with: python dataset_generator.py --review")
            return "skipped"
        print("Please choose A, E, R, or S.")


# ----------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------

def run_generation_loop(knowledge_text):
    print("\nChoose a task:")
    for key, info in TASKS.items():
        print(f"  {key}. {info['task']}")

    while True:
        task_choice = ask("\nTask number (1-4), or 'quit' to exit: ").strip()
        if task_choice.lower() in ("quit", "exit"):
            print("Goodbye!")
            return

        task_info = TASKS.get(task_choice)
        if not task_info:
            print("Unknown task. Choose 1-4, or 'quit'.")
            continue

        user_input = ask(f"\nEnter the {task_info['what']}: ").strip()
        if not user_input:
            print("Empty input — nothing to analyze.")
            continue

        print(f"\nSending to {ollama_client.MODEL_NAME}... (can take a while)")
        try:
            example_id = next_example_id()
            example = generate_example(
                example_id, task_info["task"], user_input, knowledge_text)
        except RuntimeError as error:
            print(f"\n[ERROR] {error}")
            continue

        path = save_example(example, RAW_DIR)
        print(f"\nDraft saved to {path}")
        show_example(example)
        review_one(example)


def run_review_loop():
    """Review every raw example that has not been reviewed yet."""
    if not RAW_DIR.exists():
        print(f"No raw examples found ({RAW_DIR} does not exist). "
              "Generate some first.")
        return

    raw_files = sorted(RAW_DIR.glob("example_*.jsonl"))
    if not raw_files:
        print("No raw examples to review.")
        return

    reviewed_ids = {
        file.stem for file in REVIEWED_DIR.glob("example_*.jsonl")
    } if REVIEWED_DIR.exists() else set()

    count = 0
    for file in raw_files:
        if file.stem in reviewed_ids:
            continue
        try:
            example = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"\n[ERROR] Could not parse {file} — skipping. "
                  "Check the file manually.")
            continue
        count += 1
        print(f"\n=== {file.stem} ({file}) ===")
        show_example(example)
        review_one(example)

    if count == 0:
        print("All raw examples have already been reviewed.")


# ----------------------------------------------------------------------
# Batch generation
# ----------------------------------------------------------------------

def normalize_idea(text):
    """Lowercase, strip punctuation, collapse whitespace."""
    words = [ch for ch in text.strip().lower()
             if ch.isalnum() or ch.isspace()]
    return " ".join("".join(words).split())


def load_concept_inventory():
    """Compact concept info for every example already in the dataset
    (raw + reviewed): id, idea, improved_version.

    Used for conceptual-similarity checking and for the batch prompt.
    """
    entries = []
    for folder in (RAW_DIR, REVIEWED_DIR):
        if not folder.exists():
            continue
        for file in sorted(folder.glob("example_*.jsonl")):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            idea = (data.get("input") or {}).get("idea")
            if not isinstance(idea, str) or not idea.strip():
                continue
            improved = data.get("improved_version")
            entries.append({
                "id": file.stem,
                "idea": idea.strip(),
                "improved_version": (improved.strip() if isinstance(improved, str) else ""),
            })
    return entries


def format_concept_list(entries):
    """Render inventory entries as one compact line each for the prompt.

    Sends idea and improved_version (the concept, not the full analysis),
    truncated so the prompt stays small.
    """
    lines = []
    for entry in entries:
        idea = entry["idea"]
        if len(idea) > 160:
            idea = idea[:160] + "..."
        line = f"- {entry['id']}: {idea}"
        improved = entry.get("improved_version") or ""
        if improved.strip() and normalize_idea(improved) != normalize_idea(idea):
            if len(improved) > 120:
                improved = improved[:120] + "..."
            line += f"  (improved: {improved})"
        lines.append(line)
    return "\n".join(lines) or "(none)"


def find_duplicate(idea, used_texts, existing_texts):
    """Return the text a new idea duplicates, or None if it is new.

    Checks exact matches plus near-duplicates (one text containing the
    other), so "100 Days in the Nether" and "100 Days in the Nether
    (Hardcore)" both get caught.
    """
    norm = normalize_idea(idea)
    if not norm:
        return None
    for other in sorted(used_texts | existing_texts):
        other_norm = normalize_idea(other)
        if not other_norm:
            continue
        if norm == other_norm:
            return other
        if len(norm) >= 10 and (norm in other_norm or other_norm in norm):
            return other
    return None


def build_batch_prompt(used_ideas, inventory, knowledge_text):
    """One prompt asking the model for ONE new, distinct candidate."""
    used = "\n".join(f"- {idea}" for idea in sorted(used_ideas)) or "(none yet)"
    existing = format_concept_list(inventory)
    return (prompts.BATCH_INSTRUCTION
            .replace("$USED$", used)
            .replace("$EXISTING$", existing)
            .replace("$KNOWLEDGE$", knowledge_text))


def generate_batch_candidate(example_id, used_ideas, inventory,
                             knowledge_text, duplicate_note=None):
    """Ask the model for one batch candidate (idea + self-evaluation).

    Returns (example, idea). Retries once if the JSON is invalid.
    Raises RuntimeError if the model's output cannot be parsed.
    """
    prompt = build_batch_prompt(used_ideas, inventory, knowledge_text)
    if duplicate_note:
        prompt += ("\n\nYOUR LAST IDEA WAS REJECTED:\n" + duplicate_note +
                   "\nGenerate a genuinely different idea this time.")
    messages = [{"role": "system", "content": prompt}]

    last_error = None
    for attempt in range(1, 3):
        try:
            reply = ollama_client.send_messages(messages)
        except Exception as error:
            raise RuntimeError(f"Ollama request failed: {error}") from error

        try:
            data = extract_json(reply)
            validate_example(data)
            idea = data.get("idea")
            if not isinstance(idea, str) or not idea.strip():
                raise ValueError("Model output is missing the 'idea' field.")
            idea = idea.strip()
            break
        except (json.JSONDecodeError, ValueError) as error:
            last_error = error
            if attempt == 2:
                raise RuntimeError(
                    "Could not parse the model's JSON after 2 attempts.\n"
                    f"Last error: {last_error}\n\n"
                    f"Model's raw output:\n{reply}"
                ) from error

    return {
        "id": example_id,
        "task": "video_idea_evaluation",
        "input": {"idea": idea},
        "analysis": data["analysis"],
        "judgment": data["judgment"],
        "reasoning": data["reasoning"],
        "improved_version": data["improved_version"],
        "improvement_reasoning": data["improvement_reasoning"],
        "status": "raw",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, idea


def check_similarity(example, entries):
    """STAGE 2: ask the model whether a candidate's CORE CONCEPT is too
    similar to any existing idea (or to ideas saved earlier in this
    batch).

    Returns (too_similar: bool, similar_to: str|None, reason: str|None).
    If the model's verdict cannot be parsed after one retry, the
    candidate is allowed through with a visible warning.
    """
    prompt = (prompts.SIMILARITY_CHECK_INSTRUCTION
              .replace("$CANDIDATE$", format_concept_list(
                  [{"id": "candidate", "idea": example["input"]["idea"],
                    "improved_version": example["improved_version"]}]))
              .replace("$EXISTING$", format_concept_list(entries)))
    messages = [{"role": "system", "content": prompt}]

    last_error = None
    for attempt in range(1, 3):
        try:
            reply = ollama_client.send_messages(messages)
        except Exception as error:
            raise RuntimeError(f"Ollama request failed: {error}") from error

        try:
            data = extract_json(reply)
            if not isinstance(data.get("too_similar"), bool):
                raise ValueError("Missing boolean 'too_similar' field.")
            too_similar = data["too_similar"]
            similar_to = data.get("similar_to")
            reason = data.get("reason")
            if too_similar and not isinstance(similar_to, str):
                similar_to = "(unknown)"
            if not isinstance(reason, str):
                reason = None
            return bool(too_similar), similar_to, reason
        except (json.JSONDecodeError, ValueError) as error:
            last_error = error
            if attempt == 2:
                print("\n[WARNING] Could not parse the similarity check "
                      f"({last_error}) — saving the candidate anyway.\n"
                      f"Model's raw output:\n{reply}")

    return False, None, None


def generate_one_candidate(example_id, used_texts, inventory,
                           knowledge_text, batch_entries):
    """Generate one candidate with conceptual-duplicate protection.

    Stage 1: the model generates a candidate.
    Stage 2: the model judges whether its core concept is too similar to
    existing ideas (or to ideas saved earlier in this batch).

    Exact/near duplicates are caught deterministically before Stage 2
    (free — no extra model call).

    Returns ("saved", example, idea, rejections), ("skipped", None, None,
    rejections), or ("stopped", None, None, rejections). Nothing is
    saved by this function.
    """
    duplicate_note = None
    example = idea = None
    rejections = 0
    existing_texts = {entry["idea"] for entry in inventory}
    for attempt in range(1, BATCH_MAX_ATTEMPTS + 1):
        if attempt > 1:
            print(f"\nCandidate attempt {attempt}/{BATCH_MAX_ATTEMPTS}")

        try:
            example, idea = generate_batch_candidate(
                example_id, used_texts, inventory, knowledge_text,
                duplicate_note)
        except RuntimeError as error:
            print(f"\n[ERROR] {error}")
            choice = ask("\n[C]ontinue with the next candidate, or "
                         "[Q]uit the batch? -> ").strip().upper()
            if choice.startswith("Q"):
                return "stopped", None, None, rejections
            return "skipped", None, None, rejections

        match = find_duplicate(idea, used_texts, existing_texts)
        if match is not None:
            print(f"\n[DUPLICATE] The model proposed: '{idea}'")
            print(f"  This duplicates an existing idea: '{match}'")
            duplicate_note = (f'Your idea "{idea}" duplicates an idea that '
                              f'is already used: "{match}". Generate '
                              f"something genuinely different.")
            print("  Generating replacement...")
            continue

        print("  Checking conceptual similarity...")
        too_similar, similar_to, reason = check_similarity(
            example, inventory + batch_entries)

        if too_similar:
            rejections += 1
            print(f"\nToo similar to {similar_to}")
            print(f"Reason: {reason}")
            duplicate_note = (
                f'Your idea "{idea}" is too conceptually similar to '
                f'"{similar_to}": {reason}. Choose a genuinely different '
                f"creative direction — different premise, challenge, "
                f"setting, twist, or main mechanic.")
            print("Generating replacement...")
            continue

        return "saved", example, idea, rejections

    choice = ask("\nThe model kept producing similar ideas. [K]eep this "
                 "one anyway, or [S]kip it? -> ").strip().upper()
    if choice.startswith("K"):
        return "saved", example, idea, rejections
    return "skipped", None, None, rejections


def run_batch_loop(knowledge_text):
    """Batch mode: generate N different candidates, raw only."""
    print("\nBATCH CANDIDATE GENERATION")
    print("  Task: video_idea_evaluation (the only batch task for now)")
    print("  Everything is saved to dataset/raw/ as RAW ONLY.")
    print("  Nothing enters dataset/reviewed/ until you review it.\n")

    while True:
        line = ask("How many candidates should I generate? ").strip()
        if line.lower() in ("quit", "exit"):
            print("Goodbye!")
            return
        try:
            target = int(line)
        except ValueError:
            print(f"'{line}' is not a number. Try again.")
            continue
        if target < 1:
            print("At least 1 candidate.")
            continue
        if target > 50:
            confirm = ask(f"{target} candidates will take a while. "
                          "Continue anyway? (y/n): ").strip().lower()
            if confirm not in ("y", "yes"):
                continue
        break

    inventory = load_concept_inventory()
    print(f"\nConcept inventory: {len(inventory)} existing example(s) "
          "loaded from dataset/raw/ and dataset/reviewed/.")
    print("Every candidate is checked for CONCEPTUAL similarity before "
          "it is saved (Stage 2).")

    used_texts = set()
    batch_entries = []
    saved_count = 0
    rejections = 0
    for number in range(1, target + 1):
        print(f"\n{'=' * 56}")
        print(f"  Candidate {number}/{target}  "
              f"(model: {ollama_client.MODEL_NAME})")
        print(f"{'=' * 56}")

        status, example, idea, rejections_now = generate_one_candidate(
            next_example_id(), used_texts, inventory, knowledge_text,
            batch_entries)
        rejections += rejections_now
        if status == "stopped":
            print("Batch stopped early.")
            break
        if status == "skipped":
            print("Candidate skipped.")
            continue

        path = save_example(example, RAW_DIR)
        used_texts.add(idea)
        batch_entries.append({
            "id": example["id"],
            "idea": idea,
            "improved_version": example["improved_version"],
        })
        saved_count += 1
        print(f"\nSaved raw candidate -> {path}")
        print(f"  idea:      {idea}")
        print(f"  judgment:  {example['judgment']}")
        print(f"  reasoning: {example['reasoning']}")

    print(f"\n{'=' * 56}")
    print("  BATCH RESULT")
    print(f"  Generated: {saved_count}")
    print(f"  Similarity rejections: {rejections}")
    print(f"  Saved: {saved_count}")
    print(f"{'=' * 56}")
    if saved_count == 0:
        return

    review_now = ask("\nReview the candidates now? (y/n): ").strip().lower()
    if review_now in ("y", "yes"):
        run_review_loop()
    else:
        print("Review them later with: "
              "python dataset_generator.py --review")


def main():
    print("=" * 56)
    print("  DATASET GENERATOR  (V0.5)")
    print(f"  Model: {ollama_client.MODEL_NAME}")
    print("=" * 56)

    try:
        knowledge_text = knowledge.load_knowledge()
    except FileNotFoundError as error:
        print(f"\n[ERROR] {error}")
        return

    if "--review" in sys.argv:
        run_review_loop()
        return

    if "--batch" in sys.argv:
        run_batch_loop(knowledge_text)
        return

    try:
        ollama_client.verify_setup()
    except (ConnectionError, LookupError) as error:
        print(f"\n[ERROR] {error}")
        return

    print(f"\nKnowledge loaded: {len(knowledge_text):,} characters.")
    run_generation_loop(knowledge_text)


if __name__ == "__main__":
    main()
