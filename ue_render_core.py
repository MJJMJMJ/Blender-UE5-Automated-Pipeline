# -*- coding: utf-8 -*-
"""
ue_render_core.py —— UE5 渲染核心模块（在 UE 编辑器的 Python 环境里运行）

包含三大能力：
  1. setup_hdri_environment()  HDRI 环境光 + SkyLight + 天空球
  2. setup_cine_camera_dof()   CineCamera 景深，按包围盒自动对焦
  3. apply_all_settings()      从 render_config.json 读到的参数统一应用

设计原则：
  · 全部参数来自 config dict，没有写死的魔法数字
  · 每个函数都可以单独调用，也都有完整的异常处理
  · 优先用 EditorActorSubsystem（UE5 推荐），
    仅在它确实没有等价接口时（如 get_editor_world）才用 LevelEditorSubsystem
"""

import os
import math

# ---------------------------------------------------------------------------
# unreal 模块的延迟导入
#
# 这个文件会被两种环境导入：
#   1. UE 编辑器的 Python（有 unreal 模块）→ 正常使用
#   2. Pipeline_Tool 的 GUI 进程（纯 Python，没有 unreal）→ 只做"能否加载"探测
#
# 如果在模块级直接 `import unreal`，第 2 种情况会抛 ImportError，
# 导致 Pipeline_Tool 的 Try-Import 判定模块不可用 —— 但实际上
# 真正调用这些函数时是在 UE 进程里，那时 unreal 是存在的。
#
# 所以改成延迟导入：模块级拿不到就设为 None，
# 真正的函数调用发生在 UE 内部，那时再取就一定有。
# ---------------------------------------------------------------------------
try:
    import unreal
except ImportError:
    unreal = None


def _require_unreal():
    """在真正需要 unreal 时取它；取不到就抛出明确错误。"""
    global unreal
    if unreal is None:
        try:
            import unreal as _u
            unreal = _u
        except ImportError:
            raise RuntimeError(
                "ue_render_core 必须在 UE 编辑器的 Python 环境中运行"
                "（当前缺少 unreal 模块）")
    return unreal


# =============================================================================
# 通用小工具
# =============================================================================

def log(msg):
    """统一日志：同时输出到 UE 日志和 stdout。"""
    text = "[UE-Render] {}".format(msg)
    try:
        unreal.log(text)
    except Exception:
        pass
    try:
        print(text, flush=True)
    except Exception:
        pass


def comp_of(actor, comp_cls):
    """取 actor 上第一个指定类型的组件。"""
    try:
        lst = actor.get_components_by_class(comp_cls)
        return lst[0] if lst else None
    except Exception:
        return None


def set_props(obj, mapping):
    """
    批量设置编辑器属性，逐个 try —— 某个属性不存在不会影响其他属性。

    返回 (成功数, 失败列表)
    """
    ok = 0
    fails = []
    for k, v in mapping.items():
        try:
            obj.set_editor_property(k, v)
            ok += 1
        except Exception as exc:
            fails.append((k, str(exc)[:80]))
    return ok, fails


def get_world():
    """
    获取当前编辑器世界。

    ⚠️ EditorActorSubsystem 没有直接给 world 的方法，
       这里用 LevelEditorSubsystem.get_current_level() 再取 world。
    """
    try:
        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        world = les.get_current_level()
        if world:
            return world
    except Exception:
        pass
    # 老接口兜底
    try:
        return unreal.EditorLevelLibrary.get_editor_world()
    except Exception:
        return None


def destroy_actors_of_class(ss, actor_cls):
    """删掉关卡里所有指定类型的 actor（用于重建环境，避免重复叠加）。"""
    removed = 0
    try:
        for a in list(ss.get_all_level_actors()):
            if isinstance(a, actor_cls):
                ss.destroy_actor(a)
                removed += 1
    except Exception as exc:
        log("  清理 {} 失败：{}".format(actor_cls.__name__, exc))
    return removed


# =============================================================================
# 场景清理与背景板
# =============================================================================

# 本模块创建的所有辅助 Actor 都会被打上这个标签前缀，
# cleanup_previous_scene() 靠它精确识别、避免误删用户自己的东西。
DSH_ACTOR_PREFIX = "DSH_"


def cleanup_previous_scene(ss=None, verbose=True):
    """
    【状态清理】删除上一轮渲染遗留的辅助 Actor。

    ⚠️ 为什么必须做：
       批量处理模型时，如果不清掉上一轮的 SkyLight / CineCamera /
       PostProcessVolume / RectLight / 背景板，它们会叠加到下一个模型上：
         · 多个 PPV 的曝光/泛光参数互相冲突
         · 多个 SkyLight 环境光翻倍 → 越来越亮
         · 旧相机会干扰 SceneCapture 的朝向判断

    清理范围（只删本模块创建的，按 label 前缀 + 类型双重判断）：
       SkyLight / SkyAtmosphere / DirectionalLight / RectLight /
       PointLight / SpotLight / PostProcessVolume / CineCameraActor /
       SceneCapture2D / ExponentialHeightFog，
       以及 label 以 DSH_ 开头的 StaticMeshActor（背景板/地面）

    返回删除的 Actor 数量。
    """
    if ss is None:
        ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    # 需要清理的 Actor 类型
    target_classes = []
    for name in ("SkyLight", "SkyAtmosphere", "DirectionalLight", "RectLight",
                 "PointLight", "SpotLight", "PostProcessVolume",
                 "CineCameraActor", "CameraActor", "SceneCapture2D",
                 "ExponentialHeightFog"):
        cls = getattr(unreal, name, None)
        if cls is not None:
            target_classes.append(cls)

    removed = 0
    removed_names = []

    try:
        for a in list(ss.get_all_level_actors()):
            try:
                hit = False

                # ① 按类型判断
                for cls in target_classes:
                    if isinstance(a, cls):
                        hit = True
                        break

                # ② 按 label 前缀判断（覆盖背景板这类 StaticMeshActor）
                if not hit:
                    try:
                        label = a.get_actor_label() or ""
                        if label.startswith(DSH_ACTOR_PREFIX) and \
                                isinstance(a, unreal.StaticMeshActor):
                            hit = True
                    except Exception:
                        pass

                if hit:
                    try:
                        removed_names.append(a.get_actor_label())
                    except Exception:
                        removed_names.append(type(a).__name__)
                    ss.destroy_actor(a)
                    removed += 1
            except Exception:
                continue
    except Exception as exc:
        log("  cleanup 遍历失败：{}".format(str(exc)[:100]))

    if verbose:
        if removed:
            log("[清理] 已移除上一轮辅助 Actor {} 个：{}".format(
                removed, ", ".join(removed_names[:8]) +
                ("…" if len(removed_names) > 8 else "")))
        else:
            log("[清理] 无遗留辅助 Actor")

    return removed


# =============================================================================
# 背景墙（用 Cube 实现，替代之前失败的 Plane 方案）
# =============================================================================

def fix_mesh_normals(mesh_paths, verbose=True):
    """
    强制重算所有网格的法线。

    ═══════════════════════════════════════════════════════════════════
    recompute_normals 不是 StaticMesh 的属性，而是在 MeshBuildSettings
    结构体里，必须通过 EditorStaticMeshLibrary 读写：

        bs = lib.get_lod_build_settings(mesh, lod)
        bs.set_editor_property("recompute_normals", True)
        lib.set_lod_build_settings(mesh, lod, bs)

    UE 5.8 的默认值是：
        recompute_normals                  = False
        use_high_precision_tangent_basis   = False

    不打开这两个开关，高模导入后会保留 FBX 里错误的法线，
    渲染出来就是"碎玻璃"式的着色。
    ═══════════════════════════════════════════════════════════════════
    """
    try:
        lib = unreal.EditorStaticMeshLibrary
    except Exception as exc:
        log("[法线] EditorStaticMeshLibrary 不可用：{}".format(str(exc)[:80]))
        return 0

    n = 0
    for path in mesh_paths:
        try:
            mesh = unreal.EditorAssetLibrary.load_asset(path)
            if not mesh:
                continue
            done = False
            for lod in range(2):
                try:
                    bs = lib.get_lod_build_settings(mesh, lod)
                    bs.set_editor_property("recompute_normals", True)
                    bs.set_editor_property(
                        "use_high_precision_tangent_basis", True)
                    bs.set_editor_property("recompute_tangents", True)
                    lib.set_lod_build_settings(mesh, lod, bs)
                    done = True
                except Exception:
                    break
            if done:
                try:
                    unreal.EditorAssetLibrary.save_loaded_asset(mesh)
                except Exception:
                    pass
                n += 1
                if verbose:
                    log("  [法线] {} 已重算".format(mesh.get_name()))
        except Exception:
            continue

    log("[法线] 完成：{} 个网格重算法线".format(n))
    return n


def rebuild_static_meshes(mesh_paths):
    """
    重建 StaticMesh，让改动过的构建参数（法线）真正生效。

    只改 MeshBuildSettings 不会自动触发重编译，
    需要显式调用 build_static_mesh 或者重新保存资产。

    返回成功重建的数量。
    """
    n = 0
    for path in mesh_paths:
        try:
            mesh = unreal.EditorAssetLibrary.load_asset(path)
            if not mesh:
                continue
            try:
                # UE5.8: rebuild 通过 EditorStaticMeshLibrary
                unreal.EditorStaticMeshLibrary.bulk_set_lod(
                    mesh, 0, True)
            except Exception:
                pass
            try:
                unreal.EditorAssetLibrary.save_loaded_asset(mesh, False)
                n += 1
            except Exception:
                pass
        except Exception:
            continue
    log("[重建] {} 个网格已重建".format(n))
    return n


