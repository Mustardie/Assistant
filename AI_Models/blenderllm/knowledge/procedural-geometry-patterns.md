keywords: procedural, generate mesh, geometry pattern, from_pydata, mathutils, random, loops, bmesh
# Procedural Geometry Patterns
- Build geometry in plain Python first: create lists of vertex coordinates, edges, and faces, then build the mesh in one step:
  `mesh.from_pydata(verts, edges, faces)` then `mesh.update()`.
- Loop over parameters (rows, rings, angles) to compute vertices with math, e.g. a ring of points at radius `r`, angle `i * step`.
- `mathutils.Vector` math: `a + b`, `a * s`, `a.cross(b)`, `a.normalized()`, `a.rotate(rot)`.
- Reproducible randomness: `import random; random.seed(42)` before generating, so reruns give the same scene.
- Always link the resulting object into the scene collection and call `mesh.update()` (and `calc_normals()` when normals matter).
- bmesh alternative for live editing: `bmesh.new()`, `bmesh.from_mesh(mesh)`, `bmesh.ops.create_grid(...)`, edit, `bmesh.to_mesh(mesh)`, `bmesh.free()`.
- Watch out: bmesh elements are only valid while the bmesh is alive - do not keep references after `free()`.
