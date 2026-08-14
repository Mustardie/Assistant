keywords: script structure, idempotent, cleanup, reset, run twice, delete existing, main guard
# Script Structure and Cleanup
- Recommended structure: imports first (bpy, math, mathutils as needed), then helper functions, then a main() function, guarded by `if __name__ == "__main__":` so parts can be imported.
- Cleanup/reset pattern before regenerating a scene:
  remove objects by name with `bpy.data.objects.get("Name")` then `bpy.data.objects.remove(obj, do_unlink=True)`.
- Simple full-scene reset:
  `for obj in list(bpy.data.objects): bpy.data.objects.remove(obj, do_unlink=True)`.
- Idempotency: check before creating. If a script may run twice, delete or reuse existing data blocks (objects, materials, meshes) instead of blindly creating duplicates with `.001` names.
- Deterministic scripts set everything explicitly (names, locations, settings) and do not rely on the current selection or active object.
- Scripts should run without the UI: the same code works in `--background` mode, where viewport operators do not exist.
- Keep the script flat and linear for simple tasks; split repetitive work into loops over lists of parameters.
