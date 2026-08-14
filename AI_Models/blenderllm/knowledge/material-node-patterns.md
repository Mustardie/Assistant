keywords: material pattern, create material, node setup, emission, texture, node group, get or create material
# Material and Node Patterns
- Get-or-create a material:
  `mat = bpy.data.materials.get("Name") or bpy.data.materials.new("Name")`.
- Enable nodes: `mat.use_nodes = True`; the default tree has a Principled BSDF and a Material Output.
- Set inputs by name:
  `principled = mat.node_tree.nodes.get("Principled BSDF")`
  `principled.inputs["Base Color"].default_value = (r, g, b, 1.0)`.
- Add nodes and link them:
  `emission = mat.node_tree.nodes.new("ShaderNodeEmission")`
  `mat.node_tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])`.
- Common nodes: `ShaderNodeEmission`, `ShaderNodeTexChecker`, `ShaderNodeTexImage`, `ShaderNodeBsdfPrincipled`.
- Set a simple color without nodes: `mat.diffuse_color = (r, g, b, a)`.
- Assign to an object: `obj.data.materials.append(mat)` (append adds a slot; use `obj.data.materials.clear()` first when regenerating).
- Glow in EEVEE needs bloom; emission strength behaves differently between EEVEE and Cycles.
