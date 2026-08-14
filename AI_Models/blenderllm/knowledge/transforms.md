keywords: transform, transforms, location, rotation, scale, euler, quaternion, matrix_world, origin, radians
# Transforms
- `obj.location` - Vector (x, y, z) world position.
- `obj.rotation_euler` - in RADIANS, not degrees. Use `math.radians(45)`.
- `obj.rotation_mode` - `'XYZ'` (Euler), `'QUATERNION'`, `'AXIS_ANGLE'`.
- `obj.scale` - Vector scale factor.
- `obj.matrix_world` - the full 4x4 world transform (combines location, rotation, scale and parents).
- `obj.matrix_local` - transform relative to the parent object.
- Apply transforms to the actual mesh data: `bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)` (object mode).
- Object origin vs geometry: mesh vertex coordinates are relative to the object origin; `bpy.ops.object.origin_set(...)` moves the origin.
- Operator versions act on the selection: e.g. `bpy.ops.transform.translate(value=(1, 0, 0))`.
