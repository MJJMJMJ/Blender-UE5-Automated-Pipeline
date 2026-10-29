# -*- coding: utf-8 -*-
# Blender 渲染（Lite 版）：EEVEE 基础渲染，通用三点布光
import bpy
import math
import os
from mathutils import Vector

FBX = r"@fbx@"
TEXDIR = r"@texdir@"
OUT = r"@outdir@"
RES_X, RES_Y = @width@, @height@
ANGLES = @angles@
FOV = @fov@
DIST_FACTOR = @dist_factor@
EXPOSURE = @exposure@
LIGHT_INTENSITY = @light_intensity@

os.makedirs(OUT, exist_ok=True)


def log(m):
    print("[Render] " + str(m), flush=True)


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.fbx(filepath=FBX)
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
log("网格 %d 个" % len(meshes))

_TEX = {}


def load_img(path, non_color=False):
    k = (path, non_color)
    if k in _TEX:
        return _TEX[k]
    img = bpy.data.images.load(path, check_existing=True)
    if non_color:
        img.colorspace_settings.name = "Non-Color"
    _TEX[k] = img
    return img


def make_mat(part):
    # 基础 PBR：贴图直连，不做染色或调色
    p_a = os.path.join(TEXDIR, "T_StoneGolem_%s_Albedo_Skin1.tga" % part)
    p_r = os.path.join(TEXDIR, "T_StoneGolem_%s_Roughness.tga" % part)
    p_n = os.path.join(TEXDIR, "T_StoneGolem_%s_Normal.tga" % part)

    mat = bpy.data.materials.new("M_" + part)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    if os.path.exists(p_a):
        t = nt.nodes.new("ShaderNodeTexImage")
        t.image = load_img(p_a)
        nt.links.new(t.outputs["Color"], bsdf.inputs["Base Color"])

    if os.path.exists(p_r):
        t = nt.nodes.new("ShaderNodeTexImage")
        t.image = load_img(p_r, True)
        nt.links.new(t.outputs["Color"], bsdf.inputs["Roughness"])
    else:
        bsdf.inputs["Roughness"].default_value = 0.7

    if os.path.exists(p_n):
        t = nt.nodes.new("ShaderNodeTexImage")
        t.image = load_img(p_n, True)
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(t.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])

    return mat


def pick(n):
    n = n.lower()
    if "armor" in n:
        return "armor"
    if "hammer" in n or "shield" in n:
        return "weapon"
    return "body"


mats = {k: make_mat(k) for k in ("body", "armor", "weapon")}
for o in meshes:
    o.data.materials.clear()
    o.data.materials.append(mats[pick(o.name)])
    for p in o.data.polygons:
        p.use_smooth = True

lo = Vector((1e30,) * 3)
hi = Vector((-1e30,) * 3)
for o in meshes:
    for c in o.bound_box:
        w = o.matrix_world @ Vector(c)
        for i in range(3):
            lo[i] = min(lo[i], w[i])
            hi[i] = max(hi[i], w[i])
center = (lo + hi) / 2
size = max(hi[i] - lo[i] for i in range(3))


def add_light(name, loc, energy, sz, color):
    ld = bpy.data.lights.new(name, "AREA")
    ld.energy = energy
    ld.size = sz
    ld.color = color
    ob = bpy.data.objects.new(name, ld)
    ob.location = loc
    bpy.context.collection.objects.link(ob)
    d = center - Vector(loc)
    ob.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()


d = size * 2.0
add_light("Key", (center.x - d * 0.6, center.y - d * 0.7, center.z + d * 0.5),
          size * size * LIGHT_INTENSITY, size * 0.8, (1, 1, 1))
add_light("Fill", (center.x + d * 0.7, center.y - d * 0.5, center.z + d * 0.1),
          size * size * LIGHT_INTENSITY * 0.6, size * 0.9, (1, 1, 1))
add_light("Top", (center.x, center.y, center.z + d * 1.1),
          size * size * LIGHT_INTENSITY * 0.8, size * 0.7, (1, 1, 1))

world = bpy.data.worlds.new("W")
world.use_nodes = True
world.node_tree.nodes["Background"].inputs[0].default_value = (0.12, 0.12, 0.14, 1)
bpy.context.scene.world = world

cam_data = bpy.data.cameras.new("Cam")
cam = bpy.data.objects.new("Cam", cam_data)
bpy.context.collection.objects.link(cam)
bpy.context.scene.camera = cam
cam_data.lens = 70.0 * (50.0 / max(FOV, 1.0))

# 按最宽的水平轴计算距离：
#   侧视图（180/270度）看到的宽度可能大于 size，
#   直接用 size 会导致模型被裁切。
extent_x = hi.x - lo.x
extent_y = hi.y - lo.y
horiz = max(extent_x, extent_y)
cam_dist = max(size, horiz) * DIST_FACTOR * 1.15

sc = bpy.context.scene
sc.render.engine = "BLENDER_EEVEE"
try:
    sc.eevee.taa_render_samples = 32
except Exception:
    pass
sc.render.resolution_x = RES_X
sc.render.resolution_y = RES_Y
sc.render.image_settings.file_format = "PNG"
sc.view_settings.view_transform = "Filmic"
sc.view_settings.exposure = EXPOSURE

for ang in ANGLES:
    r = math.radians(ang)
    loc = Vector((center.x + cam_dist * math.cos(r),
                  center.y - cam_dist * math.sin(r),
                  center.z + size * 0.08))
    cam.location = loc
    cam.rotation_euler = (center - loc).to_track_quat("-Z", "Y").to_euler()

    fn = "render_%03d.png" % int(ang)
    fp = os.path.join(OUT, fn)
    sc.render.filepath = fp
    log("渲染角度 %d ..." % ang)
    bpy.ops.render.render(write_still=True)
    if os.path.exists(fp):
        log("  -> %s (%d KB)" % (fn, os.path.getsize(fp) // 1024))

log("DONE")
