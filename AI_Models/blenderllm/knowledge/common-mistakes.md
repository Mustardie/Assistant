keywords: mistake, common mistake, troubleshooting, error, not working, debug, gotcha, help
# Common Blender Python Mistakes
- Forgetting to link new objects: `bpy.context.scene.collection.objects.link(obj)`. The object exists in memory but is not in the scene until linked.
- Radians vs degrees: `rotation_euler` uses radians; use `math.radians(45)`.
- Editing mesh data without `mesh.update()` / `mesh.calc_normals()` - geometry looks wrong or normals are stale.
- Assigning a material by index before appending: use `obj.data.materials.append(mat)` first.
- Running scripts in system Python: `bpy` only exists inside Blender's bundled interpreter.
- Calling `bpy.ops.mesh.*` without switching to edit mode first.
- Creating data blocks without `.get()` first: Blender appends `.001`, `.002` names on duplicates.
- Confusing the object with its data: `obj.data` is the mesh; deleting the mesh leaves objects broken.
- Expecting viewport updates in `--background` mode (there is no viewport).
- Rendering without setting `scene.camera`.
- Confusing `bpy.context.collection` (active collection) with `bpy.context.scene.collection` (scene root).
- Expecting `bpy.ops.*` to work with the wrong active object or mode - always set mode and active object explicitly.
