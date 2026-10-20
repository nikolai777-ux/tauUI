"""Blender (background): an Alembic file → GLB with its cameras and animation, for the Geometry node's viewer.
Run by scene_cache.py: blender --background --factory-startup --python abc_to_glb.py -- <in.abc> <out.glb>
Alembic motion comes into Blender as live "transform cache" constraints, which the glTF exporter doesn't see: it is
baked to keyframes first (objects and cameras, every frame of the file's range). Deforming geometry (Alembic's own
vertex caches) has no place in a GLB and stays still."""
import sys

import bpy

src, out = sys.argv[sys.argv.index("--") + 1:][:2]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.alembic_import(filepath=src, set_frame_range=True)
scene = bpy.context.scene
moving = [o for o in scene.objects if any(c.type == "TRANSFORM_CACHE" for c in o.constraints)]
if moving:
    bpy.ops.object.select_all(action="DESELECT")
    for o in moving:
        o.select_set(True)
    bpy.context.view_layer.objects.active = moving[0]
    bpy.ops.nla.bake(frame_start=scene.frame_start, frame_end=scene.frame_end, only_selected=True,
                     visual_keying=True, clear_constraints=True, bake_types={"OBJECT"})
bpy.ops.export_scene.gltf(filepath=out, export_format="GLB", export_cameras=True, export_animations=True,
                          export_force_sampling=True)
print("ABC TO GLB DONE", out, "baked", len(moving), "frames", scene.frame_start, scene.frame_end,
      "fps", scene.render.fps)
