keywords: mode, context, edit mode, object mode, active object, selected objects, bpy.context, context override
# Modes and Context
- `bpy.context` exposes the current UI state: `bpy.context.scene`, `bpy.context.object`, `bpy.context.active_object`, `bpy.context.selected_objects`.
- The active object can be read/written: `bpy.context.view_layer.objects.active = obj`.
- Switch modes: `bpy.ops.object.mode_set(mode='EDIT')`, `'OBJECT'`, `'SCULPT'`, `'POSE'`, ...
- Many operators need a specific mode:
  - `bpy.ops.mesh.*` requires edit mode on a mesh object.
  - `bpy.ops.object.*` usually requires object mode.
- Wrong context raises `RuntimeError: Operator ... context is incorrect`.
- Selection: `obj.select_set(True)`, `bpy.ops.object.select_all(action='DESELECT')`.
- Context overrides (passing a context dict to operators) exist but are fragile - avoid them unless necessary.
- In `--background` mode there is no real UI context: prefer the data API over operators.
