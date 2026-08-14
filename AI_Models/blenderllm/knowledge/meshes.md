keywords: mesh, meshes, vertex, vertices, edge, edges, polygon, face, bmesh, mesh.update, normals, from_pydata
# Meshes
- A mesh is a data block (`bpy.data.meshes`) containing vertices, edges, and polygons.
- Access an object's mesh: `obj.data` (several objects can share one mesh).
- Vertex position in local space: `obj.data.vertices[i].co`.
- World position: `obj.matrix_world @ vertex.co`.
- Create a mesh from lists: `mesh.from_pydata(verts, edges, faces)` then `mesh.update()`.
- After editing vertex coordinates directly, call `mesh.update()` (and `mesh.calc_normals()` for correct shading).
- Computed normals: `obj.data.vertices[i].normal` (after `calc_normals()`).
- Advanced editing: the `bmesh` module - `bmesh.new()`, `bmesh.from_mesh(obj.data)`, edit, `bmesh.to_mesh(obj.data)`, then `bmesh.free()`.
- Deleting mesh data while objects still reference it leaves broken objects.
