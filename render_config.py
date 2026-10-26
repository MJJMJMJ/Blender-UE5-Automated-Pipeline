# -*- coding: utf-8 -*-
"""UE5 渲染参数外置化模块 —— render_config.json

这个模块负责：
  1. 定义 render_config.json 的完整结构与默认值
  2. 启动时读取；文件不存在则自动生成默认配置
  3. 提供带类型校验的安全取值接口（配置写错不会让程序崩）

设计原则：
  · 所有渲染参数都在这个文件里，改参数不用改代码、不用重新打包
  · 每一层都有默认值兜底，用户只改自己关心的字段即可
  · 读取失败时不会抛异常，而是退回默认值并记录日志
"""

import io
import json
import os


# =============================================================================
# 默认配置：完整的 render_config.json 结构
# =============================================================================
# 下面这份字典就是生成出来的 render_config.json 的内容。
# 每个字段都有中文说明，用户照着改就行。
DEFAULT_RENDER_CONFIG = {
    "_说明": "UE5 渲染参数配置。改这里不用改代码，重启工具生效。",

    # -------------------------------------------------------------------------
    # 输出
    # -------------------------------------------------------------------------
    "output": {
        "_说明": "图片输出设置",
        "width": 1920,
        "height": 1536,
        "file_prefix": "render",       # 输出文件名前缀，最终为 <前缀>_<角度>.png
        "min_valid_bytes": 102400,     # 小于这个字节数视为黑图/空图
    },

    # -------------------------------------------------------------------------
    # 渲染质量
    # -------------------------------------------------------------------------
    "quality": {
        "_说明": "抗锯齿与渲染质量。TSR 是 UE5 新一代时域超分抗锯齿",
        "anti_aliasing_method": 4,     # 0=None 1=FXAA 2=TAA 3=MSAA 4=TSR 5=SMAA
        "screen_percentage": 100,      # 100=原生分辨率渲染
        "tsr_history_percentage": 200, # TSR 历史缓冲超采样，越高越锐利
        "texture_mip_bias": 0,         # 负数更锐利但可能有噪点
        "max_anisotropy": 16,
        "shadow_quality": 5,
        "disable_motion_blur": True,
        "disable_dynamic_resolution": True,
        # 景深质量：0=关闭 1=低 2=高。设为 0 会让 DoF 完全失效，别改
        "depth_of_field_quality": 2,
    },

    # -------------------------------------------------------------------------
    # 曝光与色彩
    # -------------------------------------------------------------------------
    "exposure": {
        "_说明": "手动曝光。EV 越负越暗。实测 -4.0 左右最合适",
        "method": "manual",            # manual | auto
        "ev": -3.5,                    # 曝光补偿 EV
        "color_contrast": [1.08, 1.08, 1.08, 1.0],
        "color_saturation": [1.12, 1.12, 1.12, 1.0],
        "film_slope": 0.88,
        "film_toe": 0.55,
    },

    # -------------------------------------------------------------------------
    # 泛光（Bloom）
    # -------------------------------------------------------------------------
    "bloom": {
        "_说明": "泛光让高光溢出、增加光感。阈值越高只有越亮的部分才发光",
        "_说明2": "泛光会让画面发糊发灰，锁画质时默认关闭",
        "enabled": False,
        "intensity": 0.0,
        "threshold": 1.0,
    },

    # -------------------------------------------------------------------------
    # HDRI 环境光（任务1）
    # -------------------------------------------------------------------------
    "hdri": {
        "_说明": "HDRI 环境贴图。path 为空或文件不存在时，自动退回程序化天空",
        "hdri_intensity": 1.5,
        # ═══════════════════════════════════════════════════════════
        # HDRI 风格预设：填文件名（不含扩展名），程序会在 hdri_dir
        # 及其子目录中自动搜索匹配的 .hdr / .exr
        #
        # 示例：
        #   "hdri_preset": "studio_kontrast_04_4k"
        #   "hdri_preset": "sunset"        （模糊匹配，找名字含 sunset 的）
        # 留空则使用 path 字段的完整路径
        # ═══════════════════════════════════════════════════════════
        "hdri_preset": "",
        # HDRI 素材库根目录（程序会递归搜索其下所有子目录）
        "hdri_dir": "",
        "path": "",                    # 例：D:/Assets/HDRI/studio.hdr
        "intensity": 1.0,              # SkyLight 强度
        "indirect_lighting_intensity": 1.0,
        "source_cubemap_angle": 90.0,  # 天空球角度，影响环境光方向
        "lower_hemisphere_is_black": False,
        "real_time_capture": False,    # True=实时刷新（更贵）
        "recapture_on_setup": True,    # 设置完是否强制刷新一次
        "fallback_sky_color": [0.05, 0.06, 0.09],  # 无 HDRI 时的天空底色
    },

    # -------------------------------------------------------------------------
    # 灯光（任务3外置化）
    # -------------------------------------------------------------------------
    "lights": {
        "_说明": "三灯布光。单位为坎德拉(cd)",
        "key": {
            "_说明": "主光：冷白，从相机侧打来，决定主体明暗。"
                     "调低后靠 HDRI 环境光补足，避免高光死白",
            "intensity": 50.0,
            "color": [180, 208, 255],
            "offset": [-0.9, 0.35, 0.45],   # 相对包围盒尺寸的偏移倍率
            "rotation": [-15, 250, 0],
            "size": [200.0, 1.5],           # [宽, 高倍率]
        },
        "fill": {
            "_说明": "补光：暖色，压暗部，让细节不死黑",
            "intensity": 20.0,
            "color": [255, 222, 185],
            "offset": [0.85, -0.3, 0.15],
            "rotation": [0, 70, 0],
            "size": [160.0, 240.0],
        },
        "top": {
            "_说明": "顶光：柔光，补亮头顶和肩甲。随主光一起调低，保持光比",
            "intensity": 25.0,
            "color": [235, 240, 255],
            "offset": [0.0, 0.0, 1.1],
            "rotation": [-90, 0, 0],
            "size": [0.35, 0.6],            # 相对尺寸倍率
        },
    },

    # -------------------------------------------------------------------------
    # 摄像机与景深（任务2）
    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # 天光（SkyLight）
    # -------------------------------------------------------------------------
        # -------------------------------------------------------------------------
    # 交付归档
    # -------------------------------------------------------------------------
    "archive": {
        "_说明": "渲染成功后，把图片/日志/资产自动归档到这里",
        "enabled": True,
        "archive_dir": "",
    },
    "work_root": {
        "_说明": "接单工作区。zip 包会解压到这里并重命名为英文",
        "path": "",
    },
    "inbox": {
        "_说明": "接单收件箱目录。放客户发来的 zip，一键扫描即可选",
        "dir": "",
    },

    "skylight": {
        "_说明": "强制补环境光。HDRI cubemap 加载失败时靠它避免背光面纯黑",
        "enabled": True,
        "intensity": 1.5,
        "color": [0.55, 0.62, 0.78],
    },

    "camera": {
        "_说明": "电影摄像机。开启景深可虚化背景、突出主体",
        "fov": 38.0,
        "distance_factor": 2.2,        # 相机距离 = 模型最大尺寸 × 这个系数
        "angles": [180, 0, 90, 270],   # 候选方位角，按顺序尝试
        "height_offset": 0.0,          # 相机相对模型中心的高度偏移倍率

        "depth_of_field": {
            "_说明": "景深。enabled=false 时背景和主体一样清晰",
            "enabled": True,
            "f_stop": 2.8,             # 光圈，越小背景越虚
            "focal_length": 50.0,      # 焦距(mm)，越长压缩感越强
            "sensor_width": 36.0,      # 传感器宽度(mm)，全画幅=36
            "focus_method": "auto",    # auto=按包围盒自动对焦 / manual=用 focus_distance
            "focus_distance": 0.0,     # focus_method=manual 时生效
            "focus_offset": 0.0,       # 对焦点相对模型中心的偏移（米）
        },
    },

    # -------------------------------------------------------------------------
    # 背景墙（Cube 实现）
    # -------------------------------------------------------------------------
    "background_wall": {
        "_说明": "用 Cube 生成的背景墙。Cube 是实体，无朝向问题",
        "enabled": True,
        "color": [0.5, 0.5, 0.52],
        "emissive_strength": 6.0,
        "distance_factor": 1.5,
        "wall_scale": 8.0,
    },

    # -------------------------------------------------------------------------
    # 风格预设（材质 + HDRI 捆绑）
    # -------------------------------------------------------------------------
    "preset": {
        "_说明": "选择 preset 名，自动套用对应的材质模式和 HDRI。留空则不启用",
        "active": "",
    },
    "presets": {
        "_说明": "预设定义。每个 preset 捆绑一套材质模式 + HDRI + 曝光 + 灯光",
        "Mech_DarkMetal": {
            "material_mode": "dark_metal",
            "hdri_preset": "studio_kontrast",
            "hdri_intensity": 1.5,
            "exposure_ev": -3.0,
            "lights": {
                "key": {"intensity": 60.0, "color": [200, 215, 255]},
                "fill": {"intensity": 25.0, "color": [255, 225, 195]},
                "top": {"intensity": 30.0, "color": [235, 240, 255]},
            },
            "base_color": [0.10, 0.08, 0.05],
        },
        "Sunset_Biological": {
            "material_mode": "rough_skin",
            "hdri_preset": "sunset",
            "hdri_intensity": 1.8,
            "exposure_ev": -1.8,
            "lights": {
                "key": {"intensity": 80.0, "color": [255, 190, 140]},
                "fill": {"intensity": 30.0, "color": [180, 200, 255]},
                "top": {"intensity": 20.0, "color": [255, 220, 180]},
            },
            "base_color": [0.22, 0.16, 0.12],
        },
        "Cyber_Emissive": {
            "material_mode": "emissive",
            "hdri_preset": "cyberpunk",
            "hdri_intensity": 0.8,
            "exposure_ev": -2.5,
            "lights": {
                "key": {"intensity": 40.0, "color": [120, 200, 255]},
                "fill": {"intensity": 45.0, "color": [255, 80, 200]},
                "top": {"intensity": 15.0, "color": [100, 255, 220]},
            },
            "base_color": [0.03, 0.05, 0.08],
        },
        "Ruins_Dark": {
            "material_mode": "dark_metal",
            "hdri_preset": "ruins",
            "hdri_intensity": 0.6,
            "exposure_ev": -2.0,
            "lights": {
                "key": {"intensity": 35.0, "color": [190, 180, 200]},
                "fill": {"intensity": 12.0, "color": [255, 200, 150]},
                "top": {"intensity": 10.0, "color": [200, 200, 220]},
            },
            "base_color": [0.08, 0.07, 0.06],
        },
    },

    # -------------------------------------------------------------------------
    # 模型变换
    # -------------------------------------------------------------------------
    "model": {
        "_说明": "模型导入后的变换",
        "rotation_yaw": 0.0,           # 绕 Z 轴（UE 的 Yaw）旋转角度，正数逆时针
        "scale": 1.0,                  # 整体缩放
        "force_lod0": True,            # 强制最高精度 LOD
        "apply_smooth": True,
        # ★ 固化暗金属基底色：Albedo 贴图缺色，改用纯色才有质感
        "use_custom_basecolor": True,
        "custom_basecolor": [0.1, 0.08, 0.05],
        "debug_red_basecolor": False,          # 尝试应用平滑着色
    },
}


