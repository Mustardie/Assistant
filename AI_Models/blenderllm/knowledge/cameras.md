keywords: camera, cameras, bpy.data.cameras, scene.camera, lens, view camera, depth of field
# Cameras
- Create a camera:
  `cam = bpy.data.cameras.new("Cam")`
  `obj = bpy.data.objects.new("Cam", cam)`
  `bpy.context.scene.collection.objects.link(obj)`
- Make it the scene camera (the one used for rendering):
  `bpy.context.scene.camera = obj`
- Focal length: `cam.lens = 50` (millimeters).
- Projection type: `cam.type = 'PERSP'` or `'ORTHO'` (orthographic size: `cam.ortho_scale`).
- Clipping: `cam.clip_start`, `cam.clip_end`.
- Depth of field: `cam.dof.use_dof = True`, `cam.dof.focus_object = target`, `cam.dof.aperture_fstop = 2.8`.
- Viewport helpers: `bpy.ops.view3d.view_camera()`, `bpy.ops.view3d.camera_to_view()`.
- Rendering uses `scene.camera`. If it is not set, renders can fail or come out wrong.