def ensure_skylight(ss=None, intensity=1.5, color=(0.55, 0.62, 0.78)):
    """
    强制创建一个 SkyLight，提供全局环境光。

    ═══════════════════════════════════════════════════════════════════
    为什么需要这个：
      HDRI cubemap 加载失败时（本机实测一直是 False），
      场景没有任何环境光照，模型背光面会黑成一团、甚至整体消失。

      SkyLight 用纯色天空即可解决这个问题：
      它给所有物体提供均匀的环境光，背光面也有基础亮度。

    参数：
      intensity : 环境光强度，1.0~2.0 比较合适
      color     : 天光颜色（偏冷的浅蓝，接近自然天光）
    ═══════════════════════════════════════════════════════════════════
    """
    if ss is None:
        ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    try:
        # 先删掉旧的 DSH 天光，避免累积
        for a in list(ss.get_all_level_actors()):
            try:
                if isinstance(a, unreal.SkyLight) and \
                        a.get_actor_label().startswith("DSH_"):
                    ss.destroy_actor(a)
            except Exception:
                pass

        sky = ss.spawn_actor_from_class(
            unreal.SkyLight, unreal.Vector(0, 0, 400), unreal.Rotator(0, 0, 0))
        if not sky:
            log("  [天光] 创建失败")
            return None

        sky.set_actor_label("DSH_SkyLight")
        comp = sky.get_components_by_class(unreal.SkyLightComponent)[0]

        ok = 0
        # ---- 用指定的纯色作为环境光照来源 ----
        # SLS_SpecifiedCubemap 在没有 cubemap 时不可用；
        # 这里用 CapturedScene + 纯色天空盒，或者直接给低阶环境色。
        for k, v in (
            ("intensity", float(intensity)),
            ("light_color", unreal.Color(
                int(color[0] * 255), int(color[1] * 255), int(color[2] * 255), 255)),
            ("real_time_capture", True),
            ("mobility", unreal.ComponentMobility.MOVABLE),
            ("affects_world", True),
        ):
            try:
                comp.set_editor_property(k, v)
                ok += 1
            except Exception:
                pass

        # 强制重新捕获
        try:
            comp.recapture_sky()
        except Exception:
            pass
        try:
            comp.set_editor_property("real_time_capture", True)
        except Exception:
            pass

        log("[天光] DSH_SkyLight 已创建（强度 {:.1f}，{} 项生效）".format(intensity, ok))
        return sky

    except Exception as exc:
        log("  [天光] 创建异常：{}".format(str(exc)[:120]))
        return None


