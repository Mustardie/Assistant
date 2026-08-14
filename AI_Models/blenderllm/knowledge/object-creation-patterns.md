keywords: create object, get or create, naming, generate scene, object pattern, add object to scene
# Object Creation Patterns
- Get-or-create for idempotency:
  `obj = bpy.data.objects.get("Name")` and if it exists, reuse or remove it; otherwise create and link.
- Create a mesh object and link it in three steps:
  1. `mesh = bpy.data.meshes.new("NameMesh")`
  2. `obj = bpy.data.objects.new("Name", mesh)`
  3. `bpy.context.scene.collection.objects.link(obj)` - never skip the link step.
- Name check before creating: `bpy.data.objects.get("Name")` returns None when the name is free.
- Remove before regenerate: `bpy.data.objects.remove(obj, do_unlink=True)`.
- Build many objects with a loop over parameter lists (positions, sizes, names); set `obj.location` and `obj.scale` per item.
- Set the active object explicitly when needed: `bpy.context.view_layer.objects.active = obj`.
- For primitives the fast path is fine: `bpy.ops.mesh.primitive_cube_add(location=(x, y, z))` - but it depends on context; the data-API path above is more deterministic.
