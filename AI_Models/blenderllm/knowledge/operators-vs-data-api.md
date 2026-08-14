keywords: bpy.ops, operator, operators, direct api, data api, undo, idempotent
# Operators vs. the Data API
- `bpy.ops.*` calls operators - the same functions the UI buttons call. They act on the current context and selection, are undo-able, and are slower.
- The data API (`bpy.data.*`, direct property changes) is fast, reliable, needs no selection, and has no undo.
- Rule of thumb: use the data API in scripts; use operators only for undo-able, user-facing actions or when no direct API exists.
- Examples: `bpy.ops.object.mode_set(mode='EDIT')`, `bpy.ops.mesh.primitive_cube_add()`.
- Operators can raise `RuntimeError` when the context is wrong (wrong mode, nothing selected, wrong object active).
- With the data API you control the active object explicitly: `bpy.context.view_layer.objects.active = obj`.
- Idempotency: check before creating, e.g. `bpy.data.objects.get("Name")`, to avoid `.001` duplicates when scripts run twice.
