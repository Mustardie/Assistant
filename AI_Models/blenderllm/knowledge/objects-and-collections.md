keywords: object, objects, collection, collections, link, parent, bpy.data.objects, scene collection
# Objects and Collections
- An object is the transformable entity in the scene. Its data (`obj.data`) is the mesh, light, or camera it displays.
- Create a mesh object:
  `mesh = bpy.data.meshes.new("CubeMesh")`
  `obj = bpy.data.objects.new("Cube", mesh)`
- New objects are NOT automatically in the scene. You must link them:
  `bpy.context.scene.collection.objects.link(obj)`
  Forgetting this is one of the most common Blender Python mistakes.
- Create and fill collections:
  `col = bpy.data.collections.new("Props")`
  `bpy.context.scene.collection.children.link(col)`
  `col.objects.link(obj)`
- `bpy.context.scene.collection` is the scene's root collection.
- Parenting: `obj.parent = parent_obj`.
- Visibility toggles: `obj.hide_viewport`, `obj.hide_render`, `obj.visible_get()`.
- Remove an object from the scene: `bpy.data.objects.remove(obj)`.
