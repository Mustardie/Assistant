keywords: material, materials, bpy.data.materials, shader, principled bsdf, node tree, base color, node
# Materials
- Create: `mat = bpy.data.materials.new("MyMat")`.
- Assign to an object: `obj.data.materials.append(mat)` (adds a material slot). `obj.data.materials.clear()` removes slots.
- Simple color without nodes: `mat.diffuse_color = (r, g, b, a)`.
- Node-based material:
  `mat.use_nodes = True`
  `tree = mat.node_tree`
  `nodes = tree.nodes`
  `links = tree.links`
- The Principled BSDF node is created automatically with `use_nodes = True`: `nodes.get("Principled BSDF")`.
- Set an input value, e.g. `principled.inputs["Base Color"].default_value = (1.0, 0.2, 0.2, 1.0)`.
- EEVEE and Cycles both support Principled BSDF, but some behavior differs (for example glow needs bloom in EEVEE).
- Materials are shared data blocks: multiple objects can reference the same material.
