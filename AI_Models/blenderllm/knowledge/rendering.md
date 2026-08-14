keywords: render, rendering, engine, eevee, cycles, samples, resolution, output, filepath, write_still, background
# Rendering
- Engine: `bpy.context.scene.render.engine = 'CYCLES'`, `'BLENDER_EEVEE'`, or `'BLENDER_WORKBENCH'`.
- Resolution: `scene.render.resolution_x`, `scene.render.resolution_y`, `scene.render.resolution_percentage`.
- Samples: Cycles -> `scene.cycles.samples`; EEVEE -> `scene.eevee.taa_render_samples`.
- Output: `scene.render.filepath` (full path or directory), `scene.render.image_settings.file_format = 'PNG'` (also `'JPEG'`, `'OPEN_EXR'`, ...).
- Render the current frame: `bpy.ops.render.render(write_still=True)` - renders and writes the image to the filepath, and loads it into `bpy.data.images["Render Result"]`.
- Render an animation: `bpy.ops.render.render(animation=True)`.
- Background mode (`blender --background --python script.py`) renders fine, but viewport-dependent operators (`bpy.ops.view3d.*`) fail there.
- Make sure `scene.camera` is set before rendering.
