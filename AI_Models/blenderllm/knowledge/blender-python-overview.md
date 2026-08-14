keywords: bpy, blender python, python module, blender script, scripting
# Blender Python Overview
- `bpy` is Blender's Python API module. It only exists inside the Python interpreter bundled with Blender.
- Regular system Python (e.g. `python.exe`) cannot `import bpy` without a special bpy build; the normal way is running inside Blender.
- Ways to run Blender Python:
  - Scripting workspace -> Text Editor -> Run Script button.
  - Blender's Python Console.
  - From a terminal: `blender --background --python script.py`.
- Scripts run against the currently open .blend file: changes go into the live session's data.
- Blender version: `bpy.app.version`.
- A script is not an add-on: add-ons use `register`/`unregister` functions and live in add-on directories.
- `bpy.ops.*` calls UI-style operations; `bpy.data.*` and property changes are the direct data API (see the Operators vs. Data API topic).
