# BlenderLLM V0.4

A small local AI terminal chat application for Windows that talks to your
locally installed [Ollama](https://ollama.com) server. It is the fourth step
of a project that will eventually become a Blender-specialist assistant:
V0.1 added the terminal chat, V0.2 added a Blender knowledge/context layer,
V0.3 added Blender Python generation, V0.4 adds a controlled execution
bridge that runs user-approved generated scripts inside a local Blender
5.2 instance.

## 1. What BlenderLLM is

BlenderLLM is a plain Python terminal chat:

1. Connects to your local Ollama server.
2. Uses a local model (default `qwen2.5-coder:14b`).
3. Lets you type messages in the terminal.
4. Sends the conversation to the model.
5. Streams the model's reply to your screen.
6. Keeps conversation history during the session.
7. Injects relevant Blender reference knowledge into the prompt when a
   question matches a knowledge topic (V0.2).
8. Detects Blender Python requests and guides the model toward a
   structured PLAN / BLENDER PYTHON / NOTES response (V0.3).
9. Can send user-approved generated scripts to a local Blender via the
   execution bridge (`/execute`, V0.4) — never automatically.
10. Exits cleanly with `/exit`.

There is no GUI, no web interface, and no autonomous agents. Code execution
happens ONLY when the user explicitly confirms it.

## 2. Requirements

- Windows (this project targets the current machine) with Python 3.11+
- [Ollama](https://ollama.com/download) installed and running
- The model `qwen2.5-coder:14b` pulled into Ollama (see section 4)
- The `ollama` Python package (see section 5)
- **Blender 5.2** (only needed for the V0.4 execution bridge)
- Python's standard library only beyond that — no extra dependencies

## 3. How Ollama must be running

Start Ollama before launching BlenderLLM, either by:

- running the Ollama desktop app, or
- running `ollama serve` in a terminal.

The default server address is `http://localhost:11434` (the standard local
Ollama server). If your server is somewhere else, change `OLLAMA_HOST` in
`config.py`.

## 4. The default model

The default model is `qwen2.5-coder:14b`. It is defined in **one place**:
`config.py` (`MODEL`). Change it there if you want another model.

If it is not installed yet, pull it first:

```text
ollama pull qwen2.5-coder:14b
```

## 5. Install dependencies

The only dependency is the official Ollama Python client:

```text
pip install -r requirements.txt
```

(which installs the `ollama` package). You should be in the project folder
`E:\Assistant\AI_Models\blenderllm` when you run it.

## 6. How to run BlenderLLM

From the project folder:

```text
python main.py
```

You should see:

```text
BlenderLLM V0.4
Model: qwen2.5-coder:14b
Ollama: connected
Knowledge: 18 topics loaded
Type /help for commands, /exit to quit.
```

If Ollama is not running, or the model is missing, you will get a clear
message instead of a crash. If the knowledge folder is missing or empty,
BlenderLLM still runs — it just answers without the knowledge layer.

Commands:

| Command       | What it does                        |
|---------------|-------------------------------------|
| `/exit`       | Exit BlenderLLM                     |
| `/clear`      | Reset the current conversation      |
| `/knowledge`  | Show knowledge layer status and the list of topics |
| `/execute`    | Execute the last generated Blender Python in Blender (asks for explicit confirmation) |
| `/help`       | Show the command list               |

You can also quit with Ctrl+C or Ctrl+Z (EOF) at the `You:` prompt. Pressing
Ctrl+C while the model is replying cancels the reply (the partial reply is
discarded).

## 6b. What V0.2 adds: the Blender knowledge/context layer

The knowledge layer gives the model relevant Blender reference material
when it is answering a Blender question. This is **prompt-time context**,
NOT fine-tuning or training — the model itself is never modified.

### How the knowledge base works

- Knowledge lives in small, manually curated Markdown files in the
  `knowledge/` folder (18 topics: bpy data API, objects and collections,
  meshes, materials, modifiers, cameras, lights, rendering, transforms,
  modes and context, operators vs data API, common mistakes, plus
  generation-focused topics: script structure, object creation patterns,
  material/node patterns, procedural geometry).
- Each file starts with a keyword line, e.g.:

  ```markdown
  keywords: material, materials, principled bsdf, shader
  ```

  The rest of the file is the reference text (with a `# Title` heading).
- These files are reference material. They are loaded at startup and read
  at selection time — they are never used to train or fine-tune anything.

### How context selection works (keyword matching)

For every user message, the knowledge layer:

1. Lowercases the question.
2. Counts how many of each file's keywords appear in it (plain substring
   match — no embeddings, no vector database).
3. Ranks the files by number of matches and takes the best ones
   (`MAX_CONTEXT_FILES`, default 3).
4. Formats them into a clearly separated reference block, capped at
   `MAX_CONTEXT_CHARS` characters (default 4000).
5. If nothing matches, no knowledge is sent at all — the model gets only
   the system instructions and the conversation.

Example: *"how do I change the camera lens?"* matches the `cameras.md`
keywords (camera, lens) and that file is the only knowledge injected.
A question about nothing Blender-related sends no knowledge.

### Prompt architecture

Each request to the model is assembled in this order:

1. System instructions (the BlenderLLM system prompt — always present).
2. Retrieved Blender knowledge (only when something matches), introduced
   with a note that it is reference material and that the system
   instructions always take priority.
3. Blender Python format guidance (V0.3, only for code requests — see
   section 6c).
4. Conversation history.
5. The current user request.

### How to add another Blender knowledge topic

1. Create `knowledge/<topic-name>.md`.
2. First line: `keywords: word1, word2, word3` (lowercase, comma-separated;
   these drive selection).
3. Then a `# Title` line and the reference content.
4. Restart BlenderLLM. `/knowledge` shows the updated topic list.

If a file has no keyword line, the words of its file name are used instead.

### What V0.2 does NOT do

- No embeddings, no vector database, no RAG pipeline, no LangChain/LlamaIndex.
- No web scraping or downloaded documentation sets — everything is the
  hand-written `knowledge/` folder.
- No fine-tuning, LoRA, or any modification of the model.
- No code execution, Blender control, or any new ability for the model —
  it still only produces text.

## 6c. What V0.3 adds: Blender Python generation

V0.3 makes BlenderLLM good at producing Blender Python code. It is
**generation only**: generating code never executes anything. Execution is
a separate, explicit step added by V0.4 (`/execute` — see section 6d).

### How Blender Python generation works

1. **Request detection** (`generation.py::is_code_request`): a lightweight,
   keyword-based check decides whether the user is asking for Blender
   Python. Strong signals (`script`, `code`, `blender python`, `write a`,
   `generate`, `procedural`, `debug`, `fix`, ...) mark a code request even
   inside a question; question-style openings (`what is`, `how do i`,
   `explain`, ...) mark conceptual questions; a weak signal (`create a`,
   `add a`, `nodes`, ...) alone is treated as a code request.
2. **Format guidance**: for code requests, a short block is appended to the
   system content telling the model to answer in a fixed structure
   (`PLAN` / `BLENDER PYTHON` / `NOTES`). Conceptual questions get no such
   guidance and are answered normally.
3. **Knowledge**: the same V0.2 keyword-based knowledge layer injects
   relevant reference topics (for example the procedural geometry or
   material/node pattern files for a "make a staircase" request).
4. **Code extraction** (`generation.py::extract_python_code`): the python
   fenced code block is pulled cleanly out of a model reply, separated
   from the explanation and notes — the V0.4 `/execute` flow sends exactly
   this extracted code, never the full natural-language response.

### Expected response structure (for code requests)

```text
PLAN
Short explanation of what the script will do.

BLENDER PYTHON
```python
import bpy
# ... the complete script ...
```

NOTES
Important assumptions, Blender version considerations, usage notes.
```

The model is told to use this structure only when it actually provides
code, to never claim the code was executed or tested, and to say when it
is unsure about a Blender API.

### How Blender knowledge is used in generation

The V0.2 knowledge base was extended with 4 focused topics used mostly by
code requests: `script-structure` (cleanup/reset and idempotency patterns),
`object-creation-patterns`, `material-node-patterns`, and
`procedural-geometry-patterns`. Selection still works exactly as in V0.2 —
no embeddings, no vector database.

### User-provided code

Users can paste Blender Python and ask for debugging. The model reads the
code, explains likely problems, and provides corrected code in the same
structured format. It never executes the supplied code.

### V0.3 does NOT execute anything

- The generation pipeline has no shell execution, no filesystem execution,
  and no tool access.
- Generated code is displayed to the user only, and the app notes when a
  runnable script is available (`[generated code ready - type /execute to
  run it in Blender]`).
- Execution exists only through the explicit V0.4 bridge flow (section 6d).

## 6d. What V0.4 adds: the Blender execution bridge

V0.4 is a **controlled execution bridge**, not an autonomous Blender agent.
It connects the terminal app to a local, user-approved Blender 5.2 instance
so that generated scripts can actually create things in Blender — but ONLY
when the user explicitly triggers and confirms execution.

The flow is:

```text
User request
 ↓
LLM generates code (V0.3, displayed with PLAN/BLENDER PYTHON/NOTES)
 ↓
User types /execute
 ↓
BlenderLLM shows a warning and asks for confirmation [y/N]
 ↓
On 'y': the extracted python code is sent to the bridge
 ↓
Blender executes it on the main thread
 ↓
Bridge returns SUCCESS or ERROR (type + message + traceback)
 ↓
BlenderLLM displays the result
```

### Communication mechanism

- A Blender add-on (`blender_bridge_addon.py`) runs a tiny TCP server bound
  ONLY to `127.0.0.1` (localhost) on port `41987` (configurable in
  `config.py`).
- The terminal app connects to it via `blender_bridge.py`, sends the script
  as a one-line JSON message (`{"script": "..."}`), and reads back a JSON
  result line: `{"status": "SUCCESS", "stdout", "stderr"}` or
  `{"status": "ERROR", "error_type", "error", "traceback"}`.
- Scripts run on Blender's main thread through a `bpy.app.timers` callback,
  so Blender data is only ever touched from the main thread (the socket
  thread only queues work).
- No shell commands, no remote access, no web server.

### Install/enable the bridge in Blender 5.2

1. Open Blender 5.2.
2. `Edit > Preferences > Add-ons > Install...`, select
   `blender_bridge_addon.py` from the project folder.
3. Tick the checkbox to enable **BlenderLLM Bridge**.
4. Press `F3`, search **"Start BlenderLLM Bridge"**, press Enter.
   (The status bar reports the listening address `127.0.0.1:41987`.)
5. To stop it: `F3` -> **"Stop BlenderLLM Bridge"** (it also stops when
   Blender closes).

### How /execute works

1. Ask for a script, e.g. `Create a cube in Blender.` The model replies
   with PLAN / BLENDER PYTHON / NOTES and the app notes that code is ready.
2. Type `/execute`.
3. BlenderLLM prints:

   ```text
   WARNING: This will execute Python inside Blender.
   Execute the last generated script? [y/N]:
   ```

4. Only `y`/`yes` sends the script. Anything else cancels.
5. Result: `Blender: execution successful.` (plus any printed output) or
   `Blender: execution failed.` with the error type, message, and
   traceback.
6. If there is no generated code yet, `/execute` says so instead.
7. `/clear` also discards the pending generated code.

### Security limitations (V0.4)

- Execution NEVER happens automatically after generation.
- The bridge binds to `127.0.0.1` only — no remote machine can connect.
- The user must explicitly type `/execute` AND confirm with `y`.
- The bridge cannot run shell commands or touch the filesystem on its own;
  it only runs scripts the user sent it.
- This is a privileged capability: only run scripts you trust, because
  Blender Python can modify the .blend file.

### Troubleshooting

| Problem | What to do |
|---|---|
| `Blender bridge error: cannot reach the Blender bridge at 127.0.0.1:41987` | Blender is not running, or the add-on is not enabled/started. Open Blender 5.2, enable the add-on, `F3` -> Start BlenderLLM Bridge. |
| `Blender: execution failed.` | The script raised an exception inside Blender. The bridge returns the exception type, message, and traceback — paste it back to BlenderLLM for debugging. |
| Script does nothing / wrong result | The script ran successfully but did what it was told. Ask BlenderLLM to modify the script and `/execute` again. |
| `no response from the Blender bridge within 15 seconds` | Blender is busy or the bridge stalled; retry, or raise `BLENDER_BRIDGE_TIMEOUT` in `config.py`. |
| Port already in use | The bridge start operator reports the failure; stop any other instance or change `BLENDER_BRIDGE_PORT` in `config.py` and the port constant in `blender_bridge_addon.py` (keep both in sync). |


## 7. Example conversation

```text
BlenderLLM V0.4
Model: qwen2.5-coder:14b
Ollama: connected
Knowledge: 18 topics loaded
Type /help for commands, /exit to quit.

You: What is bpy?

BlenderLLM: bpy is Blender's Python module. It exposes Blender's
application data and operators, so you can build and edit scenes
with Python...

You: Create a cube in Blender.

BlenderLLM: PLAN
Add a default cube at the origin using a data-API approach.

BLENDER PYTHON
```python
import bpy

mesh = bpy.data.meshes.new("CubeMesh")
mesh.from_pydata([(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
                  (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)],
                 [], [(0, 1, 2, 3), (4, 5, 6, 7),
                      (0, 1, 5, 4), (1, 2, 6, 5),
                      (2, 3, 7, 6), (3, 0, 4, 7)])
mesh.update()
obj = bpy.data.objects.new("Cube", mesh)
bpy.context.scene.collection.objects.link(obj)
```

NOTES
Modern Blender (5.x); runs from the Scripting workspace or
--background mode. Idempotent: remove "Cube" first to rerun.

[generated code ready - type /execute to run it in Blender]

You: /execute

WARNING: This will execute Python inside Blender.
Execute the last generated script? [y/N]: y

Blender: execution successful.

You: /exit
Goodbye!
```

(The knowledge layer automatically injected the relevant topics for each
request — that is prompt-time context, not training. The cube appears in
Blender only after the explicit `/execute` + `y` confirmation.)

## 8. Current limitations

- Only chat in the terminal. No GUI or web interface.
- The knowledge base is small (18 hand-written topics) and selection is
  simple keyword matching: a question that does not mention a topic's
  keywords gets no knowledge, even when a human would say it is related.
- The model cannot verify its own code: it cannot run, test, or check
  generated scripts, and it must never claim it did. Execution is only the
  explicit `/execute` flow, and there is no automatic error correction.
- Code-request detection is keyword-based: it can occasionally tag a
  borderline question as a code request or miss an unusual phrasing. The
  guidance it adds is soft, so this is not fatal.
- Execution requires Blender 5.2 to be open with the bridge started; the
  bridge returns only success/error text — no screenshots, no scene
  inspection, no iteration.
- Conversation history lives only in memory and is lost when you exit.

## 9. What is intentionally NOT implemented yet

Later versions will add, one step at a time, and V0.4 deliberately does
**not** include any of it:

- V0.5 — screenshots and vision analysis
- V0.6 — automatic iteration (generate → render → check → fix)
- V0.7 — dataset generation
- V0.8 — human review workflow
- V0.9 — fine-tuning
- V1.0 — Nova/JARVIS integration

Within the knowledge area specifically, V0.4 still uses keywords only —
real semantic retrieval (embeddings, vector database, full RAG) is future
work, as is any form of training or fine-tuning.

Also not implemented: LangChain, LlamaIndex, autonomous agents, tool
calling frameworks, web scraping, cloud APIs, plugins, screenshots, vision,
automatic error-repair loops, and any ability for the model to execute
shell commands, modify arbitrary files, or control the computer.

## Project structure

```text
blenderllm/
  main.py               entry point: terminal chat loop, commands
                        (incl. /execute with explicit confirmation),
                        assembles system + knowledge + guidance +
                        history + request
  config.py             one place for model, Ollama host, knowledge
                        settings, and bridge settings
  system_prompt.py      the BlenderLLM system prompt and the V0.3
                        Blender Python format guidance
  generation.py         V0.3 generation support: code-request detection
                        and python code block extraction
  blender_bridge.py     V0.4 bridge client: sends scripts to Blender over
                        localhost, parses SUCCESS/ERROR results, clear
                        error messages for bridge-down/timeout/malformed
  blender_bridge_addon.py  V0.4 Blender 5.2 add-on: localhost TCP
                        listener, main-thread execution via bpy.app.timers,
                        start/stop operators (F3)
  ollama_client.py      thin wrapper around the Ollama Python client,
                        translates library errors into simple messages
  knowledge.py          V0.2 knowledge layer: loads Markdown files,
                        keyword matching, context selection and formatting
  knowledge/            18 curated Blender reference topics (Markdown)
    blender-python-overview.md
    bpy-data-api.md
    objects-and-collections.md
    scenes.md
    meshes.md
    materials.md
    modifiers.md
    cameras.md
    lights.md
    rendering.md
    transforms.md
    modes-and-context.md
    operators-vs-data-api.md
    common-mistakes.md
    script-structure.md          (V0.3: cleanup/reset, idempotency)
    object-creation-patterns.md  (V0.3)
    material-node-patterns.md    (V0.3)
    procedural-geometry-patterns.md (V0.3)
  requirements.txt      the single dependency: ollama
  tests/
    test_ollama_client.py  unit tests for the Ollama client (no server)
    test_knowledge.py      unit tests for the knowledge layer
    test_generation.py     unit tests for V0.3 detection/extraction/prompts
    test_blender_bridge.py unit tests for the V0.4 bridge client and
                           /execute flow (mocked sockets, no Blender)
  README.md             this file
```

## Running the tests

Tests need no Ollama server, no Blender, and make no network calls:

```text
python -m unittest discover tests -v
```
