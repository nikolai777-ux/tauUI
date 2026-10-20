"""Blender (background): a BVH motion-capture file → GLB for the Geometry node's viewer.
Run by scene_cache.py: blender --background --factory-startup --python bvh_to_glb.py -- <in.bvh> <out.glb> <fps.txt>
A BVH is a skeleton and its motion - nothing to see. Every bone gets a bone shape (an elongated octahedron from its
head to its tail, as Blender / Maya draw bones) that rides on it rigidly, so the GLB shows the skeleton moving. Units
are the file's own (no scaling). The file's frame rate goes into <fps.txt>: glTF keeps seconds, not frames."""
import json
import struct
import sys

import bpy
from mathutils import Vector

src, out, fps_file = sys.argv[sys.argv.index("--") + 1:][:3]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_anim.bvh(filepath=src, global_scale=1.0, frame_start=1, use_fps_scale=False,
                        update_scene_fps=True, update_scene_duration=True, axis_forward="-Z", axis_up="Y")
scene = bpy.context.scene
arm = bpy.context.view_layer.objects.active
fps = scene.render.fps / scene.render.fps_base
action = arm.animation_data.action if arm.animation_data else None
if action:  # the exported range = the file's frames (the importer leaves a shorter scene's default 1-250)
    scene.frame_start, scene.frame_end = (int(round(v)) for v in action.frame_range)
with open(fps_file, "w", encoding="utf-8") as f:
    f.write(f"{fps:.6f}")

bones = [b for b in arm.data.bones if b.length > 1e-9]
corners = [arm.matrix_world @ p for b in bones for p in (b.head_local, b.tail_local)]
lo = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
hi = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
height = max((hi - lo).length, 1e-6)

mat = bpy.data.materials.new("bone")
mat.use_nodes = True
mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.85, 0.72, 0.5, 1.0)
mat.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.6

arm.data.pose_position = "REST"  # place each shape on its bone at rest, then let the motion carry it
bpy.context.view_layer.update()
for b in bones:
    length = b.length
    r = min(0.1 * length, 0.02 * height)  # thin for long bones, never fat on a skeleton
    w = 0.15 * length  # the widest point, near the head (as Blender's octahedral bones)
    # bone space: head at the origin, the bone along +Y
    verts = [(0, 0, 0), (r, w, 0), (0, w, r), (-r, w, 0), (0, w, -r), (0, length, 0)]
    faces = [(0, 2, 1), (0, 3, 2), (0, 4, 3), (0, 1, 4), (5, 1, 2), (5, 2, 3), (5, 3, 4), (5, 4, 1)]
    mesh = bpy.data.meshes.new(b.name)
    mesh.from_pydata(verts, [], faces)
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(b.name + "_shape", mesh)
    scene.collection.objects.link(obj)
    obj.parent = arm
    obj.parent_type = "BONE"
    obj.parent_bone = b.name
    obj.matrix_world = arm.matrix_world @ b.matrix_local  # its place at rest; Blender works out the bone-local part
arm.data.pose_position = "POSE"
bpy.context.view_layer.update()

bpy.ops.export_scene.gltf(filepath=out, export_format="GLB", export_animations=True, export_force_sampling=True,
                          export_cameras=False, export_lights=False)


def exact_times(path, rate):
    """Key times = (frame - first frame) / the file's rate. The exporter samples every frame but times them off at
    NTSC rates (23.976 came out as 1/24.024 s per frame, drifting against the source video) and starts at the scene's
    first frame (1) instead of 0 s."""
    data = bytearray(open(path, "rb").read())
    size = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20:20 + size])
    binary = 20 + size + 8
    inputs = {s["input"] for a in gltf.get("animations", []) for s in a["samplers"]}
    for i in inputs:
        acc = gltf["accessors"][i]
        view = gltf["bufferViews"][acc["bufferView"]]
        at = binary + view.get("byteOffset", 0) + acc.get("byteOffset", 0)
        n = acc["count"]
        old = struct.unpack_from(f"<{n}f", data, at)
        step = (old[-1] - old[0]) / (n - 1) if n > 1 else 1.0
        new = [round((t - old[0]) / step) / rate if step > 0 else 0.0 for t in old]  # the first frame at 0 s
        struct.pack_into(f"<{n}f", data, at, *new)
        acc["min"], acc["max"] = [min(new)], [max(new)]
    text = json.dumps(gltf, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)  # GLB chunks stay 4-byte aligned
    out_bytes = data[:12] + struct.pack("<I", len(text)) + data[16:20] + text + data[20 + size:]
    struct.pack_into("<I", out_bytes, 8, len(out_bytes))
    open(path, "wb").write(out_bytes)


exact_times(out, fps)
print("BVH TO GLB DONE", out, "bones", len(bones), "frames", scene.frame_start, scene.frame_end, "fps", round(fps, 3),
      "height", round(height, 3))
