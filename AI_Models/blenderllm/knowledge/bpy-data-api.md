keywords: bpy.data, data block, datablock, library, access data
# bpy.data - The Data API
- `bpy.data` holds every data block in the current .blend: objects, meshes, materials, scenes, collections, cameras, lights, images, node groups, and more.
- Access by name: `bpy.data.objects["Cube"]`, `bpy.data.materials["MyMat"]`.
- `.get("Name")` returns `None` instead of raising `KeyError` when the name does not exist.
- Create data blocks: `bpy.data.materials.new("MyMat")`, `bpy.data.meshes.new("MyMesh")`.
- Delete data blocks: `bpy.data.materials.remove(mat)`.
- Names are unique per collection; Blender appends `.001`, `.002` to duplicates.
- Data blocks are shared: many objects can reference the same mesh via `obj.data`.
- Data blocks belong to the .blend file, not to objects. Removing the last object does not remove its data block.
