keywords: scene, scenes, bpy.context.scene, frame, frame_current, animation frame
# Scenes
- `bpy.context.scene` is the active scene.
- Access any scene by name: `bpy.data.scenes["Scene"]`.
- Every scene has a root collection (`scene.collection`) where its objects live.
- Frame control: `bpy.context.scene.frame_current = 1` or `bpy.context.scene.frame_set(1)`.
- The current frame drives which keyframe values are evaluated and what gets rendered.
- Render settings are per scene: `scene.render.engine`, `scene.render.resolution_x`, etc.
- `bpy.context.scene.frame_set()` is preferred when you need data blocks to refresh, instead of writing `frame_current` directly.
- Scenes can be switched at runtime, but scripts usually act on the context scene.
