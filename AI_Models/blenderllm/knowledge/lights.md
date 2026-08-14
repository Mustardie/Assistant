keywords: light, lights, lamp, bpy.data.lights, point light, sun light, spot light, area light, energy
# Lights
- Create a light:
  `light = bpy.data.lights.new("Sun", type='SUN')`
  `obj = bpy.data.objects.new("Sun", light)`
  `bpy.context.scene.collection.objects.link(obj)`
- Light types: `'POINT'`, `'SUN'`, `'SPOT'`, `'AREA'`.
- Intensity: `light.energy` (watts in modern Blender for all light types).
- Color: `light.color = (r, g, b)`.
- Spot cone: `light.spot_size` (radians), `light.spot_blend`.
- Area shape/size: `light.shape = 'SQUARE'` or `'RECT'`, `light.size`.
- Sun lights only have a direction: rotate the sun object; its position does not matter.
- EEVEE and Cycles map energy to brightness differently - verify with test renders.
