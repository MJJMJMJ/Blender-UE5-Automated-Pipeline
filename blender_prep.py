# -*- coding: utf-8 -*-
# Blender 预处理（Lite 版）：平滑着色 + 逐级减面 + 导出 FBX
import bpy
import os

SRC_FBX = r"@src_fbx@"
DST_DIR = r"@dst_dir@"
LOD_RATIOS = @lod_ratios@
MIN_VERTS = @min_verts@

os.makedirs(DST_DIR, exist_ok=True)


def log(m):
    print("[Prep] " + str(m), flush=True)


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.fbx(filepath=SRC_FBX)

meshes = [o for o in bpy.data.objects if o.type == "MESH"]
log("导入网格 %d 个" % len(meshes))

if not meshes:
    log("没有网格，退出")
    raise SystemExit(1)

for o in meshes:
    for p in o.data.polygons:
        p.use_smooth = True

base = os.path.splitext(os.path.basename(SRC_FBX))[0]

for idx, ratio in enumerate(LOD_RATIOS, start=1):
    for o in meshes:
        if len(o.data.vertices) < MIN_VERTS:
            continue
        try:
            mod = o.modifiers.new(name="LOD%d" % idx, type="DECIMATE")
            mod.ratio = ratio
        except Exception as e:
            log("减面失败 %s: %s" % (o.name, e))

    out = os.path.join(DST_DIR, "%s_LOD%d.fbx" % (base, idx))
    try:
        bpy.ops.export_scene.fbx(filepath=out, use_selection=False)
        log("导出 LOD%d -> %s" % (idx, os.path.basename(out)))
    except Exception as e:
        log("导出失败 LOD%d: %s" % (idx, e))

    for o in meshes:
        for m in list(o.modifiers):
            o.modifiers.remove(m)

log("DONE")
