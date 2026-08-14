keywords: modifier, modifiers, subsurf, solidify, mirror, bevel, apply modifier, boolean, array
# Modifiers
- Add a modifier: `mod = obj.modifiers.new("Bevel", "BEVEL")`.
  The second argument is the modifier type string: `'SUBSURF'`, `'SOLIDIFY'`, `'MIRROR'`, `'BEVEL'`, `'ARRAY'`, `'BOOLEAN'`, and others.
- Common settings are properties on the modifier:
  - `mod.levels` / `mod.render_levels` (Subdivision Surface)
  - `mod.thickness` (Solidify)
  - `mod.use_x`, `mod.use_y`, `mod.use_z` (Mirror axes)
  - `mod.count` (Array)
- Modifier order matters: they evaluate top to bottom. Reorder with `obj.modifiers.move(index, target)`.
- Modifiers do not change the base mesh until applied:
  `obj.modifiers.apply(modifier=mod)` (destructive - bakes the effect into the mesh).
- Operator version: `bpy.ops.object.modifier_apply(modifier="Bevel")` (requires the object to be active).
- Per-viewport display: `mod.show_viewport`, `mod.show_render`.