def create_background_wall(center, size, camera_loc, bg_cfg=None, ss=None):
    """
    在模型【正后方】生成一堵巨大的 Cube 背景墙。

    ═══ 为什么用 Cube 而不是 Plane ═══
      Plane 是单面片，朝向和法线一旦算错就完全不可见，
      而且背面剔除会让它从背后看是透明的 —— 这是之前反复失败的原因。

      Cube 是闭合实体，任何角度都有面朝向相机，不存在朝向问题，
      也不需要计算任何旋转（rotation 保持 0,0,0 即可）。

    ═══ 摆放逻辑 ═══
      1. 方向：从相机指向模型，再继续延伸（模型在中间，墙在最外侧）
      2. 位置：center + direction * (size * distance_factor)
      3. 缩放：X 极薄（0.16 ≈ 16 单位厚），Y/Z 放大成墙
      4. 缩放量根据到相机的距离计算，确保填满整个视野

    实测参数（已验证可行）：
      wall 距 center = size * 1.5
      scale = (0.16, 8.0, 8.0)  →  800x800 的墙面

    参数：
      center      : 模型包围盒中心 unreal.Vector
      size        : 模型最大尺寸
      camera_loc  : 相机位置 unreal.Vector
      bg_cfg      : 配置段，可含 color / emissive_strength / brightness
      ss          : EditorActorSubsystem

    返回 (wall_actor, material)；失败返回 (None, None)
    """
    if ss is None:
        ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    cfg = bg_cfg or {}
    color = cfg.get("color", [0.5, 0.5, 0.52])
    strength = float(cfg.get("emissive_strength", 6.0))
    dist_f = float(cfg.get("distance_factor", 1.5))
    wall_scale = float(cfg.get("wall_scale", 8.0))

    try:
        cube = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
        if not cube:
            log("  [背景墙] 找不到 /Engine/BasicShapes/Cube")
            return None, None

        # ---- 1. 建灰色自发光材质 ----
        env = "/Game/DSH_Env"
        if not unreal.EditorAssetLibrary.does_directory_exist(env):
            unreal.EditorAssetLibrary.make_directory(env)

        mat_path = env + "/M_BgWall"
        if unreal.EditorAssetLibrary.does_asset_exist(mat_path):
            try:
                unreal.EditorAssetLibrary.delete_asset(mat_path)
            except Exception:
                pass

        mel = unreal.MaterialEditingLibrary

        # ★ 材质创建要可靠：create_asset 在资产已存在/被锁时会返回 None
        #   这里带重试 + 时间戳兜底命名
        import time as _t
        mat = None
        _tool = unreal.AssetToolsHelpers.get_asset_tools()
        for _try in range(4):
            try:
                mat = _tool.create_asset(
                    "M_BgGrad", env, unreal.Material,
                    unreal.MaterialFactoryNew())
                if mat is not None:
                    break
            except Exception:
                pass
            # 换名重试
            try:
                mat = _tool.create_asset(
                    "M_BgGrad_{}".format(_t.strftime("%H%M%S")),
                    env, unreal.Material, unreal.MaterialFactoryNew())
                if mat is not None:
                    break
            except Exception:
                pass
            _t.sleep(0.4)

        if mat is None:
            log("  [背景球] 材质创建失败（资产可能被占用）")
            return None, None
        log("  [背景球] 材质已创建: {}".format(mat.get_name()))

        # ═══════════════════════════════════════════════════════════════
        # ★ V23.0 无限渐变背景材质
        #
        # 用 WorldPosition 的 Z 分量驱动垂直渐变：
        #   画面底部 → 深灰（近乎黑，模拟地面反射的暗部）
        #   画面顶部 → 稍亮的中性灰
        #
        # 因为是【世界坐标】驱动，背景跟相机位置、朝向完全无关，
        # 也不存在任何几何交界 —— 物理上不可能出现横线。
        #
        # 节点链：
        #   WorldPosition → ComponentMask(Z) → Subtract(baseZ) → Divide(range)
        #   → Clamp → Lerp(底部色, 顶部色) → × 亮度 → Emissive
        # ═══════════════════════════════════════════════════════════════
        bottom = cfg.get("gradient_bottom", [0.02, 0.02, 0.025])
        top = cfg.get("gradient_top", [0.30, 0.31, 0.34])

        # --- 世界坐标 ---
        wp = mel.create_material_expression(
            mat, unreal.MaterialExpressionWorldPosition, -1400, 0)

        # --- 取 Z 分量（UE 里 WorldPosition 的 B 通道 = Z） ---
        mask = mel.create_material_expression(
            mat, unreal.MaterialExpressionComponentMask, -1200, 0)
        try:
            mask.set_editor_property("r", False)
            mask.set_editor_property("g", False)
            mask.set_editor_property("b", True)
            mask.set_editor_property("a", False)
        except Exception:
            pass
        mel.connect_material_expressions(wp, "", mask, "")

        # --- 减去基准高度（模型中心 Z）---
        base_z = mel.create_material_expression(
            mat, unreal.MaterialExpressionConstant, -1200, 150)
        # 基准点：模型中心下方 1.2*size 处，
        # 让模型所在高度落在渐变中上部，脚下自然过渡到底部暗色
        base_z.set_editor_property("r", float(center.z - size * 1.2))

        sub = mel.create_material_expression(
            mat, unreal.MaterialExpressionSubtract, -1000, 60)
        mel.connect_material_expressions(mask, "", sub, "A")
        mel.connect_material_expressions(base_z, "", sub, "B")

        # --- 除以渐变高度（决定过渡范围）---
        grad_h = mel.create_material_expression(
            mat, unreal.MaterialExpressionConstant, -1000, 250)
        # 渐变高度：越大过渡越平缓，越不容易出现"可见的分界线"
        grad_h.set_editor_property("r", float(max(size * 4.5, 1.0)))

        div = mel.create_material_expression(
            mat, unreal.MaterialExpressionDivide, -820, 130)
        mel.connect_material_expressions(sub, "", div, "A")
        mel.connect_material_expressions(grad_h, "", div, "B")

        # --- 加 0.5：让模型中心处位于渐变中点 ---
        half = mel.create_material_expression(
            mat, unreal.MaterialExpressionConstant, -820, 300)
        half.set_editor_property("r", 0.5)

        add = mel.create_material_expression(
            mat, unreal.MaterialExpressionAdd, -660, 180)
        mel.connect_material_expressions(div, "", add, "A")
        mel.connect_material_expressions(half, "", add, "B")

        # --- Clamp 到 [0,1] ---
        clamp = mel.create_material_expression(
            mat, unreal.MaterialExpressionClamp, -520, 180)
        mel.connect_material_expressions(add, "", clamp, "")

        # --- 两端颜色 ---
        c_bottom = mel.create_material_expression(
            mat, unreal.MaterialExpressionVectorParameter, -660, -120)
        c_bottom.set_editor_property("parameter_name", "BgColorBottom")
        try:
            c_bottom.set_editor_property("default_value", unreal.LinearColor(
                bottom[0], bottom[1], bottom[2], 1.0))
        except Exception:
            pass

        c_top = mel.create_material_expression(
            mat, unreal.MaterialExpressionVectorParameter, -660, 420)
        c_top.set_editor_property("parameter_name", "BgColorTop")
        try:
            c_top.set_editor_property("default_value", unreal.LinearColor(
                top[0], top[1], top[2], 1.0))
        except Exception:
            pass

        # --- Lerp ---
        lerp = mel.create_material_expression(
            mat, unreal.MaterialExpressionLinearInterpolate, -350, 150)
        mel.connect_material_expressions(c_bottom, "", lerp, "A")
        mel.connect_material_expressions(c_top, "", lerp, "B")
        mel.connect_material_expressions(clamp, "", lerp, "Alpha")

        # --- 亮度倍数 ---
        con = mel.create_material_expression(
            mat, unreal.MaterialExpressionConstant, -350, 380)
        con.set_editor_property("r", strength)

        mul = mel.create_material_expression(
            mat, unreal.MaterialExpressionMultiply, -150, 200)
        mel.connect_material_expressions(lerp, "", mul, "A")
        mel.connect_material_expressions(con, "", mul, "B")
        mel.connect_material_property(
            mul, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)

        # 设为无光照模型：颜色恒定，不受灯光影响
        try:
            mat.set_editor_property(
                "shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
        except Exception:
            pass
        try:
            mat.set_editor_property("two_sided", True)
        except Exception:
            pass

        mel.recompile_material(mat)
        try:
            unreal.EditorAssetLibrary.save_loaded_asset(mat)
        except Exception:
            pass

        # ---- 2. 计算墙的位置：模型正后方 ----
        # 方向 = 从相机指向模型（归一化），墙放在 center + dir * dist
        dist = size * dist_f

        if camera_loc is not None:
            vx = center.x - camera_loc.x
            vy = center.y - camera_loc.y
            norm = math.sqrt(vx * vx + vy * vy)
            if norm < 1e-6:
                vx, vy, norm = 1.0, 0.0, 1.0
            dx, dy = vx / norm, vy / norm
        else:
            dx, dy = 1.0, 0.0

        # 墙中心下移到模型底部附近，让墙覆盖整个画面高度
        # （相机水平看向 center，墙若以 center 为中心，
        #   下半视野就没有墙，会露出黑色）
        wall_cz = center.z - size * 1.5
        wall_loc = unreal.Vector(center.x + dx * dist,
                                 center.y + dy * dist,
                                 wall_cz)

        # ---- 3. 生成全包围背景球 ----
        #
        # ═══════════════════════════════════════════════════════════════
        # ★ V21.9 关键修复：用 Sphere 而不是 Cube
        #
        # 旧方案是一块有朝向的 Cube 墙，只有一面朝着相机。
        # 相机转到侧面/背面时，墙的边角就露出来 ——
        # 这就是用户看到的「白色斜边」。
        #
        # 换成 Sphere 后：
        #   · 从任何角度看，背景都是完整包住的球体内壁
        #   · 不存在"边界"，也就不可能露边
        #   · 不需要算朝向、不需要旋转
        #
        # 球心放在相机和模型之间偏模型处，半径大到完全包住场景。
        # ═══════════════════════════════════════════════════════════════
        sphere = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Sphere")
        if not sphere:
            log("  [背景球] 找不到 /Engine/BasicShapes/Sphere")
            return None, None

        import math as _m

        # 球心：模型中心（比偏移到墙上的方案更简单可靠）
        ball_loc = unreal.Vector(center.x, center.y, center.z)

        # 半径：必须包住模型 + 相机 + 整个视野
        # 相机距离 ≈ size * distance_factor，再留 3 倍余量
        _radius = size * max(dist_f * 3.0, 12.0)

        ball = ss.spawn_actor_from_class(
            unreal.StaticMeshActor, ball_loc, unreal.Rotator(0, 0, 0))
        if not ball:
            log("  [背景球] 创建失败")
            return None, None

        ball.set_actor_label("DSH_BgBall")

        smc = ball.static_mesh_component
        smc.set_static_mesh(sphere)

        # Sphere 引擎默认半径 50 单位（直径 100）
        # 缩放系数 = 目标半径 / 50
        _s = _radius / 50.0
        ball.set_actor_scale3d(unreal.Vector(_s, _s, _s))

        # ★ 关键：必须双面渲染
        #   Sphere 从内部看是背面，不设双面就会完全透明
        try:
            smc.set_editor_property("bReverseCulling", True)
        except Exception:
            pass

        # 材质强制双面（从球内看是背面）
        try:
            mat.set_editor_property("two_sided", True)
        except Exception:
            pass

        try:
            smc.set_material(0, mat)
        except Exception:
            pass
        try:
            smc.set_editor_property(
                "mobility", unreal.ComponentMobility.MOVABLE)
        except Exception:
            pass

        log("[背景球] 已创建无限渐变背景：球心({:.0f},{:.0f},{:.0f}) "
            "半径 {:.0f} 底部{} 顶部{}".format(
                ball_loc.x, ball_loc.y, ball_loc.z, _radius, bottom, top))
        return ball, mat

    except Exception as exc:
        import traceback
        log("  [背景球] 创建失败：{}".format(str(exc)[:200]))
        log(traceback.format_exc()[:600])
        return None, None


def lock_normals_and_lod0(mesh_paths, actors, ss=None):
    """
    任务3：渲染前最后一次确认法线与 LOD。

    1. 对所有网格重新确认 recompute_normals = True
       use_high_precision_tangent_basis = True
    2. 所有 StaticMeshComponent 强制 forced_lod_model = 0
       （锁死最高精度 LOD，禁止自动降级）

    返回 (法线修复数, LOD锁定数)
    """
    n_norm = 0
    n_lod = 0

    # ---- 1. 法线 ----
    lib = None
    try:
        lib = unreal.EditorStaticMeshLibrary
    except Exception:
        pass

    if lib and mesh_paths:
        for path in mesh_paths:
            try:
                mesh = unreal.EditorAssetLibrary.load_asset(path)
                if not mesh:
                    continue
                for lod in range(2):
                    try:
                        bs = lib.get_lod_build_settings(mesh, lod)
                        bs.set_editor_property("recompute_normals", True)
                        bs.set_editor_property(
                            "use_high_precision_tangent_basis", True)
                        bs.set_editor_property("recompute_tangents", True)
                        lib.set_lod_build_settings(mesh, lod, bs)
                    except Exception:
                        break
                try:
                    unreal.EditorAssetLibrary.save_loaded_asset(mesh)
                except Exception:
                    pass
                n_norm += 1
            except Exception:
                continue

    # ---- 2. LOD0 ----
    for a in (actors or []):
        try:
            smc = a.static_mesh_component
            smc.set_editor_property("forced_lod_model", 0)
            try:
                smc.set_editor_property("override_min_lod", True)
                smc.set_editor_property("min_lod", 0)
            except Exception:
                pass
            n_lod += 1
        except Exception:
            continue

    log("[锁法线] 法线确认 {} 个网格，LOD0 锁定 {} 个组件".format(n_norm, n_lod))
    return n_norm, n_lod


# =============================================================================
# 背景板功能已移除
# =============================================================================
#
# 原 create_background_plane() / _look_at_rotation() 已删除。
#
# 删除原因：
#   动态生成 Plane + 计算朝向 + 运行时建材质这套方案，在实测中
#   无法保证背景板出现在画面里（几何、材质、朝向全部验证正确，
#   但 SceneCapture 就是不渲染它），排查成本过高。
#
# 替代做法：
#   请在 UE5 模板工程里【手动摆放】一块背景板（Plane / Cube 均可），
#   本工具会用 PRM_RENDER_SCENE 模式渲染关卡内所有可见物体，
#   你手动放的背景板会正常出现在画面里，也能被景深虚化。


def import_hdri_texture(hdri_path, dest_path="/Game/DSH_Env", asset_name=None):
    """
    把 .hdr / .exr 文件导入 UE。

    返回 (texture_asset, is_cubemap) ；失败返回 (None, False)。

    ⚠️ 关于"设为 Cubemap"的重要说明（实测结论）：

      UE5 的 Python API **没有** TextureCubeFactory，
      TextureFactory 也**没有** is_cube_map 属性。
      把 .exr 直接导入，得到的永远是 Texture2D（实测确认）。

      而 SkyLightComponent.set_cubemap() 只接受 TextureCube，
      传 Texture2D 会报：
        Failed to convert parameter 'new_cubemap' when calling SetCubemap

      正确做法有两种：
        A. 在编辑器里手动把 HDRI 资源的 Texture Type 改成 Cubemap
           （右键 Texture → Texture → Texture Type → Cubemap），
           然后本函数检测到 TextureCube 就会自动用上。
        B. 不用 SkyLight.cubemap，改用 SkyAtmosphere 或
           "SLS_CAPTURED_SCENE" 的实时捕获 —— 这是本工具的默认路径。

    ⚠️ 关于 sRGB 的重要说明（实测结论）：

      不要给 HDR/EXR 设 srgb=True！
      HDR/EXR 存的是**线性**辐照度数据，srgb=False 才是正确的。
      设成 True 会让 UE 多做一次 sRGB→线性 反向解码，
      导致整个 HDRI 变暗 —— 这才是"亮度不对"的真正原因。

      实测：在 Texture2D 上 set_editor_property("srgb", True) 会被引擎
      强制拒绝（读回仍是 False），因为 HDR 压缩格式不允许 sRGB。

      所以本函数强制 srgb=False，并在日志里说明。
    """
    if not hdri_path:
        return None, False

    if not os.path.isfile(hdri_path):
        log("  HDRI 文件不存在：{}".format(hdri_path))
        return None, False

    name = asset_name or os.path.splitext(os.path.basename(hdri_path))[0]
    name = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
    full_path = "{}/{}".format(dest_path, name)

    try:
        if not unreal.EditorAssetLibrary.does_directory_exist(dest_path):
            unreal.EditorAssetLibrary.make_directory(dest_path)

        # ---- 已有资产检查 ----
        existing = unreal.EditorAssetLibrary.load_asset(full_path)
        if existing is not None:
            is_cube = False
            try:
                is_cube = isinstance(existing, unreal.TextureCube)
            except Exception:
                pass

            if is_cube:
                log("  HDRI 资产已存在且是 Cubemap：{}".format(full_path))
                return existing, True

            # ★ 关键修复：已有资产是 Texture2D，说明之前导入错了。
            #   删掉它，重新以 Cubemap 方式导入。
            log("  HDRI 资产存在但类型是 Texture2D，删除后重新导入为 Cubemap")
            try:
                unreal.EditorAssetLibrary.delete_asset(full_path)
                log("  已删除旧资产")
            except Exception as exc:
                log("  删除失败：{}".format(str(exc)[:80]))

        # ---- 导入 ----
        task = unreal.AssetImportTask()
        task.filename = hdri_path
        task.destination_path = dest_path
        task.destination_name = name
        task.automated = True
        task.save = True
        task.replace_existing = True

        factory = unreal.TextureFactory()
        # TC_HDR 保证不把高动态范围压成 8bit
        try:
            factory.set_editor_property(
                "compression_settings",
                unreal.TextureCompressionSettings.TC_HDR)
        except Exception:
            pass
        task.factory = factory

        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

        tex = unreal.EditorAssetLibrary.load_asset(full_path)
        if tex is None:
            log("  HDRI 导入后加载失败：{}".format(full_path))
            return None, False

        # ---- ★ 尝试把 Texture2D 转成 TextureCube ----
        #
        # 方法 1：设置 texture_type / lod_group 为 CUBE
        #   UE5 里 Texture2D 无法运行时变成 TextureCube，
        #   但可以尝试通过重构资产实现。
        # 方法 2：用 TextureCube 资产的 Texture Type 属性
        #   如果引擎支持，texture_type = TT_CUBE 会让它成为 Cube。
        converted = False
        try:
            tt = getattr(unreal, "TextureType", None)
            if tt and hasattr(tt, "TEXTURE_TYPE_CUBE"):
                tex.set_editor_property("texture_type", tt.TEXTURE_TYPE_CUBE)
                unreal.EditorAssetLibrary.save_loaded_asset(tex)
                tex2 = unreal.EditorAssetLibrary.load_asset(full_path)
                if isinstance(tex2, unreal.TextureCube):
                    converted = True
                    tex = tex2
                    log("  已转换为 TextureCube")
                else:
                    log("  texture_type 设置后仍不是 TextureCube")
        except Exception as exc:
            log("  转换为 Cubemap 失败：{}".format(str(exc)[:100]))

        if isinstance(tex, unreal.TextureCube):
            return tex, True
        log("  HDRI 最终类型：{}（非 Cubemap）".format(type(tex).__name__))

        # ---- 强制 sRGB = False（HDR 是线性数据）----
        try:
            tex.set_editor_property("srgb", False)
            log("  HDRI sRGB = False（HDR 为线性数据，设 True 会变暗）")
        except Exception as exc:
            log("  sRGB 设置跳过：{}".format(str(exc)[:60]))

        try:
            tex.set_editor_property(
                "compression_settings",
                unreal.TextureCompressionSettings.TC_HDR)
        except Exception:
            pass

        try:
            unreal.EditorAssetLibrary.save_loaded_asset(tex)
        except Exception:
            pass

        is_cube = False
        try:
            is_cube = isinstance(tex, unreal.TextureCube)
        except Exception:
            pass

        log("  HDRI 导入成功：{}（类型 {}，Cubemap={}）".format(
            full_path, type(tex).__name__, is_cube))
        return tex, is_cube

    except Exception as exc:
        log("  HDRI 导入异常：{}".format(str(exc)[:150]))
        return None, False


def setup_sky_atmosphere(ss, sun_intensity=2.0, sun_angle=35.0):
    """
    创建一个基础的 SkyAtmosphere + DirectionalLight 作为环境光兜底。

    当没有 HDRI 时，靠这个保证场景有天空色和方向光，
    而不是纯黑背景 + 死板的三点光。
    """
    created = []

    # 天空大气
    try:
        sky = ss.spawn_actor_from_class(
            unreal.SkyAtmosphere, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
        if sky:
            created.append(sky)
            log("  已创建 SkyAtmosphere")
    except Exception as exc:
        log("  创建 SkyAtmosphere 失败：{}".format(str(exc)[:100]))

    # 太阳光（也是 SkyAtmosphere 需要的）
    try:
        sun = ss.spawn_actor_from_class(
            unreal.DirectionalLight, unreal.Vector(0, 0, 500),
            unreal.Rotator(-sun_angle, 0, 0))
        if sun:
            c = comp_of(sun, unreal.DirectionalLightComponent)
            if c:
                set_props(c, {
                    "mobility": unreal.ComponentMobility.MOVABLE,
                    "intensity": float(sun_intensity),
                    "light_color": unreal.Color(255, 248, 235, 255),
                })
            created.append(sun)
            log("  已创建 DirectionalLight（太阳）")
    except Exception as exc:
        log("  创建 DirectionalLight 失败：{}".format(str(exc)[:100]))

    return created


def find_hdri_file(preset, hdri_dir="", explicit_path=""):
    r"""
    根据 preset 名称在 HDRI 素材库中搜索对应的 .hdr / .exr 文件。

    ═══════════════════════════════════════════════════════════════════
    搜索优先级：
      1. explicit_path 存在 → 直接用
      2. hdri_dir 下递归找名字包含 preset 的文件
      3. 默认库目录（deliver 上级的 hdri/）下递归找
      4. 常见下载目录里找
      5. 全都没找到 → 返回 None（调用方优雅降级）

    匹配规则（不区分大小写）：
      · 优先完全同名（去掉扩展名后相等）
      · 其次文件名包含 preset 关键字
      · 多个匹配时取文件最大的（通常画质更高）

    返回绝对路径字符串或 None。
    ═══════════════════════════════════════════════════════════════════
    """
    if not preset:
        return explicit_path if (explicit_path and os.path.isfile(explicit_path)) else None

    key = str(preset).lower().strip()

    # ---- 候选根目录 ----
    roots = []
    if hdri_dir and os.path.isdir(hdri_dir):
        roots.append(hdri_dir)

    # 默认库：<deliver>/../hdri
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        for _c in (os.path.join(_here, "hdri"),
                   os.path.join(os.path.dirname(_here), "hdri")):
            if os.path.isdir(_c) and _c not in roots:
                roots.append(_c)
    except Exception:
        pass

    # 用户下载目录
    try:
        _dl = os.path.join(os.path.expanduser("~"), "Downloads")
        if os.path.isdir(_dl) and _dl not in roots:
            roots.append(_dl)
    except Exception:
        pass

    exts = (".hdr", ".exr")
    exact = []
    fuzzy = []

    for root in roots:
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                for fn in filenames:
                    if not fn.lower().endswith(exts):
                        continue
                    stem = os.path.splitext(fn)[0].lower()
                    full = os.path.join(dirpath, fn)
                    try:
                        size = os.path.getsize(full)
                    except Exception:
                        size = 0
                    if stem == key:
                        exact.append((size, full))
                    elif key in stem or stem in key:
                        fuzzy.append((size, full))
        except Exception:
            continue

    pool = exact if exact else fuzzy
    if not pool:
        log("[HDRI] 未找到 preset '{}' 对应的 HDRI 文件".format(preset))
        return None

    # 取最大的（画质最高）
    pool.sort(reverse=True)
    chosen = pool[0][1]
    log("[HDRI] preset '{}' -> {}".format(preset, chosen))
    if len(pool) > 1:
        log("[HDRI] 共 {} 个候选，已选最大的".format(len(pool)))
    return chosen


def setup_hdri_environment(hdri_path, hdri_cfg, ss=None,
                           hide_sky_background=True):
    """
    布置环境光：优先用 HDRI Cubemap，失败则退回程序化天空或纯色。

    ═══════════════════════════════════════════════════════════════════
    V9.0 重写要点：

      1. 读取资产时先判断类型：
         TextureCube → 直接 set_cubemap()  ✅ 最理想
         Texture2D   → 尝试在运行时转成 Cubemap，失败则退回捕获模式

      2. 赋值后必须 recapture_sky()，否则 SkyLight 不会刷新，
         环境光仍是旧的（这就是之前"HDRI 不生效"的原因之一）。

      3. source_type 必须设成 SLS_SPECIFIED_CUBEMAP，
         否则即使设了 cubemap 属性，引擎仍然用捕获模式，cubemap 被忽略。

      4. 成功加载 HDRI 时，销毁 SkyAtmosphere，
         避免程序化天空和 HDRI 两个光源打架。

      5. hdri_intensity 独立控制环境光强度（配置项，默认 1.0）。

    参数：
      hdri_path  : .hdr/.exr 文件路径
      hdri_cfg   : render_config.json 的 hdri 段
      hide_sky_background : True 时不创建 SkyAtmosphere（背景保持纯色）

    返回 SkyLight actor（或 None）
    ═══════════════════════════════════════════════════════════════════
    """
    if ss is None:
        ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    cfg = hdri_cfg or {}

    # ═══════════════════════════════════════════════════════════════════
    # ★ hdri_preset 自动解析
    #   preset 优先于 path：如果填了 preset 且能找到文件，就用它；
    #   找不到则退回 path，再找不到就用纯色环境光（不崩溃）。
    # ═══════════════════════════════════════════════════════════════════
    _preset = str(cfg.get("hdri_preset") or "").strip()
    _hdir = str(cfg.get("hdri_dir") or "").strip()
    _explicit = str(hdri_path or "").strip()

    # ═══════════════════════════════════════════════════════════════════
    # ★ 主 preset 覆盖：如果 preset.active 指定了 hdri_preset，优先用它
    #   （由主程序在填充模板时已经算好，这里再兜一层）
    # ═══════════════════════════════════════════════════════════════════
    if not _preset:
        try:
            _pp = str(cfg.get("_preset_hdri") or "").strip()
            if _pp:
                _preset = _pp
                log("[HDRI] 使用 preset 指定的 HDRI: '{}'".format(_pp))
        except Exception:
            pass

    if _preset:
        log("[HDRI] 解析 preset: '{}'".format(_preset))
        _found = find_hdri_file(_preset, _hdir, "")
        if _found:
            hdri_path = _found
        else:
            log("[HDRI] preset 未命中，退回 path: {}".format(
                _explicit[:70] if _explicit else "(空)"))

    intensity = float(cfg.get("hdri_intensity", cfg.get("intensity", 1.0)))
    indirect = float(cfg.get("indirect_lighting_intensity", 1.0))
    sky_angle = float(cfg.get("source_cubemap_angle", 90.0))
    lower_black = bool(cfg.get("lower_hemisphere_is_black", False))

    log("[HDRI] ===== 开始布置环境光 =====")
    log("[HDRI] 目标强度 hdri_intensity = {:.2f}".format(intensity))

    # ---- 1. 清理旧环境光 ----
    try:
        n1 = destroy_actors_of_class(ss, unreal.SkyLight)
        n2 = destroy_actors_of_class(ss, unreal.SkyAtmosphere)
        n3 = destroy_actors_of_class(ss, unreal.DirectionalLight)
        n4 = destroy_actors_of_class(ss, unreal.ExponentialHeightFog)
        if n1 or n2 or n3 or n4:
            log("  清理旧环境光：Sky {} / Atmos {} / Sun {} / Fog {}".format(
                n1, n2, n3, n4))
    except Exception as exc:
        log("  清理旧环境光失败：{}".format(str(exc)[:100]))

    # ---- 2. 导入/加载 HDRI 资产 ----
    hdri_asset = None
    is_cube = False
    if hdri_path:
        try:
            hdri_asset, is_cube = import_hdri_texture(hdri_path)
        except Exception as exc:
            log("  [HDRI] 导入失败：{}".format(str(exc)[:120]))

    # 如果导入得到 2D，再试着按资产路径找同名 Cubemap
    if hdri_asset is not None and not is_cube:
        log("  [HDRI] 导入结果是 Texture2D，查找同名 TextureCube 资产")
        try:
            stem = os.path.splitext(os.path.basename(hdri_path))[0]
            for folder in ("/Game/DSH_Golem/Textures", "/Game/DSH_Env",
                           "/Game/DSH_Hdri", "/Game"):
                cand = "{}/{}".format(folder, stem)
                if unreal.EditorAssetLibrary.does_asset_exist(cand):
                    a = unreal.EditorAssetLibrary.load_asset(cand)
                    if a and isinstance(a, unreal.TextureCube):
                        hdri_asset = a
                        is_cube = True
                        log("  [HDRI] 找到 Cubemap 资产：{}".format(cand))
                        break
        except Exception as exc:
            log("  [HDRI] 查找 Cubemap 失败：{}".format(str(exc)[:100]))

    use_cubemap = (hdri_asset is not None and is_cube)
    if use_cubemap:
        log("  [HDRI] 资产类型：TextureCube ✓")
    elif hdri_asset is not None:
        log("  [HDRI] 资产类型：{} （非 Cubemap）".format(type(hdri_asset).__name__))
    else:
        log("  [HDRI] 无可用资产")

    # ---- 3. 决定是否建程序化天空 ----
    # ★ 成功加载 HDRI 时【绝不】创建 SkyAtmosphere，
    #    否则两个天空光会打架（你要求的第 3 点）
    need_atmos = (not use_cubemap) and (not hide_sky_background)
    if use_cubemap:
        # 再确认一次：销毁任何残留的 SkyAtmosphere
        try:
            n = destroy_actors_of_class(ss, unreal.SkyAtmosphere)
            if n:
                log("  [HDRI] 已销毁 {} 个残留 SkyAtmosphere（避免与 HDRI 冲突）".format(n))
        except Exception:
            pass
    elif need_atmos:
        log("  [HDRI] 使用程序化天空（SkyAtmosphere + 太阳）")
        try:
            setup_sky_atmosphere(ss)
        except Exception as exc:
            log("  [HDRI] 程序化天空失败：{}".format(str(exc)[:100]))

    # ---- 4. 创建 SkyLight ----
    skylight = None
    try:
        skylight = ss.spawn_actor_from_class(
            unreal.SkyLight, unreal.Vector(0, 0, 1000), unreal.Rotator(0, 0, 0))
        if not skylight:
            log("  [HDRI] SkyLight 创建失败")
            return None

        try:
            skylight.set_actor_label("DSH_SkyLight")
        except Exception:
            pass

        slc = comp_of(skylight, unreal.SkyLightComponent)
        if not slc:
            log("  [HDRI] 找不到 SkyLightComponent")
            return skylight

        # 基础属性
        props = {
            "mobility": unreal.ComponentMobility.MOVABLE,
            "intensity": intensity,
            "indirect_lighting_intensity": indirect,
            "source_cubemap_angle": sky_angle,
            "lower_hemisphere_is_black": lower_black,
        }
        ok, fails = set_props(slc, props)
        log("  [HDRI] 基础属性 {}/{} 项".format(ok, ok + len(fails)))

        # ---- 5. 核心：赋 Cubemap ----
        assigned = False
        if use_cubemap:
            # 5.1 先设 source_type —— 必须在赋 cubemap 之前，
            #     否则引擎仍走捕获模式，cubemap 被忽略
            try:
                slc.set_editor_property(
                    "source_type",
                    unreal.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP)
                log("  [HDRI] source_type = SLS_SPECIFIED_CUBEMAP")
            except Exception as exc:
                log("  [HDRI] source_type 设置失败：{}".format(str(exc)[:80]))

            # 5.2 赋 cubemap（两种 API 都试）
            for method in ("set_cubemap",):
                try:
                    getattr(slc, method)(hdri_asset)
                    assigned = True
                    log("  [HDRI] {}(asset) 成功".format(method))
                    break
                except Exception as exc:
                    log("  [HDRI] {} 失败：{}".format(method, str(exc)[:80]))
            if not assigned:
                try:
                    slc.set_editor_property("cubemap", hdri_asset)
                    assigned = True
                    log("  [HDRI] set_editor_property('cubemap', asset) 成功")
                except Exception as exc:
                    log("  [HDRI] cubemap 属性赋值失败：{}".format(str(exc)[:80]))

            # 5.3 ★ 必须刷新，否则环境光不更新
            if assigned:
                try:
                    slc.recapture_sky()
                    log("  [HDRI] recapture_sky() 已调用 ✓")
                    # ⚠️ 捕获是异步的，必须等它完成再继续，
                    #    否则后续场景操作可能和捕获线程冲突导致崩溃
                    import time as _t
                    _t.sleep(1.5)
                except Exception as exc:
                    log("  [HDRI] recapture_sky 失败：{}".format(str(exc)[:80]))
                    try:
                        slc.set_editor_property("real_time_capture", True)
                        log("  [HDRI] 退回 real_time_capture = True")
                    except Exception:
                        pass

            # 5.4 验证
            try:
                cur = slc.get_editor_property("cubemap")
                st = slc.get_editor_property("source_type")
                it = slc.get_editor_property("intensity")
                log("  [HDRI] 验证 cubemap={} source={} intensity={:.2f}".format(
                    cur.get_name() if cur else "None", st, it))
            except Exception:
                pass

        else:
            # 没有 Cubemap：捕获模式或纯色
            try:
                if need_atmos:
                    slc.set_editor_property(
                        "source_type", unreal.SkyLightSourceType.SLS_CAPTURED_SCENE)
                    slc.set_editor_property("real_time_capture", True)
                    try:
                        slc.recapture_sky()
                    except Exception:
                        pass
                    log("  [HDRI] 捕获模式（有程序化天空）")
                else:
                    slc.set_editor_property(
                        "source_type", unreal.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP)
                    log("  [HDRI] 无 cubemap，使用纯色环境光")
            except Exception as exc:
                log("  [HDRI] 设置环境光模式失败：{}".format(str(exc)[:80]))

        log("[HDRI] ===== 环境光布置完成（Cubemap={}）=====".format(assigned))
        return skylight

    except Exception as exc:
        log("  [HDRI] 布置失败：{}".format(str(exc)[:150]))
        return skylight


def compute_focus_distance(camera_loc, target_loc):
    """两点间距离 —— 手动对焦距离就用这个。"""
    dx = target_loc.x - camera_loc.x
    dy = target_loc.y - camera_loc.y
    dz = target_loc.z - camera_loc.z
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def setup_cine_camera_dof(center, size, camera_loc, look_at, cam_cfg, ss=None):
    """
    【任务 2】创建 CineCameraActor 并开启景深。

    参数：
      center      : 模型包围盒中心 unreal.Vector
      size        : 模型最大尺寸（float）
      camera_loc  : 相机位置 unreal.Vector
      look_at     : 看向的目标点 unreal.Vector（通常是模型中心）
      cam_cfg     : 配置字典（render_config.json 的 camera 段）

    返回 (CineCameraActor, dof_setup_ok)

    自动对焦逻辑：
      对焦距离 = 相机到目标的欧氏距离 + focus_offset
      这样无论相机放多远，焦点都精确落在模型中心上。
    """
    if ss is None:
        ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    cfg = cam_cfg or {}
    dof_cfg = cfg.get("depth_of_field", {}) or {}
    fov = float(cfg.get("fov", 38.0))

    dof_enabled = bool(dof_cfg.get("enabled", True))
    f_stop = float(dof_cfg.get("f_stop", 2.8))
    focal = float(dof_cfg.get("focal_length", 50.0))
    sensor = float(dof_cfg.get("sensor_width", 36.0))
    focus_method = str(dof_cfg.get("focus_method", "auto")).lower()
    focus_offset = float(dof_cfg.get("focus_offset", 0.0))

    log("[相机] 创建 CineCamera（f/{}, {}mm, DoF={}）".format(
        f_stop, focal, "开" if dof_enabled else "关"))

    # ---- 1. 计算朝向 ----
    dx = look_at.x - camera_loc.x
    dy = look_at.y - camera_loc.y
    dz = look_at.z - camera_loc.z
    horiz = math.sqrt(dx * dx + dy * dy)
    pitch = math.degrees(math.atan2(dz, horiz)) if horiz > 1e-6 else 0.0
    yaw = math.degrees(math.atan2(dy, dx))

    # ---- 2. 计算对焦距离 ----
    if focus_method == "manual" and float(dof_cfg.get("focus_distance", 0)) > 0:
        focus_dist = float(dof_cfg.get("focus_distance"))
        log("  对焦距离：手动指定 {:.1f}".format(focus_dist))
    else:
        focus_dist = compute_focus_distance(camera_loc, look_at) + focus_offset
        log("  对焦距离：按包围盒自动计算 {:.1f}（相机到模型中心）".format(focus_dist))

    # ---- 3. 清理旧相机 ----
    try:
        n = destroy_actors_of_class(ss, unreal.CineCameraActor)
        if n:
            log("  清理旧 CineCamera {} 个".format(n))
    except Exception:
        pass

    # ---- 4. 生成 CineCameraActor ----
    cam = None
    try:
        cam = ss.spawn_actor_from_class(
            unreal.CineCameraActor,
            unreal.Vector(camera_loc.x, camera_loc.y, camera_loc.z),
            unreal.Rotator(pitch, yaw, 0.0))
        if not cam:
            log("  CineCameraActor 创建失败")
            return None, False

        try:
            cam.set_actor_label("DSH_CineCamera")
        except Exception:
            pass

        cc = comp_of(cam, unreal.CineCameraComponent)
        if not cc:
            log("  找不到 CineCameraComponent")
            return cam, False

        # ---- 5. 基础光学参数 ----
        set_props(cc, {
            "current_focal_length": focal,
            "current_aperture": f_stop,
            # 视场角：用 filmback 控制更准确，但也设一下 FOV 兜底
            "field_of_view": fov,
        })

        # Filmback（传感器尺寸），影响景深计算
        try:
            fb = cc.get_editor_property("filmback")
            for prop, val in (("sensor_width", sensor),
                              ("sensor_height", sensor * 0.5625),  # 16:9
                              ("sensor_aspect_ratio", 16.0 / 9.0)):
                try:
                    fb.set_editor_property(prop, val)
                except Exception:
                    pass
            cc.set_editor_property("filmback", fb)
            log("  Filmback 传感器宽度 {:.1f}mm".format(sensor))
        except Exception as exc:
            log("  Filmback 设置失败：{}".format(str(exc)[:80]))

        # ---- 6. 开启景深 ★核心 ----
        dof_ok = False
        if dof_enabled:
            try:
                # UE5 的景深参数在 focus_settings 里
                fs = cc.get_editor_property("focus_settings")
                # ⚠️ 字段名以实测为准：
                #    focus_smoothing      → 实际是 focus_smoothing_interp_speed
                #    rack_focus_pause     → 实际是 smooth_focus_changes
                ok, fails = set_props(fs, {
                    "focus_method": unreal.CameraFocusMethod.MANUAL,
                    "manual_focus_distance": focus_dist,
                    "smooth_focus_changes": False,
                    "focus_offset": 0.0,
                })
                log("  FocusSettings 设置 {}/{} 项".format(ok, ok + len(fails)))
                for k, v in fails:
                    log("    未设置 {}：{}".format(k, v))
                cc.set_editor_property("focus_settings", fs)
                dof_ok = ok > 0
            except Exception as exc:
                log("  focus_settings 设置失败：{}".format(str(exc)[:100]))

            # 直接设 current_focus_distance 作为双保险
            try:
                cc.set_editor_property("current_focus_distance", focus_dist)
            except Exception:
                pass
        else:
            # 关闭景深：对焦到无限远，全画面清晰
            try:
                fs = cc.get_editor_property("focus_settings")
                fs.set_editor_property("focus_method", unreal.CameraFocusMethod.DISABLE)
                cc.set_editor_property("focus_settings", fs)
            except Exception:
                pass
            log("  景深已关闭（全部清晰）")

        log("[相机] 完成：位置 ({:.1f},{:.1f},{:.1f}) 朝向 (pitch={:.1f}, yaw={:.1f})".format(
            camera_loc.x, camera_loc.y, camera_loc.z, pitch, yaw))
        return cam, dof_ok

    except Exception as exc:
        log("  CineCamera 布置异常：{}".format(str(exc)[:150]))
        return cam, False


# =============================================================================
# 任务 3：把 config 应用到 PPV / 灯光 / 渲染质量
# =============================================================================

def apply_render_quality(cfg):
    """
    应用抗锯齿、屏幕百分比等质量参数（通过 CVar）。

    UE5 的 PostProcessSettings 里没有这些字段，只能走 CVar。
    """
    q = cfg.get("quality", {}) or {}
    aa = int(q.get("anti_aliasing_method", 4))
    sp = int(q.get("screen_percentage", 100))
    tsr = int(q.get("tsr_history_percentage", 200))

    log("[质量] AA={} ScreenPercentage={} TSRHistory={}".format(aa, sp, tsr))

    def set_cvar(name, value):
        try:
            lib = unreal.ConsoleVariablesEditorFunctionLibrary
            if isinstance(value, bool):
                lib.set_console_variable_by_name_bool(name, value)
            elif isinstance(value, float):
                lib.set_console_variable_by_name_float(name, value)
            else:
                lib.set_console_variable_by_name_int(name, int(value))
            return True
        except Exception:
            return False

    set_cvar("r.AntiAliasingMethod", aa)
    set_cvar("r.ScreenPercentage", sp)
    if aa == 4:   # TSR
        set_cvar("r.TSR.History.ScreenPercentage", tsr)

    set_cvar("r.MaxAnisotropy", int(q.get("max_anisotropy", 16)))
    set_cvar("r.ShadowQuality", int(q.get("shadow_quality", 5)))
    set_cvar("r.Streaming.MipBias", int(q.get("texture_mip_bias", 0)))

    if q.get("disable_motion_blur", True):
        # 只关运动模糊。⚠️ 千万不要在这里关景深！
        #   r.DepthOfFieldQuality = 0 会让 UE 直接禁用整个 DoF 后处理，
        #   表现为「PPV / SceneCapture 上的景深参数都设置成功了，
        #   但画面毫无虚化效果」——这个坑踩过一次，务必保留。
        set_cvar("r.MotionBlurQuality", 0)
        set_cvar("r.DefaultFeature.MotionBlur", 0)

    # ★ 景深质量：必须显式拉高，否则 DoF 不会生效
    #   0=关闭 1=低 2=高
    dof_quality = int(q.get("depth_of_field_quality", 2))
    if dof_quality <= 0:
        dof_quality = 2
    set_cvar("r.DepthOfFieldQuality", dof_quality)

    if q.get("disable_dynamic_resolution", True):
        set_cvar("r.DynamicRes.OperationMode", 0)


def setup_post_process(center, cfg, ss=None):
    """
    【任务 3】创建 PostProcessVolume，参数全部来自 config。

    包括：曝光 EV、泛光、对比度/饱和度、色调映射曲线。
    """
    if ss is None:
        ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    exp = cfg.get("exposure", {}) or {}
    bloom = cfg.get("bloom", {}) or {}

    # 清理旧 PPV
    try:
        n = destroy_actors_of_class(ss, unreal.PostProcessVolume)
        if n:
            log("  清理旧 PPV {} 个".format(n))
    except Exception:
        pass

    try:
        ppv = ss.spawn_actor_from_class(
            unreal.PostProcessVolume, unreal.Vector(center.x, center.y, center.z),
            unreal.Rotator(0, 0, 0))
        if not ppv:
            log("  PPV 创建失败")
            return None

        try:
            ppv.set_actor_label("DSH_PPV")
        except Exception:
            pass

        set_props(ppv, {"unbound": True, "blend_weight": 1.0,
                        "priority": 10.0, "enabled": True})

        pps = ppv.get_editor_property("settings")

        is_manual = str(exp.get("method", "manual")).lower() != "auto"
        props = {
            "override_auto_exposure_method": True,
            "auto_exposure_method": (
                unreal.AutoExposureMethod.AEM_MANUAL if is_manual
                else unreal.AutoExposureMethod.AEM_HISTOGRAM),
            "override_auto_exposure_bias": True,
            "auto_exposure_bias": float(exp.get("ev", -4.0)),
            "override_auto_exposure_apply_physical_camera_exposure": True,
            "auto_exposure_apply_physical_camera_exposure": False,
            "override_color_contrast": True,
            "color_contrast": unreal.Vector4(*[float(x) for x in
                                              exp.get("color_contrast", [1, 1, 1, 1])]),
            "override_color_saturation": True,
            "color_saturation": unreal.Vector4(*[float(x) for x in
                                                 exp.get("color_saturation", [1, 1, 1, 1])]),
            "override_film_slope": True,
            "film_slope": float(exp.get("film_slope", 0.88)),
            "override_film_toe": True,
            "film_toe": float(exp.get("film_toe", 0.55)),
        }

        if bloom.get("enabled", True):
            props.update({
                "override_bloom_intensity": True,
                "bloom_intensity": float(bloom.get("intensity", 1.0)),
                "override_bloom_threshold": True,
                "bloom_threshold": float(bloom.get("threshold", 1.0)),
            })
        else:
            props.update({"override_bloom_intensity": True, "bloom_intensity": 0.0})

        ok, fails = set_props(pps, props)
        log("[PPV] 设置 {}/{} 项（EV {}, 泛光 {}）".format(
            ok, ok + len(fails), props["auto_exposure_bias"],
            "开" if bloom.get("enabled", True) else "关"))
        for k, v in fails:
            log("    未设置 {}：{}".format(k, v))

        ppv.set_editor_property("settings", pps)
        return ppv

    except Exception as exc:
        log("  PPV 布置异常：{}".format(str(exc)[:150]))
        return None


def apply_dof_to_scene_capture(capture_comp, focus_distance, cam_cfg):
    """
    【关键】把景深参数应用到 SceneCaptureComponent2D。

    ⚠️ 为什么必须单独做这一步：
       SceneCaptureComponent2D.capture_scene() 使用的是它自己的
       post_process_settings，不会读取 CineCameraComponent.focus_settings。
       只在 CineCamera 上设 DoF，出的图依然是全清晰的（实测验证过）。

    参数：
      capture_comp    : unreal.SceneCaptureComponent2D
      focus_distance  : 对焦距离，单位【厘米】（UE 内部单位）
      cam_cfg         : render_config.json 的 camera 段

    返回是否成功应用。
    """
    if not capture_comp:
        return False

    cfg = cam_cfg or {}
    dof_cfg = cfg.get("depth_of_field", {}) or {}
    enabled = bool(dof_cfg.get("enabled", True))
    f_stop = float(dof_cfg.get("f_stop", 2.8))

    try:
        pps = capture_comp.get_editor_property("post_process_settings")
    except Exception as exc:
        log("  取 post_process_settings 失败：{}".format(str(exc)[:100]))
        return False

    if not enabled:
        # 关闭景深：直接关掉 override_depth_of_field_enabled
        ok, _ = set_props(pps, {
            "override_depth_of_field_enabled": False,
        })
        try:
            capture_comp.set_editor_property("post_process_settings", pps)
            capture_comp.set_editor_property("post_process_blend_weight", 1.0)
        except Exception:
            pass
        log("  [DoF] 已在 SceneCapture 上关闭景深")
        return ok > 0

    # ---- 开启景深 ----
    #
    # ⚠️ 字段名必须精确，以下是【实测确认】的 UE5.8 正确名称：
    #    depth_of_field_fstop           （不是 f_stop！）
    #    override_depth_of_field_fstop
    #    override_depth_of_field_enabled （总开关，必须显式打开）
    #    depth_of_field_focal_distance   （对焦距离，单位 cm）
    #    depth_of_field_depth_blur_amount（虚化强度 0~10）
    props = {
        # ★ 总开关
        "override_depth_of_field_enabled": True,
        # 光圈
        "override_depth_of_field_fstop": True,
        "depth_of_field_fstop": f_stop,
        # 对焦距离（cm）
        "override_depth_of_field_focal_distance": True,
        "depth_of_field_focal_distance": float(focus_distance),
        # 虚化强度，越大背景越糊
        "override_depth_of_field_depth_blur_amount": True,
        "depth_of_field_depth_blur_amount": 5.0,
        # 传感器宽度，影响景深范围
        "override_depth_of_field_sensor_width": True,
        "depth_of_field_sensor_width": float(
            (cam_cfg or {}).get("depth_of_field", {}).get("sensor_width", 36.0)),
    }
    ok, fails = set_props(pps, props)
    try:
        capture_comp.set_editor_property("post_process_settings", pps)
    except Exception as exc:
        log("  回写 post_process_settings 失败：{}".format(str(exc)[:100]))

    # ⚠️ 关键补充：SceneCaptureComponent2D 只有在
    #    post_process_blend_weight > 0 时才会应用自己的 post_process_settings。
    #    默认值是 0，表现为「设置都写进去了、但渲染出来毫无变化」。
    try:
        capture_comp.set_editor_property("post_process_blend_weight", 1.0)
        log("  [DoF] post_process_blend_weight = 1.0（让后处理生效）")
    except Exception as exc:
        log("  [DoF] 设置 blend_weight 失败：{}".format(str(exc)[:100]))

    log("  [DoF] SceneCapture 景深：f/{:.1f}, 对焦 {:.1f}cm（{}/{} 项）".format(
        f_stop, focus_distance, ok, ok + len(fails)))
    for k, v in fails:
        log("    未设置 {}：{}".format(k, v))
    return ok > 0


def setup_lights(center, size, cfg, ss=None):
    """
    【任务 3】三灯布光，强度/颜色/位置全部来自 config。
    """
    if ss is None:
        ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    L = cfg.get("lights", {}) or {}

    try:
        n = destroy_actors_of_class(ss, unreal.RectLight)
        if n:
            log("  清理旧 RectLight {} 个".format(n))
    except Exception:
        pass

    created = 0
    for key in ("key", "fill", "top"):
        spec = L.get(key) or {}
        try:
            off = spec.get("offset", [0, 0, 0])
            rot = spec.get("rotation", [0, 0, 0])
            col = spec.get("color", [255, 255, 255])
            sz = spec.get("size", [100, 100])

            loc = unreal.Vector(
                center.x + size * float(off[0]),
                center.y + size * float(off[1]),
                center.z + size * float(off[2]))

            a = ss.spawn_actor_from_class(
                unreal.RectLight, loc,
                unreal.Rotator(float(rot[0]), float(rot[1]), float(rot[2])))
            if not a:
                continue
            a.set_actor_label("DSH_Light_" + key)

            c = comp_of(a, unreal.RectLightComponent)
            if c:
                set_props(c, {
                    "mobility": unreal.ComponentMobility.MOVABLE,
                    "intensity_units": unreal.LightUnits.CANDELAS,
                    "intensity": float(spec.get("intensity", 100.0)),
                    "source_width": float(sz[0]),
                    "source_height": float(sz[1]),
                    "light_color": unreal.Color(int(col[0]), int(col[1]), int(col[2]), 255),
                })
                created += 1
        except Exception as exc:
            log("  灯光 {} 创建失败：{}".format(key, str(exc)[:100]))

    log("[灯光] 创建 {} 盏灯（主 {}cd / 补 {}cd / 顶 {}cd）".format(
        created,
        (L.get("key") or {}).get("intensity", 0),
        (L.get("fill") or {}).get("intensity", 0),
        (L.get("top") or {}).get("intensity", 0)))
    return created


# =============================================================================
# Movie Render Queue（替代 SceneCapture，解决涂抹感）
# =============================================================================
#
# 为什么要用 MRQ：
#   SceneCapture2D 的 capture_scene() 是「即时抓一帧」，
#   TSR 时域抗锯齿在单帧下没有历史累积，会留下明显的涂抹感/拖影。
#   而 MRQ 会：
#     · 按 Spatial Sample × Temporal Sample 多次累积采样
#     · 每帧之间推进一个固定的小时间步，让 TSR 有时间收敛
#     · 输出干净的、无涂抹的高质量图像
#
# 需要的 import（全部来自 unreal，无需额外 pip 包）：
#     import unreal
#   用到的类：
#     unreal.MoviePipelineQueueSubsystem      队列系统
#     unreal.MoviePipelineMasterConfig        渲染配置
#     unreal.MoviePipelineOutputSetting       输出分辨率/路径/帧范围
#     unreal.MoviePipelineAntiAliasingSetting 空间/时间采样
#     unreal.MoviePipelineImageSequenceOutput_PNG   PNG 输出
#     unreal.LevelSequence / LevelSequenceActor    序列与相机绑定
#     unreal.MovieSceneCameraCutTrack              相机切换轨道
#     unreal.MovieSceneCameraCutSection
#
# 本文件顶部已经 `import unreal`（延迟导入），所以只需在这里用 unreal.xxx。



def reposition_lights(center, size, shot_angle_deg):
    """
    把已有的三盏灯移动到新的拍摄角度方向。

    ═══════════════════════════════════════════════════════════════════
    为什么不重新 setup_lights：

      重建会新建 3 盏灯而不删旧的，多个角度后灯光叠加严重。
      这里只移动【已存在】的 DSH_ 灯，不新建、不删除，
      每个角度都能得到正确布光，且不会累积。

    参数：
      center          : 模型中心 unreal.Vector
      size            : 模型尺寸
      shot_angle_deg  : 当前拍摄角度（0/90/180/270…）
    ═══════════════════════════════════════════════════════════════════
    """
    import math as _m

    ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    # 找出所有 DSH 灯光
    lights = []
    try:
        for a in ss.get_all_level_actors():
            lbl = ""
            try:
                lbl = a.get_actor_label() or ""
            except Exception:
                continue
            if lbl.startswith("DSH_") and "Light" in lbl:
                lights.append((lbl, a))
    except Exception as exc:
        log("  [灯光] 枚举失败：{}".format(str(exc)[:60]))
        return 0

    if not lights:
        return 0

    lr = _m.radians(float(shot_angle_deg))
    k_dist = size * 1.2

    # 主光：角度 +45°
    k_ang = lr + _m.radians(45.0)
    k_loc = unreal.Vector(
        center.x + k_dist * _m.cos(k_ang),
        center.y + k_dist * _m.sin(k_ang),
        center.z + size * 0.55)

    # 补光：角度 -45°
    f_ang = lr - _m.radians(45.0)
    f_dist = size * 1.0
    f_loc = unreal.Vector(
        center.x + f_dist * _m.cos(f_ang),
        center.y + f_dist * _m.sin(f_ang),
        center.z + size * 0.25)

    # 顶光：模型正上方
    t_loc = unreal.Vector(center.x, center.y, center.z + size * 1.3)

    moved = 0
    for lbl, actor in lights:
        try:
            # 标签是 DSH_Light_key / DSH_Light_fill / DSH_Light_top（小写）
            low = lbl.lower()
            if "key" in low:
                actor.set_actor_location(k_loc, False, False)
                actor.set_actor_rotation(
                    unreal.Rotator(-15.0, _m.degrees(k_ang) + 180.0, 0.0), False)
            elif "fill" in low:
                actor.set_actor_location(f_loc, False, False)
                actor.set_actor_rotation(
                    unreal.Rotator(0.0, _m.degrees(f_ang) + 180.0, 0.0), False)
            elif "top" in low:
                actor.set_actor_location(t_loc, False, False)
            else:
                continue
            moved += 1
        except Exception:
            pass

    return moved


def _create_temp_level_sequence(seq_name, camera_actor, fps=24):
    """
    创建一个临时的 LevelSequence，把相机绑定到 CameraCut 轨道。

    参数：
      seq_name      : 序列资产名（不带路径，会自动放到 /Game/DSH_Seq）
      camera_actor  : 要绑定的 CineCameraActor
      fps           : 序列帧率

    返回 LevelSequence 资产；失败返回 None。

    ⚠️ 为什么要建 LevelSequence：
       MRQ 的渲染单元是「Job → 若干 Shot → 若干 Frame」，
       而 Shot 必须来自一个 LevelSequence。没有序列就没法指定相机。
    """
    try:
        seq_dir = "/Game/DSH_Seq"
        if not unreal.EditorAssetLibrary.does_directory_exist(seq_dir):
            unreal.EditorAssetLibrary.make_directory(seq_dir)

        full_path = "{}/{}".format(seq_dir, seq_name)

        # 若同名已存在，先删掉，避免累积垃圾
        if unreal.EditorAssetLibrary.does_asset_exist(full_path):
            try:
                unreal.EditorAssetLibrary.delete_asset(full_path)
            except Exception:
                pass

        # 用工厂创建
        tools = unreal.AssetToolsHelpers.get_asset_tools()
        seq = tools.create_asset(
            seq_name, seq_dir,
            unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
        if not seq:
            log("[MRQ] LevelSequence 创建失败")
            return None

        # ---- 1. 设置播放范围：0 到 1 帧（单帧出图）----
        try:
            seq.set_playback_start(0)
            seq.set_playback_end(1)
        except Exception:
            try:
                seq.set_editor_property("playback_start", 0)
                seq.set_editor_property("playback_end", 1)
            except Exception:
                pass

        # ---- 2. 添加相机绑定 ----
        binding = None
        try:
            # UE5 推荐用 add_possessable
            binding = seq.add_possessable(camera_actor)
        except Exception:
            try:
                binding = seq.add_actor(camera_actor)
            except Exception as exc:
                log("[MRQ] 相机绑定失败：{}".format(str(exc)[:100]))
                return None

        if not binding:
            log("[MRQ] 相机绑定返回空")
            return None
        log("[MRQ] 相机已绑定到序列")

        # ---- 3. 建 CameraCut 轨道（MRQ 靠它识别用什么相机渲染）----
        #
        # ⚠️ 关键：CameraCutSection 的相机绑定方式
        #   实测发现 add_possessable() 返回的 binding.get_id() 是【空 GUID】，
        #   直接用 set_editor_property("camera_binding_id", ...) 会关联失败，
        #   导致 MRQ 报 "No existing shot found in the job"，
        #   然后把整个序列范围（如 0-800 帧）都当成渲染范围 → 崩溃。
        #
        #   正确做法：用 CameraCutTrack.set_section_to_key(section, binding_id)
        #   或者用 section.set_editor_property("camera_binding_id", <有效GUID>)。
        #   这里两种都试，并做有效性校验。
        try:
            cut_track = seq.add_track(unreal.MovieSceneCameraCutTrack)
            section = cut_track.add_section()
            section.set_range(0, 1)

            # 取绑定 ID
            bid = None
            for getter in ("get_id",):
                try:
                    bid = getattr(binding, getter)()
                    break
                except Exception:
                    continue

            # 判断 GUID 是否为空（全 0）
            guid_ok = False
            try:
                s = str(bid)
                # 空 GUID 形如 "<Struct 'Guid' (...) {}>" 或全 0
                if bid is not None and "{}" not in s and "'a'" not in s:
                    guid_ok = True
            except Exception:
                pass

            if guid_ok:
                # 方式 A：set_section_to_key
                used = False
                try:
                    cut_track.set_section_to_key(section, bid)
                    used = True
                    log("[MRQ] CameraCut 已用 set_section_to_key 关联")
                except Exception as exc:
                    log("[MRQ] set_section_to_key 失败：{}".format(str(exc)[:80]))

                # 方式 B：直接设属性
                if not used:
                    try:
                        section.set_editor_property("camera_binding_id", bid)
                        used = True
                        log("[MRQ] CameraCut 已用 camera_binding_id 关联")
                    except Exception as exc:
                        log("[MRQ] camera_binding_id 失败：{}".format(str(exc)[:80]))
            else:
                # 绑定 ID 无效：退而求其次，用 binding 对象本身
                log("[MRQ] 绑定 ID 为空 GUID，尝试用绑定对象直接关联")
                try:
                    section.set_editor_property("camera_binding_id", binding)
                    log("[MRQ] 已用 binding 对象关联")
                except Exception as exc:
                    log("[MRQ] binding 对象关联也失败：{}".format(str(exc)[:80]))

        except Exception as exc:
            log("[MRQ] CameraCut 轨道创建失败：{}".format(str(exc)[:120]))

        # ---- 4. 设置显示帧率 ----
        try:
            seq.set_display_rate(unreal.FrameRate(fps, 1))
        except Exception:
            pass

        try:
            unreal.EditorAssetLibrary.save_loaded_asset(seq)
        except Exception:
            pass

        log("[MRQ] 临时序列已创建：{}".format(full_path))
        return seq

    except Exception as exc:
        log("[MRQ] 创建序列异常：{}".format(str(exc)[:150]))
        return None


def render_with_mrq(camera_actor, output_dir, cfg,
                    file_prefix="render", seq_name="DSH_TempSeq"):
    """
    用 Movie Render Queue 渲染单帧高质量 PNG。

    参数：
      camera_actor : 已创建的 CineCameraActor
      output_dir   : 输出目录（png 会落在这里）
      cfg          : render_config.json 的完整配置字典
      file_prefix  : 文件名前缀
      seq_name     : 临时序列名

    返回渲染出的 png 路径；失败返回 None。

    配置项（render_config.json → mrq 段，可缺省）：
      "mrq": {
        "enabled": true,
        "spatial_sample_count": 16,
        "temporal_sample_count": 16,
        "output_format": "png",
        "fps": 24
      }
    """
    try:
        mrq_cfg = (cfg or {}).get("mrq", {}) or {}
        spatial = int(mrq_cfg.get("spatial_sample_count", 16))
        temporal = int(mrq_cfg.get("temporal_sample_count", 16))
        fps = int(mrq_cfg.get("fps", 24))

        out_cfg = (cfg or {}).get("output", {}) or {}
        width = int(out_cfg.get("width", 1920))
        height = int(out_cfg.get("height", 1080))

        log("[MRQ] 开始渲染：{}x{}，Spatial={} Temporal={}".format(
            width, height, spatial, temporal))

        # ---- 1. 建临时序列 ----
        seq = _create_temp_level_sequence(seq_name, camera_actor, fps)
        if not seq:
            log("[MRQ] 序列创建失败，无法渲染")
            return None

        # ---- 2. 取队列子系统 ----
        try:
            qsub = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
            queue = qsub.get_queue()
            if not queue:
                log("[MRQ] 取不到队列")
                return None
        except Exception as exc:
            log("[MRQ] 队列子系统异常：{}".format(str(exc)[:120]))
            return None

        # ---- 3. 新建 Job ----
        try:
            job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
        except Exception as exc:
            log("[MRQ] 创建 Job 失败：{}".format(str(exc)[:120]))
            return None

        try:
            job.set_editor_property("sequence", unreal.SoftObjectPath(seq.get_path_name()))
        except Exception:
            try:
                job.sequence = seq
            except Exception as exc:
                log("[MRQ] 设置序列失败：{}".format(str(exc)[:100]))
                return None

        # ---- 4. 配置 ----
        master = job.get_configuration()
        if not master:
            try:
                master = unreal.MoviePipelineMasterConfig()
                job.set_configuration(master)
            except Exception as exc:
                log("[MRQ] 取配置失败：{}".format(str(exc)[:120]))
                return None

        # 4.1 输出设置：分辨率 / 目录 / 帧范围
        try:
            out_setting = master.find_or_add_setting_by_class(
                unreal.MoviePipelineOutputSetting)
            out_setting.set_editor_property(
                "output_resolution", unreal.IntPoint(width, height))
            # 输出目录用 DirectoryPath 结构
            try:
                dp = out_setting.get_editor_property("output_directory")
                dp.set_editor_property("path", output_dir.replace("\\", "/"))
                out_setting.set_editor_property("output_directory", dp)
            except Exception:
                try:
                    out_setting.set_editor_property("output_directory", output_dir)
                except Exception:
                    pass
            out_setting.set_editor_property("file_name_format", file_prefix + "_")
            # ⚠️ 属性名是 use_custom_playback_range，不是 use_custom_frame_range
            try:
                out_setting.set_editor_property("use_custom_playback_range", True)
            except Exception:
                pass
            out_setting.set_editor_property("custom_start_frame", 0)
            out_setting.set_editor_property("custom_end_frame", 0)
            out_setting.set_editor_property("zero_pad_frame_numbers", 0)
            log("[MRQ] 输出：{}  {}x{}".format(output_dir, width, height))
        except Exception as exc:
            log("[MRQ] 输出设置部分失败：{}".format(str(exc)[:120]))

        # 4.2 抗锯齿：空间/时间采样
        try:
            aa = master.find_or_add_setting_by_class(
                unreal.MoviePipelineAntiAliasingSetting)
            aa.set_editor_property("spatial_sample_count", spatial)
            aa.set_editor_property("temporal_sample_count", temporal)
            # 引擎预热帧，让 TSR 收敛
            aa.set_editor_property("engine_warm_up_count", 32)
            log("[MRQ] 采样：Spatial={} Temporal={} 预热=32".format(
                spatial, temporal))
        except Exception as exc:
            log("[MRQ] 抗锯齿设置失败：{}".format(str(exc)[:120]))

        # 4.3 输出格式：PNG
        try:
            master.find_or_add_setting_by_class(
                unreal.MoviePipelineImageSequenceOutput_PNG)
            log("[MRQ] 输出格式：PNG")
        except Exception as exc:
            log("[MRQ] PNG 输出设置失败：{}".format(str(exc)[:120]))

        # ---- 5. 执行渲染（阻塞式）----
        log("[MRQ] 开始执行（首次编译着色器可能较慢）…")
        try:
            qsub.render_queue_with_executor(unreal.MoviePipelineInProcessExecutor)
        except Exception as exc:
            log("[MRQ] 执行失败：{}".format(str(exc)[:150]))
            return None

        # ---- 6. 找输出文件 ----
        import time as _t
        found = None
        for _ in range(60):     # 最多等 30 秒
            try:
                cands = [f for f in os.listdir(output_dir)
                         if f.lower().endswith(".png") and file_prefix in f]
                if cands:
                    cands.sort(key=lambda f: os.path.getmtime(
                        os.path.join(output_dir, f)), reverse=True)
                    found = os.path.join(output_dir, cands[0])
                    break
            except Exception:
                pass
            _t.sleep(0.5)

        # ---- 7. 清理：删 Job + 临时序列 ----
        try:
            queue.delete_job(job)
            log("[MRQ] 已清理 Job")
        except Exception:
            pass
        try:
            unreal.EditorAssetLibrary.delete_asset(seq.get_path_name().split(".")[0])
            log("[MRQ] 已清理临时序列")
        except Exception:
            pass

        if found:
            log("[MRQ] 渲染完成：{}（{} 字节）".format(
                found, os.path.getsize(found)))
        else:
            log("[MRQ] 未找到输出文件（检查 {}）".format(output_dir))
        return found

    except Exception as exc:
        import traceback
        log("[MRQ] 渲染异常：{}".format(str(exc)[:200]))
        log(traceback.format_exc()[:800])
        return None