# =============================================================================
# 默认配置文件名
# =============================================================================
RENDER_CONFIG_FILENAME = "render_config.json"


def get_render_config_path(base_dir=None):
    """返回 render_config.json 的绝对路径（与工具同级）。"""
    if base_dir:
        return os.path.join(base_dir, RENDER_CONFIG_FILENAME)
    # 与 Pipeline_Tool.py / exe 同目录
    import sys
    if getattr(sys, "frozen", False):
        d = os.path.dirname(os.path.abspath(sys.executable))
    else:
        d = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(d, RENDER_CONFIG_FILENAME)


def _deep_merge(defaults, override):
    """
    递归合并：以 defaults 为骨架，用 override 覆盖。
    这样用户只写改了的那几个字段也能正常工作。
    """
    if not isinstance(override, dict):
        return defaults

    out = dict(defaults)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _sanitize(cfg):
    """
    把配置里的值做类型清洗，防止用户手写出错（如把数字写成字符串）。
    任何清洗失败都退回默认值，绝不因为配置错误让程序崩。
    """
    def to_int(v, d):
        try:
            return int(float(v))
        except Exception:
            return d

    def to_float(v, d):
        try:
            return float(v)
        except Exception:
            return d

    def to_bool(v, d):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        try:
            return bool(int(v))
        except Exception:
            return d

    def to_list(v, d, n=None):
        try:
            if not isinstance(v, (list, tuple)):
                return d
            out = [float(x) for x in v]
            if n and len(out) != n:
                return d
            return out
        except Exception:
            return d

    D = DEFAULT_RENDER_CONFIG

    o = cfg.get("output", {})
    o["width"] = max(64, to_int(o.get("width"), D["output"]["width"]))
    o["height"] = max(64, to_int(o.get("height"), D["output"]["height"]))
    o["file_prefix"] = str(o.get("file_prefix") or D["output"]["file_prefix"])
    o["min_valid_bytes"] = to_int(o.get("min_valid_bytes"), D["output"]["min_valid_bytes"])

    q = cfg.get("quality", {})
    q["anti_aliasing_method"] = to_int(q.get("anti_aliasing_method"), 4)
    q["screen_percentage"] = max(10, to_int(q.get("screen_percentage"), 100))
    q["tsr_history_percentage"] = max(100, to_int(q.get("tsr_history_percentage"), 200))
    q["texture_mip_bias"] = to_int(q.get("texture_mip_bias"), 0)
    q["max_anisotropy"] = to_int(q.get("max_anisotropy"), 16)
    q["shadow_quality"] = to_int(q.get("shadow_quality"), 5)
    q["disable_motion_blur"] = to_bool(q.get("disable_motion_blur"), True)
    q["disable_dynamic_resolution"] = to_bool(q.get("disable_dynamic_resolution"), True)
    # 景深质量必须 >= 1，否则 DoF 被引擎直接禁用
    q["depth_of_field_quality"] = max(1, to_int(q.get("depth_of_field_quality"), 2))

    e = cfg.get("exposure", {})
    e["method"] = "auto" if str(e.get("method", "")).lower() == "auto" else "manual"
    e["ev"] = to_float(e.get("ev"), D["exposure"]["ev"])
    e["color_contrast"] = to_list(e.get("color_contrast"), D["exposure"]["color_contrast"], 4)
    e["color_saturation"] = to_list(e.get("color_saturation"), D["exposure"]["color_saturation"], 4)
    e["film_slope"] = to_float(e.get("film_slope"), D["exposure"]["film_slope"])
    e["film_toe"] = to_float(e.get("film_toe"), D["exposure"]["film_toe"])

    b = cfg.get("bloom", {})
    b["enabled"] = to_bool(b.get("enabled"), True)
    b["intensity"] = to_float(b.get("intensity"), D["bloom"]["intensity"])
    b["threshold"] = to_float(b.get("threshold"), D["bloom"]["threshold"])

    h = cfg.get("hdri", {})
    h["path"] = str(h.get("path") or "")
    h["hdri_intensity"] = to_float(
        h.get("hdri_intensity"), D["hdri"].get("hdri_intensity", 1.5))
    h["hdri_preset"] = str(h.get("hdri_preset") or "")
    h["hdri_dir"] = str(h.get("hdri_dir") or "")
    h["intensity"] = to_float(h.get("intensity"), D["hdri"]["intensity"])
    h["indirect_lighting_intensity"] = to_float(
        h.get("indirect_lighting_intensity"), 1.0)
    h["source_cubemap_angle"] = to_float(h.get("source_cubemap_angle"), 90.0)
    h["lower_hemisphere_is_black"] = to_bool(h.get("lower_hemisphere_is_black"), False)
    h["real_time_capture"] = to_bool(h.get("real_time_capture"), False)
    h["recapture_on_setup"] = to_bool(h.get("recapture_on_setup"), True)
    h["fallback_sky_color"] = to_list(
        h.get("fallback_sky_color"), D["hdri"]["fallback_sky_color"], 3)

    L = cfg.get("lights", {})
    for key in ("key", "fill", "top"):
        src = L.get(key, {})
        dst = D["lights"][key]
        src["intensity"] = to_float(src.get("intensity"), dst["intensity"])
        src["color"] = to_list(src.get("color"), dst["color"], 3)
        src["offset"] = to_list(src.get("offset"), dst["offset"], 3)
        src["rotation"] = to_list(src.get("rotation"), dst["rotation"], 3)
        src["size"] = to_list(src.get("size"), dst["size"], 2)
        L[key] = src

    ar = cfg.get("archive", {})
    if not isinstance(ar, dict):
        ar = {}
    ar["enabled"] = to_bool(ar.get("enabled"), True)
    ar["archive_dir"] = str(ar.get("archive_dir") or "")
    cfg["archive"] = ar

    wr = cfg.get("work_root", {})
    if not isinstance(wr, dict):
        wr = {}
    wr["path"] = str(wr.get("path") or "")
    cfg["work_root"] = wr

    ib = cfg.get("inbox", {})
    if not isinstance(ib, dict):
        ib = {}
    ib["dir"] = str(ib.get("dir") or "")
    cfg["inbox"] = ib

    sk = cfg.get("skylight", {})
    if not isinstance(sk, dict):
        sk = {}
    sk["enabled"] = to_bool(sk.get("enabled"), True)
    sk["intensity"] = to_float(sk.get("intensity"), 1.5)
    sk["color"] = to_list(sk.get("color"), [0.55, 0.62, 0.78], 3)
    cfg["skylight"] = sk

    c = cfg.get("camera", {})
    c["fov"] = to_float(c.get("fov"), D["camera"]["fov"])
    c["distance_factor"] = to_float(c.get("distance_factor"), D["camera"]["distance_factor"])
    c["angles"] = [to_int(x, 0) for x in (c.get("angles") or D["camera"]["angles"])]
    if not c["angles"]:
        c["angles"] = list(D["camera"]["angles"])
    c["height_offset"] = to_float(c.get("height_offset"), 0.0)

    dof = c.get("depth_of_field", {})
    ddof = D["camera"]["depth_of_field"]
    dof["enabled"] = to_bool(dof.get("enabled"), True)
    dof["f_stop"] = max(0.1, to_float(dof.get("f_stop"), ddof["f_stop"]))
    dof["focal_length"] = max(1.0, to_float(dof.get("focal_length"), ddof["focal_length"]))
    dof["sensor_width"] = max(1.0, to_float(dof.get("sensor_width"), ddof["sensor_width"]))
    dof["focus_method"] = "manual" if str(dof.get("focus_method", "")).lower() == "manual" else "auto"
    dof["focus_distance"] = to_float(dof.get("focus_distance"), 0.0)
    dof["focus_offset"] = to_float(dof.get("focus_offset"), 0.0)
    c["depth_of_field"] = dof

    bw = cfg.get("background_wall", {})
    if not isinstance(bw, dict):
        bw = {}
    bw["enabled"] = to_bool(bw.get("enabled"), True)
    bw["color"] = to_list(bw.get("color"), [0.5, 0.5, 0.52], 3)
    bw["emissive_strength"] = to_float(bw.get("emissive_strength"), 6.0)
    bw["distance_factor"] = to_float(bw.get("distance_factor"), 1.5)
    bw["wall_scale"] = to_float(bw.get("wall_scale"), 8.0)
    cfg["background_wall"] = bw

    m = cfg.get("model", {})
    m["use_custom_basecolor"] = to_bool(m.get("use_custom_basecolor"), True)
    m["custom_basecolor"] = to_list(m.get("custom_basecolor"), [0.1, 0.08, 0.05], 3)
    m["debug_red_basecolor"] = to_bool(m.get("debug_red_basecolor"), False)
    m["rotation_yaw"] = to_float(m.get("rotation_yaw"), 0.0)
    m["scale"] = max(0.001, to_float(m.get("scale"), 1.0))
    m["force_lod0"] = to_bool(m.get("force_lod0"), True)
    m["apply_smooth"] = to_bool(m.get("apply_smooth"), True)

    return cfg


def load_render_config(path=None, logger=None):
    """
    读取 render_config.json。

    · 文件不存在 → 自动生成一份默认配置并返回
    · 文件损坏   → 备份为 .bad，重新生成默认配置
    · 字段缺失   → 用默认值补齐（支持只写部分字段）
    · 类型错误   → 自动纠正或退回默认值

    返回 dict（保证结构完整、可直接用）。
    """
    def log(msg):
        if logger:
            try:
                logger(msg)
            except Exception:
                pass

    p = path or get_render_config_path()

    # ---- 1. 不存在 → 生成默认 ----
    if not os.path.isfile(p):
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(DEFAULT_RENDER_CONFIG, ensure_ascii=False, indent=2))
            log("[配置] 未找到 {}，已生成默认配置".format(os.path.basename(p)))
        except Exception as exc:
            log("[配置] 生成默认配置失败：{}（使用内置默认值）".format(exc))
        return _sanitize(json.loads(json.dumps(DEFAULT_RENDER_CONFIG)))

    # ---- 2. 读取并合并 ----
    try:
        with io.open(p, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        if not isinstance(user_cfg, dict):
            raise ValueError("根节点不是对象")
    except Exception as exc:
        log("[配置] {} 解析失败：{}，已备份并使用默认配置".format(
            os.path.basename(p), exc))
        # 备份损坏的旧文件
        try:
            os.replace(p, p + ".bad")
        except Exception:
            pass
        # 同时重新生成一份可用的默认配置，这样用户下次打开能直接编辑
        try:
            with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(DEFAULT_RENDER_CONFIG, ensure_ascii=False, indent=2))
            log("[配置] 已重新生成默认配置文件：{}".format(os.path.basename(p)))
        except Exception:
            pass
        return _sanitize(json.loads(json.dumps(DEFAULT_RENDER_CONFIG)))

    merged = _deep_merge(DEFAULT_RENDER_CONFIG, user_cfg)
    merged = json.loads(json.dumps(merged))   # 深拷贝，避免污染 DEFAULT
    merged = _sanitize(merged)

    log("[配置] 已读取 {}（{}x{}, EV {}, DoF {})".format(
        os.path.basename(p),
        merged["output"]["width"], merged["output"]["height"],
        merged["exposure"]["ev"],
        "开" if merged["camera"]["depth_of_field"]["enabled"] else "关"))
    return merged


def dump_render_config(cfg, path=None):
    """把配置写回文件（用于工具里保存用户改动）。返回 (ok, path)。"""
    p = path or get_render_config_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(cfg, ensure_ascii=False, indent=2))
        return True, p
    except Exception as exc:
        return False, str(exc)
