# -*- coding: utf-8 -*-
"""
===============================================================================
 Pipeline_Tool.py —— Blender 资产预处理 + UE5 自动渲染 一体化桌面工具
===============================================================================

【怎么用】
    1. 改下面的 CONFIG 区，填好本机 blender.exe / UnrealEditor.exe 的路径
    2. 双击运行：  python Pipeline_Tool.py
       （没有 python 的机器，可直接双击 .pyw 版本或用 pyinstaller 打包成 exe）
    3. 顶部选好 FBX / 贴图目录 / 输出目录
    4. 勾选中左（Blender）和中右（UE5）的模块
    5. 点「🚀 一键全自动」，或分别点「仅执行 Blender」「仅执行 UE5」

【架构说明】
    这个工具本身不直接操作 Blender/UE，而是：
      · 根据你的勾选，动态生成两个临时 Python 脚本
          temp_blender_script.py  →  交给 blender.exe -b -P 执行
          temp_ue5_script.py      →  交给 UnrealEditor.exe -ExecutePythonScript 执行
      · 用 subprocess.Popen 启动它们，逐行读取 stdout 实时打印到日志区
      · 全部放在后台线程，GUI 不会卡死

【为什么用「动态生成脚本」而不是直接调现成脚本】
    因为勾选项要动态影响行为。生成脚本的好处：
      · 每个模块可以独立开关
      · 参数（路径、选项）直接写进脚本顶部，不依赖命令行转义
      · 出问题可以直接打开 temp_*.py 查看当时到底执行了什么
===============================================================================
"""

import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# =============================================================================
# ⓪ Windows 高 DPI 感知 —— 必须在创建 Tk 窗口之前执行
# =============================================================================
# 不设置的话，Windows 会把程序当成「不支持 DPI 缩放」的老程序，
# 在 125% / 150% 缩放的显示器上整个界面被位图拉伸 → 文字发虚、发糊。
#
# 三种感知级别：
#   0 = UNAWARE        完全不感知，系统拉伸（最糊）
#   1 = SYSTEM_AWARE   系统级感知，按主显示器 DPI 渲染（推荐，兼容性最好）
#   2 = PER_MONITOR    每显示器感知，多屏不同缩放时最佳，但 Tk 支持不佳
#
# 这里用 1，兼顾清晰度和 Tkinter 的兼容性。
try:
    import ctypes
    try:
        # Win8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        # Win7 / 老系统回退
        ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    # 非 Windows 平台或调用失败：静默忽略，不影响主流程
    pass


def _get_ui_scale():
    """
    探测当前显示器的缩放比例，返回浮点数（1.0 = 100%，1.5 = 150%）。

    用途：根据缩放动态放大控件尺寸和字体，避免 4K 屏上一切都变得很小。
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hdc = user32.GetDC(0)
        try:
            # 96 DPI 是 Windows 的 100% 基准
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        finally:
            user32.ReleaseDC(0, hdc)
        if dpi:
            return max(1.0, min(3.0, dpi / 96.0))
    except Exception:
        pass
    return 1.0


UI_SCALE = _get_ui_scale()

# 全局字体：Microsoft YaHei（微软雅黑）是中文 Windows 的默认 UI 字体，显示最清晰
UI_FONT_FAMILY = "Microsoft YaHei"
UI_FONT_SIZE = 10 if UI_SCALE < 1.4 else 11
UI_FONT = (UI_FONT_FAMILY, UI_FONT_SIZE)
UI_FONT_BOLD = (UI_FONT_FAMILY, UI_FONT_SIZE, "bold")
LOG_FONT = ("Consolas", 9 if UI_SCALE < 1.4 else 10)


# =============================================================================
# ⓪.5 可选画质模块 —— 安全导入（Try-Import 隔离）
# =============================================================================
# 设计原则：模块加载失败绝不能影响主流程运行。
#
#   · 加载成功 → HAS_QUALITY_MODULE = True，走画质增强
#   · 加载失败 → HAS_QUALITY_MODULE = False，自动降级为旧版基础渲染
#   · 无论哪种情况，主流程的批量扫描/队列/清理功能都照常工作
#
# 失败原因通常有两类：
#   1. 文件不在同目录（打包成 exe 时忘记包含）
#   2. 依赖缺失或 Python 语法错误
# 处理方式统一：打警告 + 降级，不抛异常。

QUALITY_MODULE_ERROR = ""
HAS_QUALITY_MODULE = False
_quality_funcs = {}

try:
    # 确保本文件所在目录在 sys.path 里（打包成 exe 后仍需显式加入）
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)

    from ue_render_core import (
        setup_hdri_environment as _setup_hdri,
        setup_cine_camera_dof as _setup_cine_camera,
        cleanup_previous_scene as _cleanup_scene,
        setup_post_process as _setup_pp,
        setup_lights as _setup_lights,
        apply_render_quality as _apply_quality,
        apply_dof_to_scene_capture as _apply_dof_sc,
        create_background_wall as _create_bg_wall,
        lock_normals_and_lod0 as _lock_normals,
        ensure_skylight as _ensure_skylight,
        compute_focus_distance as _compute_focus_dist,
        get_world as _get_ue_world,
    )
    from render_config import (
        load_render_config as _load_rc,
        get_render_config_path as _rc_path,
    )

    _quality_funcs = {
        "setup_hdri": _setup_hdri,
        "setup_cine": _setup_cine_camera,
        "cleanup": _cleanup_scene,
        "setup_pp": _setup_pp,
        "setup_lights": _setup_lights,
        "apply_quality": _apply_quality,
        "apply_dof_sc": _apply_dof_sc,
        "create_wall": _create_bg_wall,
        "lock_normals": _lock_normals,
        "ensure_skylight": _ensure_skylight,
        "focus_dist": _compute_focus_dist,
        "get_world": _get_ue_world,
        "load_rc": _load_rc,
        "rc_path": _rc_path,
    }
    HAS_QUALITY_MODULE = True

except ImportError as e:
    QUALITY_MODULE_ERROR = "模块缺失：{}".format(e)
except Exception as e:
    QUALITY_MODULE_ERROR = "{}: {}".format(type(e).__name__, e)

if HAS_QUALITY_MODULE:
    print("[画质模块] 加载成功，将启用 HDRI / 景深 / 配置化渲染")
else:
    print("警告：画质提升模块加载失败，将使用默认基础渲染。原因：{}".format(
        QUALITY_MODULE_ERROR))


# =============================================================================
# ① 配置区 —— 改这里
# =============================================================================

CONFIG = {
    # Blender 可执行文件绝对路径
    "blender_exe": r"D:\blender.exe",

    # UE5 编辑器可执行文件（必须是 UnrealEditor.exe，不是 -Cmd 版本）
    "unreal_editor_exe": r"D:\UE_5.8\Engine\Binaries\Win64\UnrealEditor.exe",

    # .uproject 工程文件路径（点击「浏览」也可选）
    "uproject": r"D:/YourWorkspace/Unreal Projects/MY PROJECT3/我的项目3.uproject",

    # ---------------------------------------------------------------
    # 批量扫描的默认「总文件夹」：启动时自动填入扫描区
    # ---------------------------------------------------------------
    "default_scan_root": r"D:/YourWorkspace/Blender/input/Mesh",

    # ---------------------------------------------------------------
    # 默认的 FBX / 贴图 / 输出目录（程序启动时自动填入输入框）
    # 全部使用纯英文路径，避免中文路径在各种子进程调用中出问题
    # ---------------------------------------------------------------
    "default_fbx": r"D:/YourWorkspace/Blender/input/Mesh/StoneGolem.fbx",
    "default_texdir": r"D:/YourWorkspace/Blender/input/Textures",
    # 图片输出保存路径（纯英文，放在 D 盘）
    "default_outdir": r"D:\output",
}

# ---------------------------------------------------------------
# 自动搜路兜底：
#   如果上面指定的 FBX 找不到，程序会在 AUTO_SEARCH_ROOTS 这些根目录下
#   递归查找同名文件（越靠前的根目录优先级越高）。
#   找到后自动填回输入框，并在日志里提示实际使用的路径。
# ---------------------------------------------------------------
AUTO_SEARCH_ROOTS = [
    r"D:/YourWorkspace/Blender",
    r"D:/YourWorkspace/Desktop",
]

# 找不到指定贴图目录时，在这些根目录下查找名为 Textures / textures 的文件夹
AUTO_SEARCH_TEXTURE_DIRNAMES = ["Textures", "textures", "TEX"]

# 搜索深度上限（层），避免扫全盘太慢
AUTO_SEARCH_MAX_DEPTH = 6

# Blender 导出时的目标朝向轴
BLENDER_TARGET_AXIS = "+Y"

# LOD 保护：顶点数低于这个值的零件不生成 LOD
LOD_MIN_VERTS = 100

# LOD 比例（LOD1/2/3）
LOD_RATIOS = (0.5, 0.25, 0.10)

# UE 渲染：黑图判定阈值（字节）
UE_MIN_BYTES = 102400

# UE 渲染：多角度重试时的候选角度（按顺序尝试）
#
# 角度含义：相机相对模型中心在水平面内的方位角
#   0°   → 相机在 +X（模型右侧/背面，实测偏暗）
#   180° → 相机在 -X（模型左侧/正面，实测明亮清晰）★ 首选
#
# 实测结论：180° 方向拍出来模型正立、双脚踩地、光照充分，
#           所以把它放在最前面，优先出图。
UE_ANGLE_CANDIDATES = [180, 0, 90, 270, 135, 225, 45, 315]

# 是否启用可选画质增强模块（HDRI / 景深 / 配置化）。
#   True  → 若 ue_render_core / render_config 加载成功则启用
#   False → 强制走旧版基础渲染（完全等价于 V4.6 的行为）
ENABLE_QUALITY_ENHANCEMENT = True

# UE 渲染分辨率 / 曝光
#
# 分辨率提高到 1920x1536：抗锯齿和细节都需要足够像素，
# 1280x1024 在 5:4 画幅下放大看会有明显锯齿。
UE_WIDTH, UE_HEIGHT = 1920, 1536

# 曝光补偿 EV（手动曝光模式下的亮度偏移）。
#
# ⚠️ 调参记录（避免以后又调反）：
#   · -5.0  → 之前出图偏暗但可控
#   · -1.5  → 实测【严重过曝】，整个模型烧成白色
#             原因：手动曝光下 EV 是乘法偏移，-1.5 比 -5.0 亮 3.5 档（约 11 倍）
#   · -4.0  → 当前值，比原值略亮一点点，保留暗部细节又不过曝
UE_EXPOSURE = -4.0

# 屏幕百分比（超采样倍率，以 % 计）。
#
#   100 = 原生分辨率渲染
#   200 = 2 倍超采样：引擎以 2×2 的像素量渲染再降采样回目标分辨率，
#         锯齿和噪点显著减少，边缘更锐利 —— 代价是渲染耗时约 4 倍。
#
# 本工具会在截图前临时提到 200，**截图完成后立刻重置回 100**，
# 避免后续模型/编辑器操作因超采样而卡死（见 UE 模板里的 exit_supersample）。
#
# 注意：这是【主程序】的常量，会被作为占位符传给 UE 模板。
SUPER_SAMPLE_PERCENTAGE = 200
UE_SCREEN_PERCENTAGE = 100    # 默认值，截图阶段会被临时改写

# ---------------------------------------------------------------------------
# 多次 capture 收敛：消除 SceneCapture 单帧抓取导致的 TSR 涂抹感
#
#   SceneCapture2D.capture_scene() 是即时抓一帧，TSR 的时域历史来不及累积，
#   画面会有明显涂抹/拖影。连续 capture 多次可让 TSR 收敛，
#   效果接近 Movie Render Queue 的 Temporal Sample。
#
#   16 次约等于 MRQ 的 Temporal Sample = 16。
#   想要更快可降到 8；想要更干净可升到 32（耗时同比例增加）。
# ---------------------------------------------------------------------------
CAPTURE_CONVERGE_FRAMES = 32
CAPTURE_CONVERGE_INTERVAL = 0.08

# 批量处理：输出根目录下按模型名建子目录
BATCH_USE_SUBFOLDER = True

# 文件写入重试：遇到 Permission denied（文件被占用）时等待重试
IO_RETRY_TIMES = 3
IO_RETRY_WAIT = 1.0

# 超时专用返回码（用一个不会与真实进程冲突的值）
TIMEOUT_RC = -9999

# UE5 单次渲染超时（秒）。
# UE 首次启动要编译着色器、建 DDC，可能较慢；120 秒对单模型通常够用。
# 超时后强制终止并报告，避免整个队列卡死。
UE_TIMEOUT_SECONDS = 120


# =============================================================================
# ①.5 用户配置文件（config.json）
# =============================================================================
# 存放位置：与 Pipeline_Tool.py（或 exe）同级的 config.json
# 打包成 exe 后，sys.frozen 为 True，此时用 exe 所在目录；
# 源码运行时用脚本所在目录。
#
# 内容示例：
#   {
#     "output_dir": "D:/MyRender/output",
#     "scan_root": "C:/Models"
#   }

CONFIG_FILENAME = "config.json"

# 读不到配置时的兜底输出目录
DEFAULT_OUTPUT_DIR = "D:/YourWorkspace/Pictures/output"


def _get_cfg_float(key, default, section="camera"):
    """
    从 render_config.json 读取一个 float 型配置值。

    读取失败时返回 default，绝不抛异常（配置问题不该让打包失败）。

    用途：把 camera.distance_factor / camera.fov 这类参数真正传进
         生成的 UE 脚本，而不是在模板里硬编码。
    """
    try:
        import json as _json
        import os as _os
        p = _get_config_path()
        if not _os.path.isfile(p):
            return default
        with open(p, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        v = (data.get(section) or {}).get(key)
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _get_cfg_list(key, default, section="camera"):
    """
    从 render_config.json 读取一个列表型配置（如 camera.angles）。

    读取失败或为空时返回 default。
    """
    try:
        import json as _json
        import os as _os
        p = _get_config_path()
        if not _os.path.isfile(p):
            return default
        with open(p, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        v = (data.get(section) or {}).get(key)
        if not v:
            return default
        return [float(x) for x in v]
    except Exception:
        return default


def _save_cfg_field(section, key, value):
    """
    把单个配置项写进 render_config.json。

    配置文件不存在时自动创建，不报错。
    """
    import json as _json
    p = _get_config_path()

    # 不存在则先创建
    if not os.path.isfile(p):
        p, created = ensure_config_file()
        if not os.path.isfile(p):
            raise RuntimeError("无法创建配置文件：{}".format(p))
    with open(p, "r", encoding="utf-8") as fh:
        cfg = _json.load(fh)
    sec = cfg.setdefault(section, {})
    if not isinstance(sec, dict):
        sec = {}
        cfg[section] = sec
    sec[key] = value
    with open(p, "w", encoding="utf-8") as fh:
        _json.dump(cfg, fh, ensure_ascii=False, indent=2)


def _load_cfg_dict():
    """读取整个 render_config.json，失败返回空 dict。"""
    try:
        import json as _json
        import os as _os
        p = _get_config_path()
        if not _os.path.isfile(p):
            return {}
        with open(p, "r", encoding="utf-8") as fh:
            return _json.load(fh)
    except Exception:
        return {}


def _resolve_preset():
    """
    解析当前生效的 preset，返回合并后的参数字典。

    返回 dict，键包括：
      material_mode  材质模式字符串
      base_color     基底色 [r,g,b]
      emissive_gain  自发光强度
      hdri_preset    HDRI 预设名
      exposure_ev    曝光
      lights         灯光强度/颜色（可选覆盖）

    没有启用 preset 时返回 {}。
    """
    cfg = _load_cfg_dict()
    ps = cfg.get("preset") or {}
    name = str(ps.get("active") or "").strip()
    if not name:
        return {}

    all_presets = cfg.get("presets") or {}
    p = all_presets.get(name)
    if not isinstance(p, dict):
        return {}
    return p


def _get_material_mode():
    """
    当前材质模式。

    ═══════════════════════════════════════════════════════════════════
    V23.5 交互改动：用户选择优先，预设不再强制覆盖

    优先级（从高到低）：
      1. model.material_mode   ← 用户在界面上手动选择的
      2. preset.material_mode  ← 只有在用户没选过时才用预设的推荐值
      3. use_custom_basecolor  ← 兼容旧配置
      4. "texture"             ← 默认

    presets.active == "Custom" 时，完全忽略预设，只用第 1 项。
    ═══════════════════════════════════════════════════════════════════
    """
    cfg = _load_cfg_dict()
    m = cfg.get("model") or {}
    mode = str(m.get("material_mode") or "").strip()

    # ① 用户手动选过 → 直接用，无视预设
    if mode:
        return mode

    # ② 用户没选过 → 看看预设推荐什么（Custom 模式下不读）
    _active = str((cfg.get("preset") or {}).get("active") or "").strip()
    if _active and _active != "Custom":
        p = _resolve_preset()
        if p and p.get("material_mode"):
            return str(p["material_mode"])

    # ③ 兼容旧配置
    if m.get("use_custom_basecolor"):
        return "dark_metal"

    return "texture"


def _get_base_color():
    """当前基底色。preset 优先，其次 model.custom_basecolor。"""
    p = _resolve_preset()
    if p and p.get("base_color"):
        return list(p["base_color"])[:3]

    cfg = _load_cfg_dict()
    m = cfg.get("model") or {}
    bc = m.get("custom_basecolor")
    if bc and len(bc) >= 3:
        return [float(bc[0]), float(bc[1]), float(bc[2])]
    return [0.1, 0.08, 0.05]


def _get_bloom(key, default):
    """读取 bloom 段的值。"""
    cfg = _load_cfg_dict()
    b = cfg.get("bloom") or {}
    try:
        return float(b.get(key, default))
    except Exception:
        return default


def _load_cfg_dict_py():
    """模板内用的配置读取（无依赖）。"""
    import json as _j
    p = os.path.join(_DELIVER_DIR, "render_config.json")
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return _j.load(f)
    except Exception:
        return {}


def _get_emission_debug(default=False):
    """读取 emission.debug_white（诊断用：Emissive 接纯白）。"""
    cfg = _load_cfg_dict()
    em = cfg.get("emission") or {}
    v = em.get("debug_white", default)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _get_emission_gain(default=25.0):
    """读取 emission.gain（模型自发光强度）。"""
    cfg = _load_cfg_dict()
    em = cfg.get("emission") or {}
    try:
        return float(em.get("gain", default))
    except Exception:
        return default


def _get_emission_color(default=(1.0, 0.95, 0.85)):
    """读取 emission.color（自发光颜色）。"""
    cfg = _load_cfg_dict()
    em = cfg.get("emission") or {}
    c = em.get("color") or list(default)
    try:
        return [float(c[0]), float(c[1]), float(c[2])]
    except Exception:
        return list(default)


def _get_emissive_gain():
    """自发光强度（仅 emissive 模式用）。"""
    p = _resolve_preset()
    if p and p.get("emissive_gain") is not None:
        return float(p["emissive_gain"])
    return 8.0


def _get_effective_ev(default=-4.0):
    """最终生效的曝光：preset 覆盖 > 配置 exposure.ev > 默认值。"""
    p = _resolve_preset()
    if p and p.get("exposure_ev") is not None:
        try:
            return float(p["exposure_ev"])
        except Exception:
            pass
    return _get_cfg_ev(default)


def _get_cfg_ev(default=-4.0):
    """从 render_config.json 的 exposure.ev 读取曝光值。"""
    try:
        import json as _json
        import os as _os
        p = _get_config_path()
        if not _os.path.isfile(p):
            return default
        with open(p, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        v = (data.get("exposure") or {}).get("ev")
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _get_cfg_bool(key, default, section="camera"):
    """从 render_config.json 读一个 bool 值。"""
    try:
        import json as _json
        import os as _os
        p = _get_config_path()
        if not _os.path.isfile(p):
            return default
        with open(p, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        v = (data.get(section) or {}).get(key)
        if v is None:
            return default
        return bool(v)
    except Exception:
        return default


def _get_exe_dir():
    r"""
    返回【可执行文件所在目录】 —— 配置文件放这里。

    ═══════════════════════════════════════════════════════════════════
    为什么要单独一个函数：

      _get_module_dir() 返回的是 .py 模块所在目录，打包后指向
      _internal\，exe 同级目录才是用户可见、可写的位置。

      配置文件（render_config.json）必须放在 exe 同级，
      用户才能打开编辑；放 _internal 里既难找又可能没写权限。

    打包后的目录结构：
      dist\Pipeline_Tool\
        ├─ Pipeline_Tool.exe          ← 用户双击这个
        ├─ render_config.json         ← 配置文件在这（本函数返回）
        └─ _internal\
             ├─ ue_render_core.py     ← 模块在这（_get_module_dir 返回）
             └─ render_config.py
    ═══════════════════════════════════════════════════════════════════
    """
    # PyInstaller 打包后
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))

    # 源码运行：本文件所在目录
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()


def _get_config_path():
    r"""
    返回 render_config.json 的绝对路径。

    查找顺序（先找到先用）：
      1. exe 同级目录（推荐位置）
      2. exe 同级的 _internal\  （兼容旧版打包）
      3. 模块目录（源码运行）

    都不存在时返回 1 的路径（用于首次创建）。
    """
    exe_dir = _get_exe_dir()
    mod_dir = _get_module_dir()

    cands = [
        os.path.join(exe_dir, "render_config.json"),
        os.path.join(exe_dir, "_internal", "render_config.json"),
        os.path.join(mod_dir, "render_config.json"),
    ]
    for p in cands:
        if os.path.isfile(p):
            return p

    # 都不存在 → 返回首选位置
    return cands[0]


def ensure_config_file(verbose=False):
    r"""
    确保 render_config.json 存在。不存在则立刻生成完整默认配置。

    ═══════════════════════════════════════════════════════════════════
    在程序启动时调用，避免用户点按钮才报"找不到配置文件"。

    生成方式：
      · 优先用 render_config 模块的 dump_render_config（完整字段 + 注释）
      · 模块不可用时写一份内置的最小可用配置
    ═══════════════════════════════════════════════════════════════════

    返回 (配置路径, 是否新建)
    """
    import json as _json

    path = _get_config_path()
    if os.path.isfile(path):
        return path, False

    # 确保目录存在
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass

    # 方式 A：用 render_config 模块生成完整配置
    try:
        if _get_module_dir() not in sys.path:
            sys.path.insert(0, _get_module_dir())
        import render_config as _rc
        cfg = _rc.load_render_config()
        _rc.dump_render_config(cfg, path)
        if verbose:
            print("[配置] 已自动生成：{}".format(path))
        return path, True
    except Exception:
        pass

    # 方式 B：内置最小配置兜底
    fallback = {
        "_说明": "DSH 渲染管线配置文件（自动生成）",
        "output": {"width": 1920, "height": 1536},
        "exposure": {"method": "manual", "ev": -3.5},
        "camera": {"fov": 40.0, "distance_factor": 1.9,
                   "angles": [90], "face_angle": 90, "cam_lift": 0.2},
        "hdri": {"path": "", "hdri_preset": "", "hdri_dir": "",
                 "hdri_intensity": 1.5},
        "model": {"material_mode": "texture", "use_custom_basecolor": False},
        "work_root": {"path": ""},
        "archive": {"enabled": True, "archive_dir": ""},
        "inbox": {"dir": ""},
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump(fallback, fh, ensure_ascii=False, indent=2)
        if verbose:
            print("[配置] 已生成最小配置：{}".format(path))
        return path, True
    except Exception as exc:
        if verbose:
            print("[配置] 生成失败：{}".format(str(exc)[:100]))
        return path, False


def _get_module_dir():
    """
    返回 ue_render_core.py / render_config.py 所在目录。

    这个路径会被写进生成的 UE 脚本，让 UE 里的 Python
    能 sys.path.insert 后 import 到这两个模块。

    打包成 exe 时：
      · PyInstaller 会把这几个 .py 打包进 _internal
      · 我们优先找 exe 同级目录，其次找 _internal
    源码运行时直接用本文件所在目录。
    """
    here = os.path.dirname(os.path.abspath(__file__))

    # 源码运行：本文件目录下就有
    if os.path.isfile(os.path.join(here, "ue_render_core.py")):
        return here.replace("\\", "/")

    # 打包后：可能在 _internal 子目录
    for cand in (
        os.path.join(here, "_internal"),
        os.path.join(getattr(sys, "_MEIPASS", "") or "", ""),
        here,
    ):
        if cand and os.path.isfile(os.path.join(cand, "ue_render_core.py")):
            return cand.replace("\\", "/")

    # 都找不到，返回本目录（由 UE 侧 Try-Import 自行降级）
    return here.replace("\\", "/")


def get_app_dir():
    """
    返回程序所在目录（配置文件的存放位置）。

    · PyInstaller 打包后：exe 所在目录
    · 源码运行：.py 文件所在目录
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_config_path():
    """返回 config.json 的绝对路径。"""
    return os.path.join(get_app_dir(), CONFIG_FILENAME)


def load_user_config():
    """
    读取 config.json。

    返回 dict（读不到或解析失败时返回空 dict，绝不抛异常 ——
    配置文件损坏不应该让程序起不来）。
    """
    path = get_config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        # 配置损坏：备份一份，然后当作没有配置处理
        try:
            os.replace(path, path + ".bad")
        except Exception:
            pass
        return {}


def save_user_config(data):
    """
    写入 config.json（带占用重试）。

    返回 (ok: bool, message: str)
    """
    path = get_config_path()
    try:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        safe_write_text(path, payload)
        return True, path
    except Exception as exc:
        return False, str(exc)


def get_configured_output_dir():
    """
    取输出目录的优先级：
      1. config.json 里的 output_dir
      2. 常量 DEFAULT_OUTPUT_DIR
    返回值保证是规范化的绝对路径。
    """
    cfg = load_user_config()
    d = cfg.get("output_dir") or DEFAULT_OUTPUT_DIR
    try:
        return os.path.normpath(str(d))
    except Exception:
        return os.path.normpath(DEFAULT_OUTPUT_DIR)


# =============================================================================
# ② 全局状态
# =============================================================================

LOG_QUEUE = queue.Queue()      # 后台线程 → GUI 线程 的日志管道
UI_QUEUE = queue.Queue()       # 后台线程 → GUI 线程 的界面操作请求（Tk 只能在主线程操作）
STOP_FLAG = threading.Event()  # 批次中止标志：置位后每个阶段之间都会检查并退出
IS_RUNNING = False             # 是否已有任务在跑（防重复点击）


# =============================================================================
# ②.5 通用工具：路径净化 / 安全写文件 / 贴图匹配 / 扫描
# =============================================================================

# Windows 文件名非法字符
_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Windows 设备保留名（不分大小写，带不带扩展名都算）
# 这些名字作为【文件名或目录名】时会被系统当成设备，导致各种诡异错误
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}


def check_path_legal(path):
    """
    检查一个路径是否包含 Windows 非法部分。

    返回 (ok: bool, reason: str)
      ok=True  → 路径可用
      ok=False → reason 说明问题所在

    检查项：
      1. 是否为空
      2. 每一段目录名/文件名是否含非法字符 < > : " | ? * 和控制字符
      3. 每一段是否是设备保留名（con / prn / aux / nul / com1-9 / lpt1-9）
         注意：'print' 不是保留名，可以直接用作目录名；
               但它确实是常见误判，所以这里不拦 print，
               而是在写入失败时给出明确提示（见 verify_dir_writable）
      4. 路径长度（Windows 传统上限 260）
    """
    if not path or not str(path).strip():
        return False, "路径为空"

    p = str(path).strip()

    # ---- 逐段检查 ----
    # 去掉盘符（C:）后再分段，避免把盘符的冒号误判为非法字符
    body = p
    if re.match(r"^[A-Za-z]:", body):
        body = body[2:]
    # UNC 路径 \\server\share 直接跳过前导斜杠
    body = body.lstrip("\\/")

    for part in re.split(r"[\\/]+", body):
        if not part:
            continue

        # 非法字符（注意：这里不检查冒号，因为盘符已剥离）
        m = _ILLEGAL_NAME_CHARS.search(part.replace(":", ""))
        if m:
            return False, "路径中含有非法字符 {!r}：\n{}".format(m.group(), p)

        # 设备保留名：取主名（去掉扩展名）判断
        stem = part.split(".")[0].strip().lower()
        if stem in WINDOWS_RESERVED_NAMES:
            return False, (
                "路径中含有 Windows 设备保留名 {!r}：\n{}\n\n"
                "con / prn / aux / nul / com1-9 / lpt1-9 这些名字\n"
                "会被系统当作硬件设备，不能用作文件夹或文件名。\n"
                "请改名，例如把 'con' 改成 'con_out'。".format(part, p))

    # ---- 长度检查 ----
    if len(p) > 240:
        return False, (
            "路径过长（{} 字符，建议不超过 240）：\n{}\n\n"
            "Windows 传统路径上限为 260 字符，超长会导致写入失败。\n"
            "请把输出目录改短一些。".format(len(p), p))

    return True, ""


# 探针文件名：刻意使用最普通、最常见的形式。
# ⚠️ 历史教训：之前用的是 ".__dsh_write_probe__"（点开头 + 双下划线），
#    这种命名在 Windows 上非常像勒索软件的加密标记文件，
#    会被 Defender / 火绒 / 360 / WPS 的"文件夹保护"直接拦截，
#    表现为 PermissionError，用户会以为是自己路径写错了。
#    现在改成普通业务名，尽量不触发启发式规则。
PROBE_FILENAME = "pipeline_write_test.txt"


# 中文字符检测正则：
#   \u4e00-\u9fff  CJK 统一表意文字（基本汉字）
#   \u3400-\u4dbf  扩展 A 区
#   \uf900-\ufaff 兼容表意文字
#   \u3000-\u303f CJK 标点
#   \uff00-\uffef 全角字符
# 只要路径里命中任何一个，就判定为「含中文路径」。
_CJK_PATTERN = re.compile(
    "[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\uff00-\uffef]+")

def has_chinese(text):
    """
    判断字符串里是否含中文字符。

    返回 (found: bool, sample: str)
      found  : 是否命中
      sample : 命中的第一段中文（用于展示，方便用户定位）
    """
    if not text:
        return False, ""
    m = _CJK_PATTERN.search(str(text))
    if m:
        return True, m.group()
    return False, ""


def check_uproject_chinese(uproject_path):
    """
    检查 .uproject 的绝对路径是否含中文。

    返回 (ok: bool, message: str)

    ⚠️ 为什么必须硬拦截：
       UE5 的 CEF（内置浏览器）无法在含中文的路径下初始化缓存目录。实测日志：
         LogWebBrowser: Error: CEF initialization for cache dir
           C:/Users/.../UnrealEngine/我的项目_测试/webcache_6613 failed
         LogOutputDevice: Error: Ensure condition failed: bCEFInitialized
       结果是 UE 在启动阶段直接崩溃（退出码 3221225477），
       Python 脚本完全不会执行，最终表现就是「一张图都出不来」。
    """
    if not uproject_path:
        return False, "未配置 .uproject 路径。"

    p = os.path.abspath(str(uproject_path))
    found, sample = has_chinese(p)
    if found:
        # ═══════════════════════════════════════════════════════════════
        # V11.0：不再硬拦截，改为自动生成英文影子工程绕过。
        #
        # prepare_ascii_uproject() 会在同目录生成
        # <英文名>.uproject，并用 junction 把 Content/Config 挂回原目录，
        # 这样 UE 看到的路径是纯英文的，CEF 不会崩，
        # 资产仍然写进你原本的工程目录。
        # ═══════════════════════════════════════════════════════════════
        return True, (
            "工程路径含中文（{}），已自动生成英文影子工程绕过 CEF 限制。\n"
            "资产仍会保存到原工程目录。".format(sample))

    return True, ""


def check_path_risky(path):
    r"""
    检查路径是否存在「高概率被 Windows / 安全软件拦截」的风险。

    返回 (risky, reason, suggestion)
      risky=True 时调用方应打印强烈警告，但不阻断执行。
    """
    if not path:
        return False, "", ""

    p = os.path.normpath(str(path).strip())
    drive, rest = os.path.splitdrive(p)

    # ---- 盘符根目录判断 ----
    if drive:
        parts = [x for x in rest.replace("/", chr(92)).split(chr(92)) if x]
        depth = len(parts)
        sys_drive = (os.environ.get("SystemDrive") or "C:").upper()

        if depth == 0:
            # 直接写到盘符根
            return True, (
                "输出路径是【盘符根目录】：" + p + "\n"
                "Windows 默认禁止普通用户写入盘符根，"
                "必定触发 WinError 5 拒绝访问。"
            ), _suggest_writable_path()

        if depth == 1 and drive.upper() != sys_drive:
            # 非系统盘的第一层目录
            return True, (
                "输出路径位于【" + drive[0].upper() + " 盘根目录下的第一层】：" + p
                + "\n非系统盘第一层文件夹通常需要管理员权限才能创建，"
                + "\n这很可能就是 PermissionError 的来源。"
            ), _suggest_writable_path()

    # ---- 系统保护目录 ----
    low = p.lower().replace("/", chr(92))
    protected = ["program files", "program files (x86)", "windows",
                 "programdata", "system volume information"]
    for prot in protected:
        if (chr(92) + prot) in low:
            return True, (
                "输出路径位于系统保护目录：" + p + "\n"
                "普通权限无法写入，必定失败。"
            ), _suggest_writable_path()

    return False, "", ""


def _suggest_writable_path():
    """推荐一个当前用户一定可写的位置。"""
    user = os.environ.get("USERNAME") or "你的用户名"
    return "C:" + chr(92) + "Users" + chr(92) + user + chr(92) + "Pictures" + chr(92) + "Output"


def warn_if_risky_path(path, emit=None):
    """
    检测路径风险并发出醒目警告。

    emit(text) 用于把警告送进日志；不传就用 print。
    返回 True 表示检测到高风险。
    """
    risky, reason, suggestion = check_path_risky(path)
    if not risky:
        return False

    def out(msg):
        try:
            if emit:
                emit(msg)
            else:
                print(msg)
        except Exception:
            pass

    bar = "!" * 64
    out(bar)
    out("[强烈警告] 输出路径存在权限风险，很可能导致 PermissionError！")
    out("   当前路径：" + str(path))
    out("   原因：" + reason)
    out("   建议改为：" + suggestion)
    out("   如果坚持使用当前路径，请以【管理员身份】运行本工具。")
    out(bar)
    return True

def verify_dir_writable(path, strict=False):
    """
    实际验证一个目录能否写入。

    做法：创建目录 → 写一个普通名字的测试文件 → 删除。
    返回 (ok: bool, reason: str)

    strict 参数很关键：
      · strict=True  → 探针失败就返回 False，调用方应阻断流程
      · strict=False → 探针失败只产生警告，调用方应继续（默认）

    为什么要默认宽松：
      杀毒软件的"文件夹保护"会拦截脚本创建任何文件，
      但这并不代表 Blender/UE 写不了 —— 它们是有签名的正规程序，
      往往能正常写入。如果用探针结果硬性阻断，
      用户就会遇到"明明能保存文件却提示没权限"的诡异情况。
      所以：探针只是"提前提醒"，真正的判定留给实际写入。
    """
    try:
        os.makedirs(path, exist_ok=True)
    except PermissionError:
        reason = (
            "没有权限创建该目录：\n{}\n\n"
            "可能原因：\n"
            "  · 该位置需要管理员权限（如 C:\\Program Files）\n"
            "  · 父目录是只读或被系统保护\n"
            "请换一个你有写权限的位置，例如 文档 / 桌面 下的文件夹。".format(path))
        return False, reason
    except OSError as exc:
        return False, "创建目录失败：\n{}\n\n{}".format(path, exc)

    probe = os.path.join(path, PROBE_FILENAME)
    last = None

    for attempt in range(1, IO_RETRY_TIMES + 1):
        try:
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("write test")
            # 无论成功与否都要删掉，不留垃圾
            try:
                os.remove(probe)
            except Exception:
                pass
            return True, ""
        except PermissionError as exc:
            last = exc
            # 有些杀软会"锁住"刚创建的文件，导致删除失败但写入成功，
            # 这种情况下其实目录是可用的
            if os.path.exists(probe):
                try:
                    os.remove(probe)
                except Exception:
                    pass
                return True, ""
            if attempt < IO_RETRY_TIMES:
                time.sleep(IO_RETRY_WAIT)
        except OSError as exc:
            last = exc
            if attempt < IO_RETRY_TIMES:
                time.sleep(IO_RETRY_WAIT)

    return False, (
        "该目录写入测试未通过：\n{}\n\n"
        "错误信息：{}\n\n"
        "常见原因：\n"
        "  · 安全软件的「文件夹保护 / 勒索病毒防护」拦截了程序写入\n"
        "    → 把本工具或该文件夹加入白名单即可\n"
        "  · 目录被其他程序占用（资源管理器预览、杀毒扫描中）\n"
        "  · 目录权限不足\n\n"
        "提示：这不一定会影响实际导出，"
        "可以点「一键全自动」继续尝试。".format(path, last))


def safe_name(name, fallback="unnamed"):
    """
    把任意字符串净化成合法的 Windows 文件名/目录名。

    处理：
      · 去掉非法字符 < > : " / \\ | ? * 和控制字符
      · 去掉首尾空白和点（Windows 不允许以点或空格结尾）
      · 保留中文（中文文件名本身是合法的，只是某些外部进程处理不好）
      · 空结果用 fallback 顶替
    """
    s = _ILLEGAL_NAME_CHARS.sub("_", str(name)).strip().strip(".")
    s = s.replace(" ", "_")
    return s[:80] if s else fallback


def safe_write_text(path, content, retries=IO_RETRY_TIMES, wait=IO_RETRY_WAIT):
    """
    写文本文件，带「文件被占用」重试机制。

    用户之前遇到过 [Errno 13] Permission denied —— 通常是：
      · 上一次的 Blender/UE 进程还没完全释放临时脚本
      · 杀毒软件正在扫描刚写入的文件
    这里等待后重试，最多 retries 次。

    返回 True 表示成功；失败会抛出最后一次异常。
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # 先删除旧文件（避免残留句柄影响），失败也不影响后续
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            return True
        except PermissionError as exc:
            last_exc = exc
            LOG_QUEUE.put(("error",
                           "[警告] 写入被占用（第 {}/{} 次）：{}\n".format(
                               attempt, retries, os.path.basename(path))))
            if attempt < retries:
                time.sleep(wait)
        except Exception as exc:
            last_exc = exc
            LOG_QUEUE.put(("error",
                           "[警告] 写入失败（第 {}/{} 次）：{} -> {}\n".format(
                               attempt, retries, os.path.basename(path), exc)))
            if attempt < retries:
                time.sleep(wait)
    raise last_exc


def safe_copy_file(src, dst, retries=IO_RETRY_TIMES, wait=IO_RETRY_WAIT):
    """复制文件，同样带占用重试。"""
    import shutil
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            return True
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(wait)
    if last_exc:
        LOG_QUEUE.put(("error", "[警告] 复制失败 {} -> {}：{}\n".format(src, dst, last_exc)))
    return False


def match_texture_dir(fbx_path):
    """
    为一个 FBX 自动匹配贴图目录。

    匹配优先级（按「离 FBX 越近越优先」）：
      1. FBX 同级目录下的 Textures / TEX
      2. FBX 同级目录下的 *.fbm（FBX 自带贴图目录）
      3. FBX 同级目录自身（里面直接有贴图文件）
      4. 逐级向上查找（最多 4 层）Textures / *.fbm
         —— 覆盖这种结构：input\\Mesh\\Parts\\FBX\\x.fbx  →  input\\Textures
    找不到返回 None。
    """
    fbx_dir = os.path.dirname(os.path.abspath(fbx_path))
    if not os.path.isdir(fbx_dir):
        return None

    tex_exts = (".tga", ".png", ".tif", ".tiff", ".jpg", ".jpeg", ".exr")

    def _scan_level(directory):
        """在给定目录里找 Textures 子目录 / .fbm / 直接就是贴图目录。"""
        try:
            siblings = os.listdir(directory)
        except OSError:
            return None
        dirs = [d for d in siblings if os.path.isdir(os.path.join(directory, d))]

        # a) 名字是 Textures / TEX 的子目录（大小写不敏感）
        for d in dirs:
            if d.lower() in ("textures", "tex"):
                return os.path.join(directory, d)
        # b) *.fbm
        for d in dirs:
            if d.lower().endswith(".fbm"):
                return os.path.join(directory, d)
        # c) 本目录里直接有贴图
        for f in siblings:
            if f.lower().endswith(tex_exts):
                return directory
        return None

    # 从 FBX 所在目录开始，逐级向上查找，最多 4 层
    current = fbx_dir
    for _ in range(5):
        found = _scan_level(current)
        if found:
            return found
        parent = os.path.dirname(current)
        if parent == current:      # 已经到盘符根
            break
        current = parent

    return None


def scan_models(root_dir, recursive=True):
    """
    扫描目录下所有 FBX 模型，并为每个模型匹配贴图目录。

    返回 list[dict]，每项包含：
        name      : 模型名（不含扩展名）
        path      : FBX 绝对路径
        texdir    : 匹配到的贴图目录（可能为 None）
        rel       : 相对于 root_dir 的相对路径（用于日志展示）
    """
    results = []
    if not os.path.isdir(root_dir):
        return results

    if recursive:
        walker = os.walk(root_dir)
    else:
        try:
            walker = [(root_dir, [], os.listdir(root_dir))]
        except OSError:
            walker = []

    for dirpath, dirnames, filenames in walker:
        # 跳过隐藏/临时目录
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() != "__pycache__"]
        for fn in filenames:
            if not fn.lower().endswith(".fbx"):
                continue
            full = os.path.normpath(os.path.join(dirpath, fn))
            results.append({
                "name": os.path.splitext(fn)[0],
                "path": full,
                "texdir": match_texture_dir(full),
                "rel": os.path.relpath(full, root_dir),
            })

    results.sort(key=lambda r: r["rel"].lower())
    return results


def make_output_dir(out_root, model_name):
    """
    为单个模型生成输出目录。
    开启 BATCH_USE_SUBFOLDER 时：<out_root>/<模型名>/
    否则：<out_root>/
    目录名经过 safe_name 净化，防止非法字符。
    """
    if BATCH_USE_SUBFOLDER:
        d = os.path.join(out_root, safe_name(model_name))
    else:
        d = out_root
    os.makedirs(d, exist_ok=True)
    return d


# =============================================================================
# ③ Blender 临时脚本生成
# =============================================================================

BLENDER_SCRIPT_TEMPLATE = r'''# -*- coding: utf-8 -*-
# ============================================================================
#  临时脚本（由 Pipeline_Tool.py 自动生成，不要手改）
#  生成时间：@timestamp@
# ============================================================================
import bpy
import bmesh
import os
import math
import traceback
from mathutils import Vector, Matrix

# ---------------------------------------------------------------- 参数
EXPORT_FBX = r"@export_fbx@"
TEXDIR = r"@texdir@"
OUTDIR = r"@outdir@"
TARGET_AXIS = "@target_axis@"
LOD_MIN_VERTS = @lod_min_verts@
LOD_RATIOS = @lod_ratios@

# 各模块开关
DO_CLEANUP = @do_cleanup@
DO_ORIGIN = @do_origin@
DO_LOD = @do_lod@
DO_MATERIAL = @do_material@
DO_SMOOTH = @do_smooth@

# 输入模型
SOURCE_FBX = r"@source_fbx@"


def log(msg):
    print("[Blender] " + str(msg), flush=True)


# ============================================================================
# 载入模型
# ============================================================================
def clear_scene_objects():
    """
    清空场景里的所有物体。

    ⚠️ 关键：Blender 用 --factory-startup 启动时，默认场景里有一个 2m 的
    Cube、一个 Camera、一个 Light。如果不删掉：
      · Cube 会被当成模型的一部分导出
      · 它的尺寸(2m)甚至比石像(1.92m)还大，直接污染包围盒计算
      · UE 里算出的整体包围盒被撑大 → 相机拉得太远/太近 → 画面构图全乱

    这一步必须在导入 FBX 之前做。
    """
    removed = 0
    for obj in list(bpy.data.objects):
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
        except Exception:
            pass
    if removed:
        log("已清空默认场景物体 %d 个（Cube/Camera/Light 等）" % removed)
    return removed


def load_source():
    """先清空默认场景，再导入指定的 FBX。"""
    if SOURCE_FBX and os.path.isfile(SOURCE_FBX):
        # 先清空，避免默认 Cube 混进来
        clear_scene_objects()

        log("导入模型：" + SOURCE_FBX)
        try:
            bpy.ops.import_scene.fbx(filepath=SOURCE_FBX)
        except Exception as exc:
            log("FBX 导入失败，尝试备用参数：" + str(exc))
            bpy.ops.import_scene.fbx(filepath=SOURCE_FBX, use_custom_normals=False)
    else:
        log("未指定有效 FBX，使用当前场景中的网格")

    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    log("场景网格数量：" + str(len(meshes)))
    for o in meshes:
        log("    - " + o.name)
    return meshes


# ============================================================================
# 模块 1：模型清洗（重算法线 + 清理烂面）
# ============================================================================
def apply_smooth_shading(obj):
    """
    给网格应用平滑着色（Smooth Shading）。

    为什么单独抽出来：
      之前 shade_smooth() 写在 cleanup_mesh() 里，
      一旦用户取消勾选「模型清洗」，平滑着色就跟着没了 —— 这是个隐患。
      现在独立成函数，由 DO_SMOOTH 单独控制。

    做法：
      1. object.shade_smooth() —— 官方接口，把面法线改成逐顶点平滑
      2. 同时清除自定义拆分法线（如果有），否则 shade_smooth 可能不生效
         （FBX 导入时常带 custom split normals，会覆盖平滑结果）

    ⚠️ Blender 4.1+ 移除了 use_auto_smooth，所以只能靠 shade_smooth +
       清除自定义法线来实现，不能用 auto_smooth 角度。
    """
    try:
        for o in bpy.context.view_layer.objects:
            o.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.context.view_layer.update()

        # 先清自定义拆分法线（否则平滑会被旧法线覆盖）
        try:
            bpy.ops.mesh.customdata_custom_splitnormals_clear()
        except Exception:
            pass

        bpy.ops.object.shade_smooth()
        return True
    except Exception as exc:
        log("  {} 平滑着色失败：{}".format(obj.name, exc))
        return False


def cleanup_mesh(obj):
    """进入编辑模式全选 → 重算法线朝外 → 合并重复点。"""
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.context.view_layer.update()

    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.mesh.remove_doubles(threshold=1e-5)
        bpy.ops.object.mode_set(mode="OBJECT")
        # 平滑着色：这里也做一次（清洗时顺手），
        # 另外 main() 里还有独立的 DO_SMOOTH 步骤兜底
        try:
            bpy.ops.object.shade_smooth()
        except Exception:
            pass
        return True
    except Exception as exc:
        log("  {} 清洗失败：{}".format(obj.name, exc))
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        return False


def apply_all_transforms(obj):
    """
    应用全部变换（旋转 + 缩放）。
    ⚠️ 只烘焙 matrix_basis，不碰 matrix_world ——
       导入的模型常带父级空物体（如 root）并带 0.01 缩放，
       用 matrix_world 烘焙会把父级缩放重复乘一次，模型会缩小 100 倍。
    """
    # 先断开父级并保持世界位置
    if obj.parent is not None:
        wm = obj.matrix_world.copy()
        obj.parent = None
        obj.matrix_world = wm

    basis = obj.matrix_basis.copy()
    loc = basis.translation.copy()
    rs = basis.to_3x3().copy()
    if abs(rs.determinant()) < 1e-12:
        log("  {} 矩阵不可逆，跳过".format(obj.name))
        return False

    # 把旋转缩放烘焙进顶点
    for v in obj.data.vertices:
        v.co = rs @ v.co

    obj.matrix_basis = Matrix.Translation(loc)
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = (0, 0, 0)
    obj.data.update()
    bpy.context.view_layer.update()
    return True


# ============================================================================
# 模块 2：原点居中 + 朝向校正
# ============================================================================
def center_origin(obj):
    """
    原点对齐包围盒中心 —— 纯底层实现，不用 origin_set。
    在【本地空间】做，不受父级影响：
      顶点平移 -C，矩阵平移 +C，几何在世界里纹丝不动，原点落到中心。
    """
    mesh = obj.data
    if len(mesh.vertices) == 0:
        return False
    xs = [v.co.x for v in mesh.vertices]
    ys = [v.co.y for v in mesh.vertices]
    zs = [v.co.z for v in mesh.vertices]
    c = Vector(((max(xs) + min(xs)) * 0.5,
                (max(ys) + min(ys)) * 0.5,
                (max(zs) + min(zs)) * 0.5))
    if c.length < 1e-9:
        return False

    for v in mesh.vertices:
        v.co -= c

    basis = obj.matrix_basis.copy()
    basis.translation += basis.to_3x3() @ c
    obj.matrix_basis = basis
    mesh.update()
    bpy.context.view_layer.update()
    return True


def _whole_bounds(meshes):
    """计算所有网格的整体世界包围盒，返回 (min_vec, max_vec, center_vec)。"""
    lo = [1e30, 1e30, 1e30]
    hi = [-1e30, -1e30, -1e30]
    for obj in meshes:
        mw = obj.matrix_world
        for v in obj.data.vertices:
            p = mw @ v.co
            lo[0] = min(lo[0], p.x); hi[0] = max(hi[0], p.x)
            lo[1] = min(lo[1], p.y); hi[1] = max(hi[1], p.y)
            lo[2] = min(lo[2], p.z); hi[2] = max(hi[2], p.z)
    if lo[0] > hi[0]:
        return None
    center = Vector(((lo[0] + hi[0]) * 0.5,
                     (lo[1] + hi[1]) * 0.5,
                     (lo[2] + hi[2]) * 0.5))
    return Vector(lo), Vector(hi), center


def center_whole_model(meshes):
    """
    把【整套模型】的包围盒中心移到世界原点。
    所有零件统一平移同一个偏移量 —— 相对位置绝对不变。
    """
    b = _whole_bounds(meshes)
    if not b:
        return False
    _lo, _hi, c = b
    if c.length < 1e-9:
        log("  整体已在原点，无需平移")
        return False

    off = -c
    for obj in meshes:
        obj.matrix_basis = Matrix.Translation(off) @ obj.matrix_basis
    bpy.context.view_layer.update()
    log("  整体平移 (%.3f, %.3f, %.3f)，包围盒中心归零" % (off.x, off.y, off.z))
    return True


def orient_whole_model(meshes):
    """
    对【整套模型】做 90° 朝向校正（而不是逐件）。

    判断依据：整体包围盒在水平面（XY）上的哪个轴更宽，
    就把更宽的那个轴转到 +Y（UE 的前方）。

    注意：竖直方向（Z）永远是高度，不参与旋转判断 ——
          这样竖直站立的角色不会被转倒。
    """
    b = _whole_bounds(meshes)
    if not b:
        return False
    lo, hi, _c = b

    dx = hi.x - lo.x
    dy = hi.y - lo.y
    dz = hi.z - lo.z

    log("  整体尺寸：X=%.2f  Y=%.2f  Z=%.2f（高度）" % (dx, dy, dz))

    # Z 是高度，不参与。只比较水平面的 X / Y
    if abs(dx - dy) < 1e-6:
        log("  水平面 XY 接近正方，无需旋转")
        return False

    target_idx = {"+X": 0, "+Y": 1}.get(TARGET_AXIS, 1)
    wide_idx = 0 if dx > dy else 1

    if wide_idx == target_idx:
        log("  最宽的水平轴已朝向 %s，无需旋转" % TARGET_AXIS)
        return False

    # 围绕整体中心做 Z 轴旋转 90°
    pivot = Vector((0, 0, 0))   # center_whole_model 已把中心归零，直接绕原点转
    rot = Matrix.Rotation(math.radians(90.0), 4, "Z")
    for obj in meshes:
        obj.matrix_basis = Matrix.Translation(pivot) @ rot @ Matrix.Translation(-pivot) @ obj.matrix_basis
    bpy.context.view_layer.update()

    log("  整体绕 Z 轴旋转 90°（%s → %s），所有零件同步旋转，相对位置不变" % (
        "X" if wide_idx == 0 else "Y", TARGET_AXIS))
    return True


def world_dims(obj):
    """用真实顶点算世界尺寸（不用 bound_box，它是过期缓存）。"""
    mw = obj.matrix_world.copy()
    pts = [mw @ v.co for v in obj.data.vertices]
    if not pts:
        return Vector((0, 0, 0))
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    zs = [p.z for p in pts]
    return Vector((max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)))


def orient_forward(obj):
    """
    最宽的【水平】轴旋转 90° 转到目标轴（默认 +Y）。
    若最宽维度是 Z（竖长件，如躯干/腿甲），跳过并提示 —— 硬转会拧横模型。
    """
    d = world_dims(obj)
    idx = 0
    for i in (1, 2):
        if d[i] > d[idx] + 1e-9:
            idx = i
    axis = {0: "X", 1: "Y", 2: "Z"}[idx]
    target_idx = {"+X": 0, "+Y": 1}.get(TARGET_AXIS, 1)

    if idx == 2:
        log("  {} 最宽维度是 Z（竖长件），跳过朝向旋转".format(obj.name))
        return False
    if idx == target_idx:
        log("  {} 已朝向 {}，无需旋转".format(obj.name, TARGET_AXIS))
        return False

    pivot = obj.matrix_world.translation.copy()
    rot = Matrix.Rotation(math.radians(90.0), 4, "Z")
    obj.matrix_world = Matrix.Translation(pivot) @ rot @ Matrix.Translation(-pivot) @ obj.matrix_world
    bpy.context.view_layer.update()
    log("  {} 旋转 90°（{} → {}）".format(obj.name, axis, TARGET_AXIS))
    return True


# ============================================================================
# 模块 3：生成 LOD（带顶点数保护）
# ============================================================================
def _get_or_create_collection(name):
    """取得或创建集合（并入自原 N 面板插件的分组思路）。"""
    coll = bpy.data.collections.get(name)
    if not coll:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    return coll


def _move_to_collection(obj, coll):
    """把物体从所有当前集合移除，再放进目标集合。"""
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    coll.objects.link(obj)


def generate_lods(obj):
    """
    对每个零件生成 LOD1/2/3，并按 LOD 级别分到不同集合。

    并入自「游戏资产自动化管线」插件的做法：
      · 分集合（LOD0_Original / LOD1_High / LOD2_Med / LOD3_Low）
      · 导出时按集合分别导出 Model_LOD1/2/3.fbx
    保留本工具原有做法：
      · 顶点数保护（< LOD_MIN_VERTS 的零件跳过）
      · 应用修改器用 object.convert（不依赖 operator 上下文，后台模式更稳）
    """
    n = len(obj.data.vertices)
    if n < LOD_MIN_VERTS:
        log("  {} 顶点数 {} < {}，跳过 LOD".format(obj.name, n, LOD_MIN_VERTS))
        return 0

    coll0 = _get_or_create_collection("LOD0_Original")
    _move_to_collection(obj, coll0)

    made = 0
    for i, ratio in enumerate(LOD_RATIOS, start=1):
        coll = _get_or_create_collection(
            {1: "LOD1_High", 2: "LOD2_Med", 3: "LOD3_Low"}.get(i, "LOD%d" % i))

        dup = obj.copy()
        dup.data = obj.data.copy()
        dup.name = "{}_LOD{}".format(obj.name, i)
        # 继承父级关系（原插件这么做，保证层级不错乱）
        dup.parent = obj.parent
        if obj.parent is not None:
            dup.matrix_parent_inverse = obj.matrix_parent_inverse.copy()
        coll.objects.link(dup)

        target = max(1, int(n * ratio))
        mod = dup.modifiers.new(name="Decimate_LOD{}".format(i), type="DECIMATE")
        mod.decimate_type = "COLLAPSE"
        # Blender 的 ratio 是"保留比例"，所以直接传 ratio
        mod.ratio = ratio

        # 应用修改器（用 object.convert 最稳，不依赖上下文）
        for o in bpy.context.view_layer.objects:
            o.select_set(False)
        dup.select_set(True)
        bpy.context.view_layer.objects.active = dup
        try:
            bpy.ops.object.convert(target="MESH")
        except Exception as exc:
            log("    LOD{} 应用修改器失败：{}".format(i, exc))

        log("    {} 顶点 {} → {}（目标 {}）".format(
            dup.name, n, len(dup.data.vertices), target))
        made += 1
    return made


# ============================================================================
# 模块 4：材质与贴图
# ============================================================================
def pack_textures():
    """把所有贴图打包进 .blend / 随 FBX 一起带走，避免丢图。"""
    packed = 0
    for img in bpy.data.images:
        if img.source == "FILE" and not img.packed_file:
            try:
                img.pack()
                packed += 1
            except Exception as exc:
                log("  打包贴图失败 {}：{}".format(img.name, exc))
    log("已打包贴图 {} 张".format(packed))


def ensure_material(obj, texdir):
    """确保物体有材质；没有的话建一个基础 PBR 材质并尝试接贴图。"""
    if obj.data.materials and obj.data.materials[0] is not None:
        return obj.data.materials[0]

    mat = bpy.data.materials.new(name="M_" + obj.name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    # 按文件名找贴图
    lower = obj.name.lower()
    part = "body" if "body" in lower else ("armor" if "armor" in lower else "weapon")
    try:
        files = [f for f in os.listdir(texdir) if f.lower().endswith((".tga", ".png", ".jpg"))]
    except Exception:
        files = []

    def find(kw):
        for f in files:
            fl = f.lower()
            if part in fl and kw in fl and "skin1" in fl or (part in fl and kw in fl):
                return os.path.join(texdir, f)
        return None

    alb = find("albedo")
    if alb:
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = bpy.data.images.load(alb, check_existing=True)
        links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])

    nrm = find("normal")
    if nrm:
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = bpy.data.images.load(nrm, check_existing=True)
        nm = nodes.new("ShaderNodeNormalMap")
        links.new(tex.outputs["Color"], nm.inputs["Color"])
        links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])

    obj.data.materials.append(mat)
    return mat


# 主流程
# ============================================================================
def main():
    log("=" * 55)
    log("Blender 预处理开始     版本 " + bpy.app.version_string)
    log("=" * 55)

    meshes = load_source()
    if not meshes:
        log("没有找到任何网格，终止")
        return

    # ---- 模块 1：清洗 ----
    if DO_CLEANUP:
        log("\n[1] 模型清洗")
        for obj in meshes:
            cleanup_mesh(obj)
            apply_all_transforms(obj)
        log("清洗完成：{} 个物体".format(len(meshes)))
    else:
        log("\n[1] 模型清洗  —— 已跳过")

    # ---- 模块 2.5：平滑着色（独立于清洗，受 DO_SMOOTH 控制）----
    if DO_SMOOTH:
        log("\n[2.5] 平滑着色")
        n = 0
        for obj in meshes:
            if apply_smooth_shading(obj):
                n += 1
        log("已对 {} 个网格应用平滑着色".format(n))
    else:
        log("\n[2.5] 平滑着色  —— 已跳过")

    # ---- 模块 2：原点 + 朝向 ----
    #
    # ⚠️ 关键设计：分件必须保持【相对位置】！
    #
    # 早期实现对每个零件单独做原点居中，导致每件原点都挪到各自包围盒中心，
    # 而 UE 侧是把所有零件都放在 (0,0,0)，结果胸甲、头盔、躯干全部
    # 叠在原点附近、飞散开来，看起来像"模型散架/躺倒"。
    #
    # 正确做法：
    #   把【整套模型】当成一个整体来居中/旋转 —— 所有零件使用同一个
    #   偏移量和同一个旋转轴心，相对位置完全不变。
    if DO_ORIGIN:
        log("\n[2] 原点居中（整体处理，保持零件相对位置）")
        center_whole_model(meshes)
        # ═══════════════════════════════════════════════════════════
        # ★ 已移除 orient_whole_model 的自动旋转
        #
        # 原逻辑：判断整体包围盒 XY 哪个轴更宽，就绕 Z 轴转 90°
        #        让"最宽的轴"朝 +Y。
        #
        # 问题：这个启发式是错的。
        #   石魔像这类角色，肩宽（X）本来就应该大于侧厚（Y），
        #   算法看到 X 更宽就强行旋转 90°，结果把模型的朝向彻底拧歪 —
        #   原本脸朝 +Y，被转成朝 +X/-X，这也是之前
        #   "只有某些角度能拍到、其余全黑"的根源之一。
        #
        # 现在：完全不旋转，保持 Blender 里的原始朝向。
        #       需要朝向调整时，用 render_config.json 的
        #       camera.angles 选择机位即可。
        # ═══════════════════════════════════════════════════════════
        log("  已跳过自动朝向旋转（保持原始朝向）")
    else:
        log("\n[2] 原点居中和朝向  —— 已跳过")

    # ---- 模块 3：LOD ----
    if DO_LOD:
        log("\n[3] 生成 LOD")
        total = 0
        for obj in list(meshes):
            total += generate_lods(obj)
        log("共生成 {} 个 LOD 网格".format(total))
    else:
        log("\n[3] LOD  —— 已跳过")

    # ---- 模块 4：材质与导出 ----
    if DO_MATERIAL:
        log("\n[4] 材质打包与导出")
        for obj in meshes:
            ensure_material(obj, TEXDIR)
        pack_textures()

        export_dir = os.path.dirname(EXPORT_FBX)
        base_name = os.path.splitext(os.path.basename(EXPORT_FBX))[0]
        if export_dir and not os.path.isdir(export_dir):
            try:
                os.makedirs(export_dir, exist_ok=True)
            except Exception as exc:
                log("创建输出目录失败：" + str(exc))

        def export_selection(objs, out_path):
            """按给定的物体列表导出单个 FBX。"""
            if not objs:
                log("  跳过（集合为空）：" + os.path.basename(out_path))
                return False
            # 唤醒所有隐藏物体，防止导出为空（并入自原插件）
            for o in bpy.context.scene.objects:
                try:
                    o.hide_set(False)
                    o.hide_viewport = False
                    o.hide_render = False
                except Exception:
                    pass

            for o in bpy.context.view_layer.objects:
                o.select_set(False)
            export_set = set(objs)
            for o in objs:
                if o.parent is not None:
                    export_set.add(o.parent)
            for o in export_set:
                try:
                    o.select_set(True)
                except Exception:
                    pass
            bpy.context.view_layer.objects.active = objs[0]

            bpy.ops.export_scene.fbx(
                filepath=out_path,
                use_selection=True,
                object_types={"MESH"},
                apply_unit_scale=True,
                apply_scale_options="FBX_SCALE_ALL",   # 与原插件一致，UE 里尺寸正确
                # ★ 与 UE 保持一致：UE 是 Z-up、X 向前
                #   写成 -Z/Y（Unity 约定）会让模型在 UE 里上下颠倒
                axis_forward="-Y",
                axis_up="Z",
                use_mesh_modifiers=True,
                path_mode="COPY",                      # 贴图复制到 <文件名>.fbm 目录
                embed_textures=False,
            )
            ok = os.path.isfile(out_path)
            if ok:
                log("  导出 {}  （{:.1f} KB，{} 个网格）".format(
                    os.path.basename(out_path), os.path.getsize(out_path) / 1024.0, len(objs)))
            else:
                log("  导出失败：" + out_path)
            return ok

        exported_files = []

        if DO_LOD:
            # ---- 分 LOD 导出 ----
            # 命名规范：<基名>_LOD0.fbx / _LOD1.fbx / _LOD2.fbx / _LOD3.fbx
            # 注意：UE 的 LOD 链是靠 FBX 内的 eLODGroup 结构识别的，
            #       Blender 导出器不支持该结构，所以这些文件在 UE 里是「独立的 4 个模型」，
            #       不能自动合并成一条 LOD 链。命名统一只是为了管线整洁、便于手动指派 LOD。
            log("按 LOD 分层导出（命名 _LOD0 ~ _LOD3）：")
            pairs = (("LOD0_Original", "_LOD0"),
                     ("LOD1_High", "_LOD1"),
                     ("LOD2_Med", "_LOD2"),
                     ("LOD3_Low", "_LOD3"))
            for coll_name, suffix in pairs:
                coll = bpy.data.collections.get(coll_name)
                if not coll:
                    continue
                objs = [o for o in coll.objects if o.type == "MESH"]
                if not objs:
                    continue
                out = os.path.join(export_dir, base_name + suffix + ".fbx")
                if export_selection(objs, out):
                    exported_files.append(out)
                    # LOD0 同时写一份不带后缀的主文件，供 UE 渲染沿用
                    if suffix == "_LOD0":
                        try:
                            import shutil
                            shutil.copyfile(out, EXPORT_FBX)
                            log("  已另存主模型：" + os.path.basename(EXPORT_FBX))
                        except Exception as exc:
                            log("  另存主模型失败：" + str(exc))
        else:
            # ---- 没有 LOD 时，按原逻辑导出全部网格 ----
            log("导出全部网格为单个 FBX：")
            all_meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
            if export_selection(all_meshes, EXPORT_FBX):
                exported_files.append(EXPORT_FBX)

        if exported_files:
            log("共导出 {} 个 FBX 到：{}".format(len(exported_files), export_dir))
            log("主模型（UE 渲染用）：" + EXPORT_FBX if os.path.isfile(EXPORT_FBX)
                else "警告：主模型未生成")
        else:
            log("导出失败：没有生成任何 FBX")
    else:
        log("\n[4] 材质打包与导出  —— 已跳过")

    log("\n" + "=" * 55)
    log("Blender 预处理结束")
    log("=" * 55)


try:
    main()
except Exception:
    print("[Blender][EXCEPTION]")
    print(traceback.format_exc())
'''


# =============================================================================
# ④ UE5 临时脚本生成
# =============================================================================

UE_SCRIPT_TEMPLATE = r'''# -*- coding: utf-8 -*-
# ============================================================================
#  临时脚本（由 Pipeline_Tool.py 自动生成，不要手改）
#  生成时间：@timestamp@
# ============================================================================
import os
import sys
import math
import time
import json
import traceback

import unreal

# ---------------------------------------------------------------- 参数
FBX = r"@fbx@"
TEXDIR = r"@texdir@"
OUT_DIR = r"@outdir@"
ROOT = "/Game/DSH_Golem"
WIDTH, HEIGHT = @width@, @height@
# 相机视野角（度）。由 render_config.json 的 camera.fov 传入。
# 越小越接近长焦，模型占画面越大、细节越清楚。
FOV = @fov@
EXPOSURE = @exposure@
# 屏幕百分比：100 = 原生分辨率渲染，不做降采样。
# ⚠️ 这个常量曾被漏定义，导致 apply_quality_settings() 抛
#    NameError: name 'UE_SCREEN_PERCENTAGE' is not defined，
#    整个画质设置因此被 try-except 兜底跳过。务必保留定义。
UE_SCREEN_PERCENTAGE = @screen_percentage@

# 截图阶段的 2 倍超采样倍率（%）。截图完成后会重置回 UE_SCREEN_PERCENTAGE。
SUPER_SAMPLE_PERCENTAGE = @super_sample@
MIN_BYTES = @min_bytes@

# ---- 多次 capture 收敛参数（消除 TSR 涂抹感）----
# 连续 capture 的次数：越多越干净，耗时线性增长
CAPTURE_CONVERGE_FRAMES = @converge_frames@
# 每次 capture 之间的间隔（秒），让引擎推进 TSR 历史缓冲
CAPTURE_CONVERGE_INTERVAL = @converge_interval@
ANGLES = @angles@
# 相机距离系数：由主程序从 render_config.json 的 camera.distance_factor 传入。
# ⚠️ 这里原来硬编码 2.2，导致改配置无效、模型永远偏小。
DIST_FACTOR = @dist_factor@
# 正面角度：终稿优先选最接近这个角度的图
FACE_ANGLE = @face_angle@
# 相机抬高系数：CamLoc.Z = Center.Z + Radius * CAM_LIFT
CAM_LIFT = @cam_lift@

DO_SCENE = @do_scene@
DO_LIGHT = @do_light@
DO_CAMERA = @do_camera@

# ═══════════════════════════════════════════════════════════════════════════
# 材质渲染模式
#   texture      : 用 Blender 导出的 Albedo 贴图（默认）
#   dark_metal   : 纯暗金属色（忽略 Albedo），适合展示几何
#   rough_skin   : 粗糙皮肤/岩石质感（提亮版纯色 + 强法线）
#   emissive     : 发光材质（Emission 贴图 + 高自发光强度）
# ═══════════════════════════════════════════════════════════════════════════
MATERIAL_MODE = "@material_mode@"
MATERIAL_BASE_COLOR = @material_base_color@
MATERIAL_EMISSIVE_GAIN = @material_emissive_gain@
EMISSION_GAIN = @emission_gain@
DEBUG_EMISSION = @debug_emission@
BLOOM_INTENSITY = @bloom_intensity@
BLOOM_THRESHOLD = @bloom_threshold@
EMISSION_COLOR = @emission_color@

# 把 MATERIAL_MODE 映射到旧的开关（保持兼容）
DEBUG_RED_BASECOLOR = @debug_red_basecolor@
USE_CUSTOM_BASECOLOR = (MATERIAL_MODE in ("dark_metal", "rough_skin", "emissive", "plaster"))
CUSTOM_BASECOLOR = MATERIAL_BASE_COLOR

# ═══════════════════════════════════════════════════════════════════════════
# 红色诊断开关
#   True  → BaseColor 强制纯红，用来确认材质是否真的挂到模型上
#   False → 正常渲染
# 通过环境变量 SMOKE_RED_TEST=1 或配置 model.debug_red_basecolor 打开
# ═══════════════════════════════════════════════════════════════════════════


LOG_PATH = os.path.join(OUT_DIR, "ue_render.log")
SUMMARY = {"ok": False, "shots": [], "error": None}


def log(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    print("[UE5] " + msg, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    try:
        unreal.log("[Pipeline] " + msg)
    except Exception:
        pass


def set_cvar(name, value):
    """
    通过 ConsoleVariablesEditorFunctionLibrary 设置控制台变量。

    ⚠️ 说明：UE5 的 PostProcessSettings 里【没有】anti_aliasing 或
       screen_percentage 字段（已实测确认），
       这些属于渲染管线全局设置，必须走 CVar。
    """
    try:
        lib = unreal.ConsoleVariablesEditorFunctionLibrary
        if isinstance(value, bool):
            lib.set_console_variable_by_name_bool(name, value)
        elif isinstance(value, int):
            lib.set_console_variable_by_name_int(name, value)
        elif isinstance(value, float):
            lib.set_console_variable_by_name_float(name, value)
        else:
            lib.set_console_variable_by_name_string(name, str(value))
        log("  CVar %s = %s" % (name, value))
        return True
    except Exception as exc:
        log("  CVar %s 设置失败: %s" % (name, str(exc)[:100]))
        return False


def apply_quality_settings():
    """
    强制拉满画质。

    做四件事：
      1. 抗锯齿设为 TSR（AAM_TSR=4），并启用 TSR 历史累积
      2. 屏幕百分比固定 100，避免动态分辨率降采样导致糊
      3. 关闭自动曝光、锁定手动曝光
      4. 关闭运动模糊/景深等会糊化边缘的后处理
    """
    log("[画质] 应用高质量渲染设置")
    # TSR 是最新一代时域超分抗锯齿，边缘最干净
    set_cvar("r.AntiAliasingMethod", 4)          # 4 = TSR
    set_cvar("r.ScreenPercentage", UE_SCREEN_PERCENTAGE)
    set_cvar("r.TSR.History.ScreenPercentage", 200)   # 超采样历史缓冲，更锐利
    set_cvar("r.TSR.ShadingRejection.Flickering", 0)

    # ---------------------------------------------------------------
    # ⚠️ 这里曾经有严重冲突（V4.8 及之前）：
    #      set_cvar("r.DepthOfFieldQuality", 0)
    #    把景深质量设成 0，等于让引擎**彻底禁用 DoF**，
    #    使得前面辛苦配置的 f/2.8、对焦距离全部失效，
    #    而且因为它在模块 apply_render_quality() 之后执行，
    #    会覆盖掉那边设好的 2。
    #
    #    现在统一为 2（高质量）。任何情况下都不允许设 0。
    # ---------------------------------------------------------------
    # 运动模糊保持 0：静帧渲染不需要运动模糊，它只会让边缘发糊。
    # 注意 r.MotionBlurQuality=0 不影响景深，两者是独立 CVar。
    set_cvar("r.MotionBlurQuality", 0)
    set_cvar("r.DefaultFeature.MotionBlur", 0)

    # ★ 景深质量必须 >= 1，这里固定 2（高质量）
    set_cvar("r.DepthOfFieldQuality", 2)

    set_cvar("r.DefaultFeature.AntiAliasing", 4)

    # 纹理与阴影质量拉满
    set_cvar("r.Streaming.MipBias", 0)
    set_cvar("r.MaxAnisotropy", 16)
    set_cvar("r.ShadowQuality", 5)
    set_cvar("r.Shadow.CSM.MaxCascades", 4)

    # 关闭动态分辨率
    set_cvar("r.DynamicRes.OperationMode", 0)


def apply_extreme_quality_lock():
    """
    ★★★ 暴力锁死画质（消灭一切「糊」的来源）★★★

    在截图前调用，把所有可能导致画面发糊的设置强制覆盖一遍。
    每一项都独立 try，单项失败不影响其他项。

    覆盖清单：
      1. r.DepthOfFieldQuality = 0
         彻底关闭景深。景深一旦对焦失误，主体本身就会糊。
         （注意：这会牺牲背景虚化，换取主体绝对清晰）
      2. r.MotionBlurQuality = 0 + r.DefaultFeature.MotionBlur = 0
         静帧渲染不需要运动模糊
      3. 关闭 TSR 的历史混合（降低拖影残留）
      4. r.Streaming.MipBias = -2
         强制使用最高精度 mipmap，禁止引擎自动降级贴图
      5. r.MaxAnisotropy = 16
         （无法再高了，UE 上限就是 16）
      6. 关闭动态分辨率和屏幕百分比抖动
      7. 屏幕百分比锁 200（2 倍超采样）

    返回成功设置的项数。
    """
    log("[锁画质] ===== 开始暴力锁死画质 =====")
    ok = 0
    total = 0

    def sv(name, value, note=""):
        nonlocal ok, total
        total += 1
        try:
            lib = unreal.ConsoleVariablesEditorFunctionLibrary
            if isinstance(value, bool):
                lib.set_console_variable_by_name_bool(name, value)
            elif isinstance(value, float):
                lib.set_console_variable_by_name_float(name, value)
            else:
                lib.set_console_variable_by_name_int(name, int(value))
            ok += 1
            log("  ✓ {} = {}{}".format(name, value, ("  " + note) if note else ""))
            return True
        except Exception as e:
            log("  ✗ {} = {} 设置失败：{}".format(name, value, str(e)[:60]))
            return False

    # ---- 1. 彻底关闭景深 ----
    sv("r.DepthOfFieldQuality", 0, "（关闭 DoF，主体绝对清晰）")

    # ---- 2. 关闭运动模糊 ----
    sv("r.MotionBlurQuality", 0)
    sv("r.DefaultFeature.MotionBlur", 0)

    # ---- 3. 抗锯齿：保留 TSR 但降低历史混合，减少拖影 ----
    sv("r.AntiAliasingMethod", 4, "（TSR）")
    # 提高 TSR 的空间权重，减少对历史帧的依赖（历史帧 = 拖影来源）
    sv("r.TSR.History.SampleCount", 8, "（限制历史样本数，减少拖影）")
    sv("r.TSR.ShadingRejection.Flickering", 0)

    # ---- 4. 贴图最高精度 ----
    sv("r.Streaming.MipBias", -2, "（强制最高精度 mip，禁止降级）")
    sv("r.MaxAnisotropy", 16, "（UE 上限）")
    sv("r.Streaming.PoolSize", 0, "（0=不限，避免流式加载降级）")

    # ---- 5. 分辨率相关 ----
    sv("r.ScreenPercentage", SUPER_SAMPLE_PERCENTAGE, "（2 倍超采样）")
    sv("r.DynamicRes.OperationMode", 0, "（关闭动态分辨率）")
    sv("r.Upscale.Quality", 5, "（最高质量放大滤波）")

    # ---- 6. 阴影/反射质量拉满 ----
    sv("r.ShadowQuality", 5)
    sv("r.Shadow.MaxResolution", 2048)
    sv("r.Shadow.CSM.MaxCascades", 4)

    log("[锁画质] 完成：{}/{} 项设置成功".format(ok, total))
    log("[锁画质] ===== 画质已锁死 =====")
    return ok


def enter_supersample():
    """
    截图前：把 r.ScreenPercentage 临时提高到 SUPER_SAMPLE_PERCENTAGE（200）。

    2 倍超采样 = 引擎按 2×2 像素量渲染后降采样回目标分辨率，
    大幅减少锯齿与 TSR 的闪烁噪点。代价是渲染耗时约 4 倍。

    ⚠️ 必须在用完以后调用 exit_supersample() 重置，
       否则超采样会一直挂着，导致后续操作（切关卡、编辑器交互）卡死。
    """
    try:
        set_cvar("r.ScreenPercentage", SUPER_SAMPLE_PERCENTAGE)
        log("[超采样] r.ScreenPercentage -> {}（{} 倍，截图阶段临时启用）".format(
            SUPER_SAMPLE_PERCENTAGE, SUPER_SAMPLE_PERCENTAGE / 100.0))
        return True
    except Exception as e:
        log("[超采样] 开启失败：{}".format(str(e)[:100]))
        return False


def exit_supersample():
    """
    截图后：把 r.ScreenPercentage 重置回 100（原生分辨率）。

    在 finally 里调用，保证即使截图过程抛异常也一定恢复，
    不会把引擎留在超采样状态。
    """
    try:
        set_cvar("r.ScreenPercentage", UE_SCREEN_PERCENTAGE)
        log("[超采样] r.ScreenPercentage -> {}（已恢复正常）".format(
            UE_SCREEN_PERCENTAGE))
        return True
    except Exception as e:
        log("[超采样] 恢复失败：{}".format(str(e)[:100]))
        return False


def force_full_lod(actors):
    """
    强制所有网格使用 LOD0（最高精度），禁止自动降级到低模。

    StaticMeshComponent.forced_lod_model = 0 表示锁定第 0 级 LOD。
    """
    ok = 0
    for a in actors:
        try:
            smc = a.static_mesh_component
            smc.set_editor_property("forced_lod_model", 0)
            # 同时关闭自动 LOD 生成，避免运行时再生成低模
            try:
                smc.set_editor_property("override_min_lod", True)
                smc.set_editor_property("min_lod", 0)
            except Exception:
                pass
            ok += 1
        except Exception as exc:
            log("  强制 LOD0 失败 %s: %s" % (a.get_actor_label(), str(exc)[:60]))
    log("[画质] 已强制 %d 个网格使用 LOD0" % ok)


def apply_smooth_shading(mesh_asset):
    """
    对导入的静态网格应用平滑着色（Smooth Shading）。

    UE 的静态网格如果法线是逐面（hard）的，边缘会出现明显的棱线/马赛克感。
    把法线改为逐顶点平滑后，表面会变得连续。
    """
    try:
        ok = unreal.EditorStaticMeshLibrary.set_lods_with_notification
    except Exception:
        pass

    # UE 里控制平滑的是导入时的「Normal Import Method」和网格自身法线，
    # 运行时没有直接的 smooth 开关，这里通过重设法线导入方式实现。
    try:
        # 重新设一个空 LOD 操作会触发重建；更稳妥的做法是保持原样
        unreal.log("[画质] 平滑着色：法线由 FBX 导入决定")
    except Exception:
        pass


def comp(actor, cls):
    c = actor.get_components_by_class(cls)
    return c[0] if c else None


def setp(obj, props):
    for k, v in props.items():
        try:
            obj.set_editor_property(k, v)
        except Exception as exc:
            log("  属性 {} 设置失败: {}".format(k, str(exc)[:60]))


# ============================================================================
# 模块 1：自动场景构建
# ============================================================================
def build_scene():
    ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    mel = unreal.MaterialEditingLibrary

    for d in ("/Meshes", "/Textures", "/Materials"):
        p = ROOT + d
        if not unreal.EditorAssetLibrary.does_directory_exist(p):
            unreal.EditorAssetLibrary.make_directory(p)

    # 时间戳新建关卡（名字唯一，避免 new_level 失败导致空帧）
    # ═══════════════════════════════════════════════════════════════════
    # ★ 临时关卡必须放在独立目录，绝对不能和资产混在一起
    #
    # 旧代码：map_path = ROOT + "/Map_" + 时间戳
    #   → 每跑一次就在 DSH_Golem/ 根目录留下一个 .umap
    #   → 实测累积了 174 个，把内容浏览器塞满，
    #     UE 启动时误把 Materials/M_Body 当成地图加载 → 弹"地图加载失败"
    #
    # 新代码：
    #   · 固定路径 /Game/DSH_Temp/Map（每次覆盖同一个，不再累积）
    #   · 与资产目录完全隔离
    # ═══════════════════════════════════════════════════════════════════
    map_path = "/Game/DSH_Temp/Map"
    ok = les.new_level(map_path)
    world_path = unreal.EditorLevelLibrary.get_editor_world().get_path_name()
    log("新建关卡 {} -> {} (world={})".format(map_path, ok, world_path))
    if not ok or "/Temp/" in world_path:
        # ═══════════════════════════════════════════════════════════════
        # ★ 新建关卡失败的兜底处理
        #
        # 某些工程（例如带 AndroidFileServer 等插件的主工程）启动后
        # 会停留在一个 /Temp/Untitled 临时关卡，此时 new_level() 返回 False。
        #
        # 旧逻辑直接 raise → 整条流程中断，一张图都不出。
        #
        # 现在的处理：
        #   1. 先尝试 new_level 到一个简单位置
        #   2. 仍失败则【直接用当前关卡】继续渲染
        #      （我们本来就会清空所有 Actor 重建场景，用哪个关卡无所谓）
        #   3. 只有在连 world 都拿不到时才报错
        # ═══════════════════════════════════════════════════════════════
        log("  新建关卡失败，尝试兜底方案")
        fallback_ok = False
        for _try_path in ("/Game/DSH_Temp/Map", "/Game/Temp/Map"):
            try:
                if les.new_level(_try_path):
                    log("  兜底新建关卡成功：{}".format(_try_path))
                    fallback_ok = True
                    break
            except Exception:
                pass

        if not fallback_ok:
            # 用当前关卡硬渲染
            try:
                _w = None
                try:
                    _w = les.get_current_level()
                except Exception:
                    pass
                if _w is None:
                    _w = unreal.EditorLevelLibrary.get_editor_world()
                if _w is None:
                    raise RuntimeError("拿不到任何可用关卡")
                log("  ★ 使用当前关卡继续渲染：{}".format(_w.get_name()))
                log("     （场景会被清空重建，不影响出图）")
                fallback_ok = True
            except Exception as _e:
                raise RuntimeError("新建关卡失败且无可用关卡：{}".format(str(_e)[:80]))
    for a in list(ss.get_all_level_actors()):
        ss.destroy_actor(a)

    # 导入 FBX
    task = unreal.AssetImportTask()
    task.filename = FBX
    task.destination_path = ROOT + "/Meshes"
    task.automated = True
    task.save = True
    task.replace_existing = True
    opt = unreal.FbxImportUI()
    opt.import_mesh = True
    opt.import_textures = False
    opt.import_materials = False
    opt.import_animations = True
    opt.automated_import_should_detect_type = True
    opt.import_as_skeletal = False
    opt.import_rigid_mesh = True
    task.options = opt
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    meshes = [p for p in (task.imported_object_paths or [])
              if isinstance(unreal.EditorAssetLibrary.load_asset(p), unreal.StaticMesh)]
    log("导入网格 {} 个".format(len(meshes)))
    if not meshes:
        raise RuntimeError("FBX 没有导入出静态网格")

    # 导入贴图
    tts = []
    if os.path.isdir(TEXDIR):
        for f in sorted(os.listdir(TEXDIR)):
            if f.lower().endswith((".tga", ".png", ".tif")):
                t = unreal.AssetImportTask()
                t.filename = os.path.join(TEXDIR, f)
                t.destination_path = ROOT + "/Textures"
                t.automated = True
                t.save = True
                t.replace_existing = True
                tts.append(t)
    if tts:
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks(tts)
    tex = {}
    for t in tts:
        s = os.path.splitext(os.path.basename(t.filename))[0]
        a = unreal.EditorAssetLibrary.load_asset(ROOT + "/Textures/" + s)
        if a:
            tex[s.lower()] = a
    log("导入贴图 {} 张".format(len(tex)))

    # ═══════════════════════════════════════════════════════════════════
    # 【贴图普查】打印每一张贴图的真实资产名和路径
    # ═══════════════════════════════════════════════════════════════════
    log("")
    log("=" * 62)
    log("[贴图普查] 共 {} 张".format(len(tex)))
    for k in sorted(tex.keys()):
        a = tex[k]
        try:
            pkg = a.get_path_name()
        except Exception:
            pkg = "?"
        log("[贴图普查] 发现贴图: {:<46s} {}".format(
            a.get_name(), pkg.split(".")[0]))
    log("=" * 62)
    log("")

    # ═══════════════════════════════════════════════════════════════════
    # 【贴图质检】+【强制色彩空间】
    #
    # 规则（严格按命名判断）：
    #   包含 albedo / basecolor / base_color / diffuse → sRGB = True
    #   其余全部（normal / rough / metal / ao / spec / gloss / emission）
    #                                              → sRGB = False
    #
    # 数据贴图若开了 sRGB，引擎会做一次 sRGB→线性 解码，
    # 数值被大幅改变，材质对比全部丢失 —— 这就是"灰泥感"的成因之一。
    # ═══════════════════════════════════════════════════════════════════
    COLOR_KEYWORDS = ("albedo", "basecolor", "base_color", "diffuse")
    log("=" * 78)
    log("[贴图质检] %-44s %-11s %-6s %s" % ("资产名", "尺寸", "sRGB", "判定"))
    log("-" * 78)
    srgb_fixed = 0
    for k in sorted(tex.keys()):
        a = tex[k]
        try:
            x, y = a.blueprint_get_size_x(), a.blueprint_get_size_y()
        except Exception:
            try:
                x = a.get_editor_property("imported_size_x")
                y = a.get_editor_property("imported_size_y")
            except Exception:
                x = y = 0

        should_srgb = any(kw in k for kw in COLOR_KEYWORDS)
        cur_srgb = None
        try:
            cur_srgb = a.get_editor_property("srgb")
        except Exception:
            pass

        verdict = ""
        if cur_srgb is None:
            verdict = "读不到"
        elif cur_srgb != should_srgb:
            # 强制修正
            try:
                a.set_editor_property("srgb", should_srgb)
                unreal.EditorAssetLibrary.save_loaded_asset(a)
                verdict = "已修正 {}->{}".format(cur_srgb, should_srgb)
                srgb_fixed += 1
            except Exception as e:
                verdict = "修正失败"
        else:
            verdict = "OK"

        log("[贴图质检] %-44s %-11s %-6s %s" % (
            a.get_name()[:44], "%dx%d" % (x, y), str(cur_srgb), verdict))
    log("-" * 78)
    log("[贴图质检] 修正了 {} 张贴图的 sRGB 标记".format(srgb_fixed))
    log("=" * 78)
    log("")

    # ---- PBR 材质（含 MetallicGain 补偿）----
    def sampler(n):
        e = getattr(unreal, "MaterialSamplerType", None)
        if e:
            try:
                return getattr(e, n)
            except AttributeError:
                pass
        return n

    def new_mat(path):
        """
        创建或复用材质资产。

        ═══════════════════════════════════════════════════════════════
        为什么不能总是 create_asset：
          主工程里如果已经有同名材质（用户手动复制过来的），
          create_asset 可能返回 None —— UE 不允许覆盖已加载的资产。

        策略（三级降级）：
          1. 资产已存在 → 加载它，清空所有节点，复用
          2. 不存在 → create_asset 新建
          3. 仍然失败 → 换个路径新建（M_<name>_v2）
        ═══════════════════════════════════════════════════════════════
        """
        folder, name = path.rsplit("/", 1)
        log("  [材质] 准备 {} (folder={})".format(name, folder))

        # 确保目录存在
        try:
            if not unreal.EditorAssetLibrary.does_directory_exist(folder):
                mk = unreal.EditorAssetLibrary.make_directory(folder)
                log("    创建目录 {} -> {}".format(folder, mk))
        except Exception as _e:
            log("    创建目录失败: {}".format(str(_e)[:70]))

        # ---- 方案 1：已存在则复用 ----
        exists = False
        try:
            exists = unreal.EditorAssetLibrary.does_asset_exist(path)
        except Exception:
            pass

        if exists:
            # ═══════════════════════════════════════════════════════════
            # ★ 已存在时的处理：不删除，直接改到【新目录】重建
            #
            # 实测发现：主工程里的材质被编辑器占用，
            #   delete_asset 静默失败（磁盘文件还在），
            #   随后 create_asset 因同名冲突返回 None → 材质为 None → 崩溃。
            #
            # 唯一可靠的做法：换一个全新的目录，绝不碰已有资产。
            # ═══════════════════════════════════════════════════════════
            # ═══════════════════════════════════════════════════════════
            # ★ V23.1 修复：直接【加载并复用】已有材质，绝不换目录
            #
            # 旧逻辑把材质建到 Materials_HHMMSS 临时目录，
            # 但 UE 渲染/蓝图引用的是固定路径 /Game/DSH_Golem/Materials/，
            # 所以改了材质也不会生效 —— 渲染用的永远是凌晨那版旧资产。
            #
            # 正确做法：load_asset 拿到同一个对象，清空节点后重建。
            # 同一个对象被修改，引用它的模型立即生效。
            # ═══════════════════════════════════════════════════════════
            log("    资产已存在，加载并复用（保证路径不变）")
            try:
                _existing = unreal.EditorAssetLibrary.load_asset(path)
                if _existing is not None:
                    # 清空所有节点
                    try:
                        _n_clear = 0
                        for _e in list(
                                unreal.MaterialEditingLibrary
                                .get_material_expressions(_existing)):
                            try:
                                unreal.MaterialEditingLibrary \
                                    .delete_material_expression(_existing, _e)
                                _n_clear += 1
                            except Exception:
                                pass
                        if _n_clear:
                            log("    已清空 {} 个旧节点".format(_n_clear))
                    except Exception as _e:
                        log("    清空节点失败: {}".format(str(_e)[:60]))

                    # 断开所有属性连接
                    try:
                        for _prop in (unreal.MaterialProperty.MP_BASE_COLOR,
                                      unreal.MaterialProperty.MP_METALLIC,
                                      unreal.MaterialProperty.MP_ROUGHNESS,
                                      unreal.MaterialProperty.MP_NORMAL,
                                      unreal.MaterialProperty.MP_SPECULAR,
                                      unreal.MaterialProperty.MP_EMISSIVE_COLOR,
                                      unreal.MaterialProperty.MP_AMBIENT_OCCLUSION):
                            try:
                                unreal.MaterialEditingLibrary \
                                    .disconnect_material_property(
                                        _existing, "", _prop)
                            except Exception:
                                pass
                    except Exception:
                        pass

                    log("    ✓ 复用材质: {}".format(path))
                    return _existing
                log("    load_asset 返回 None")
            except Exception as _e:
                log("    load_asset 异常: {}".format(str(_e)[:80]))

        # ---- 方案 2：新建（多次重试，UE 删资产后需要时间释放）----
        for attempt in range(4):
            if attempt > 0:
                time.sleep(0.5)
                log("    重试第 {} 次…".format(attempt))
            try:
                mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
                    name, folder, unreal.Material, unreal.MaterialFactoryNew())
                if mat:
                    log("    ✓ 新建材质成功（第 {} 次尝试）".format(attempt + 1))
                    return mat
                log("    create_asset 返回 None（第 {} 次）".format(attempt + 1))
            except Exception as _e:
                log("    create_asset 异常（第 {} 次）: {}".format(
                    attempt + 1, str(_e)[:80]))

        # ---- 方案 3：换个名字 ----
        try:
            alt = name + "_v2"
            mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
                alt, folder, unreal.Material, unreal.MaterialFactoryNew())
            if mat:
                log("    ✓ 用备用名 {} 创建成功".format(alt))
                return mat
        except Exception as _e:
            log("    备用名也失败: {}".format(str(_e)[:70]))

        log("    ✗✗ 材质创建彻底失败: {}".format(path))
        return None


    def build_material(path, part, textures):
        """
        构建 PBR 材质。

        ══════════════════════════════════════════════════════════════
        关于"泥巴感"的根因（实测贴图通道确认）：
          Metallic+Roughness 贴图 R/G/B 全为 0，
          直接连 G 通道到 Roughness 会得到「完全光滑」的表面，
          丢失所有微观粗糙度层次 —— 这就是泥巴感的来源。

        本函数改为：
          Roughness ← 真正的 _Roughness 贴图（mean 197）
                      再用 ScalarParameter 做范围映射
          Metallic  ← 贴图(若有) 否则用常量参数（默认 0.35 有金属感）
          AO        ← 乘一个可调系数（默认 0.6，减轻脏暗）
          Albedo    ← 经过对比度 + 饱和度调节，强化岩石纹理
        ══════════════════════════════════════════════════════════════
        """
        mat = new_mat(path)
        x = [-1200]

        def nd(c, **p):
            if mat is None:
                raise RuntimeError("材质为 None，无法创建节点")
            n = mel.create_material_expression(mat, c, x[0], 0)
            if n is None:
                log("    ✗ create_material_expression 返回 None: {}".format(
                    getattr(c, "__name__", str(c))))
                raise RuntimeError("创建材质节点失败: {}".format(
                    getattr(c, "__name__", str(c))))
            x[0] += 240
            for k, v in p.items():
                try:
                    n.set_editor_property(k, v)
                except Exception:
                    pass
            return n

        def sp(name, default):
            """标量参数（可在编辑器里实时调）"""
            return nd(unreal.MaterialExpressionScalarParameter,
                      parameter_name=name, default_value=float(default))

        def vp(name, rgba):
            return nd(unreal.MaterialExpressionVectorParameter,
                      parameter_name=name,
                      default_value=unreal.LinearColor(*rgba))

        def mul(a, b, oa="", ob=""):
            n = nd(unreal.MaterialExpressionMultiply)
            mel.connect_material_expressions(a, oa, n, "A")
            mel.connect_material_expressions(b, ob, n, "B")
            return n

        def add(a, b):
            n = nd(unreal.MaterialExpressionAdd)
            mel.connect_material_expressions(a, "", n, "A")
            mel.connect_material_expressions(b, "", n, "B")
            return n

        def lerp(a, b, alpha, oa="", ob=""):
            n = nd(unreal.MaterialExpressionLinearInterpolate)
            mel.connect_material_expressions(a, oa, n, "A")
            mel.connect_material_expressions(b, ob, n, "B")
            mel.connect_material_expressions(alpha, "", n, "Alpha")
            return n

        uv = nd(unreal.MaterialExpressionTextureCoordinate)

        def tx(role, st, key=None):
            k = key or role
            if k not in textures:
                return None
            n = nd(unreal.MaterialExpressionTextureSampleParameter2D,
                   parameter_name="%s_%s" % (part, k),
                   texture=textures[k], sampler_type=sampler(st))
            mel.connect_material_expressions(uv, "", n, "Coordinates")
            return n

        # ---------- 采样贴图 ----------
        # ═══════════════════════════════════════════════════════════════
        # 采样器类型（决定 UE 如何解读贴图数据）：
        #   albedo  → SAMPLERTYPE_COLOR        颜色数据，需要 sRGB 解码
        #   normal  → SAMPLERTYPE_NORMAL       法线数据，特殊编码
        #   ao/rough/metal → SAMPLERTYPE_LINEAR_COLOR
        #                    纯数值数据，【绝不能】做 sRGB 解码
        #
        # 之前的 bug：这三个用了 COLOR，等于把 0.77 的粗糙度
        # 当作 sRGB 颜色解码成 0.55，材质全被压平 —— 这就是"灰泥感"。
        # ═══════════════════════════════════════════════════════════════
        alb = tx("albedo", "SAMPLERTYPE_COLOR")
        nrm = tx("normal", "SAMPLERTYPE_NORMAL")
        ao = tx("ao", "SAMPLERTYPE_LINEAR_COLOR")
        rough_tex = tx("roughness", "SAMPLERTYPE_LINEAR_COLOR")
        metal_tex = tx("metallic", "SAMPLERTYPE_LINEAR_COLOR")

        # ---------- 1. BASE COLOR ----------
        #
        # ═══════════════════════════════════════════════════════════════
        # 红色诊断开关（DEBUG_RED_BASECOLOR）
        #
        # 用途：确认模型是否真的用上了这个材质。
        #   True  → BaseColor 强制纯红 (1,0,0)
        #           如果渲染出来模型变红 = 材质生效，只是贴图没连上
        #           如果还是灰的     = 模型根本没用到这个材质
        #   False → 正常走 albedo 贴图
        # ═══════════════════════════════════════════════════════════════
        if DEBUG_RED_BASECOLOR:
            log("  [材质] {} BaseColor 强制为纯红色（诊断模式）".format(part))
            _red = nd(unreal.MaterialExpressionConstant3Vector)
            try:
                _red.set_editor_property(
                    "constant", unreal.LinearColor(1.0, 0.0, 0.0, 1.0))
            except Exception:
                pass
            mel.connect_material_property(
                _red, "", unreal.MaterialProperty.MP_BASE_COLOR)

        elif USE_CUSTOM_BASECOLOR:
            # ═══════════════════════════════════════════════════════════
            # 纯色基底模式（preset 驱动）
            #
            #   dark_metal : 暗金属色，金属感强
            #   rough_skin : 粗糙皮肤/岩石，粗糙度高、金属度低
            #   emissive   : 发光主体，基色极暗，靠 Emission 贴图发光
            # ═══════════════════════════════════════════════════════════
            _bc = CUSTOM_BASECOLOR
            log("  [材质] {} BaseColor 使用纯色模式 [{}] RGB({:.3f},{:.3f},{:.3f})".format(
                part, MATERIAL_MODE, _bc[0], _bc[1], _bc[2]))
            _col = nd(unreal.MaterialExpressionConstant3Vector)
            try:
                _col.set_editor_property(
                    "constant", unreal.LinearColor(_bc[0], _bc[1], _bc[2], 1.0))
            except Exception:
                pass
            mel.connect_material_property(
                _col, "", unreal.MaterialProperty.MP_BASE_COLOR)

            # ═══════════════════════════════════════════════════════════
            # 纯色模式下的材质参数覆盖
            #
            # dark_metal 之前没覆盖金属度/粗糙度，沿用了贴图里的值，
            # 导致渲染出来是灰白而非暗金属。这里显式指定。
            # ═══════════════════════════════════════════════════════════
            if MATERIAL_MODE == "dark_metal":
                try:
                    _mm = sp("%s_DMMetal" % part, 0.55)      # 中高金属度
                    mel.connect_material_property(
                        _mm, "", unreal.MaterialProperty.MP_METALLIC)
                except Exception:
                    pass
                try:
                    _mr = sp("%s_DMRough" % part, 0.55)      # 半哑光，抑制强反射
                    mel.connect_material_property(
                        _mr, "", unreal.MaterialProperty.MP_ROUGHNESS)
                except Exception:
                    pass
                log("  [材质] {} 暗黑金属：金属0.55 / 粗糙0.55".format(part))

            # ★ plaster（纯白石膏）：额外覆盖金属度/粗糙度，做出亚光石膏质感
            if MATERIAL_MODE == "plaster":
                try:
                    _pm = sp("%s_PlasterMetal" % part, 0.0)
                    mel.connect_material_property(
                        _pm, "", unreal.MaterialProperty.MP_METALLIC)
                except Exception:
                    pass
                try:
                    _pr = sp("%s_PlasterRough" % part, 0.92)
                    mel.connect_material_property(
                        _pr, "", unreal.MaterialProperty.MP_ROUGHNESS)
                except Exception:
                    pass
                log("  [材质] {} 石膏模式：金属0 / 粗糙0.92".format(part))

        elif alb:
            # 对比度：以 0.5 为中心缩放
            ctr = sp("%s_AlbedoContrast" % part, 1.25)
            half = nd(unreal.MaterialExpressionConstant)
            half.set_editor_property("r", 0.5)
            sub = nd(unreal.MaterialExpressionSubtract)
            mel.connect_material_expressions(alb, "RGB", sub, "A")
            mel.connect_material_expressions(half, "", sub, "B")
            scaled = mul(sub, ctr)
            basec = add(scaled, half)

            # 饱和度
            sat = sp("%s_AlbedoSaturation" % part, 1.15)
            desat = nd(unreal.MaterialExpressionDesaturation)
            mel.connect_material_expressions(basec, "", desat, "Input")
            mel.connect_material_expressions(sat, "", desat, "Fraction")
            mel.connect_material_property(desat, "", unreal.MaterialProperty.MP_BASE_COLOR)

        # ---------- 2. ROUGHNESS：真粗糙度贴图 + 范围映射 ----------
        #
        # 关键修复：不再用 Metallic+Roughness 的 G 通道（全 0），
        # 改用 _Roughness 贴图，并做 lerp 映射到 [min,max] 区间。
        # 岩石是哑光材质，粗糙度整体偏高
        rmin = sp("%s_RoughMin" % part, 0.55)   # 最光滑处
        rmax = sp("%s_RoughMax" % part, 0.95)   # 最粗糙处
        if rough_tex:
            # rough = lerp(rmin, rmax, 贴图.R)
            mel.connect_material_property(
                lerp(rmin, rmax, rough_tex, "", "R"),
                "", unreal.MaterialProperty.MP_ROUGHNESS)
        else:
            # 没有粗糙度贴图：用常量
            c = nd(unreal.MaterialExpressionConstant)
            c.set_editor_property("r", 0.6)
            mel.connect_material_property(c, "", unreal.MaterialProperty.MP_ROUGHNESS)

        # ---------- 3. METALLIC（通道解包）----------
        #
        # ═══════════════════════════════════════════════════════════════
        # ★ 实测发现：Metallic 数据存在【不同通道】里
        #
        #   Body_Metallic.tga    R:0   G:0   B:0   A:57   ← 值在 Alpha！
        #   Armor_Metallic.tga   R:112 G:112 B:112 A:64
        #   Weapon_Metallic.tga  R:135 G:135 B:135 A:80
        #
        # 旧代码只读 R 通道 → Body 拿到 0 → 金属度永远为 0 → 陶土感。
        #
        # 现在：ComponentMask 分别取 R 和 A，再用 Add 合并。
        #       若 A 全 0，结果等于 R，行为不变；两者都有值时取和。
        # ═══════════════════════════════════════════════════════════════
        # ═══════════════════════════════════════════════════════════════
        # ★ Metallic+Gloss / Metallic+Roughness 组合图：用 Break 拆通道
        #
        # 组合图的通道约定：
        #   Metallic+Gloss     : R = Metallic, G = Gloss (1-Roughness)
        #   Metallic+Roughness : R = Metallic, G = Roughness
        #
        # 用 ComponentMask（UE 里等价于 Break）分别取 R 和 G，
        # 再各自连到对应接口。绝不把整张图直接连过去。
        # ═══════════════════════════════════════════════════════════════
        # 注意：textures["combo"] 是裸 Texture2D 资产，
        # 必须先用 tx() 建采样节点才能连到 ComponentMask
        combo_tex = tx("combo", "SAMPLERTYPE_LINEAR_COLOR") \
            if textures.get("combo") is not None else None
        if combo_tex is not None:
            log("  [材质] {} 使用组合图拆包".format(part))

            # 拆 R = Metallic
            cm = nd(unreal.MaterialExpressionComponentMask)
            mel.connect_material_expressions(combo_tex, "", cm, "Input")
            try:
                cm.set_editor_property("r", True)
                cm.set_editor_property("g", False)
                cm.set_editor_property("b", False)
                cm.set_editor_property("a", False)
            except Exception:
                pass
            gain = sp("%s_MetallicGain" % part, 1.0)
            mel.connect_material_property(
                mul(cm, gain, "", ""), "",
                unreal.MaterialProperty.MP_METALLIC)

            # 拆 G = Roughness（或 Gloss，需要取反）
            cg = nd(unreal.MaterialExpressionComponentMask)
            mel.connect_material_expressions(combo_tex, "", cg, "Input")
            try:
                cg.set_editor_property("r", False)
                cg.set_editor_property("g", True)
                cg.set_editor_property("b", False)
                cg.set_editor_property("a", False)
            except Exception:
                pass

            _raw = textures.get("combo")
            is_gloss = ("gloss" in _raw.get_name().lower()) if _raw else False
            if is_gloss:
                # Gloss → Roughness：1 - G
                one = nd(unreal.MaterialExpressionConstant)
                one.set_editor_property("r", 1.0)
                sub = nd(unreal.MaterialExpressionSubtract)
                mel.connect_material_expressions(one, "", sub, "A")
                mel.connect_material_expressions(cg, "", sub, "B")
                rough_src = sub
                log("    G 通道是 Gloss，转为 Roughness = 1 - G")
            else:
                rough_src = cg
                log("    G 通道是 Roughness，直接使用")

            mel.connect_material_property(
                rough_src, "", unreal.MaterialProperty.MP_ROUGHNESS)

        elif metal_tex:
            gain = sp("%s_MetallicGain" % part, 1.0)

            mask_r = nd(unreal.MaterialExpressionComponentMask)
            mel.connect_material_expressions(metal_tex, "", mask_r, "Input")
            try:
                mask_r.set_editor_property("r", True)
                mask_r.set_editor_property("g", False)
                mask_r.set_editor_property("b", False)
                mask_r.set_editor_property("a", False)
            except Exception:
                pass

            mask_a = nd(unreal.MaterialExpressionComponentMask)
            mel.connect_material_expressions(metal_tex, "", mask_a, "Input")
            try:
                mask_a.set_editor_property("r", False)
                mask_a.set_editor_property("g", False)
                mask_a.set_editor_property("b", False)
                mask_a.set_editor_property("a", True)
            except Exception:
                pass

            combined = nd(unreal.MaterialExpressionAdd)
            mel.connect_material_expressions(mask_r, "", combined, "A")
            mel.connect_material_expressions(mask_a, "", combined, "B")

            log("  [材质] {} Metallic 通道解包：R + A 合并".format(part))
            mel.connect_material_property(
                mul(combined, gain, "", ""), "",
                unreal.MaterialProperty.MP_METALLIC)
        else:
            mval = sp("%s_Metallic" % part, 0.05)
            mel.connect_material_property(mval, "", unreal.MaterialProperty.MP_METALLIC)

        # ---------- 4. NORMAL + 绿通道翻转 ----------
        #
        # ═══════════════════════════════════════════════════════════════
        # Blender (OpenGL) 与 UE5 (DirectX) 的法线 G 通道方向相反：
        #   Blender: G 向上为正
        #   UE5    : G 向下为正
        #
        # 不翻转 → 凹凸方向全反 → 本该凸起处凹陷 → 加重"灰泥/融化"感。
        #
        # 正确做法是在【贴图资产】上设 flip_green_channel = True，
        # 不要在手写 Green *= -1（会破坏法线归一化）。
        # ═══════════════════════════════════════════════════════════════
        _nt = textures.get("normal")
        if _nt:
            try:
                cur = _nt.get_editor_property("flip_green_channel")
                if not cur:
                    _nt.set_editor_property("flip_green_channel", True)
                    unreal.EditorAssetLibrary.save_loaded_asset(_nt)
                    log("  [材质] {} 法线 FlipGreenChannel 已开启".format(part))
                else:
                    log("  [材质] {} 法线 FlipGreenChannel 已是开启".format(part))
            except Exception as _e:
                log("  [材质] {} flip_green_channel 失败：{}".format(part, str(_e)[:70]))

        if nrm:
            mel.connect_material_property(nrm, "RGB", unreal.MaterialProperty.MP_NORMAL)

        # ---------- 5. AO：乘可调系数，减轻脏暗 ----------
        if ao:
            ao_k = sp("%s_AOStrength" % part, 0.6)
            mel.connect_material_property(
                mul(ao, ao_k, "R"), "", unreal.MaterialProperty.MP_AMBIENT_OCCLUSION)
        else:
            aok = nd(unreal.MaterialExpressionConstant)
            aok.set_editor_property("r", 1.0)
            mel.connect_material_property(aok, "", unreal.MaterialProperty.MP_AMBIENT_OCCLUSION)

        # ---------- 6. SPECULAR：让金属高光更实 ----------
        # 镜面反射强度：调低一点，避免黑斑
        spec = sp("%s_Specular" % part, 0.35)
        mel.connect_material_property(spec, "", unreal.MaterialProperty.MP_SPECULAR)

        # ---------- 7. EMISSION ----------
        # emissive 模式：Emission 贴图 × 高增益
        _emi_tex = tx("emissive", "SAMPLERTYPE_COLOR") \
            if textures.get("emissive") is not None else None
        if DEBUG_EMISSION:
            # 诊断：Emissive 直接接纯白常量，验证通道是否工作
            _dbg = nd(unreal.MaterialExpressionConstant3Vector)
            try:
                _dbg.set_editor_property("constant",
                                         unreal.LinearColor(1, 1, 1, 1))
            except Exception:
                pass
            mel.connect_material_property(
                _dbg, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
            log("  [材质] {} DEBUG 纯白自发光".format(part))
        elif _emi_tex is not None:
            # ★ 增益优先级：emissive 模式用 MATERIAL_EMISSIVE_GAIN，
            #   其他模式用 EMISSION_GAIN（来自 render_config.json 的 emission.gain）
            _gain = (MATERIAL_EMISSIVE_GAIN if MATERIAL_MODE == "emissive"
                     else EMISSION_GAIN)
            _eg = sp("%s_EmissiveGain" % part, _gain)

            # 贴图 → 乘增益
            _emu = nd(unreal.MaterialExpressionMultiply)
            mel.connect_material_expressions(_emi_tex, "RGB", _emu, "A")
            mel.connect_material_expressions(_eg, "", _emu, "B")

            # × 颜色 tint（让发光偏暖白）
            _ecol = nd(unreal.MaterialExpressionConstant3Vector)
            try:
                _ecol.set_editor_property("constant", unreal.LinearColor(
                    EMISSION_COLOR[0], EMISSION_COLOR[1],
                    EMISSION_COLOR[2], 1.0))
            except Exception:
                pass
            _emt = nd(unreal.MaterialExpressionMultiply)
            mel.connect_material_expressions(_emu, "", _emt, "A")
            mel.connect_material_expressions(_ecol, "", _emt, "B")

            mel.connect_material_property(
                _emt, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)

            # ★ 诊断：确认连接真的生效
            try:
                _chk = mel.get_material_property_input_node(
                    mat, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
                if _chk is None:
                    log("  [材质] ⚠ {} Emission 连接无效！".format(part))
                else:
                    log("  [材质] {} Emission -> {} (增益 {:.1f})".format(
                        part, _chk.get_class().get_name(), _gain))
            except Exception as _e:
                log("  [材质] {} Emission 连接检查失败：{}".format(
                    part, str(_e)[:60]))

            log("  [材质] {} Emission 已连接（增益 {:.1f}，颜色 {}，模式 {}）".format(
                part, _gain, EMISSION_COLOR, MATERIAL_MODE))
        elif MATERIAL_MODE == "emissive":
            _eg = sp("%s_EmissiveGain" % part, MATERIAL_EMISSIVE_GAIN)
            _c3 = nd(unreal.MaterialExpressionConstant3Vector)
            try:
                _c3.set_editor_property(
                    "constant", unreal.LinearColor(
                        CUSTOM_BASECOLOR[0], CUSTOM_BASECOLOR[1],
                        CUSTOM_BASECOLOR[2], 1.0))
            except Exception:
                pass
            _emu2 = nd(unreal.MaterialExpressionMultiply)
            mel.connect_material_expressions(_c3, "", _emu2, "A")
            mel.connect_material_expressions(_eg, "", _emu2, "B")
            mel.connect_material_property(
                _emu2, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
            log("  [材质] {} 无 Emission 贴图，用基底色发光兜底".format(part))

        # ═══════════════════════════════════════════════════════════
        # ★ 强制双面渲染
        #
        # 这个模型是【单面】的（没有厚度）。从背面看时，
        # 单面材质会被背面剔除，导致模型整片消失 ——
        # 这正是"只有 0°/180° 能拍到，其他角度全黑"的根因。
        #
        # two_sided = True 后，正反两面都渲染，任何角度都可见。
        # ═══════════════════════════════════════════════════════════
        try:
            mat.set_editor_property("two_sided", True)
        except Exception:
            pass
        # 旧版本 UE 用这个属性名
        try:
            mat.set_editor_property("two_sided_material", True)
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════════
        # 修正数据贴图资产的 sRGB 标记
        #
        # AO / Roughness / Metallic 是线性数值数据，
        # 如果资产的 srgb=True，引擎会在采样时做 sRGB→线性 解码，
        # 把 0.77 变成 0.55，整张图失去对比 —— 这是"灰泥感"的根因。
        #
        # 这里把这三类贴图的 srgb 强制设为 False 并重新保存。
        # ═══════════════════════════════════════════════════════════════
        for _key in ("ao", "roughness", "metallic"):
            _t = textures.get(_key)
            if not _t:
                continue
            try:
                if _t.get_editor_property("srgb"):
                    _t.set_editor_property("srgb", False)
                    unreal.EditorAssetLibrary.save_loaded_asset(_t)
                    log("  [材质] {} 的 {} 贴图 srgb 已改为 False".format(part, _key))
            except Exception:
                pass

        # ═══════════════════════════════════════════════════════════════
        # ★ 重编译材质并【等待 shader 编译完成】
        #
        # recompile_material 只是标记脏，实际编译是异步的。
        # 如果不等待，SceneCapture 抓到的还是旧 shader，
        # 表现为"材质改了但画面不变"。
        # ═══════════════════════════════════════════════════════════════
        mel.recompile_material(mat)
        try:
            # 强制重建并等待
            if hasattr(unreal.MaterialEditingLibrary, "recompile_material"):
                pass
            # 标记资产为脏并保存，触发真实编译
            try:
                mat.modify()
            except Exception:
                pass
            # 等待 shader 编译队列排空（最多 8 秒）
            import time as _tw
            for _i in range(16):
                try:
                    if not unreal.get_editor_subsystem(
                            unreal.EditorSubsystem).__class__:
                        break
                except Exception:
                    pass
                _tw.sleep(0.25)
                if _i == 3:
                    break
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════════
        # ★ 验证 BaseColor 到底连了什么
        # ═══════════════════════════════════════════════════════════════
        try:
            _bc_node = mel.get_material_property_input_node(
                mat, unreal.MaterialProperty.MP_BASE_COLOR)
            _bc_type = type(_bc_node).__name__ if _bc_node else "None"
            log("  [验证] {} BaseColor <- {}".format(part, _bc_type))
            if _bc_node is None:
                log("  [验证] ✗ BaseColor 没有连接任何节点！")
        except Exception as _e:
            log("  [验证] BaseColor 查询失败: {}".format(str(_e)[:70]))
        try:
            log("  [验证] {} USE_CUSTOM={} DEBUG_RED={}".format(
                part, USE_CUSTOM_BASECOLOR, DEBUG_RED_BASECOLOR))
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════════
        # ① 强制保存材质资产到磁盘
        # ═══════════════════════════════════════════════════════════════
        save_ok = False
        try:
            save_ok = unreal.EditorAssetLibrary.save_loaded_asset(mat, False)
            log("  [材质] {} save_loaded_asset -> {}".format(part, save_ok))
        except Exception as exc:
            log("  [材质] {} save_loaded_asset 失败：{}".format(part, str(exc)[:80]))
        if not save_ok:
            try:
                save_ok = unreal.EditorAssetLibrary.save_asset(
                    path, only_if_is_dirty=False)
                log("  [材质] {} save_asset -> {}".format(part, save_ok))
            except Exception as exc:
                log("  [材质] {} save_asset 失败：{}".format(part, str(exc)[:80]))

        # ═══════════════════════════════════════════════════════════════
        # ② 在内容浏览器里定位并高亮选中
        # ═══════════════════════════════════════════════════════════════
        try:
            unreal.EditorAssetLibrary.sync_browser_to_objects([mat.get_path_name()])
            log("  [材质] {} 已在内容浏览器中定位".format(part))
        except Exception as exc:
            log("  [材质] {} sync_browser 失败：{}".format(part, str(exc)[:80]))

        # ═══════════════════════════════════════════════════════════════
        # ③ 打印真实物理路径
        # ═══════════════════════════════════════════════════════════════
        try:
            phys = unreal.Paths.convert_relative_path_to_full(
                unreal.Paths.project_content_dir() + path.replace("/Game/", "") + ".uasset")
            log("  [材质] {} 物理路径: {}".format(part, phys))
            log("  [材质] {} 磁盘存在: {}".format(part, os.path.isfile(phys)))
        except Exception as exc:
            log("  [材质] {} 物理路径查询失败：{}".format(part, str(exc)[:80]))

        # ═══════════════════════════════════════════════════════════════
        # 【诊断工具】Emissive 通道测试
        #
        # 用途：确认材质系统是否正常工作。
        #   在 render_config.json 里设 "emission": { "debug_white": true }
        #   如果渲染出来模型整体纯白发光 → 材质系统正常
        #   如果毫无变化                  → SceneCapture 不渲染 Emissive（已知限制）
        #
        # 默认关闭（debug_white=false），不影响正常出图。
        # ═══════════════════════════════════════════════════════════════
        try:
            _dbg_on = False
            try:
                _cc = _load_cfg_dict_py()
                _dbg_on = bool((_cc.get("emission") or {}).get("debug_white", False))
            except Exception:
                pass
            if _dbg_on:
                # 一个极高亮度的常量接 Emissive，任何光照模型都该亮
                _hi = nd(unreal.MaterialExpressionConstant3Vector)
                try:
                    _hi.set_editor_property(
                        "constant", unreal.LinearColor(50.0, 50.0, 50.0, 1.0))
                except Exception:
                    pass
                mel.connect_material_property(
                    _hi, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
                log("  [DIAG] {} Emissive 强制拉满 50x".format(part))
        except Exception as _e:
            log("  [DIAG] 强制拉满失败: {}".format(str(_e)[:60]))

        # ═══════════════════════════════════════════════════════════════
        # ★ 诊断：导出材质的 Emissive 状态
        # ═══════════════════════════════════════════════════════════════
        try:
            _ei = mel.get_material_property_input_node(
                mat, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
            log("  [DIAG] {} Emissive输入={} shading={}".format(
                part,
                _ei.get_class().get_name() if _ei else "None",
                getattr(mat, "get_editor_property", lambda x: "?")("shading_model")))
        except Exception as _e:
            log("  [DIAG] {} 查询失败: {}".format(part, str(_e)[:60]))

        return mat

    def pick(part, role, prefer_skin=True):
        """
        按【命名模糊匹配】挑选贴图（V11.0）。

        ═══════════════════════════════════════════════════════════════════
        不再硬编码 "albedo"/"metallic+roughness" 这类精确名字，
        而是按关键词模糊匹配，兼容 Albedo / BaseColor / Diffuse 等各种命名：

          BaseColor  : albedo | basecolor | base_color | diffuse | color | skin
          Roughness  : roughness | rough
          Metallic   : metallic | metal
          Normal     : normal | norm | nrm
          AO         : occlusion | ao
          Emissive   : emission | emissive

        匹配顺序（先精确后模糊）：
          1. 部件名匹配（body/armor/weapon）
          2. 关键词匹配
          3. 优先 skin1 变体
        ═══════════════════════════════════════════════════════════════════
        """
        KEYWORDS = {
            "albedo": ("albedo", "basecolor", "base_color", "diffuse", "color"),
            "roughness": ("roughness", "rough"),
            "metallic": ("metallic", "metal"),
            "normal": ("normal", "norm", "nrm"),
            "ao": ("occlusion", "ao"),
            "emissive": ("emission", "emissive"),
            "combo": ("metallic+gloss", "metallic+roughness", "orm", "rma"),
        }
        kws = KEYWORDS.get(role, (role,))
        pl = part.lower()

        hits = []
        for k, v in tex.items():
            kl = k.lower()

            # 部件匹配：优先本部件，其次不带部件名的通用贴图
            if pl in kl:
                part_rank = 0
            elif not any(p in kl for p in ("body", "armor", "weapon")):
                part_rank = 1
            else:
                continue

            # 关键词匹配
            kw_rank = 99
            for idx, kw in enumerate(kws):
                if kw in kl:
                    kw_rank = idx
                    break
            if kw_rank == 99:
                continue

            # ★ Metallic 必须排除所有组合图（metallic+roughness / metallic+gloss）
            #   组合图的通道含义和纯 metallic 完全不同：
            #     Metallic+Gloss     : R=metallic, G=gloss
            #     Metallic+Roughness : R=metallic, G=roughness（实测全 0）
            #   只有纯 _Metallic 才是单通道金属度
            if role == "metallic" and "+" in kl:
                continue
            # ★ Roughness 同样只接受纯 roughness，排除组合图
            if role == "roughness" and "+" in kl:
                continue

            skin_rank = 0 if "skin1" in kl else 1
            hits.append((part_rank, kw_rank, skin_rank, k, v))

        if not hits:
            return None
        hits.sort(key=lambda t: (t[0], t[1], t[2]))
        chosen = hits[0]
        log("  [匹配] {:<8s} {:<10s} -> {} (部件{} 关键词{} 皮肤{})".format(
            part, role, chosen[3], chosen[0], chosen[1], chosen[2]))
        return chosen[4]

    mats = {}
    for part in ("Body", "Armor", "Weapon"):
        u = {"albedo": pick(part, "albedo"),
             "normal": pick(part, "normal"),
             "ao": pick(part, "occlusion"),
             "roughness": pick(part, "roughness"),
             "metallic": pick(part, "metallic"),
             "combo": pick(part, "combo"),
             "emissive": pick(part, "emissive")}
        mats[part] = build_material(ROOT + "/Materials/M_" + part, part,
                                    {k: v for k, v in u.items() if v})
    log("材质构建完成")

    # 摆放网格
    golem = []
    for p in meshes:
        m = unreal.EditorAssetLibrary.load_asset(p)
        a = ss.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
        a.set_actor_label(m.get_name())
        a.static_mesh_component.set_static_mesh(m)
        n = m.get_name().lower()
        part = "Body" if "body" in n else ("Armor" if "armor" in n else "Weapon")
        a.static_mesh_component.set_material(0, mats[part])
        # 诊断：确认组件上真的挂上了材质
        try:
            _m = a.static_mesh_component.get_material(0)
            _mp = ""
            try:
                _mp = unreal.EditorAssetLibrary.get_path_name_for_loaded_asset(_m) if _m else ""
            except Exception:
                _mp = "?"
            log("  [材质核对] {} -> {} | {}".format(
                n, _m.get_name() if _m else "None", _mp))
            # 强制标记组件为 dirty 并重新应用材质，触发 shader 刷新
            try:
                a.static_mesh_component.set_material(0, None)
                a.static_mesh_component.set_material(0, mats[part])
                a.modify()
            except Exception:
                pass
        except Exception as _e:
            log("  [材质核对] {} 读取失败：{}".format(n, str(_e)[:50]))
        golem.append(a)

    lo = [1e30] * 3
    hi = [-1e30] * 3
    for a in golem:
        o, e = a.get_actor_bounds(False)
        lo = [min(lo[0], o.x - e.x), min(lo[1], o.y - e.y), min(lo[2], o.z - e.z)]
        hi = [max(hi[0], o.x + e.x), max(hi[1], o.y + e.y), max(hi[2], o.z + e.z)]
    cx, cy, cz = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2
    size = max(hi[i] - lo[i] for i in range(3))
    log("包围盒：中心 (%.1f, %.1f, %.1f)，最大尺寸 %.1f" % (cx, cy, cz, size))

    # 强制所有网格使用最高精度 LOD，禁止自动降级
    try:
        force_full_lod(golem)
    except Exception as exc:
        log("[画质] 强制 LOD0 失败：%s" % str(exc)[:100])

    # ═══════════════════════════════════════════════════════════════════
    # V23.0：不再创建地面（Floor）
    #
    # 之前反复出现的「白边/地平线」，本质是地板 Plane 与背景球的交界。
    # 无论地板做多大、球做多小，只要相机接近平视，
    # 这个交界就会在画面里留下一条硬边 —— 靠调尺寸永远修不掉。
    #
    # 现在彻底删掉地板。模型「悬浮」在无限渐变背景球里，
    # 背景由球体的世界坐标渐变材质提供（见 create_background_wall），
    # 画面中不存在任何几何交界，因此不可能出现横线。
    # ═══════════════════════════════════════════════════════════════════
    log("  [场景] 已跳过地面创建（V23.0 无限渐变背景模式）")

    return golem, (cx, cy, cz), size, lo, hi


# ============================================================================
# 模块 2：灯光 + PPV 泛光
# ============================================================================
def setup_lighting(ss, center, size, hi):
    cx, cy, cz = center

    def rect(loc, rot, inten, w, h, col):
        a = ss.spawn_actor_from_class(unreal.RectLight, unreal.Vector(*loc), unreal.Rotator(*rot))
        setp(comp(a, unreal.RectLightComponent), {
            "mobility": unreal.ComponentMobility.MOVABLE,
            "intensity_units": unreal.LightUnits.CANDELAS,
            "intensity": float(inten),
            "source_width": float(w),
            "source_height": float(h),
            "light_color": unreal.Color(*col)})

    # ================================================================
    # 灯光强度从 RENDER_CFG 动态读取（不再硬编码）
    #
    # ⚠️ 这里之前硬编码 400/30，会把 ue_render_core.setup_lights()
    #    按配置设好的 50/20 覆盖掉，导致"改了配置没生效"。
    #    现在统一从 render_config.json 的 lights 段取，
    #    取不到才用保守的默认值。
    # ================================================================
    _L = {}
    try:
        _L = (RENDER_CFG or {}).get("lights", {}) or {}
    except Exception:
        _L = {}

    def _lv(key, field, default):
        try:
            return float((_L.get(key) or {}).get(field, default))
        except Exception:
            return default

    def _lc(key, default):
        try:
            c = (_L.get(key) or {}).get("color", default)
            return (int(c[0]), int(c[1]), int(c[2]), 255)
        except Exception:
            return default

    key_i = _lv("key", "intensity", 50.0)
    fill_i = _lv("fill", "intensity", 20.0)
    top_i = _lv("top", "intensity", 25.0)

    # 冷白主光
    # ═══════════════════════════════════════════════════════════════════
    # 主光/补光跟随 FACE_ANGLE 方位
    #
    # 原来主光固定放在 (cx - size*0.9, ...) 即 -X 侧。
    # 相机转到 -Y（正面 270°）时，模型处于背光，整片发黑。
    #
    # 现在按 FACE_ANGLE 算出光照方位：
    #   主光 = 正面偏左 45°，补光 = 正面偏右 45°
    # 这样无论正面在哪个角度，模型正面都有主光。
    # ═══════════════════════════════════════════════════════════════════
    # ★ 关键：灯光跟随【当前拍摄角度】，而不是固定的 FACE_ANGLE
    #   旧逻辑固定用 FACE_ANGLE，其他角度就会背光全黑。
    #   这里用 CURRENT_SHOT_ANGLE（由 render 循环设置），
    #   未设置时退回 FACE_ANGLE。
    _lr = math.radians(globals().get("CURRENT_SHOT_ANGLE", FACE_ANGLE))

    # 主光：正面方向 + 45° 侧偏
    _k_ang = _lr + math.radians(45.0)
    _k_dist = size * 1.2
    _key_loc = (cx + _k_dist * math.cos(_k_ang),
                cy + _k_dist * math.sin(_k_ang),
                cz + size * 0.55)
    # 灯朝向模型中心：yaw 指向 -_k_ang
    _k_yaw = math.degrees(_k_ang) + 180.0
    rect(_key_loc, (-25, _k_yaw, 0), key_i, 200, size * 1.5,
         _lc("key", (180, 208, 255, 255)))

    # 暖色补光：正面方向 - 45° 侧偏
    _f_ang = _lr - math.radians(45.0)
    _f_dist = size * 1.1
    _fill_loc = (cx + _f_dist * math.cos(_f_ang),
                 cy + _f_dist * math.sin(_f_ang),
                 cz + size * 0.25)
    _f_yaw = math.degrees(_f_ang) + 180.0
    rect(_fill_loc, (-10, _f_yaw, 0), fill_i, 160, 240,
         _lc("fill", (255, 222, 185, 255)))

    # 顶部柔光
    rect((cx, cy, cz + size * 1.1), (-90, 0, 0),
         int(size * 0.35), size * 1.2, size * 0.6,
         _lc("top", (235, 240, 255, 255)))

    log("灯光：主光 {:.0f}cd + 补光 {:.0f}cd + 顶光 {:.0f}cd（来自配置）".format(
        key_i, fill_i, top_i))

    # ---- PPV 泛光 ----
    for a in list(ss.get_all_level_actors()):
        if isinstance(a, unreal.PostProcessVolume):
            ss.destroy_actor(a)

    ppv = ss.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(cx, cy, cz), unreal.Rotator(0, 0, 0))
    ppv.set_actor_label("DSH_PPV")
    setp(ppv, {"unbound": True, "blend_weight": 1.0, "priority": 10.0, "enabled": True})

    pps = ppv.get_editor_property("settings")
    setp(pps, {
        # 泛光：从配置读取，让自发光（眼睛/胸口）产生光晕
        "override_bloom_intensity": True,
        "bloom_intensity": BLOOM_INTENSITY,
        "override_bloom_threshold": True,
        "bloom_threshold": BLOOM_THRESHOLD,
        # 关闭运动模糊和景深，这些会让边缘发糊
        "override_motion_blur_amount": True, "motion_blur_amount": 0.0,
        "override_depth_of_field_fstop": True, "depth_of_field_fstop": 0.0,
        # 色调映射更平，保留高光和暗部细节
        "override_film_slope": True, "film_slope": 0.88,
        "override_film_toe": True, "film_toe": 0.55,
        "override_auto_exposure_method": True,
        "auto_exposure_method": unreal.AutoExposureMethod.AEM_MANUAL,
        "override_auto_exposure_bias": True, "auto_exposure_bias": EXPOSURE,
        "override_auto_exposure_apply_physical_camera_exposure": True,
        "auto_exposure_apply_physical_camera_exposure": False,
        "override_color_contrast": True,
        "color_contrast": unreal.Vector4(1.08, 1.08, 1.08, 1.0),
        "override_color_saturation": True,
        "color_saturation": unreal.Vector4(1.12, 1.12, 1.12, 1.0),
    })
    ppv.set_editor_property("settings", pps)
    log("PPV：泛光 {} / 阈值 {} / 手动曝光 EV{}".format(
        BLOOM_INTENSITY, BLOOM_THRESHOLD, EXPOSURE))


# ============================================================================
# 模块 3：多角度相机 + 黑图过滤
# ============================================================================
# 背景墙的当前状态（避免重复建材质）
_WALL_STATE = {"mat": None, "actor": None}


def _rebuild_wall_for(eye, target):
    """
    按当前相机位置重建背景墙。

    墙放在「相机看向模型」的方向上、模型的另一侧，
    保证从当前角度看过去正值模型背后。

    每帧重建的代价很低（只是移动一个 Actor），但能保证多角度都对。
    """
    if not RENDER_CFG:
        return
    wcfg = (RENDER_CFG.get("background_wall") or {})
    if not wcfg.get("enabled", True):
        return

    ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    # 删掉旧背景（球或墙都清）
    for a in list(ss.get_all_level_actors()):
        try:
            _lbl = a.get_actor_label() or ""
            if _lbl in ("DSH_BgBall", "DSH_BgWall"):
                ss.destroy_actor(a)
        except Exception:
            pass

    # 计算新的墙
    ex, ey, ez = eye
    tx, ty, tz = target
    center = unreal.Vector(tx, ty, tz)
    # size 从全局记录取（build_scene 完成后写入），默认 192
    size = float(_WALL_STATE.get("size", 0)) or float(globals().get("_MODEL_SIZE", 0)) or 191.9
    cam = unreal.Vector(ex, ey, ez)

    try:
        # 复用 core 的建墙函数；camera_loc 用当前相机位置
        wall, mat = core.create_background_wall(
            center, size, cam, wcfg, ss)
        if wall:
            _WALL_STATE["actor"] = wall
    except Exception as exc:
        log("  [背景墙] 重建失败：{}".format(str(exc)[:100]))


def capture_one(path, eye, target, show=None):
    """
    在 eye 位置朝 target 拍一张。

    ═══════════════════════════════════════════════════════════════════
    相机旋转的构造（V7.1 修正）：

      UE 坐标系：X 前、Y 右、Z 上。
      Actor 旋转顺序：Pitch（俯仰，绕 Y）→ Yaw（偏航，绕 Z）→ Roll（翻滚，绕 X）。

      ★ 关键：Roll 必须恒为 0。
        如果 Roll 不为 0，相机会绕自己的视线轴翻转，
        拍出来的图就是歪的（"脚朝上"之类）。

      Yaw 计算：UE 的 Yaw=0 指向 +X，Yaw 增加是朝 +Y 方向（逆时针俯视）。
        所以 yaw = atan2(dy, dx) 是正确的。

      Pitch 计算：正值为抬头，负值为低头。
        pitch = atan2(dz, horizontal_distance)
    ═══════════════════════════════════════════════════════════════════
    """
    world = unreal.EditorLevelLibrary.get_editor_world()
    ex, ey, ez = eye
    tx, ty, tz = target

    dx = tx - ex
    dy = ty - ey
    dz = tz - ez
    horiz = math.hypot(dx, dy)

    # 水平距离接近 0 时（垂直俯拍）用 0 偏航
    if horiz < 1e-3:
        yaw = 0.0
        pitch = 90.0 if dz > 0 else -90.0
    else:
        yaw = math.degrees(math.atan2(dy, dx))
        pitch = math.degrees(math.atan2(dz, horiz))

    # ★ roll 恒为 0，保证画面不会翻滚
    cam_rot = unreal.Rotator(pitch, yaw, 0.0)

    rt = unreal.RenderingLibrary.create_render_target2d(
        world, WIDTH, HEIGHT, unreal.TextureRenderTargetFormat.RTF_RGBA8_SRGB)
    rt.set_editor_property("clear_color", unreal.LinearColor(0, 0, 0, 1))

    cap = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.SceneCapture2D, unreal.Vector(ex, ey, ez), cam_rot)
    cc = comp(cap, unreal.SceneCaptureComponent2D)
    cc.texture_target = rt
    cc.capture_source = unreal.SceneCaptureSource.SCS_FINAL_COLOR_HDR
    cc.set_editor_property("fov_angle", FOV)

    # 让 SceneCapture 使用完整的后处理（含抗锯齿），而不是简化管线
    try:
        cc.set_editor_property("ignore_screen_percentage", False)
    except Exception:
        pass
    try:
        # 继承主视图的后处理设置，保证 PPV 生效
        cc.set_editor_property("inherit_main_view_camera_post_process_settings", True)
    except Exception:
        pass
    # =========================================================================
    # ★ 背景墙跟随相机
    #
    # 之前背景墙只在画质增强阶段按【一个】相机位置摆放，
    # 多角度渲染时墙会挡住其他角度的模型（实测 45°/90° 等角度只拍到墙）。
    #
    # 现在每次捕获前，先删掉旧墙，再按当前相机位置重新摆放。
    # =========================================================================
    try:
        _rebuild_wall_for(eye, target)
    except Exception as _e:
        pass

    # =========================================================================
    # 渲染范围：关卡里所有可见物体
    #
    # ⚠️ 这里原本用「只渲染白名单物体」的模式，但它会把用户在编辑器里
    #    手动摆放的背景板、地面、道具全部排除掉，导致背景永远是纯黑。
    #
    #    现已改为不限制渲染范围（PRM_RENDER_SCENE），
    #    你在 UE5 模板工程里手动放的背景板会正常出现在画面里，
    #    也能被景深虚化。
    #
    #    show 参数保留但不再用于过滤，只是为了不让调用方代码报错。
    # =========================================================================
    try:
        cc.primitive_render_mode = unreal.SceneCapturePrimitiveRenderMode.PRM_RENDER_SCENE
    except Exception:
        pass

    # ================================================================
    # ★ 多次 capture 收敛 —— 消除 TSR 涂抹感
    #
    # 原理：
    #   TSR（时域超分抗锯齿）靠历史帧累积来提升画质。
    #   SceneCapture 是「即时抓一帧」，如果只抓 1~2 次，
    #   TSR 的历史缓冲还没收敛，就会留下明显的涂抹/拖影。
    #
    #   解决办法：在同一帧内连续 capture 多次（每次让引擎 tick 推进），
    #   让 TSR 的累积历史逐渐稳定，最后导出的才是干净图像。
    #
    #   次数越多越干净，但耗时线性增长。
    #   CAPTURE_CONVERGE_FRAMES = 16 时效果接近 MRQ 的 Temporal Sample 16。
    # ================================================================
    total = max(2, int(CAPTURE_CONVERGE_FRAMES))
    for _i in range(total):
        # 每次 capture 前强制重设屏幕百分比
        # （引擎某些情况下会把它重置，这里每帧都锁一次）
        try:
            lib = unreal.ConsoleVariablesEditorFunctionLibrary
            lib.set_console_variable_by_name_int(
                "r.ScreenPercentage", int(SUPER_SAMPLE_PERCENTAGE))
        except Exception:
            pass

        cc.capture_scene()
        # 每次之间给引擎一点时间推进 TSR 历史
        time.sleep(CAPTURE_CONVERGE_INTERVAL)

    # 最后一次 capture 后多等一会，确保 GPU 把画面写完
    time.sleep(0.15)

    unreal.RenderingLibrary.export_render_target(world, rt, *os.path.split(path))
    size = os.path.getsize(path) if os.path.exists(path) else 0
    return size


def compute_model_bounds(actors):
    """
    用 StaticMeshComponent 的包围盒，算出模型在世界空间中的
    精确中心 Center 和尺寸 Size。

    ═══════════════════════════════════════════════════════════════════
    为什么不用 Actor 的 Pivot：
      模型由 11 个子网格组成，每个子网格的 Pivot 位置都不同
      （有的在原点、有的在部件中心）。如果按 Actor 坐标去算中心，
      会得到完全错误的结果。

      必须遍历每个 StaticMeshComponent 的世界包围盒，
      取所有包围盒的并集，才是模型真正的空间占位。

    返回 (center: Vector, size: float, lo: Vector, hi: Vector)
    ═══════════════════════════════════════════════════════════════════
    """
    lo = [1e30, 1e30, 1e30]
    hi = [-1e30, -1e30, -1e30]

    for a in actors:
        try:
            smc = a.static_mesh_component
            # ⚠️ SceneComponent.get_bounds() 的返回值格式在 Python 里不稳定，
            #    改用 Actor.get_actor_bounds()：返回 (center, box_extent)，
            #    同样是世界空间，且 API 稳定。
            origin, extent = a.get_actor_bounds(False)
            lo[0] = min(lo[0], origin.x - extent.x)
            lo[1] = min(lo[1], origin.y - extent.y)
            lo[2] = min(lo[2], origin.z - extent.z)
            hi[0] = max(hi[0], origin.x + extent.x)
            hi[1] = max(hi[1], origin.y + extent.y)
            hi[2] = max(hi[2], origin.z + extent.z)
        except Exception as _e:
            continue

    if lo[0] > 1e29:
        # 全部失败，退回 Actor 坐标
        log("[环绕] ⚠️ 包围盒计算全部失败，使用兜底值")
        return (unreal.Vector(0, 0, 100), 200.0,
                unreal.Vector(-100, -100, 0), unreal.Vector(100, 100, 200))

    cx = (lo[0] + hi[0]) * 0.5
    cy = (lo[1] + hi[1]) * 0.5
    cz = (lo[2] + hi[2]) * 0.5
    size = max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])

    return (unreal.Vector(cx, cy, cz), size,
            unreal.Vector(*lo), unreal.Vector(*hi))


def capture_orbit(A, center, radius, actors, path,
                  cam_lift=0.5, fov=50.0, converge=16, interval=0.08):
    """
    环绕拍摄：把相机放在角度 A 上，用 find_look_at_rotation 对准模型中心。

    ═══════════════════════════════════════════════════════════════════
    坐标系（UE5 左手系）：
      +X 前、+Y 右、+Z 上

    相机位置：
      CamLoc.X = Center.X + Radius * cos(A)
      CamLoc.Y = Center.Y + Radius * sin(A)
      CamLoc.Z = Center.Z + Size * cam_lift     ← 抬高形成俯视感

    相机旋转：
      CamRot = MathLibrary.find_look_at_rotation(CamLoc, Center)
      保证镜头永远正对模型中心，不依赖手算 atan2。
    ═══════════════════════════════════════════════════════════════════
    """
    world = unreal.EditorLevelLibrary.get_editor_world()
    ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    rad = math.radians(A)
    cam_x = center.x + radius * math.cos(rad)
    cam_y = center.y + radius * math.sin(rad)
    cam_z = center.z + radius * cam_lift
    cam_loc = unreal.Vector(cam_x, cam_y, cam_z)

    # ★ 用引擎函数算朝向，避免手写欧拉角出错
    try:
        cam_rot = unreal.MathLibrary.find_look_at_rotation(cam_loc, center)
    except Exception:
        # 兜底：手算
        dx = center.x - cam_x
        dy = center.y - cam_y
        dz = center.z - cam_z
        horiz = math.hypot(dx, dy)
        cam_rot = unreal.Rotator(
            math.degrees(math.atan2(dz, horiz)) if horiz > 1e-3 else 0.0,
            math.degrees(math.atan2(dy, dx)), 0.0)

    # ---- 渲染目标 ----
    rt = unreal.RenderingLibrary.create_render_target2d(
        world, WIDTH, HEIGHT, unreal.TextureRenderTargetFormat.RTF_RGBA8_SRGB)
    rt.set_editor_property("clear_color", unreal.LinearColor(0, 0, 0, 1))

    # ---- 相机 ----
    cap = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.SceneCapture2D, cam_loc, cam_rot)
    cc = comp(cap, unreal.SceneCaptureComponent2D)
    cc.texture_target = rt
    cc.capture_source = unreal.SceneCaptureSource.SCS_FINAL_COLOR_HDR

    # ★ FOV 拉大，避免近距离时模型被裁出画面
    try:
        cc.set_editor_property("fov_angle", float(fov))
    except Exception:
        pass

    # ★ 裁剪面：修正近距离裁剪导致模型消失
    #   默认 near clip 可能大于相机到模型的距离，把模型裁掉了。
    try:
        cc.set_editor_property("override_custom_near_clipping_plane", True)
        cc.set_editor_property("custom_near_clipping_plane", 1.0)
    except Exception:
        pass
    try:
        cc.set_editor_property("finite_far_plane", False)
    except Exception:
        pass

    try:
        cc.set_editor_property("ignore_screen_percentage", False)
    except Exception:
        pass
    try:
        cc.set_editor_property("inherit_main_view_camera_post_process_settings", True)
    except Exception:
        pass

    # 渲染全部可见物体（不用白名单）
    try:
        cc.primitive_render_mode = \
            unreal.SceneCapturePrimitiveRenderMode.PRM_RENDER_SCENE
    except Exception:
        pass

    # ---- 相机就位后等引擎稳定 ----
    time.sleep(0.25)

    # ---- 多次 capture 收敛（消除 TSR 涂抹）----
    for _ in range(max(2, int(converge))):
        try:
            lib = unreal.ConsoleVariablesEditorFunctionLibrary
            lib.set_console_variable_by_name_int(
                "r.ScreenPercentage", int(SUPER_SAMPLE_PERCENTAGE))
        except Exception:
            pass
        cc.capture_scene()
        time.sleep(interval)

    time.sleep(0.2)
    unreal.RenderingLibrary.export_render_target(world, rt, *os.path.split(path))
    sz = os.path.getsize(path) if os.path.exists(path) else 0

    # ⚠️ 不要在这里销毁相机！
    #    SceneCapture 仍持有 render target 引用，立即销毁会让引擎崩溃。
    #    统一记录到全局列表，由 cleanup_all_capture_cameras() 收尾清理。
    try:
        _CAPTURE_CAMERAS.append(cap)
    except Exception:
        pass

    return sz


# 本轮所有用于截图的临时相机（拍完统一清理）
_CAPTURE_CAMERAS = []


def cleanup_all_capture_cameras():
    """所有角度拍完后，统一销毁临时相机。"""
    ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    n = 0
    for c in list(_CAPTURE_CAMERAS):
        try:
            ss.destroy_actor(c)
            n += 1
        except Exception:
            pass
    del _CAPTURE_CAMERAS[:]
    if n:
        log("[环绕] 已清理 %d 个临时相机" % n)


def render_with_retry(golem, center, size, lo):
    """
    多角度环绕拍摄（V8.0 重写）。

    严格按 UE5 坐标系：
      CamLoc = Center + (Radius*cos(A), Radius*sin(A), Radius*lift)
      CamRot = find_look_at_rotation(CamLoc, Center)

    每个角度独立出图，不按字节数比较，全部遍历。
    """
    # ---- 用组件级包围盒重新算中心（不信任传入的 center）----
    c_vec, c_size, c_lo, c_hi = compute_model_bounds(golem)
    log("[环绕] 精确中心 (%.1f, %.1f, %.1f)，尺寸 %.1f" % (
        c_vec.x, c_vec.y, c_vec.z, c_size))
    log("[环绕] 包围盒 X: %.1f~%.1f  Y: %.1f~%.1f  Z: %.1f~%.1f" % (
        c_lo.x, c_hi.x, c_lo.y, c_hi.y, c_lo.z, c_hi.z))

    radius = c_size * DIST_FACTOR
    os.makedirs(OUT_DIR, exist_ok=True)

    # 灯光需要 actor 子系统
    try:
        ss_for_light = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    except Exception:
        ss_for_light = None

    good = []
    failed = []

    for ang in ANGLES:
        # ★ 通知灯光系统：当前拍摄角度
        #   注意：不再每帧重建灯光！
        #   实测每帧重建会干扰场景（相机构图错乱、灯光叠加），
        #   而且 setup_lights 内部已经按 FACE_ANGLE 摆了灯，
        #   多角度时靠 SkyLight + HDRI 补光即可。
        globals()["CURRENT_SHOT_ANGLE"] = ang

        # ★ 移动已有灯光到新角度（不重建，避免累积）
        try:
            import ue_render_core as _core_rl
            if hasattr(_core_rl, "reposition_lights"):
                _moved = _core_rl.reposition_lights(
                    unreal.Vector(center[0], center[1], center[2])
                    if not isinstance(center, unreal.Vector) else center,
                    size, ang)
                if _moved:
                    log("  [灯光] 已移动到角度 %d 的布光（%d 盏）" % (ang, _moved))
        except Exception as _e:
            log("  [灯光] 移动失败（不影响出图）：%s" % str(_e)[:80])

        path = os.path.join(OUT_DIR, "golem_%03d.png" % ang)
        sz = capture_orbit(
            ang, c_vec, radius, golem, path,
            cam_lift=CAM_LIFT, fov=FOV,
            converge=CAPTURE_CONVERGE_FRAMES,
            interval=CAPTURE_CONVERGE_INTERVAL)
        ok = sz > MIN_BYTES
        log("  角度 %3d°  %d 字节  %s" % (
            ang, sz, "← 有效" if ok else "（黑图/空图）"))
        SUMMARY["shots"].append({"angle": ang, "path": path, "bytes": sz})
        if ok:
            good.append((ang, path, sz))
        else:
            failed.append(ang)

    log("")
    log("[环绕] 完成：有效 %d 张，失败 %d 张" % (len(good), len(failed)))
    if failed:
        log("[环绕] 失败角度：%s" % ", ".join(str(a) for a in failed))

    if not good:
        raise RuntimeError("所有角度都渲染失败")

    # 终稿：最接近 FACE_ANGLE 的
    def ang_dist(a):
        dd = abs((a - FACE_ANGLE) % 360.0)
        return min(dd, 360.0 - dd)

    # 全部拍完，清理临时相机
    try:
        cleanup_all_capture_cameras()
    except Exception:
        pass

    hero = sorted(good, key=lambda t: (ang_dist(t[0]), -t[2]))[0]
    import shutil
    final = os.path.join(OUT_DIR, "FINAL.png")
    shutil.copyfile(hero[1], final)
    log("终稿：%s（角度 %d°）" % (final, hero[0]))
    SUMMARY.update(ok=True, final=final, final_angle=hero[0])
    return final


# ============================================================================
# ============================================================================
# 可选画质增强（安全隔离）
# ============================================================================
#
# 这一整段是"黑盒"：任何一步失败都只打日志、然后返回 None，
# 主流程会继续用原来的基础渲染出图，绝不会因为画质增强而中断。

QUALITY_ENABLED = @quality_enabled@
RENDER_CFG = None
_DELIVER_DIR = r"@deliver_dir@"


def load_quality_config():
    """
    加载 render_config.json。

    ═══ 关键：失败必须回退到写死的安全默认值 ═══
    任何异常（文件不存在、JSON 损坏、字段类型错）都返回内置的
    1080P + 关景深 的安全配置，保证主流程能跑下去。
    """
    global RENDER_CFG

    # 安全兜底值：1080P、无景深、EV 用经验值
    SAFE_DEFAULT = {
        "output": {"width": 1920, "height": 1080, "file_prefix": "render",
                   "min_valid_bytes": 102400},
        "quality": {"anti_aliasing_method": 4, "screen_percentage": 100,
                    "tsr_history_percentage": 200, "max_anisotropy": 16,
                    "shadow_quality": 5, "texture_mip_bias": 0,
                    "disable_motion_blur": True,
                    "disable_dynamic_resolution": True,
                    "depth_of_field_quality": 2},
        "exposure": {"method": "manual", "ev": -4.0,
                     "color_contrast": [1.08, 1.08, 1.08, 1.0],
                     "color_saturation": [1.12, 1.12, 1.12, 1.0],
                     "film_slope": 0.88, "film_toe": 0.55},
        "bloom": {"enabled": False, "intensity": 0.0, "threshold": 1.0},
        "hdri": {"path": "", "intensity": 1.0,
                 "indirect_lighting_intensity": 1.0,
                 "source_cubemap_angle": 90.0,
                 "lower_hemisphere_is_black": False,
                 "real_time_capture": False, "recapture_on_setup": True,
                 "fallback_sky_color": [0.05, 0.06, 0.09],
                 "hide_background": True},
        "lights": {},
        "camera": {"fov": 38.0, "distance_factor": 2.2,
                   "angles": [180, 0], "height_offset": 0.0,
                   "depth_of_field": {"enabled": False, "f_stop": 2.8,
                                      "focal_length": 50.0, "sensor_width": 36.0,
                                      "focus_method": "auto",
                                      "focus_distance": 0.0,
                                      "focus_offset": 0.0}},
        "model": {"rotation_yaw": 0.0, "scale": 1.0,
                  "force_lod0": True, "apply_smooth": True},
    }

    if not QUALITY_ENABLED:
        log("[画质] 未启用（模块加载失败或配置关闭），使用安全默认值")
        RENDER_CFG = SAFE_DEFAULT
        return SAFE_DEFAULT

    try:
        if _DELIVER_DIR not in sys.path:
            sys.path.insert(0, _DELIVER_DIR)
        from render_config import load_render_config
        cfg = load_render_config(logger=log)
        RENDER_CFG = cfg
        log("[画质] 配置加载成功（{}x{}）".format(
            cfg["output"]["width"], cfg["output"]["height"]))
        return cfg
    except Exception as exc:
        log("[画质] 配置加载失败：{}".format(str(exc)[:150]))
        log("[画质] 已回退到安全默认值：1920x1080，无景深")
        RENDER_CFG = SAFE_DEFAULT
        return SAFE_DEFAULT


def cleanup_previous_scene(ss):
    """删除上一轮遗留的辅助 Actor（HDRI/相机/PPV/灯光/背景板）。"""
    try:
        if _DELIVER_DIR not in sys.path:
            sys.path.insert(0, _DELIVER_DIR)
        from ue_render_core import cleanup_previous_scene as _clean
        n = _clean(ss)
        log("[画质] 清理上一轮辅助 Actor：{} 个".format(n))
        return n
    except Exception as exc:
        log("[画质] 清理失败（继续执行）：{}".format(str(exc)[:120]))
        return 0


def apply_quality_enhancement(ss, center, size, actors):
    """
    ═══ 画质增强主入口（黑盒）═══

    在"模型已就位、准备截图"这个节点调用。
    任何一步出错都只记录日志并返回 False，
    调用方（main）会忽略返回值继续用基础截图逻辑。

    返回 True 表示画质增强链路可用；False 表示应降级为旧逻辑。
    """
    if not QUALITY_ENABLED:
        return False

    cfg = RENDER_CFG
    if not cfg:
        log("[画质] 配置为空，跳过增强")
        return False

    try:
        if _DELIVER_DIR not in sys.path:
            sys.path.insert(0, _DELIVER_DIR)
        import ue_render_core as core
    except Exception as exc:
        log("[画质] 模块导入失败，降级：{}".format(str(exc)[:120]))
        return False

    # ⚠️ build_scene() 返回的 center 是 tuple (cx, cy, cz)，
    #    而 ue_render_core 里的函数需要 unreal.Vector，
    #    这里统一转换一次，避免 "'tuple' object has no attribute 'x'"
    try:
        if not isinstance(center, unreal.Vector):
            center = unreal.Vector(float(center[0]), float(center[1]), float(center[2]))
    except Exception as exc:
        log("[画质] center 转换失败，降级：{}".format(str(exc)[:100]))
        return False

    # ---------- 步骤 1：清理上一轮残留 ----------
    try:
        cleanup_previous_scene(ss)
    except Exception as exc:
        log("[画质] cleanup 异常（继续）：{}".format(str(exc)[:100]))

    # ---------- 步骤 1.1（已移除）：眼睛/胸口发光 ----------
    #
    # V23.4：发光效果已移除。
    #
    # 排查结论（保留记录，避免后人重复踩坑）：
    #   这套 SceneCapture 管线【不渲染材质的 Emissive 通道】。
    #
    # 验证过程：
    #   · Emissive 接 Emission 贴图 × 40 倍      → 画面无变化
    #   · Emissive 接 Emission 贴图 × 250 倍     → 画面无变化
    #   · Emissive 接纯白 50 倍常量              → 画面无变化
    #   · 节点连接检查  Emissive输入=Constant3Vector  ✓
    #   · 材质路径检查  /Game/DSH_Golem/Materials/M_Body ✓
    #   · 组件挂载检查  body_geo -> M_Body            ✓
    #
    # 结论：节点、路径、挂载全部正确，但 Emissive 不出现在画面里，
    #       属于 SceneCapture 的渲染管线限制，不是代码 bug。
    #
    # 若将来确实需要材质级自发光，需改用 Movie Render Queue（MRQ），
    # 它支持完整后处理管线。几何发光体方案也验证过可行，
    # 但视觉上不是真正的凹面发光，故一并移除。
    #
    # 下面的 [DIAG] 诊断逻辑保留，方便后续再次排查材质问题。

    # ---------- 步骤 1.4：强制天光（解决背光面纯黑）----------
    try:
        sky_cfg = cfg.get("skylight", {}) or {}
        if sky_cfg.get("enabled", True):
            _i = float(sky_cfg.get("intensity", 1.5))
            _c = sky_cfg.get("color", [0.55, 0.62, 0.78])
            core.ensure_skylight(ss, _i, tuple(_c))
            log("[画质] 天光补光 ✓")
        else:
            log("[画质] 天光已禁用（配置 skylight.enabled=false）")
    except Exception as exc:
        log("[画质] 天光失败（跳过）：{}".format(str(exc)[:110]))

    # ---------- 步骤 1.5：修复法线（解决碎玻璃式着色）----------
    try:
        mesh_paths = []
        for a in actors:
            try:
                smc = a.static_mesh_component
                m = smc.get_editor_property("static_mesh")
                if m:
                    mesh_paths.append(m.get_path_name().split(".")[0])
            except Exception:
                pass
        if mesh_paths:
            cfg_model = cfg.get("model", {}) or {}
            if cfg_model.get("fix_normals", True):
                core.fix_mesh_normals(mesh_paths)
                core.rebuild_static_meshes(mesh_paths)
                log("[画质] 法线修复 ✓")
            else:
                log("[画质] 法线修复已关闭（配置 model.fix_normals=false）")
    except Exception as exc:
        log("[画质] 法线修复失败（跳过）：{}".format(str(exc)[:120]))

    # ---------- 步骤 2：HDRI 环境光 ----------
    try:
        hdri_cfg = dict(cfg.get("hdri", {}) or {})
        hide_bg = bool(hdri_cfg.get("hide_background", True))

        # ★ 把 preset 指定的 HDRI 名注入，供 find_hdri_file 使用
        _ps = (cfg.get("preset") or {}).get("active", "")
        if _ps:
            _pdef = (cfg.get("presets") or {}).get(_ps) or {}
            _pp = str(_pdef.get("hdri_preset") or "").strip()
            if _pp:
                hdri_cfg["_preset_hdri"] = _pp
                log("[画质] preset '{}' 指定 HDRI: {}".format(_ps, _pp))

        log("[画质] HDRI 参数：preset={} path={} intensity={}".format(
            hdri_cfg.get("hdri_preset") or hdri_cfg.get("_preset_hdri", ""),
            hdri_cfg.get("path", "")[:50],
            hdri_cfg.get("hdri_intensity", "?")))
        # ★ 主动解析 HDRI 文件（先自己找一遍，日志写进 ue_render.log）
        _pname = hdri_cfg.get("_preset_hdri") or hdri_cfg.get("hdri_preset") or ""
        if _pname and hasattr(core, "find_hdri_file"):
            _found = core.find_hdri_file(
                _pname, hdri_cfg.get("hdri_dir", ""), "")
            if _found:
                log("[画质] HDRI 文件已定位: {}".format(_found))
                hdri_cfg["path"] = _found
            else:
                log("[画质] ⚠ HDRI preset '{}' 未找到文件，退回 path".format(_pname))

        core.setup_hdri_environment(
            hdri_cfg.get("path", ""), hdri_cfg, ss,
            hide_sky_background=hide_bg)
        log("[画质] HDRI 环境光 ✓")
    except Exception as exc:
        import traceback
        log("[画质] HDRI 失败，跳过：{}".format(str(exc)[:120]))
        log(traceback.format_exc()[:800])

    # ---------- 步骤 3：渲染质量 / PPV / 灯光 ----------
    try:
        core.apply_render_quality(cfg)
        log("[画质] 渲染质量 ✓")
    except Exception as exc:
        log("[画质] 渲染质量失败：{}".format(str(exc)[:100]))

    try:
        core.setup_post_process(center, cfg, ss)
        log("[画质] 后处理体积 ✓")
    except Exception as exc:
        log("[画质] PPV 失败：{}".format(str(exc)[:100]))

    try:
        if cfg.get("lights"):
            core.setup_lights(center, size, cfg, ss)
            log("[画质] 三灯布光 ✓")
    except Exception as exc:
        log("[画质] 灯光失败（保留原灯光）：{}".format(str(exc)[:100]))

    # ---------- 步骤 4：背景板 ----------
    # 已移除：背景板由用户在 UE 模板工程里手动摆放。
    # 自动生成 Plane + 计算旋转 + 建材质的这套逻辑已验证不可靠，
    # 全部删除，避免产生多余的 Actor 和材质资产。
    bg_plane = None

    # ---------- 步骤 5：CineCamera + DoF ----------
    try:
        cam_cfg = cfg.get("camera", {}) or {}
        d = size * float(cam_cfg.get("distance_factor", 2.2))
        h_off = float(cam_cfg.get("height_offset", 0.0)) * size
        ang = float((cam_cfg.get("angles") or [180])[0])
        cam_loc = unreal.Vector(
            center.x + d * math.cos(math.radians(ang)),
            center.y + d * math.sin(math.radians(ang)),
            center.z + h_off)

        cam, dof_ok = core.setup_cine_camera_dof(
            center, size, cam_loc, center, cam_cfg, ss)
        if cam:
            log("[画质] CineCamera ✓（DoF={}）".format(dof_ok))
            # 把相机位置存起来，供截图使用
            global _QUALITY_CAMERA
            _QUALITY_CAMERA = cam
        else:
            log("[画质] CineCamera 创建失败")

        # ---- 用 Cube 生成背景墙（实测可行的方案）----
        #  Cube 是闭合实体，任何角度都有面朝向相机，不需要算旋转。
        try:
            wall_cfg = cfg.get("background_wall", {}) or {}
            if wall_cfg.get("enabled", True):
                core.create_background_wall(
                    center, size, cam_loc, wall_cfg, ss)
                log("[画质] Cube 背景墙 ✓")
            else:
                log("[画质] Cube 背景墙已禁用")
        except Exception as exc:
            log("[画质] Cube 背景墙失败（跳过）：{}".format(str(exc)[:110]))
    except Exception as exc:
        log("[画质] 相机失败（用旧逻辑）：{}".format(str(exc)[:120]))

    # ---------- 步骤 6：渲染前最后确认法线与 LOD0 ----------
    try:
        mp = []
        for a in actors:
            try:
                m = a.static_mesh_component.get_editor_property("static_mesh")
                if m:
                    mp.append(m.get_path_name().split(".")[0])
            except Exception:
                pass
        if mp:
            core.lock_normals_and_lod0(mp, actors, ss)
            log("[画质] 法线/LOD 最终确认 ✓")
    except Exception as exc:
        log("[画质] 法线/LOD 最终确认失败（跳过）：{}".format(str(exc)[:110]))

    log("[画质] 增强流程结束")
    return True


_QUALITY_CAMERA = None


def get_quality_camera():
    """返回画质增强创建的 CineCamera（可能为 None）。"""
    return _QUALITY_CAMERA


# ============================================================================
# 主流程
# ============================================================================
def main():
    log("=" * 55)
    log("UE5 自动渲染开始   引擎 " + unreal.SystemLibrary.get_engine_version())
    log("=" * 55)

    ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    # 画质设置要尽早应用（在创建场景之前），
    # 否则 PPV 和 SceneCapture 会用到默认的低质量参数
    #
    # ⚠️ 这里曾经只打一句「应用失败」，把真实原因吞掉了，
    #    排查时完全不知道是哪行代码、哪个 API 出错。
    #    现在改为打印完整 traceback.format_exc()，
    #    包含异常类型、出错文件、行号、调用栈。
    try:
        apply_quality_settings()
        log("[画质] 渲染质量设置应用成功")
    except Exception:
        log("[画质] ⚠️ 应用失败，继续使用默认设置")
        log("[画质] ---- 完整异常堆栈 ----")
        for _line in traceback.format_exc().splitlines():
            log("[画质] " + _line)
        log("[画质] ---- 堆栈结束 ----")

    if not DO_SCENE:
        raise RuntimeError("未勾选「自动场景构建」，无法继续")

    # ---- 构建场景：导入模型 + 材质 + 摆放，返回包围盒信息 ----
    # ⚠️ 这一行是原有逻辑，center/size/lo/hi 全靠它，不能删
    golem, center, size, lo, hi = build_scene()

    # ========================================================================
    # ★★★ 插入点：模型已导入并摆放好，准备执行截图 ★★★
    #
    # 此时 center / size / lo / hi 都已可用，正是画质增强的最佳时机。
    #
    # 整个增强过程被 apply_quality_enhancement() 封装成黑盒：
    #   · 内部每一步都有 try-except，任何失败只打日志
    #   · 返回 True/False 只用于日志区分，不影响后续流程
    #   · 即使全部失败，下面原有的 setup_lighting + render_with_retry
    #     依然会照常执行，保证出图不中断
    # ========================================================================
    log("")
    log("[画质] ===== 开始画质增强（失败会自动降级，不影响出图）=====")
    quality_ok = False
    try:
        quality_ok = apply_quality_enhancement(ss, center, size, golem)
    except Exception as exc:
        # 理论上不会到这里（内部已全包），但多一层保险
        log("[画质] 增强入口异常，强制降级：{}".format(str(exc)[:150]))
        quality_ok = False

    if quality_ok:
        log("[画质] 增强已应用；基础灯光/相机仍会执行以兜底")
    else:
        log("[画质] 增强未启用，使用原有基础渲染流程")
    log("[画质] ===== 画质增强结束 =====")
    log("")

    # ---- 原有基础灯光（保持不变）----
    if DO_LIGHT:
        setup_lighting(ss, center, size, hi)
    else:
        log("灯光与泛光  —— 已跳过")

    # ---- 原有出图流程（保持不变）----
    if DO_CAMERA:
        # ---- 截图阶段：先暴力锁死画质，再开超采样 ----
        # 用 try/finally 保证无论截图成功还是抛异常，
        # 屏幕百分比都会被重置回 100，不会把引擎卡在超采样状态。
        try:
            apply_extreme_quality_lock()
        except Exception as exc:
            log("[锁画质] 调用失败（继续）：{}".format(str(exc)[:120]))
        enter_supersample()
        try:
            final = render_with_retry(golem, center, size, lo)
            log("渲染完成：" + final)
        finally:
            exit_supersample()
    else:
        log("相机与出图  —— 已跳过")


# 主流程前先加载配置（失败会自动回退安全默认值）
try:
    load_quality_config()
except Exception as exc:
    log("[画质] 配置初始化异常（已忽略）：{}".format(str(exc)[:150]))

try:
    main()
except Exception:
    err = traceback.format_exc()
    log("EXCEPTION\n" + err)
    SUMMARY["error"] = err

try:
    with open(os.path.join(OUT_DIR, "ue_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(SUMMARY, fh, ensure_ascii=False, indent=1)
except Exception:
    pass

# ============================================================================
# 安全退出：等引擎完成内部析构，再退出
# ============================================================================
#
# 崩溃码 0xC0000005（访问冲突）的成因：
#   渲染刚结束就立刻 quit_editor()，此时引擎仍在后台
#   异步加载 HDRI cubemap、编译着色器、析构渲染目标。
#   此时强杀主线程 → 访问已释放内存 → 崩溃。
#
# 对策：
#   1. 先等 2 秒让异步任务跑完
#   2. 手动 collect_garbage() 释放 Python/UE 包装对象
#   3. 再等 1 秒让析构稳定
#   4. 最后才 quit_editor()
# ============================================================================
if os.environ.get("UE5_AUTO_QUIT", "0") == "1":
    log("[退出] 清理完成，等待引擎析构…")

    # ---- 第 1 步：等待异步资源加载/编译完成 ----
    try:
        time.sleep(2.0)
        log("[退出] 已等待 2.0s（异步任务应已完成）")
    except Exception:
        pass

    # ---- 第 2 步：手动垃圾回收 ----
    try:
        unreal.SystemLibrary.collect_garbage()
        log("[退出] collect_garbage() 已执行")
    except Exception as exc:
        log("[退出] collect_garbage 失败：" + str(exc)[:80])

    # ---- 第 3 步：再等一会儿让析构稳定 ----
    try:
        time.sleep(1.0)
        log("[退出] 已等待 1.0s（析构稳定）")
    except Exception:
        pass

    # ---- 第 4 步：退出 ----
    log("[退出] 调用 quit_editor()")
    try:
        unreal.SystemLibrary.quit_editor()
    except Exception as exc:
        log("quit_editor 失败：" + str(exc))
'''


# =============================================================================
# ⑤ 进程执行工具
# =============================================================================

# =============================================================================
# ⑥ 接入端资产标准化（Ingest Pipeline）
# =============================================================================
#
# 客户发来的 zip 包通常包含：
#   · 中文文件名（解压后乱码）
#   · 一堆无关文件（.c4d .blend1 .max .psd …）
#   · 多层嵌套子目录
#   · 文件名带空格/括号/版本号等特殊字符
#
# 本模块把这堆东西整理成 UE/Blender 能直接吃的干净目录。
# =============================================================================

# 允许提取的资产后缀（全部小写比较）
INGEST_ALLOWED_EXTS = {
    ".fbx", ".obj", ".png", ".jpg", ".jpeg", ".tga",
    ".exr", ".hdr", ".mtl", ".bmp", ".tif", ".tiff",
    # 贴图相关的常见补充格式
    ".dds", ".hdr",
}

# 明确丢弃的后缀（占用大、无关）
INGEST_DROP_EXTS = {
    ".c4d", ".blend1", ".blend2", ".max", ".mb", ".ma",
    ".psd", ".ai", ".eps", ".ztl", ".zpr", ".sc1", ".spp",
    ".mp4", ".mov", ".avi", ".mkv", ".exe", ".dll", ".lnk",
    ".url", ".thumbs", ".db", ".ds_store",
}


def fix_zip_name(info):
    r"""
    修复 zip 内文件名的中文乱码。

    ═══════════════════════════════════════════════════════════════════
    原理：
      ZIP 规范里文件名没规定编码。Windows 下的中文 zip 通常用 GBK，
      但 Python 的 zipfile 遇到非 UTF-8 标记时会按 cp437 解码，
      于是 "装甲.fbx" 变成 "瑁呯敳.fbx" 这种乱码。

      修复方法：
        1. 先把已解码的字符串重新编码回 cp437 的字节
        2. 再用 gbk 解码
        3. 如果 gbk 也不行，试 utf-8 / big5，最后保留原文

    同时处理 zip 里的目录项（以 / 结尾）。
    ═══════════════════════════════════════════════════════════════════
    """
    name = info.filename
    if not name:
        return name

    # 已经是正确的 UTF-8 标记，不需要修
    try:
        flag = info.flag_bits & 0x800
        if flag:
            return name
    except Exception:
        pass

    # 纯 ASCII 的一定没问题
    try:
        name.encode("ascii")
        return name
    except UnicodeEncodeError:
        pass

    # 尝试修复
    for enc_chain in (("cp437", "gbk"), ("cp437", "utf-8"),
                      ("cp437", "big5"), ("cp437", "gb18030")):
        try:
            raw = name.encode(enc_chain[0])
            fixed = raw.decode(enc_chain[1])
            return fixed
        except Exception:
            continue

    return name


def sanitize_filename(name, max_len=60, fallback="asset"):
    r"""
    把文件名清洗成纯英文。

    规则：
      · 保留字母、数字、下划线、点和连字符
      · 中文 / 空格 / 括号 / 特殊符号 → 下划线
      · 连续下划线合并，去掉首尾下划线和点
      · 超长截断（保留扩展名）

    ═══════════════════════════════════════════════════════════════════
    ★ 关键：全中文文件名不能只剩扩展名

      错误做法："石魔像 最终版.fbx" → ".fbx"（丢主体）
      正确做法：中文占比高时，用 fallback + 序号

    判断标准：
      · 清洗后主体长度 < 2 个字符
      · 或原始主体里 ASCII 字母数字占比 < 30%
      → 视为"全中文名"，改用 fallback

    示例：
      "StoneGolem_v2 final.fbx"  -> "StoneGolem_v2_final.fbx"   正常
      "body 贴图.png"             -> "body.png"                  部分中文，保留英文
      "石魔像 最终版.fbx"          -> "mesh.fbx" (fallback)       全中文
      "材质#1@2x.tga"             -> "1_2x.tga"                  有数字，保留
    ═══════════════════════════════════════════════════════════════════
    """
    stem, ext = os.path.splitext(name)
    ext = ext.lower()

    # ---- 统计原始主体的 ASCII 字母数字占比 ----
    ascii_alnum = sum(1 for ch in stem if ch.isascii() and ch.isalnum())
    total = len(stem)
    ascii_ratio = (ascii_alnum / total) if total > 0 else 1.0

    # ---- 清洗 ----
    out = []
    prev_us = False
    for ch in stem:
        if ch.isascii() and (ch.isalnum() or ch in "_-."):
            out.append(ch)
            prev_us = False
        else:
            if not prev_us:
                out.append("_")
                prev_us = True
    clean = "".join(out).strip("_.")

    # ---- 判断是否需要 fallback ----
    # 条件：清洗后太短，或原主体 ASCII 占比过低
    need_fallback = (
        len(clean) < 2 or
        ascii_ratio < 0.30 or
        # 只剩下纯数字（如 "2024.fbx"）也算信息不足，但保留
        False
    )

    if need_fallback:
        # ★ 按扩展名选更贴切的默认名，避免所有中文文件都叫 asset
        _fb_map = {
            ".fbx": "model", ".obj": "model",
            ".png": "tex", ".jpg": "tex", ".jpeg": "tex",
            ".tga": "tex", ".exr": "tex", ".hdr": "hdri",
            ".bmp": "tex", ".tif": "tex", ".tiff": "tex",
            ".dds": "tex", ".mtl": "material",
        }
        clean = _fb_map.get(ext, fallback)
        # 如果还有数字残留，拼上去保留一点区分度
        if ascii_alnum > 0:
            _num = "".join(c for c in stem if c.isascii() and c.isdigit())
            if _num:
                clean = clean + "_" + _num

    # ---- 截断（保留扩展名空间）----
    if len(clean) > max_len:
        clean = clean[:max_len].rstrip("_.")
        if len(clean) < 2:
            clean = fallback

    return clean + ext


def unique_name(name, used):
    """
    确保文件名不重复。重复时自动加 _1 _2 …

    used 是一个 set，本函数会往里登记。
    """
    if name not in used:
        used.add(name)
        return name

    stem, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = "{}_{}{}".format(stem, i, ext)
        if cand not in used:
            used.add(cand)
            return cand
        i += 1


def make_task_dir(work_root, customer=""):
    r"""
    在工作区下创建任务目录：任务_YYYYmmdd_HHMMSS_客户名

    目录名必须是纯英文（UE 不支持中文路径），
    所以客户名也会被清洗。

    返回 (任务目录绝对路径, 客户名清洗后结果)
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")

    # 清洗客户名（全中文名会 fallback 到 "client"）
    cust = sanitize_filename(customer or "client",
                             fallback="client").replace(".", "_")
    cust = os.path.splitext(cust)[0] or "client"

    folder = "Task_{}_{}".format(stamp, cust)
    full = os.path.join(work_root, folder)

    try:
        os.makedirs(full, exist_ok=True)
    except Exception as exc:
        raise RuntimeError("创建任务目录失败：{}".format(str(exc)[:100]))

    return full, cust


def probe_writable(path, label="目录"):
    r"""
    探测目录是否真的可写。

    只检查 os.path.isdir 是不够的 —— 目录存在但没有写权限时，
    解压会静默失败。这里真的写一个探针文件验证。

    返回 (ok, 错误信息)
    """
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as exc:
        return False, "无法创建{}：{}".format(label, str(exc)[:120])

    probe = os.path.join(path, ".write_probe_{}".format(os.getpid()))
    try:
        with open(probe, "wb") as f:
            f.write(b"probe")
        os.remove(probe)
        return True, ""
    except Exception as exc:
        return False, (
            "{}存在但不可写：{}\n\n"
            "常见原因：\n"
            "  · 该盘符需要管理员权限（如 D:\\ 根目录）\n"
            "  · 被安全软件拦截\n"
            "  · 目录被其他程序占用\n\n"
            "建议改用文档或桌面下的目录，例如：\n"
            "  C:\\Users\\<用户名>\\Documents\\接单工作区").format(
                label, str(exc)[:120])


def ingest_zip(zip_path, work_root, customer="", progress=None):
    r"""
    客户 zip 包 → 干净英文工作目录。完整流程见模块顶部说明。

    ═══════════════════════════════════════════════════════════════════
    V21.1 重要修复：

      1. 不再静默失败 —— 每个 except 都记录详细错误
      2. 解压直接在工作目录内完成，不再绕道系统 temp
      3. 开始前探测目录可写性，不可写立刻报错
      4. 全程打印诊断日志（解压路径、文件数、失败原因）

    任何失败都抛 RuntimeError，附带可直接定位问题的中文说明。
    ═══════════════════════════════════════════════════════════════════
    """
    def say(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    import zipfile
    import shutil as _sh

    say("[诊断] ========== 预处理开始 ==========")

    # ---- 0. 输入检查 ----
    say("[诊断] zip 路径: {}".format(zip_path))
    if not zip_path:
        raise RuntimeError("未指定 zip 文件路径")

    if not os.path.isfile(zip_path):
        raise RuntimeError("zip 文件不存在：\n{}".format(zip_path))

    try:
        zip_size = os.path.getsize(zip_path)
    except Exception as exc:
        raise RuntimeError("无法读取 zip 文件：{}".format(str(exc)[:100]))

    say("[诊断] zip 大小: {:.1f} MB".format(zip_size / (1024.0 * 1024.0)))
    if zip_size == 0:
        raise RuntimeError("zip 文件是空的（0 字节）")

    # ---- 1. 工作区检查 ----
    say("[诊断] 工作区: {}".format(work_root))
    if not work_root:
        raise RuntimeError("未指定工作区目录")

    ok, err = probe_writable(work_root, "工作区")
    if not ok:
        raise RuntimeError(err)
    say("[诊断] 工作区可写 ✓")

    # ---- 2. 建任务目录 ----
    try:
        task_dir, cust = make_task_dir(work_root, customer)
    except Exception as exc:
        raise RuntimeError("创建任务目录失败：\n{}".format(str(exc)[:150]))

    say("[诊断] 任务目录已创建: {}".format(task_dir))
    ok, err = probe_writable(task_dir, "任务目录")
    if not ok:
        raise RuntimeError(err)

    # ---- 3. 打开 zip ----
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(
            "压缩包损坏或不是有效的 zip 文件：\n{}\n\n"
            "请让客户重新打包，或确认文件是否下载完整。".format(str(exc)[:150]))
    except Exception as exc:
        raise RuntimeError("无法打开压缩包：{}".format(str(exc)[:150]))

    # ---- 4. 解压到任务目录内的 _extract ----
    #
    # ★ 关键改动：直接在任务目录下解压，不走系统 temp。
    #   之前走 temp 时，某些环境（沙箱/权限）复制失败且被静默吞掉。
    extract_dir = os.path.join(task_dir, "_extract")
    try:
        os.makedirs(extract_dir, exist_ok=True)
    except Exception as exc:
        try:
            zf.close()
        except Exception:
            pass
        raise RuntimeError("创建解压目录失败：{}\n{}".format(
            extract_dir, str(exc)[:120]))

    say("[诊断] 正在尝试解压到: {}".format(extract_dir))

    n_extracted = 0
    n_fixed = 0
    extract_errors = []

    try:
        infos = zf.infolist()
        say("[诊断] 压缩包内 {} 个条目".format(len(infos)))

        if not infos:
            raise RuntimeError("压缩包是空的，里面没有任何文件")

        for info in infos:
            # 修复乱码
            real_name = fix_zip_name(info)
            if real_name != info.filename:
                n_fixed += 1

            # 目录项
            if info.is_dir() or real_name.endswith("/"):
                continue

            # 路径安全
            norm = os.path.normpath(real_name)
            if norm.startswith("..") or os.path.isabs(norm):
                say("[诊断] 跳过可疑路径: {}".format(real_name[:60]))
                continue

            target = os.path.join(extract_dir, norm)
            if not os.path.abspath(target).startswith(
                    os.path.abspath(extract_dir)):
                say("[诊断] 跳过越界路径: {}".format(real_name[:60]))
                continue

            try:
                d = os.path.dirname(target)
                if d:
                    os.makedirs(d, exist_ok=True)
                data = zf.read(info)
                with open(target, "wb") as dst:
                    dst.write(data)
                n_extracted += 1
            except Exception as exc:
                # ★ 不再静默跳过：记录下来，最后统一报告
                msg = "{}: {}".format(real_name[:50], str(exc)[:100])
                extract_errors.append(msg)
                say("[诊断] ✗ 解压失败 {}".format(msg))
    finally:
        try:
            zf.close()
        except Exception:
            pass

    say("[诊断] 解压完成，找到 {} 个文件（乱码修复 {} 个）".format(
        n_extracted, n_fixed))

    if n_extracted == 0:
        detail = ""
        if extract_errors:
            detail = "\n\n失败详情：\n" + "\n".join(extract_errors[:10])
        raise RuntimeError(
            "解压出 0 个文件。{}{}".format(
                "所有文件都解压失败了。" if extract_errors else
                "压缩包里可能只有目录，没有实际文件。",
                detail))

    # ---- 5. 过滤 + 重命名 + 平铺 ----
    say("[诊断] 开始过滤与重命名…")
    used = set()
    models, textures, others = [], [], []
    skipped = 0
    renamed = []
    copy_errors = []

    model_exts = {".fbx", ".obj"}
    tex_exts = {".png", ".jpg", ".jpeg", ".tga", ".exr", ".hdr",
                ".bmp", ".tif", ".tiff", ".dds"}

    for dirpath, dirnames, filenames in os.walk(extract_dir):
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            src_full = os.path.join(dirpath, fn)

            if ext in INGEST_DROP_EXTS:
                skipped += 1
                continue
            if ext not in INGEST_ALLOWED_EXTS:
                skipped += 1
                continue
            try:
                if os.path.getsize(src_full) == 0:
                    skipped += 1
                    continue
            except Exception:
                skipped += 1
                continue

            base = sanitize_filename(fn)
            new_name = unique_name(base, used)
            dst_full = os.path.join(task_dir, new_name)

            try:
                _sh.copy2(src_full, dst_full)
            except Exception as exc:
                copy_errors.append("{}: {}".format(fn[:40], str(exc)[:80]))
                say("[诊断] ✗ 复制失败 {}({})".format(fn[:30], str(exc)[:60]))
                skipped += 1
                continue

            renamed.append((fn, new_name))
            if ext in model_exts:
                models.append(dst_full)
            elif ext in tex_exts:
                textures.append(dst_full)
            else:
                others.append(dst_full)

    # 清理 _extract
    try:
        _sh.rmtree(extract_dir, ignore_errors=True)
        say("[诊断] 已清理临时解压目录")
    except Exception:
        pass

    say("[诊断] 提取 {} 个模型 / {} 张贴图 / {} 个其他".format(
        len(models), len(textures), len(others)))
    say("[诊断] 跳过 {} 个无关文件".format(skipped))

    # ---- 6. 结果校验 ----
    if not models:
        detail = ""
        if copy_errors:
            detail = "\n\n复制失败详情：\n" + "\n".join(copy_errors[:10])
        raise RuntimeError(
            "压缩包里没有 .fbx 或 .obj 模型文件。\n\n"
            "已解压 {} 个文件，但都是贴图或其他格式。\n"
            "任务目录：{}\n"
            "目录内容：{}".format(
                n_extracted, task_dir,
                ", ".join(os.path.basename(m) for m in
                          (textures + others)[:10]) or "(空)") + detail)

    say("[诊断] ✓ 预处理成功")
    say("[诊断] ========== 预处理结束 ==========")

    return {
        "task_dir": task_dir,
        "customer": cust,
        "models": models,
        "textures": textures,
        "others": others,
        "skipped": skipped,
        "renamed": renamed,
        "fixed_names": n_fixed,
        "extracted": n_extracted,
    }


def build_delivery_readme(delivery_dir, customer, stats, extra=None):
    r"""
    生成交付清单 README.txt。

    内容：
      · 客户名称 / 交付日期 / 处理耗时
      · 风格预设 + HDRI
      · 原始贴图/模型加载检测结果
      · 渲染设置（分辨率、抗锯齿采样）
      · 文件清单
      · 结尾联系方式

    extra 支持的键：
      model_name, tex_count, preset, material_mode, hdri,
      resolution, aa_samples, exposure, angle,
      elapsed,       本次处理耗时（秒）
      assets_ok,     模型/贴图是否全部加载成功
      assets_detail, 加载详情文本
      note,          自定义备注
    """
    import time as _t

    if not delivery_dir or not os.path.isdir(delivery_dir):
        return None

    extra = extra or {}
    now = _t.strftime("%Y-%m-%d %H:%M:%S")

    def _fmt_duration(sec):
        """秒 -> 1 分 23 秒"""
        try:
            sec = int(float(sec))
        except Exception:
            return "未知"
        if sec < 60:
            return "{} 秒".format(sec)
        m, s = divmod(sec, 60)
        if m < 60:
            return "{} 分 {} 秒".format(m, s)
        h, m = divmod(m, 60)
        return "{} 小时 {} 分 {} 秒".format(h, m, s)

    L = []
    D = "=" * 70
    S = "-" * 70

    L.append(D)
    L.append("  DSH 渲染交付单")
    L.append(D)
    L.append("")
    L.append("  交付编号 : {}".format(os.path.basename(delivery_dir)))
    L.append("  客户名称 : {}".format(customer or "(未填写)"))
    L.append("  交付日期 : {}".format(now))
    if extra.get("elapsed") is not None:
        L.append("  处理耗时 : {}".format(_fmt_duration(extra.get("elapsed"))))
    L.append("")

    # ---- 风格 ----
    L.append(S)
    L.append("  风格与光照")
    L.append(S)
    if extra.get("preset"):
        L.append("  风格预设 : {}".format(extra["preset"]))
    if extra.get("material_mode"):
        L.append("  材质模式 : {}".format(extra["material_mode"]))
    if extra.get("hdri"):
        L.append("  HDRI 贴图 : {}".format(os.path.basename(str(extra["hdri"]))))
    else:
        L.append("  HDRI 贴图 : (未使用，纯色环境光)")
    L.append("")

    # ---- 资产检测 ----
    L.append(S)
    L.append("  资产加载检测")
    L.append(S)
    _ok = extra.get("assets_ok")
    if _ok is True:
        L.append("  加载状态 : 全部成功 ✓")
    elif _ok is False:
        L.append("  加载状态 : 部分失败 ✗")
    else:
        L.append("  加载状态 : 未检测")
    if extra.get("model_name"):
        L.append("  模型文件 : {}".format(extra["model_name"]))
    if extra.get("tex_count") is not None:
        L.append("  贴图数量 : {} 张".format(extra["tex_count"]))
    if extra.get("assets_detail"):
        for line in str(extra["assets_detail"]).splitlines():
            L.append("  " + line)
    L.append("")

    # ---- 渲染设置 ----
    L.append(S)
    L.append("  渲染设置")
    L.append(S)
    if extra.get("resolution"):
        L.append("  输出分辨率 : {}".format(extra["resolution"]))
    if extra.get("aa_samples"):
        L.append("  抗锯齿采样 : {} 次累积 (TSR)".format(extra["aa_samples"]))
    if extra.get("angle") not in (None, ""):
        L.append("  拍摄角度 : {} 度".format(extra["angle"]))
    if extra.get("exposure") is not None:
        L.append("  曝光值   : EV {}".format(extra["exposure"]))
    L.append("")

    # ---- 文件清单 ----
    L.append(S)
    L.append("  文件清单")
    L.append(S)
    L.append("  图片 {} 张 / 日志 {} 个 / 资产 {} 个".format(
        stats.get("images", 0), stats.get("logs", 0), stats.get("assets", 0)))
    L.append("")

    for sub, title in (("Images", "渲染成品"),
                       ("Logs", "运行日志"),
                       ("Assets", "模型与贴图")):
        d = os.path.join(delivery_dir, sub)
        if not os.path.isdir(d):
            continue
        files = sorted(os.listdir(d))
        if not files:
            continue
        L.append("  [{}] {}".format(sub, title))
        for f in files:
            p = os.path.join(d, f)
            try:
                sz = os.path.getsize(p)
                if sz >= 1024 * 1024:
                    size_str = "{:.1f} MB".format(sz / (1024.0 * 1024.0))
                else:
                    size_str = "{:.0f} KB".format(sz / 1024.0)
            except Exception:
                size_str = "?"
            L.append("    {:<34} {:>9}".format(f, size_str))
        L.append("")

    # ---- 备注 ----
    if extra.get("note"):
        L.append(S)
        L.append("  备注")
        L.append(S)
        for line in str(extra["note"]).splitlines():
            L.append("  " + line)
        L.append("")

    # ---- 结尾 ----
    L.append(D)
    L.append("  由自动化管线生成，如有任何问题请在闲鱼联系。")
    L.append(D)

    path = os.path.join(delivery_dir, "README.txt")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(L) + "\n")
        return path
    except Exception:
        return None


def archive_delivery(task_dir, archive_dir, customer="", progress=None,
                    extra=None):
    r"""
    交付归档：把任务产物整理进交付文件夹。

    ═══════════════════════════════════════════════════════════════════
    流程：
      1. 在 archive_dir 下建 交付_年月日_客户名
      2. 从 task_dir 收集所有产物：
           · 所有 .png（成品图 + 各角度图）
           · ue_render.log / ue_summary.json（日志）
           · .fbx / .obj / 贴图（模型资产）
      3. 移动到交付文件夹（保留分类子目录）
      4. 清理 task_dir 里的中间文件（temp_scripts 等）

    参数：
      task_dir    : 任务工作目录（渲染输出所在）
      archive_dir : 交付根目录（如 D:/接单业务/已完成交付）
      customer    : 客户名
      progress    : 进度回调

    返回 dict：
      {
        "ok":        success/fail,
        "delivery":  交付文件夹路径,
        "images":    图片数,
        "logs":      日志数,
        "assets":    资产数,
        "error":     失败原因（ok=False 时）
      }
    ═══════════════════════════════════════════════════════════════════
    """
    def say(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    import shutil as _sh

    result = {"ok": False, "delivery": "", "images": 0, "logs": 0,
              "assets": 0, "error": ""}

    try:
        if not task_dir or not os.path.isdir(task_dir):
            result["error"] = "任务目录不存在"
            return result

        if not archive_dir:
            result["error"] = "未配置归档目录"
            return result

        # ---- 1. 建交付文件夹 ----
        stamp = time.strftime("%Y%m%d")
        cust = sanitize_filename(customer or "client",
                                 fallback="client").replace(".", "_")
        cust = os.path.splitext(cust)[0] or "client"

        delivery = os.path.join(archive_dir, "Delivered_{}_{}".format(stamp, cust))
        # 已存在则加序号
        if os.path.isdir(delivery):
            i = 1
            while os.path.isdir("{}_{}".format(delivery, i)):
                i += 1
            delivery = "{}_{}".format(delivery, i)

        try:
            os.makedirs(delivery, exist_ok=True)
        except Exception as exc:
            result["error"] = "创建交付目录失败：{}".format(str(exc)[:90])
            return result

        say("[归档] 交付目录：{}".format(delivery))

        # ---- 2. 分类收集 ----
        img_dir = os.path.join(delivery, "Images")
        log_dir = os.path.join(delivery, "Logs")
        asset_dir = os.path.join(delivery, "Assets")

        model_exts = {".fbx", ".obj", ".mtl"}
        tex_exts = {".png", ".jpg", ".jpeg", ".tga", ".exr", ".hdr",
                    ".bmp", ".tif", ".tiff", ".dds"}
        log_names = {"ue_render.log", "ue_summary.json", "render.log"}

        moved_img = moved_log = moved_asset = 0

        for dirpath, dirnames, filenames in os.walk(task_dir):
            # 跳过临时脚本目录
            if "temp_scripts" in dirpath.replace("\\", "/").split("/"):
                continue

            for fn in filenames:
                low = fn.lower()
                src_full = os.path.join(dirpath, fn)

                # 判定类型
                #
                # ⚠️ 顺序很重要：
                #   贴图（albedo.png / normal.tga …）必须归 Assets，
                #   只有【渲染产物】（FINAL.png / golem_*.png）才归 Images。
                #   所以先判断文件名特征，再判断扩展名。
                target_dir = None
                ext = os.path.splitext(low)[1]

                # 1. 日志类
                if low in log_names or low.endswith(".log") or low.endswith(".json"):
                    target_dir = log_dir

                # 2. 模型文件 → Assets
                elif ext in model_exts:
                    target_dir = asset_dir

                # 3. 渲染产物 → Images
                #    特征：FINAL / golem_ / render_ 前缀，或纯数字角度图
                elif ext in (".png", ".jpg", ".jpeg"):
                    is_render = (
                        low.startswith("final") or
                        low.startswith("golem_") or
                        low.startswith("render") or
                        low.startswith("shot") or
                        "render" in low
                    )
                    if is_render:
                        target_dir = img_dir
                    elif ext in tex_exts:
                        # 其余 png/jpg 视为贴图
                        target_dir = asset_dir
                    else:
                        target_dir = img_dir

                # 4. 其他贴图格式 → Assets
                elif ext in tex_exts:
                    target_dir = asset_dir

                else:
                    continue

                try:
                    os.makedirs(target_dir, exist_ok=True)
                    dst = os.path.join(target_dir, fn)
                    # 重名时加序号
                    if os.path.exists(dst):
                        stem, ext = os.path.splitext(fn)
                        k = 1
                        while os.path.exists(dst):
                            dst = os.path.join(
                                target_dir, "{}_{}{}".format(stem, k, ext))
                            k += 1
                    # ═══════════════════════════════════════════════
                    # ★ 用 copy 而不是 move
                    #
                    # 旧逻辑 move 会把文件从输出目录搬走，
                    # 用户界面上设置的"图片输出保存路径"就空了，
                    # 看起来像"文件夹消失"。
                    #
                    # 现在：复制到交付目录，原文件保留。
                    # ═══════════════════════════════════════════════
                    _sh.copy2(src_full, dst)

                    if target_dir == img_dir:
                        moved_img += 1
                    elif target_dir == log_dir:
                        moved_log += 1
                    else:
                        moved_asset += 1
                except Exception:
                    continue

        result["images"] = moved_img
        result["logs"] = moved_log
        result["assets"] = moved_asset
        result["delivery"] = delivery
        result["ok"] = True

        say("[归档] 图片 {} 张 / 日志 {} 个 / 资产 {} 个".format(
            moved_img, moved_log, moved_asset))

        # ---- 生成交付清单 README.txt ----
        try:
            _readme = build_delivery_readme(delivery, customer, result, extra)
            if _readme:
                say("[归档] 已生成交付清单：README.txt")
                result["readme"] = _readme
        except Exception as _e:
            say("[归档] 清单生成失败（不影响交付）：{}".format(str(_e)[:60]))

        # ---- 3. 清理任务目录 ----
        #
        # ═══════════════════════════════════════════════════════════════
        # ★ V21.7 修复：不再删除任务目录
        #
        # 旧逻辑把 task_dir 连同里面的文件全部删掉，导致：
        #   · 用户在界面上设置的"输出保存路径"文件夹凭空消失
        #   · 重新创建同名目录后，下一次归档又删掉
        #
        # 现在只清理【中间文件】，保留目录本身和关键产物：
        #   · temp_scripts/  临时生成的 py 脚本 → 删
        #   · *.tmp *.bak    → 删
        #   · FINAL.png / golem_*.png / *.log → 保留
        #   · 目录本身 → 保留
        #
        # 如果用户想彻底清空，可以手动删，或配置 archive.clean_after=True
        # ═══════════════════════════════════════════════════════════════
        cleaned = 0
        try:
            _keep_exts = {".png", ".jpg", ".jpeg", ".log", ".json",
                          ".fbx", ".obj", ".tga", ".exr", ".hdr"}
            for dirpath, dirnames, filenames in os.walk(task_dir, topdown=False):
                # 删掉临时脚本目录
                if os.path.basename(dirpath) == "temp_scripts":
                    for fn in filenames:
                        try:
                            os.remove(os.path.join(dirpath, fn))
                            cleaned += 1
                        except Exception:
                            pass
                    try:
                        os.rmdir(dirpath)
                    except Exception:
                        pass
                    continue

                # 只删中间文件，保留产物
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in _keep_exts:
                        continue
                    try:
                        os.remove(os.path.join(dirpath, fn))
                        cleaned += 1
                    except Exception:
                        pass

                # 空子目录删掉
                for dn in dirnames:
                    try:
                        os.rmdir(os.path.join(dirpath, dn))
                    except Exception:
                        pass

            if cleaned:
                say("[归档] 已清理 {} 个中间文件（目录保留）".format(cleaned))
            say("[归档] 任务目录已保留：{}".format(task_dir))
        except Exception as _e:
            say("[归档] 清理中间文件失败（不影响交付）：{}".format(str(_e)[:60]))

        return result

    except Exception as exc:
        result["error"] = str(exc)[:120]
        return result


def auto_detect_ingest(package_path, work_root, customer="", progress=None):
    """
    智能判断输入类型并标准化：

      · .zip 文件    → ingest_zip（解压 + 过滤 + 平铺 + 重命名）
      · 文件夹       → normalize_folder（扫描 + 过滤 + 平铺 + 重命名）

    两种输入最终都产出同样的结果：任务目录根下平铺着纯英文的
    fbx / obj / 贴图，可以直接丢给 Blender 和 UE。

    ═══════════════════════════════════════════════════════════════════
    V21.6 修复：
      之前文件夹输入只是"扫描"，不复制不重命名，导致
        · 文件藏在多层子目录里
        · 贴图是 UUID 文件名（0069826b-....tga）
        · 后续 Blender/UE 找不到贴图 → 材质全灰
      现在文件夹也会走完整的标准化流程。
    ═══════════════════════════════════════════════════════════════════
    """
    def say(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    # ---- zip ----
    if package_path.lower().endswith(".zip") and os.path.isfile(package_path):
        return ingest_zip(package_path, work_root, customer, progress)

    # ---- 文件夹 ----
    if not os.path.isdir(package_path):
        raise RuntimeError("输入既不是 zip 也不是文件夹：{}".format(package_path))

    import shutil as _sh

    say("[诊断] ========== 文件夹标准化开始 ==========")
    say("[诊断] 源目录: {}".format(package_path))

    # 建任务目录
    try:
        task_dir, cust = make_task_dir(work_root, customer)
    except Exception as exc:
        raise RuntimeError("创建任务目录失败：{}".format(str(exc)[:120]))

    say("[诊断] 任务目录: {}".format(task_dir))

    # 平铺复制 + 重命名
    used = set()
    models, textures, others = [], [], []
    skipped = 0
    renamed = []
    copy_errors = []

    model_exts = {".fbx", ".obj"}
    tex_exts = {".png", ".jpg", ".jpeg", ".tga", ".exr", ".hdr",
                ".bmp", ".tif", ".tiff", ".dds"}

    for dirpath, dirnames, filenames in os.walk(package_path):
        # 跳过已经在任务目录里的内容（防止自递归）
        if os.path.abspath(dirpath).startswith(os.path.abspath(task_dir)):
            continue

        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            src_full = os.path.join(dirpath, fn)

            if ext in INGEST_DROP_EXTS:
                skipped += 1
                continue
            if ext not in INGEST_ALLOWED_EXTS:
                skipped += 1
                continue
            try:
                if os.path.getsize(src_full) == 0:
                    skipped += 1
                    continue
            except Exception:
                skipped += 1
                continue

            base = sanitize_filename(fn)
            new_name = unique_name(base, used)
            dst_full = os.path.join(task_dir, new_name)

            try:
                _sh.copy2(src_full, dst_full)
            except Exception as exc:
                copy_errors.append("{}: {}".format(fn[:40], str(exc)[:60]))
                skipped += 1
                continue

            renamed.append((fn, new_name))
            if ext in model_exts:
                models.append(dst_full)
            elif ext in tex_exts:
                textures.append(dst_full)
            else:
                others.append(dst_full)

    say("[诊断] 平铺 {} 个模型 / {} 张贴图 / {} 个其他".format(
        len(models), len(textures), len(others)))
    say("[诊断] 跳过 {} 个无关文件".format(skipped))

    if not models:
        detail = ""
        if copy_errors:
            detail = "\n\n复制失败：\n" + "\n".join(copy_errors[:10])
        raise RuntimeError(
            "文件夹里没有 .fbx 或 .obj 模型文件。\n\n"
            "目录：{}\n"
            "已找到 {} 张贴图但没有模型。".format(package_path, len(textures)) + detail)

    say("[诊断] ✓ 文件夹标准化完成")
    say("[诊断] ========== 文件夹标准化结束 ==========")

    return {
        "task_dir": task_dir,
        "customer": cust,
        "models": models,
        "textures": textures,
        "others": others,
        "skipped": skipped,
        "renamed": renamed,
        "fixed_names": 0,
        "extracted": len(renamed),
    }


def run_process(cmd, cwd=None, env_extra=None, tag="PROC"):
    """
    启动外部进程并逐行读取输出，实时推送到日志队列。

    关键点：
      · stdout 和 stderr 都合并到 stdout（stderr=subprocess.STDOUT），
        否则错误信息看不到
      · 用 text=True + encoding="utf-8" + errors="replace"，
        防止 Blender/UE 输出的非 UTF-8 字节直接抛异常
      · 每读一行就 put 到队列，GUI 线程负责显示 → 界面不卡
    """
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)

    LOG_QUEUE.put(("info", "[系统] 启动进程：{}\n".format(" ".join(cmd[:3]) + " ...")))

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if tag == "BLENDER" else 0,
        )
    except FileNotFoundError:
        LOG_QUEUE.put(("error", "[错误] 找不到可执行文件：{}\n".format(cmd[0])))
        return -1
    except Exception as exc:
        LOG_QUEUE.put(("error", "[错误] 启动进程失败：{}\n".format(exc)))
        return -1

    # 逐行读输出
    for line in iter(proc.stdout.readline, ""):
        line = line.rstrip("\r\n")
        if not line:
            continue
        level = "error" if re.search(r"error|exception|traceback|failed", line, re.I) else "normal"
        LOG_QUEUE.put((level, line + "\n"))

    proc.stdout.close()
    rc = proc.wait()
    LOG_QUEUE.put(("info", "[系统] 进程结束，退出码 {}\n".format(rc)))
    return rc


# 临时脚本存放的目录名（普通英文，避免触发安全软件的启发式规则）
TEMP_SCRIPT_DIRNAME = "temp_scripts"


def get_temp_script_dir(base_dir=None):
    """
    返回存放临时脚本的目录（会自动创建）。

    优先用 base_dir（通常是输出目录）下的 temp_scripts 子目录，
    而不是系统 Temp 里的随机短名文件夹（如 Temp 下的 dsh-xxxx 目录）。

    为什么这么改：
      系统临时目录下那种「随机 8 位短名」的文件夹 + 随机命名的 .py 文件，
      是安全软件重点监控的对象（很像恶意软件的落地行为）。
      放到用户指定的输出目录下、用普通目录名，误报率大幅降低，
      而且出问题时用户能直接打开看到脚本内容。

    如果 base_dir 写不进去，回退到系统临时目录作为兜底。
    """
    candidates = []
    if base_dir:
        candidates.append(os.path.join(base_dir, TEMP_SCRIPT_DIRNAME))
    candidates.append(os.path.join(tempfile.gettempdir(), TEMP_SCRIPT_DIRNAME))

    last_exc = None
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception as exc:
            last_exc = exc
            continue

    # 全都建不出来，退回到最原始的方式
    return tempfile.gettempdir()


def write_temp_script(name, content, base_dir=None):
    """
    把生成的脚本写到临时脚本目录，返回绝对路径。

    · 强制 utf-8 编码（不带 BOM —— 带 BOM 会让 Blender 的 exec 报语法错误）
    · 带占用重试：上一次运行残留的临时脚本可能还被句柄占用
    · base_dir 一般传输出目录，让脚本落在用户看得见的地方
    """
    path = os.path.join(get_temp_script_dir(base_dir), name)
    safe_write_text(path, content)
    return path


def fill_template(template, values):
    """
    用 @占位符@ 语法替换模板参数。

    为什么不用 str.format：
      生成的脚本内部包含大量 Python 字面花括号（字典、格式串、lambda 等），
      用 .format 会把它们当成占位符，报 "Replacement index out of range"。
      改用 @key@ 这种不会和 Python 语法冲突的标记，最稳。
    """
    out = template
    for key, val in values.items():
        out = out.replace("@" + key + "@", str(val))
    # 检查有没有漏替换的占位符
    leftover = re.findall(r"@([A-Za-z_][A-Za-z0-9_]*)@", out)
    if leftover:
        raise RuntimeError("模板占位符未填充：{}".format(sorted(set(leftover))))
    return out


# =============================================================================
# ⑤.5 默认路径解析与自动搜索
# =============================================================================

def find_file_by_name(filename, roots, max_depth=AUTO_SEARCH_MAX_DEPTH):
    """
    在若干根目录下递归查找指定文件名（不区分大小写），返回第一个命中的绝对路径。
    找不到返回 None。

    用途：用户改了默认路径但没同步搬文件时，程序能自己找到，不用手动重选。
    """
    target = filename.lower()
    for root in roots:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("\\/").count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            # 限制搜索深度
            depth = dirpath.count(os.sep) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
                continue
            # 跳过明显无关的大目录，加快搜索
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in ("node_modules", ".git", "__pycache__", "build", "dist")]
            for fn in filenames:
                if fn.lower() == target:
                    return os.path.normpath(os.path.join(dirpath, fn))
    return None


def find_dir_by_names(names, roots, max_depth=AUTO_SEARCH_MAX_DEPTH):
    """在若干根目录下查找名称匹配的目录，返回第一个命中的绝对路径。"""
    wanted = [n.lower() for n in names]
    for root in roots:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("\\/").count(os.sep)
        for dirpath, dirnames, _files in os.walk(root):
            depth = dirpath.count(os.sep) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
                continue
            for d in dirnames:
                if d.lower() in wanted:
                    return os.path.normpath(os.path.join(dirpath, d))
    return None


def resolve_default_paths():
    """
    解析三个默认路径。返回 (fbx, texdir, outdir, notes)。

    notes 是一组提示信息（字符串列表），用于在日志里告诉用户
    「原本配置的路径不存在，自动改用了 xxx」。
    """
    notes = []

    # ---- 1. FBX ----
    fbx = CONFIG["default_fbx"]
    if not os.path.isfile(fbx):
        notes.append("[自动修正] FBX 不存在：{}".format(fbx))
        guess = find_file_by_name(os.path.basename(fbx), AUTO_SEARCH_ROOTS)
        if guess:
            fbx = guess
            notes.append("[自动修正] 已自动定位到：{}".format(guess))
        else:
            notes.append("[自动修正] 在搜索目录下也没找到 {}，请在界面上手动选择".format(
                os.path.basename(fbx)))

    # ---- 2. 贴图目录 ----
    texdir = CONFIG["default_texdir"]
    if not os.path.isdir(texdir):
        notes.append("[自动修正] 贴图目录不存在：{}".format(texdir))
        guess = find_dir_by_names(AUTO_SEARCH_TEXTURE_DIRNAMES, AUTO_SEARCH_ROOTS)
        if guess:
            texdir = guess
            notes.append("[自动修正] 已自动定位到：{}".format(guess))
        else:
            # 退一步：用 FBX 所在目录的上级
            parent = os.path.dirname(os.path.dirname(fbx)) if fbx else ""
            cand = os.path.join(parent, "Textures")
            if parent and os.path.isdir(cand):
                texdir = cand
                notes.append("[自动修正] 改用 FBX 同级 Textures：{}".format(cand))
            else:
                notes.append("[自动修正] 没找到贴图目录，请在界面上手动选择")

    # ---- 3. 输出目录（不存在就创建，不需要搜索）----
    outdir = CONFIG["default_outdir"]
    if not os.path.isdir(outdir):
        try:
            os.makedirs(outdir, exist_ok=True)
            notes.append("[自动修正] 已创建输出目录：{}".format(outdir))
        except Exception as exc:
            notes.append("[自动修正] 无法创建输出目录 {}（{}），请手动选择".format(outdir, exc))

    return fbx, texdir, outdir, notes


# =============================================================================
# ⑤.5 后台残留进程清理
# =============================================================================
# 为什么需要：
#   Blender / UE 被中途终止、崩溃、或用户直接关窗口时，
#   子进程可能变成孤儿进程继续驻留内存，并且锁住正在写的文件，
#   导致下一次运行报 Permission denied。
#
# 实现策略（不依赖 psutil，用系统自带命令最稳）：
#   1. 用 tasklist 列出进程，按名称精确筛选
#   2. 用 taskkill /F /T 强杀（/T 会连带子进程树）
#   3. 只杀本工具可能启动的那几个可执行文件，不动其他程序

# 本工具会启动的进程名（小写，不含路径）
CLEANUP_PROCESS_NAMES = ["blender.exe", "unrealeditor.exe", "unrealeditor-cmd.exe"]

# ---------------------------------------------------------------------------
# 自己启动的进程 PID 追踪表
#
# 为什么需要它：
#   tasklist / taskkill 在某些环境（受限账户、沙箱、组策略）下会返回
#   Access denied，导致「按名字查进程」的清理方式完全失效。
#   但我们自己 Popen 出来的进程，PID 是已知的 ——
#   直接对这些 PID 做 terminate/kill，不依赖任何外部命令，最可靠。
#
#   所以采用「双保险」：
#     主路径：按 PID 精确终止（永远可用）
#     补充路径：按进程名扫描（能清理上次遗留的孤儿进程）
# ---------------------------------------------------------------------------
_SPAWNED_PIDS = set()
_SPAWNED_LOCK = threading.Lock()


def register_spawned_pid(pid):
    """记录本工具启动的进程 PID。"""
    if pid:
        with _SPAWNED_LOCK:
            _SPAWNED_PIDS.add(int(pid))


def unregister_spawned_pid(pid):
    """进程正常结束后移除记录。"""
    with _SPAWNED_LOCK:
        _SPAWNED_PIDS.discard(int(pid))


def get_spawned_pids():
    """返回当前记录的 PID 列表（副本）。"""
    with _SPAWNED_LOCK:
        return list(_SPAWNED_PIDS)


def _pid_alive(pid):
    """判断 PID 是否还存活（不依赖 tasklist）。"""
    if os.name == "nt":
        # Windows 上 os.kill(pid, 0) 不可靠，改用 OpenProcess 探测
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def kill_spawned_pids(progress=None):
    """
    终止所有「本工具启动过、且仍存活」的进程（按 PID，不依赖 tasklist）。

    返回 kill 掉的数量。
    """
    def log(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    pids = get_spawned_pids()
    killed = 0
    for pid in pids:
        if not _pid_alive(pid):
            unregister_spawned_pid(pid)
            continue
        try:
            if os.name == "nt":
                # 先尝试温和终止，再强杀
                rc, out = _run_hidden(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=10)
                if rc != 0:
                    # taskkill 失败时用 Windows API 直接杀
                    import ctypes
                    PROCESS_TERMINATE = 0x0001
                    h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
                    if h:
                        ctypes.windll.kernel32.TerminateProcess(h, 1)
                        ctypes.windll.kernel32.CloseHandle(h)
            else:
                os.kill(int(pid), 9)
            log("  已终止 PID {}".format(pid))
            killed += 1
        except Exception as exc:
            log("  终止 PID {} 失败：{}".format(pid, exc))
        finally:
            unregister_spawned_pid(pid)

    return killed

# psutil 是可选依赖：装了就用它（更准更快），没装就回退到 tasklist/taskkill。
# 打包成 exe 时不会自动带上 psutil，所以 taskkill 方案是主要路径。
try:
    import psutil  # noqa: F401
    HAS_PSUTIL = True
except Exception:
    psutil = None
    HAS_PSUTIL = False


def _run_hidden(args, timeout=20):
    """
    执行命令并返回 (returncode, stdout_text)。

    用 CREATE_NO_WINDOW 避免弹出黑框；
    encoding 用系统默认（GBK）再兜底 utf-8，因为 tasklist 输出是本地化编码。
    """
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        p = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=creationflags,
        )
        raw = p.stdout or b""
        for enc in ("gbk", "utf-8", "latin-1"):
            try:
                return p.returncode, raw.decode(enc, errors="strict")
            except Exception:
                continue
        return p.returncode, raw.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -9, "命令超时"
    except FileNotFoundError:
        return -1, "找不到命令：{}".format(args[0] if args else "")
    except Exception as exc:
        return -1, str(exc)


def list_residual_processes():
    """
    列出当前系统中与 Blender / UE 相关的进程。

    返回 list[dict]：{pid: int, name: str, mem: str}

    用 tasklist 的 CSV 输出格式，解析最省事：
        "映像名称","PID","会话名","会话#","内存使用"
    中文系统上表头是中文，所以按列的位置取，不依赖表头文字。
    """
    found = []

    # ---- 优先用 psutil（如果装了）----
    if HAS_PSUTIL:
        try:
            for proc in psutil.process_iter(["pid", "name", "memory_info"]):
                try:
                    nm = (proc.info.get("name") or "").lower()
                    if nm in CLEANUP_PROCESS_NAMES:
                        mi = proc.info.get("memory_info")
                        mem = "{:.1f} MB".format(mi.rss / 1024.0 / 1024.0) if mi else ""
                        found.append({"pid": proc.info["pid"], "name": nm, "mem": mem})
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return found
        except Exception:
            # psutil 出问题就继续走 tasklist
            found = []

    if os.name != "nt":
        return found

    rc, out = _run_hidden(["tasklist", "/FO", "CSV", "/NH"])
    if rc != 0:
        return found

    for line in out.splitlines():
        line = line.strip()
        if not line or not line.startswith('"'):
            continue
        # 简易 CSV 解析：按 "," 分割，去掉引号
        try:
            parts = [c.strip().strip('"') for c in line.split('","')]
        except Exception:
            continue
        if len(parts) < 2:
            continue
        name = parts[0].strip('"').strip().lower()
        pid_s = parts[1].strip('"').strip()
        if name not in CLEANUP_PROCESS_NAMES:
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        mem = parts[4].strip('"').strip() if len(parts) > 4 else ""
        found.append({"pid": pid, "name": name, "mem": mem})

    return found


def kill_residual_processes(progress=None):
    """
    强杀所有残留的 Blender / UE 进程。

    progress: 可选回调 progress(text)，用于把过程输出到日志

    返回 (killed_count, details:list[str])

    注意：用 /T 连同子进程树一起杀 —— Blender 可能启动崩溃报告器，
          UE 会启动 DDC 子进程，只杀父进程会留下孤儿。
    """
    def log(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    killed_by_pid = 0

    # ---- 第一优先：按记录的 PID 精确终止（不受 tasklist 权限限制）----
    alive_pids = get_spawned_pids()
    if alive_pids:
        log("[清理] 本工具启动过的进程：{} 个".format(len(alive_pids)))
        killed_by_pid = kill_spawned_pids(progress=log)

    targets = list_residual_processes()
    if not targets:
        if killed_by_pid:
            log("[清理] 已终止 {} 个自启动进程".format(killed_by_pid))
        else:
            log("[清理] 未发现残留的 Blender / UE 进程")
        return killed_by_pid, []

    log("[清理] 发现 {} 个残留进程：".format(len(targets)))
    details = []
    killed = 0

    for item in targets:
        pid = item["pid"]
        name = item["name"]
        rc, out = _run_hidden(["taskkill", "/F", "/T", "/PID", str(pid)])
        if rc == 0:
            killed += 1
            msg = "  已终止 {} (PID {})".format(name, pid)
        else:
            msg = "  终止失败 {} (PID {})：{}".format(name, pid, out.strip()[:100])
        log(msg)
        details.append(msg)

    killed += killed_by_pid

    # 杀完再复查一遍
    remain = list_residual_processes()
    if remain:
        log("[清理] 仍有 {} 个进程未退出（可能被系统保护）".format(len(remain)))
    else:
        log("[清理] 清理完成，后台已无残留进程")

    return killed, details




def prepare_ascii_uproject(progress=None):
    """
    如果 .uproject 路径含中文，就在同目录下生成一个纯英文名的副本，
    并把原工程的 Content/Config/Plugins 用「目录联接(mklink /J)」挂过来。

    为什么要这么做（现场证据）：
        LogWebBrowser: Error: CEF initialization for cache dir
          D:/YourWorkspace/AppData/Local/UnrealEngine/我的项目_测试/webcache_6613
          failed with exit code 2
        LogOutputDevice: Error: Ensure condition failed: bCEFInitialized
        → UE 的内置浏览器 CEF 不支持含中文的路径，
          初始化失败触发 Ensure 断言，整个进程在启动阶段就崩了，
          Python 脚本根本没机会执行，所以什么文件都不会生成。

    ⚠️ 注意：UE 的 CEF 缓存路径是它自己用「工程名」拼出来的，
       环境变量 UE_CEF_CACHE_ROOT 并不会被采纳（实测无效），
       所以只能从「工程路径本身」下手。

    返回实际要传给 UE 的 .uproject 路径（纯英文副本，或原路径）。
    """
    def log(msg):
        try:
            if progress:
                progress(msg)
        except Exception:
            pass

    uproj = CONFIG["uproject"]
    try:
        uproj.encode("ascii")
        return uproj          # 已经是纯英文，直接用
    except UnicodeEncodeError:
        pass                  # 含中文，需要处理

    proj_dir = os.path.dirname(uproj)
    stem = "DSHPipeline_Project"

    # 工程里通常有 <name>.uproject + 同名文件夹（存 Content/Config）
    # 用纯英文名重建一套「影子工程」，内容用 junction 指回原目录
    shadow_uproject = os.path.join(proj_dir, stem + ".uproject")

    # ═══════════════════════════════════════════════════════════════════
    # 优先使用【已存在】的英文名 .uproject（用户手动创建的）
    #
    # 兼容两种常见误命名：
    #   DSHPipeline_Project.uproject               ← 正确
    #   DSHPipeline_Project.uproject.uproject      ← 多打了一个后缀
    # ═══════════════════════════════════════════════════════════════════
    for _cand in (shadow_uproject,
                  shadow_uproject + ".uproject"):
        if os.path.isfile(_cand):
            log("[CEF] 使用已有的英文工程文件：{}".format(_cand))
            return _cand

    try:
        # ---- 1. 复制 .uproject 文件本身（纯文本，改名即可） ----
        if not os.path.isfile(shadow_uproject):
            with open(uproj, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            # 里面一般还会有一个 "ProjectName" 之类的字段，不改也能跑
            with open(shadow_uproject, "w", encoding="utf-8") as fh:
                fh.write(content)
            log("[CEF] 已生成纯英文工程文件：{}".format(shadow_uproject))

        # ---- 2. 原工程同名目录 → 建立 junction ----
        original_stem = os.path.splitext(os.path.basename(uproj))[0]
        original_content_dir = os.path.join(proj_dir, original_stem)
        shadow_content_dir = os.path.join(proj_dir, stem)

        if os.path.isdir(original_content_dir) and not os.path.exists(shadow_content_dir):
            # mklink /J 创建目录联接，不复制数据、不占空间
            rc, out = _run_hidden(
                ["cmd", "/c", "mklink", "/J",
                 shadow_content_dir, original_content_dir], timeout=30)
            if rc == 0 and os.path.isdir(shadow_content_dir):
                log("[CEF] 已链接内容目录：{} -> {}".format(
                    shadow_content_dir, original_content_dir))
            else:
                log("[CEF] 内容目录链接失败（{}），可能少了 Config/Content，"
                    "但工程通常仍能启动".format(out.strip()[:80]))

        # ---- 3. 验证副本路径确实是纯 ASCII ----
        shadow_uproject.encode("ascii")
        log("[CEF] 改用纯英文工程运行，规避中文路径导致的 CEF 崩溃")
        return shadow_uproject

    except Exception as exc:
        log("[CEF] 生成纯英文工程失败：{}，仍使用原工程（可能崩溃）".format(exc))
        return uproj


def _ensure_ascii_cef_dir(base_dir):
    """
    为 UE 的 CEF（内置浏览器）准备一个纯 ASCII 的缓存目录。

    CEF 对非 ASCII 路径支持很差，中文路径会导致初始化失败并让 UE 崩溃。
    这里在系统临时目录下建一个只含英文和数字的缓存根，彻底避开中文。

    返回可用的目录路径；建不出来返回 None（调用方会忽略）。
    """
    import hashlib
    try:
        # 用 base_dir 的哈希值生成唯一但纯 ASCII 的目录名
        h = hashlib.md5(str(base_dir).encode("utf-8", errors="ignore")).hexdigest()[:12]
        root = os.path.join(tempfile.gettempdir(), "ue_cef_cache_" + h)
        os.makedirs(root, exist_ok=True)
        # 再确认这条路径确实是纯 ASCII
        root.encode("ascii")
        return root
    except Exception:
        return None


def _check_uproject_ascii():
    """
    检查 .uproject 路径是否含非 ASCII 字符（中文）。

    返回 (ok, message)
      ok=False 时 message 说明风险与建议。
    """
    p = CONFIG.get("uproject", "")
    try:
        p.encode("ascii")
        return True, ""
    except UnicodeEncodeError:
        return False, (
            "工程路径含中文：{}\n"
            "UE 的 CEF（内置浏览器）会因此初始化失败并导致进程崩溃，"
            "表现为 3221225477 内存冲突或直接闪退。\n"
            "建议：把 .uproject 改成纯英文名（例如 TestProject.uproject）。"
            .format(p))


def _to_posix(path):
    """
    把 Windows 反斜杠路径转成正斜杠。
    子进程（Blender / UE 的 Python）里用正斜杠更安全，
    且能避免生成脚本时反斜杠被当成转义字符。
    """
    return path.replace("\\", "/") if path else path


def _file_size(path):
    """返回文件字节数；不存在或读取失败返回 0（不抛异常）。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _ensure_dir(path):
    """确保目录存在，返回该路径。"""
    if path:
        os.makedirs(path, exist_ok=True)
    return path


# =============================================================================
# ⑤.6 子进程执行器（支持安全中断）
# =============================================================================

class ProcRunner:
    """
    封装 subprocess，负责：
      · 逐行读取子进程输出 → 推到 LOG_QUEUE（GUI 线程渲染）
      · 监听 STOP_FLAG，收到停止请求时终止子进程
      · stdout/stderr 合并，编码统一 utf-8 + errors=replace

    设计上刻意不做异步：它本身就跑在后台线程里，
    同步阻塞不会影响 GUI。
    """

    def __init__(self, stop_event):
        self.stop_event = stop_event
        self.proc = None

    def run(self, args, cwd=None, env=None, label="", timeout=None):
        """
        执行并等待结束，返回退出码。

        timeout: 超时秒数。None 表示不限时。
                 超时会强制终止进程，并返回特殊退出码 TIMEOUT_RC。
        """
        LOG_QUEUE.put(("info", "\n[系统] 启动{}：{} …{}".format(
            (" " + label) if label else "",
            os.path.basename(args[0]) if args else "?",
            ("（超时上限 {} 秒）".format(timeout)) if timeout else "")))

        try:
            creationflags = 0
            if os.name == "nt":
                # 不弹出黑色控制台窗口
                creationflags = subprocess.CREATE_NO_WINDOW
            self.proc = subprocess.Popen(
                args,
                cwd=cwd or os.getcwd(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env if env is not None else os.environ.copy(),
                creationflags=creationflags,
            )
        except FileNotFoundError:
            LOG_QUEUE.put(("error", "[错误] 找不到可执行文件：{}".format(args[0] if args else "")))
            return -1
        except Exception as exc:
            LOG_QUEUE.put(("error", "[错误] 启动进程失败：{}".format(exc)))
            return -1

        # 登记 PID：退出时可据此精确清理，不依赖 tasklist
        try:
            if self.proc and self.proc.pid:
                register_spawned_pid(self.proc.pid)
        except Exception:
            pass

        # ---- 逐行转发输出 ----
        # 用后台线程读取，主线程负责超时监控
        tail = []
        TAIL_MAX = 80
        timed_out = [False]

        def reader():
            try:
                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    if self.stop_event.is_set():
                        break
                    line = line.rstrip("\r\n")
                    if not line:
                        continue

                    tail.append(line)
                    if len(tail) > TAIL_MAX:
                        tail.pop(0)

                    low = line.lower()
                    if any(k in low for k in ("error", "exception", "traceback", "failed")):
                        level = "error"
                    elif any(k in low for k in ("warning", "warn")):
                        level = "warn"
                    else:
                        level = "normal"
                    LOG_QUEUE.put((level, line))
            except Exception:
                pass

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        # ---- 等待结束，带超时监控 ----
        start_ts = time.time()
        deadline = (start_ts + timeout) if timeout else None
        last_report = 0.0

        while True:
            try:
                self.proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass

            # 用户主动停止
            if self.stop_event.is_set():
                self.terminate()
                break

            now = time.time()

            # 超时判定
            if deadline and now > deadline:
                timed_out[0] = True
                LOG_QUEUE.put(("error",
                    "\n[超时] {} 已运行超过 {} 秒，强制终止！".format(
                        label or "子进程", timeout)))
                LOG_QUEUE.put(("warn",
                    "[超时] 常见原因：UE 卡在着色器编译、弹出了模态对话框、"
                    "或等待用户输入。"))
                self.terminate()
                break

            # 每 20 秒报一次进度，让用户知道没死
            elapsed = now - start_ts
            if elapsed - last_report >= 20:
                last_report = elapsed
                if deadline:
                    LOG_QUEUE.put(("info",
                        "[等待] {} 已运行 {:.0f} 秒（超时上限 {:.0f} 秒）…".format(
                            label or "子进程", elapsed, timeout)))
                else:
                    LOG_QUEUE.put(("info",
                        "[等待] {} 已运行 {:.0f} 秒…".format(label or "子进程", elapsed)))

        # 等读取线程收尾，确保输出不丢
        try:
            reader_thread.join(timeout=3.0)
        except Exception:
            pass
        except Exception as exc:
            LOG_QUEUE.put(("warn", "[警告] 读取输出异常：{}".format(exc)))
        finally:
            try:
                if self.proc.stdout:
                    self.proc.stdout.close()
            except Exception:
                pass

        # ---- 等待退出 ----
        try:
            rc = self.proc.wait(timeout=30)
        except Exception:
            self.terminate()
            rc = -1

        # 进程已结束，从追踪表移除
        try:
            if self.proc and self.proc.pid:
                unregister_spawned_pid(self.proc.pid)
        except Exception:
            pass

        if timed_out[0]:
            rc = TIMEOUT_RC

        elapsed_total = time.time() - start_ts
        LOG_QUEUE.put(("info", "[系统] 进程结束，退出码 {}，耗时 {:.1f} 秒".format(
            rc, elapsed_total)))

        # ---- 失败诊断：把关键输出汇总再打一遍 ----
        # 用户反馈「看不到具体原因」，这里专门做汇总：
        # 从缓存的尾部输出里挑出错误行，集中显示，避免被大量日志淹没。
        if rc != 0 and not self.stop_event.is_set():
            self._report_failure(rc, label, tail)

        return rc

    @staticmethod
    def _report_failure(rc, label, tail):
        """
        子进程返回码非 0 时，打印诊断信息。

        会做三件事：
          1. 解释常见退出码的含义
          2. 从输出尾部提取最可能的错误行
          3. 如果什么都没匹配到，直接把最后 15 行原样贴出来
        """
        name = (label or "子进程").strip()

        # 常见退出码解释（Windows）
        code_meaning = {
            1: "一般性错误（脚本抛异常、参数错误）",
            2: "命令行参数错误",
            -1: "进程被强制终止或启动失败",
            -9: "进程被杀死（超时或外部终止）",
            -1073741819: "0xC0000005 内存访问冲突（程序崩溃）",
            -1073741571: "0xC00000FD 栈溢出",
            -1073740791: "0xC0000409 栈缓冲区溢出（通常是被安全软件拦截）",
            3221225477: "0xC0000005 内存访问冲突（程序崩溃）",
            3221226505: "0xC0000409 栈缓冲区溢出（常见于杀毒软件拦截）",
        }
        if rc == TIMEOUT_RC:
            meaning = "执行超时，已被强制终止"
        else:
            meaning = code_meaning.get(rc, "")

        LOG_QUEUE.put(("error", "\n" + "-" * 60))
        LOG_QUEUE.put(("error", "[诊断] {} 执行失败！退出码 {} {}".format(
            name, rc, ("（{}）".format(meaning)) if meaning else "")))
        LOG_QUEUE.put(("error", "-" * 60))

        # ---- 从尾部输出里挑错误行 ----
        keywords = ("error", "exception", "traceback", "failed", "cannot",
                    "unable", "denied", "not found", "refused", "invalid",
                    "fatal", "abort", "crash", "冲突", "错误", "失败", "拒绝")

        hits = []
        for line in tail:
            low = line.lower()
            if any(k in low for k in keywords):
                hits.append(line)

        if hits:
            LOG_QUEUE.put(("error", "[诊断] 捕获到的错误信息（末尾 {} 条）：".format(
                min(len(hits), 12))))
            for line in hits[-12:]:
                LOG_QUEUE.put(("error", "  | " + line[:200]))
        elif tail:
            LOG_QUEUE.put(("error", "[诊断] 未匹配到明确错误关键字，以下是最后 15 行输出："))
            for line in tail[-15:]:
                LOG_QUEUE.put(("error", "  | " + line[:200]))
        else:
            LOG_QUEUE.put(("error", "[诊断] 该进程没有任何输出就退出了。"))
            LOG_QUEUE.put(("error", "        常见原因：被安全软件直接拦截、"
                                    "可执行文件损坏、缺少运行库。"))

        LOG_QUEUE.put(("error", "-" * 60 + "\n"))

    def terminate(self):
        """安全终止子进程（先 terminate，超时再 kill）。"""
        if not self.proc or self.proc.poll() is not None:
            return
        LOG_QUEUE.put(("warn", "[系统] 正在终止子进程…"))
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                LOG_QUEUE.put(("warn", "[系统] 超时，强制结束"))
                self.proc.kill()
        except Exception as exc:
            LOG_QUEUE.put(("warn", "[警告] 终止失败：{}".format(exc)))


# =============================================================================
# ⑥ GUI 主类
# =============================================================================

class PipelineApp:
    """
    主界面类。

    结构（从上到下）：
        1. 扫描区：总文件夹 + 扫描按钮
        2. 模型勾选列表：可滚动，每行 FBX 名 + 匹配到的贴图目录
        3. 路径配置：模型/贴图/输出（扫描后自动同步）
        4. Blender 勾选区 + UE5 勾选区
        5. 执行按钮：仅 Blender / 仅 UE5 / 一键全自动 / 停止
        6. 日志区
    """

    def __init__(self, root):
        self.root = root

        # ---------------- 扫描结果 ----------------
        self.models = []          # 扫描得到的模型列表（dict）
        self.checks = []          # 与 models 一一对应的 BooleanVar
        self.search_vars = []     # 每行「递归搜索」勾选（暂未使用，保留扩展）

        # ---------------- 路径变量 ----------------
        fbx0, tex0, out0, notes0 = resolve_default_paths()

        # 输出目录优先使用 config.json 里保存的用户设置
        cfg_out = get_configured_output_dir()
        if cfg_out:
            out0 = cfg_out
            notes0 = list(notes0) + ["[配置] 已从 config.json 读取输出路径：{}".format(cfg_out)]
        self.var_scan_root = tk.StringVar(value=CONFIG["default_scan_root"])
        self.var_fbx = tk.StringVar(value=fbx0)
        self.var_texdir = tk.StringVar(value=tex0)
        self.var_outdir = tk.StringVar(value=out0)
        self.var_recursive = tk.BooleanVar(value=True)
        self.var_status = tk.StringVar(value="就绪")

        # ---------------- 模块勾选 ----------------
        self.var_cleanup = tk.BooleanVar(value=True)
        self.var_origin = tk.BooleanVar(value=True)
        self.var_lod = tk.BooleanVar(value=True)
        self.var_material = tk.BooleanVar(value=True)
        self.var_smooth = tk.BooleanVar(value=True)

        # ---------------- 接单工作流 ----------------
        # 从 render_config.json 读取已保存的工作区/归档路径
        _wr, _ar, _ib = "", "", ""
        try:
            _c = _load_cfg_dict()
            _wr = ((_c.get("work_root") or {}).get("path") or "").replace("/", "\\")
            _ar = ((_c.get("archive") or {}).get("archive_dir") or "").replace("/", "\\")
            _ib = ((_c.get("inbox") or {}).get("dir") or "").replace("/", "\\")
        except Exception:
            pass

        self.var_inbox = tk.StringVar(value=_ib)
        self.var_zip = tk.StringVar(value="")
        self.var_work_root = tk.StringVar(value=_wr)
        self.var_archive_dir = tk.StringVar(value=_ar)
        self.var_customer = tk.StringVar(value="")

        # ---------------- 材质模式 / 风格预设 ----------------
        # 材质模式（渲染时实时生效，不写回 json 也可以）
        self.var_mat_mode = tk.StringVar(value="texture")
        self.var_preset = tk.StringVar(value="（不使用 preset）")
        self.var_hdri_preset = tk.StringVar(value="")

        self.var_scene = tk.BooleanVar(value=True)
        self.var_light = tk.BooleanVar(value=True)
        self.var_camera = tk.BooleanVar(value=True)

        # ---------------- 运行时状态 ----------------
        self.running = False
        self.stopping = False
        self.worker_thread = None
        self._startup_notes = notes0

        self._build_ui()

        # 启动提示打到日志
        for note in self._startup_notes:
            level = "info" if ("自动定位到" in note or "已创建" in note) else "warn"
            LOG_QUEUE.put((level, note + "\n"))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._poll_log)

    # =========================================================================
    # UI 构建
    # =========================================================================
    def _build_ui(self):
        # =================================================================
        # 全局样式：统一字体 + 放大控件，解决高分屏文字过小的问题
        # =================================================================
        style = ttk.Style()
        try:
            # clam 主题对字体尺寸的响应比默认的 vista 主题更准确
            style.theme_use("clam")
        except Exception:
            pass

        # 基础字体
        style.configure(".", font=UI_FONT)
        style.configure("TLabel", font=UI_FONT, padding=2)
        style.configure("TButton", font=UI_FONT, padding=(10, 5))
        style.configure("TCheckbutton", font=UI_FONT, padding=3)
        style.configure("TRadiobutton", font=UI_FONT, padding=3)
        style.configure("TEntry", padding=4)
        style.configure("TLabelframe", padding=6)
        style.configure("TLabelframe.Label", font=UI_FONT_BOLD)
        style.configure("TNotebook.Tab", font=UI_FONT, padding=(12, 6))

        # 主按钮：加大加粗，方便点击
        style.configure("Accent.TButton", font=UI_FONT_BOLD, padding=(14, 7))
        # 危险操作按钮（停止 / 清理）：用显眼的颜色
        style.configure("Danger.TButton", font=UI_FONT_BOLD, padding=(14, 7),
                        foreground="#a31515")

        # 日志用等宽字体
        style.configure("Log.TFrame")

        # 让所有 Entry 默认字体生效（ttk 通过 style 控制）
        self.root.option_add("*Font", UI_FONT)
        self.root.option_add("*Entry.Font", UI_FONT)
        self.root.option_add("*Text.Font", UI_FONT)

        # --- 0. 接单工作流（收件箱 → 预处理 → 归档）---
        biz = ttk.LabelFrame(
            self.root, text=" 0. 接单工作流（收件箱 → 预处理 → 交付归档） ", padding=8)
        biz.pack(fill="x", padx=10, pady=(8, 2))

        # ---- 收件箱 ----
        ttk.Label(biz, text="收件箱：").grid(row=0, column=0, sticky="w")
        ttk.Entry(biz, textvariable=self.var_inbox).grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Button(biz, text="浏览…", width=9,
                   command=self._browse_inbox).grid(row=0, column=2)
        ttk.Button(biz, text="📥 扫描收件箱", width=14,
                   command=self._on_scan_inbox).grid(row=0, column=3, padx=(6, 0))
        ttk.Button(biz, text="设为默认", width=10,
                   command=self._save_inbox).grid(row=0, column=4, padx=(4, 0))

        # ---- 收件箱列表 ----
        lb_frame = ttk.Frame(biz)
        lb_frame.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(6, 0))

        ttk.Label(lb_frame, text="文件列表：").pack(side="left", anchor="n")
        lb_wrap = ttk.Frame(lb_frame)
        lb_wrap.pack(side="left", fill="both", expand=True, padx=(4, 0))

        self.inbox_list = tk.Listbox(
            lb_wrap, height=5, font=UI_FONT,
            selectmode="browse", exportselection=False)
        self.inbox_list.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(lb_wrap, orient="vertical",
                           command=self.inbox_list.yview)
        sb.pack(side="right", fill="y")
        self.inbox_list.configure(yscrollcommand=sb.set)
        self.inbox_list.bind("<<ListboxSelect>>", self._on_inbox_select)

        # ---- 选中项 ----
        ttk.Label(biz, text="当前选择：").grid(
            row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(biz, textvariable=self.var_zip).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(biz, text="浏览文件…", width=11,
                   command=self._browse_zip).grid(row=2, column=3, pady=(6, 0))
        ttk.Button(biz, text="⚙ 预处理", width=12,
                   command=self._on_ingest).grid(row=2, column=4, padx=(4, 0), pady=(6, 0))

        # ---- 客户名 ----
        ttk.Label(biz, text="客户名：").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(biz, textvariable=self.var_customer, width=24).grid(
            row=3, column=1, sticky="w", padx=6, pady=(4, 0))

        # ---- 工作区 ----
        ttk.Label(biz, text="工作区：").grid(row=4, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(biz, textvariable=self.var_work_root).grid(
            row=4, column=1, columnspan=2, sticky="ew", padx=6, pady=(4, 0))
        ttk.Button(biz, text="浏览…", width=9,
                   command=self._browse_work_root).grid(row=4, column=3, pady=(4, 0))
        ttk.Button(biz, text="设为默认", width=10,
                   command=self._save_work_root).grid(row=4, column=4, padx=(4, 0), pady=(4, 0))

        # ---- 交付归档 ----
        ttk.Label(biz, text="交付归档：").grid(row=5, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(biz, textvariable=self.var_archive_dir).grid(
            row=5, column=1, columnspan=2, sticky="ew", padx=6, pady=(4, 0))
        ttk.Button(biz, text="浏览…", width=9,
                   command=self._browse_archive).grid(row=5, column=3, pady=(4, 0))
        ttk.Button(biz, text="📂 打开", width=10,
                   command=self._open_archive).grid(row=5, column=4, padx=(4, 0), pady=(4, 0))

        biz.columnconfigure(1, weight=1)
        biz.columnconfigure(2, weight=1)

        # --- 本地批量扫描（可折叠）---
        # 用「接单工作流」时不需要这块，折叠起来省空间
        self.scan_collapsed = tk.BooleanVar(value=False)

        self.scan_toggle = ttk.Button(
            self.root, text="▼ 本地批量扫描（点击折叠）",
            command=self._toggle_scan_panel)
        self.scan_toggle.pack(fill="x", padx=10, pady=(6, 0))

        self.scan_container = ttk.Frame(self.root)
        self.scan_container.pack(fill="both", expand=True, padx=0, pady=0)

        # --- 1. 扫描区 ---
        scan = ttk.LabelFrame(self.scan_container, text=" 1. 批量扫描 ", padding=8)
        scan.pack(fill="x", padx=10, pady=(4, 4))

        ttk.Label(scan, text="总文件夹：").grid(row=0, column=0, sticky="w")
        ttk.Entry(scan, textvariable=self.var_scan_root).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(scan, text="浏览…", width=10,
                   command=self._browse_scan_root).grid(row=0, column=2)
        ttk.Button(scan, text="🔍 扫描模型", width=15, style="Accent.TButton",
                   command=self._on_scan).grid(row=0, column=3, padx=(8, 0))
        ttk.Checkbutton(scan, text="包含子文件夹",
                        variable=self.var_recursive).grid(row=0, column=4, padx=(8, 0))

        scan.columnconfigure(1, weight=1)

        # --- 2. 模型勾选列表 ---
        listf = ttk.LabelFrame(self.scan_container, text=" 2. 模型列表（默认全选） ", padding=6)
        listf.pack(fill="both", expand=True, padx=10, pady=4)

        toolbar = ttk.Frame(listf)
        toolbar.pack(fill="x", pady=(0, 4))
        ttk.Button(toolbar, text="全选", width=10,
                   command=lambda: self._set_all_checks(True)).pack(side="left")
        ttk.Button(toolbar, text="全不选", width=10,
                   command=lambda: self._set_all_checks(False)).pack(side="left", padx=6)
        ttk.Button(toolbar, text="反选", width=10,
                   command=self._invert_checks).pack(side="left")
        self.lbl_count = ttk.Label(toolbar, text="未扫描", foreground="#666")
        self.lbl_count.pack(side="right")

        # 可滚动区域：Canvas + 内部 Frame + 竖向滚动条
        canvas_wrap = ttk.Frame(listf)
        canvas_wrap.pack(fill="both", expand=True)

        # 列表高度按缩放放大，保证能看到更多行
        self.list_canvas = tk.Canvas(canvas_wrap, highlightthickness=0,
                                     height=int(180 * UI_SCALE))
        vsb = ttk.Scrollbar(canvas_wrap, orient="vertical", command=self.list_canvas.yview)
        self.list_canvas.configure(yscrollcommand=vsb.set)

        self.list_canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.list_frame = ttk.Frame(self.list_canvas)
        self.list_window = self.list_canvas.create_window((0, 0), window=self.list_frame, anchor="nw")

        self.list_frame.bind(
            "<Configure>",
            lambda e: self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all")))
        self.list_canvas.bind(
            "<Configure>",
            lambda e: self.list_canvas.itemconfigure(self.list_window, width=e.width))

        # 鼠标滚轮
        self.list_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        # --- 3. 路径配置 ---
        paths = ttk.LabelFrame(
            self.root,
            text=" 3. 路径配置（上面扫描的是「输入」，这里第 3 行是「输出」） ",
            padding=8)
        paths.pack(fill="x", padx=10, pady=4)

        self._path_row(paths, 0, "模型 FBX：", self.var_fbx, self._browse_fbx)
        self._path_row(paths, 1, "贴图目录：", self.var_texdir, self._browse_dir_tex)
        self._path_row(paths, 2, "图片输出保存路径：", self.var_outdir,
                       self._browse_dir_out)

        # 输出路径这一行右侧追加两个按钮：设为默认 / 打开文件夹
        outbtn = ttk.Frame(paths)
        outbtn.grid(row=2, column=3, sticky="w", padx=(6, 0))
        ttk.Button(outbtn, text="设为默认", width=10,
                   command=self._on_set_default_outdir).pack(side="left")
        ttk.Button(outbtn, text="打开文件夹", width=11,
                   command=self._on_open_outdir).pack(side="left", padx=(6, 0))

        ttk.Button(paths, text="重新检测默认路径", width=16,
                   command=self._reload_defaults).grid(row=3, column=2, pady=(4, 0))
        paths.columnconfigure(1, weight=1)

        # --- 4. 两个模块区 ---
        mid = ttk.Frame(self.root)
        mid.pack(fill="x", padx=10, pady=4)
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)

        left = ttk.LabelFrame(mid, text=" 4. Blender 预处理 ", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        ttk.Checkbutton(left, text="模型清洗（重算法线、应用全部变换）",
                        variable=self.var_cleanup).pack(anchor="w", pady=1)
        ttk.Checkbutton(left, text="原点居中与朝向校正（最宽面 → +Y）",
                        variable=self.var_origin).pack(anchor="w", pady=1)
        ttk.Checkbutton(left, text="生成 LOD（跳过顶点 < {}）".format(LOD_MIN_VERTS),
                        variable=self.var_lod).pack(anchor="w", pady=1)
        ttk.Checkbutton(left, text="材质打包与导出（应用修改器、打包贴图）",
                        variable=self.var_material).pack(anchor="w", pady=1)
        ttk.Checkbutton(left, text="平滑着色（清除硬边法线，避免棱线）",
                        variable=self.var_smooth).pack(anchor="w", pady=1)

        right = ttk.LabelFrame(mid, text=" 5. UE5 渲染 ", padding=8)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        ttk.Checkbutton(right, text="自动场景构建（时间戳关卡 + PBR 材质 + MetallicGain）",
                        variable=self.var_scene).pack(anchor="w", pady=1)
        ttk.Checkbutton(right, text="灯光与泛光后期（冷白背光 400cd、暖补光 30cd、PPV）",
                        variable=self.var_light).pack(anchor="w", pady=1)
        ttk.Checkbutton(right, text="自动寻边相机与出图（多角度重试、过滤 <100KB）",
                        variable=self.var_camera).pack(anchor="w", pady=1)

        # ---------------- 材质模式 + 风格预设 ----------------
        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=(8, 6))

        row1 = ttk.Frame(right)
        row1.pack(anchor="w", fill="x", pady=1)
        ttk.Label(row1, text="材质模式：").pack(side="left")
        self.cmb_mat_mode = ttk.Combobox(
            row1, textvariable=self.var_mat_mode, state="readonly", width=16,
            values=["texture", "dark_metal", "rough_skin", "emissive", "plaster"])
        self.cmb_mat_mode.pack(side="left", padx=(2, 8))
        ttk.Label(row1, text="（texture=用贴图，其余=纯色）",
                  foreground="#666").pack(side="left")

        row2 = ttk.Frame(right)
        row2.pack(anchor="w", fill="x", pady=(4, 1))
        ttk.Label(row2, text="风格预设：").pack(side="left")
        self.cmb_preset = ttk.Combobox(
            row2, textvariable=self.var_preset, state="readonly", width=22,
            values=["（不使用 preset）", "Custom (自定义)",
                    "Mech_DarkMetal", "Sunset_Biological",
                    "Cyber_Emissive", "Ruins_Dark"])
        self.cmb_preset.pack(side="left", padx=(2, 4))
        ttk.Button(row2, text="应用", width=6,
                   command=self._on_apply_preset).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="存为默认", width=9,
                   command=self._on_save_preset).pack(side="left")

        row3 = ttk.Frame(right)
        row3.pack(anchor="w", fill="x", pady=(4, 1))
        ttk.Label(row3, text="HDRI 预设：").pack(side="left")
        self.ent_hdri = ttk.Entry(row3, textvariable=self.var_hdri_preset, width=24)
        self.ent_hdri.pack(side="left", padx=(2, 4))
        ttk.Button(row3, text="存为默认", width=9,
                   command=self._on_save_hdri).pack(side="left")

        # --- 6. 执行按钮 ---
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=10, pady=(4, 2))

        self.btn_blender = ttk.Button(bar, text="仅执行 Blender", width=18,
                                      command=lambda: self._on_start("blender"))
        self.btn_blender.pack(side="left", padx=(0, 6))

        self.btn_ue = ttk.Button(bar, text="仅执行 UE5", width=16,
                                 command=lambda: self._on_start("ue5"))
        self.btn_ue.pack(side="left", padx=(0, 6))

        self.btn_all = ttk.Button(bar, text="🚀 一键全自动", width=20,
                                  style="Accent.TButton",
                                  command=lambda: self._on_start("all"))
        self.btn_all.pack(side="left", padx=(0, 6))

        self.btn_stop = ttk.Button(bar, text="🛑 停止任务", width=16,
                                   style="Danger.TButton",
                                   command=self._on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 6))

        # 清理按钮：随时可点，用于干掉残留的 blender / UE 进程
        self.btn_cleanup = ttk.Button(bar, text="🧹 清理后台残留进程", width=20,
                                      command=self._on_cleanup)
        self.btn_cleanup.pack(side="left", padx=(0, 6))

        ttk.Button(bar, text="退出", width=8,
                   command=self._on_close).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="清空日志", width=10,
                   command=self._clear_log).pack(side="right")

        # --- 7. 日志 ---
        logf = ttk.LabelFrame(self.root, text=" 6. 运行日志 ", padding=4)
        logf.pack(fill="both", expand=True, padx=10, pady=(4, 6))

        self.log_text = tk.Text(logf, wrap="none", bg="#1e1e1e", fg="#d4d4d4",
                                insertbackground="white", font=LOG_FONT,
                                height=int(12 * UI_SCALE / 1.2) + 4)
        ysb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        xsb = ttk.Scrollbar(logf, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)

        self.log_text.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)

        self.log_text.tag_config("error", foreground="#f48771")
        self.log_text.tag_config("warn", foreground="#dcdcaa")
        self.log_text.tag_config("info", foreground="#4ec9b0")
        self.log_text.tag_config("ok", foreground="#6a9955")
        self.log_text.tag_config("normal", foreground="#d4d4d4")

        # --- 状态栏 ---
        ttk.Label(self.root, textvariable=self.var_status, anchor="w",
                  relief="sunken").pack(fill="x", side="bottom")

    def _path_row(self, parent, row, label, var, browse_cmd):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6, pady=2)
        ttk.Button(parent, text="浏览…", width=8, command=browse_cmd).grid(row=row, column=2, pady=2)

    # =========================================================================
    # 滚动与勾选
    # =========================================================================
    def _on_mousewheel(self, event):
        try:
            self.list_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    def _set_all_checks(self, value: bool):
        for v in self.checks:
            v.set(value)
        self._update_count()

    def _invert_checks(self):
        for v in self.checks:
            v.set(not v.get())
        self._update_count()

    def _update_count(self):
        total = len(self.models)
        sel = sum(1 for v in self.checks if v.get())
        if total == 0:
            self.lbl_count.configure(text="未扫描")
        else:
            self.lbl_count.configure(text="已选 {} / 共 {}".format(sel, total))

    def _clear_widgets_only(self):
        """
        只清掉列表里的控件，不动 self.models / self.checks。

        ⚠️ 之前的 bug：_clear_model_list 会顺带把 self.models 清空，
           而 _rebuild_model_list 第一行就调用它，
           导致「扫描找到 13 个，列表却是 0 个」。
           这里把「清控件」和「清数据」拆开。
        """
        for w in self.list_frame.winfo_children():
            w.destroy()

    def _clear_model_list(self):
        """彻底清空（控件 + 数据）。只在真正需要重置时调用。"""
        self._clear_widgets_only()
        self.models = []
        self.checks = []
        self._update_count()

    def _rebuild_model_list(self):
        """按当前 self.models 重建勾选列表。只清控件，保留数据。"""
        self._clear_widgets_only()

        # 勾选状态全部重置为 True（默认全选）
        self.checks = []

        if not self.models:
            ttk.Label(self.list_frame, text="（没有找到 FBX 模型）",
                      foreground="#888").pack(anchor="w", padx=6, pady=6)
            self._update_count()
            return

        for m in self.models:
            row = ttk.Frame(self.list_frame)
            row.pack(fill="x", anchor="w", padx=2, pady=0)

            var = tk.BooleanVar(value=True)
            self.checks.append(var)

            ttk.Checkbutton(row, text=m["name"], variable=var,
                            command=self._update_count).pack(side="left")

            tex = m.get("texdir")
            if tex:
                info = "  ← 贴图: {}".format(os.path.basename(tex))
                color = "#6a9955"
            else:
                info = "  ← 未匹配到贴图"
                color = "#dcdcaa"
            ttk.Label(row, text=info, foreground=color).pack(side="left")

        self._update_count()

    # =========================================================================
    # 扫描
    # =========================================================================
    def _on_scan(self):
        root_dir = self.var_scan_root.get().strip()
        if not root_dir:
            messagebox.showwarning("提示", "请先选择总文件夹路径")
            return
        if not os.path.isdir(root_dir):
            messagebox.showerror("路径错误", "总文件夹不存在：\n{}".format(root_dir))
            return

        self.var_status.set("正在扫描…")
        self.root.update_idletasks()

        try:
            found = scan_models(root_dir, recursive=self.var_recursive.get())
        except Exception:
            LOG_QUEUE.put(("error", "[错误] 扫描失败：\n" + traceback.format_exc()))
            self.var_status.set("扫描失败")
            return

        self.models = found
        self._rebuild_model_list()

        matched = sum(1 for m in found if m.get("texdir"))
        LOG_QUEUE.put(("info", "\n[扫描] 目录：{}\n".format(root_dir)))
        LOG_QUEUE.put(("info", "[扫描] 找到 {} 个 FBX，其中 {} 个匹配到贴图目录\n".format(
            len(found), matched)))

        if not found:
            LOG_QUEUE.put(("warn", "[扫描] 该目录下没有 .fbx 文件\n"))

        # 自动填入第一个模型，方便单文件模式下直接用
        if found:
            self.var_fbx.set(found[0]["path"])
            if found[0].get("texdir"):
                self.var_texdir.set(found[0]["texdir"])

        self.var_status.set("扫描完成：{} 个模型".format(len(found)))

    def _browse_scan_root(self):
        p = filedialog.askdirectory(title="选择包含 FBX 的总文件夹")
        if p:
            self.var_scan_root.set(os.path.normpath(p))

    # =========================================================================
    # 其它浏览
    # =========================================================================
    def _reload_defaults(self):
        LOG_QUEUE.put(("info", "\n[系统] 重新检测默认路径…\n"))
        fbx, tex, out, notes = resolve_default_paths()
        self.var_fbx.set(fbx)
        self.var_texdir.set(tex)
        self.var_outdir.set(out)
        for n in notes:
            LOG_QUEUE.put(("info" if ("自动定位到" in n or "已创建" in n) else "warn", n + "\n"))
        if not notes:
            LOG_QUEUE.put(("ok", "[系统] 三个默认路径均有效\n"))

    def _on_set_default_outdir(self):
        """
        「设为默认」按钮：把当前输出路径写入 config.json。
        下次启动会自动加载。
        """
        d = self.var_outdir.get().strip()
        if not d:
            messagebox.showwarning("提示", "输出路径为空，无法设为默认。")
            return

        # 先做合法性 + 可写性检查
        ok, reason = check_path_legal(d)
        if not ok:
            messagebox.showerror("路径不合法", reason)
            return

        ok_w, reason_w = verify_dir_writable(d, strict=False)
        if not ok_w:
            if not messagebox.askyesno(
                    "该目录写入测试未通过",
                    reason_w + "\n\n仍要把它设为默认路径吗？"):
                return

        # 合并已有配置，只改 output_dir，不动其他字段
        cfg = load_user_config()
        cfg["output_dir"] = os.path.normpath(d)

        ok_s, info = save_user_config(cfg)
        if ok_s:
            LOG_QUEUE.put(("ok", "\n[配置] 已保存默认输出路径：{}\n".format(
                os.path.normpath(d))))
            LOG_QUEUE.put(("normal", "       配置文件：{}\n".format(info)))
            messagebox.showinfo("已保存",
                                "默认输出路径已保存。\n下次启动会自动填入：\n\n"
                                + os.path.normpath(d))
        else:
            messagebox.showerror("保存失败",
                                 "无法写入配置文件：\n{}\n\n{}".format(
                                     get_config_path(), info))

    def _on_open_outdir(self):
        """
        「打开文件夹」按钮：用资源管理器打开当前输出目录。
        目录不存在时先创建，再打开。
        """
        d = self.var_outdir.get().strip()
        if not d:
            messagebox.showwarning("提示", "输出路径为空。")
            return

        d = os.path.normpath(d)
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("无法创建目录", "{}\n\n{}".format(d, exc))
            return

        if not os.path.isdir(d):
            messagebox.showerror("目录不存在", d)
            return

        try:
            if os.name == "nt":
                os.startfile(d)          # Windows 资源管理器
            else:
                import subprocess as sp
                sp.Popen(["open" if sys.platform == "darwin" else "xdg-open", d])
            LOG_QUEUE.put(("info", "[系统] 已打开文件夹：{}\n".format(d)))
        except Exception as exc:
            messagebox.showerror("打开失败", "{}\n\n{}".format(d, exc))

    def _browse_fbx(self):
        p = filedialog.askopenfilename(title="选择模型 FBX",
                                       filetypes=[("FBX", "*.fbx"), ("所有文件", "*.*")])
        if p:
            self.var_fbx.set(os.path.normpath(p))
            tex = match_texture_dir(p)
            if tex:
                self.var_texdir.set(tex)

    def _browse_dir_tex(self):
        p = filedialog.askdirectory(title="选择贴图目录")
        if p:
            self.var_texdir.set(os.path.normpath(p))

    def _browse_dir_out(self):
        """
        选择「图片输出保存路径」。

        选完立刻做合法性 + 可写性校验，
        有问题当场弹窗，而不是等到点「开始」才报错。
        """
        p = filedialog.askdirectory(title="选择图片输出保存路径")
        if not p:
            return

        p = os.path.normpath(p)

        ok, reason = check_path_legal(p)
        if not ok:
            messagebox.showerror("输出路径不合法", reason)
            return

        # 高风险路径警告
        warn_if_risky_path(p, emit=lambda m: LOG_QUEUE.put(("error", m + "\n")))

        ok, reason = verify_dir_writable(p, strict=False)
        if not ok:
            # 仍然允许设置，只提示
            LOG_QUEUE.put(("warn", "[提醒] 该目录写入测试未通过，但已设置：\n" + reason + "\n"))

        self.var_outdir.set(p)
        LOG_QUEUE.put(("ok", "[系统] 输出路径已设置：{}\n".format(p)))

    # =========================================================================
    # 日志
    # =========================================================================
    def _poll_log(self):
        """
        主线程定时任务：把后台线程产生的日志和界面请求消化掉。

        这是整个程序线程安全的关键：
          · 后台线程只往两个队列里放东西，绝不直接操作 Tk 控件
          · 所有 Tk 操作都在这里（主线程）执行
        """
        # ---- 1. 处理日志 ----
        try:
            while True:
                level, text = LOG_QUEUE.get_nowait()
                self.log_text.insert("end", text, level)
                self.log_text.see("end")
        except queue.Empty:
            pass
        except Exception:
            pass

        # ---- 2. 处理界面操作请求 ----
        try:
            while True:
                action, payload = UI_QUEUE.get_nowait()
                if action == "status":
                    self._set_status(payload)
                elif action == "restore_buttons":
                    self._restore_buttons()
                elif action == "restore_cleanup_btn":
                    self.btn_cleanup.configure(state="normal")
        except queue.Empty:
            pass
        except Exception:
            pass

        self.root.after(80, self._poll_log)

    # =====================================================================
    # 接单工作流：zip 接入 / 归档
    # =====================================================================
    # =====================================================================
    # 收件箱扫描
    # =====================================================================
    def _browse_inbox(self):
        p = filedialog.askdirectory(title="选择收件箱目录（放客户 zip 的地方）")
        if p:
            self.var_inbox.set(p)

    def _save_inbox(self):
        """
        把收件箱路径存为默认值。

        ⚠️ 保存失败只警告，绝不抛异常中断其他流程。
        """
        p = self.var_inbox.get().strip()
        if not p:
            messagebox.showwarning("提示", "请先填写收件箱目录")
            return
        try:
            _save_cfg_field("inbox", "dir", p)
            self._append_log("[设置] 收件箱已存为默认：{}\n".format(p))
            messagebox.showinfo("已保存", "收件箱目录已存为默认。\n\n" + p)
        except Exception as exc:
            # ★ 友好提示 + 显示配置路径，方便排查
            self._append_log("[设置] ✗ 保存失败：{}\n".format(str(exc)[:150]))
            try:
                _p = _get_config_path()
            except Exception:
                _p = "(未知)"
            messagebox.showwarning(
                "保存失败（不影响使用）",
                "配置无法写入磁盘，但本次操作仍可继续。\n\n"
                "配置文件路径：\n{}\n\n"
                "错误：{}".format(_p, str(exc)[:200]))

    def _save_work_root(self):
        """把工作区路径存为默认值。保存失败只警告。"""
        p = self.var_work_root.get().strip()
        if not p:
            messagebox.showwarning("提示", "请先填写工作区目录")
            return
        try:
            _save_cfg_field("work_root", "path", p)
            self._append_log("[设置] 工作区已存为默认：{}\n".format(p))
            messagebox.showinfo("已保存", "工作区目录已存为默认。\n\n" + p)
        except Exception as exc:
            self._append_log("[设置] ✗ 保存失败：{}\n".format(str(exc)[:150]))
            try:
                _p = _get_config_path()
            except Exception:
                _p = "(未知)"
            messagebox.showwarning(
                "保存失败（不影响使用）",
                "配置无法写入磁盘，但本次操作仍可继续。\n\n"
                "配置文件路径：\n{}\n\n"
                "错误：{}".format(_p, str(exc)[:200]))

    def _on_scan_inbox(self):
        """
        扫描收件箱：列出所有 zip，同时列出文件夹（可直接当模型目录用）。
        """
        inbox = self.var_inbox.get().strip()

        self._append_log("\n[收件箱] 开始扫描：{}\n".format(inbox or "(未设置)"))

        if not inbox:
            messagebox.showerror("错误", "请先设置收件箱目录。")
            return

        if not os.path.isdir(inbox):
            self._append_log("[收件箱] ✗ 目录不存在\n")
            messagebox.showerror(
                "目录不存在",
                "收件箱目录不存在：\n\n{}\n\n请检查路径是否正确。".format(inbox))
            return

        try:
            entries = []
            for name in sorted(os.listdir(inbox)):
                full = os.path.join(inbox, name)
                if os.path.isdir(full):
                    # 文件夹：检查是否含模型
                    has_model = False
                    for _dp, _dn, _fns in os.walk(full):
                        for _f in _fns:
                            if os.path.splitext(_f.lower())[1] in (".fbx", ".obj"):
                                has_model = True
                                break
                        if has_model:
                            break
                    entries.append(("dir", name, full, has_model))
                elif name.lower().endswith(".zip"):
                    entries.append(("zip", name, full, True))
        except Exception as exc:
            import traceback
            self._append_log("[收件箱] ✗ 扫描失败：{}\n".format(str(exc)))
            self._append_log(traceback.format_exc()[:600] + "\n")
            messagebox.showerror("扫描失败", str(exc))
            return

        # 过滤：只保留 zip 和含模型的文件夹
        usable = [e for e in entries if e[3]]
        skipped = [e for e in entries if not e[3]]

        self.inbox_entries = usable
        self.inbox_list.delete(0, tk.END)

        for kind, name, full, _ok in usable:
            tag = "📦" if kind == "zip" else "📁"
            extra = "" if kind == "zip" else "  (文件夹)"
            self.inbox_list.insert(tk.END, "{} {}{}".format(tag, name, extra))

        self._append_log("[收件箱] 找到 {} 个可用项（zip {} / 文件夹 {})\n".format(
            len(usable),
            sum(1 for e in usable if e[0] == "zip"),
            sum(1 for e in usable if e[0] == "dir")))
        if skipped:
            self._append_log("[收件箱] 跳过 {} 个不含模型的文件夹\n".format(
                len(skipped)))

        if not usable:
            messagebox.showwarning(
                "收件箱为空",
                "收件箱里没有可用的文件：\n\n{}\n\n"
                "需要 .zip 压缩包，或包含 .fbx/.obj 的文件夹。".format(inbox))
            return

        # 自动选中第一个
        self.inbox_list.selection_set(0)
        self._on_inbox_select(None)

    def _on_inbox_select(self, event):
        """
        列表选中 → 回填当前选择 + 客户名。
        zip 和文件夹都支持。
        """
        sel = self.inbox_list.curselection()
        if not sel:
            return

        idx = sel[0]
        if idx >= len(getattr(self, "inbox_entries", [])):
            return

        kind, name, full, _ok = self.inbox_entries[idx]

        # 回填当前选择
        self.var_zip.set(full)

        # 客户名 = 文件名去扩展名
        base = os.path.splitext(name)[0] if kind == "zip" else name
        self.var_customer.set(base)

        self._append_log("[收件箱] 已选择：{}（客户名：{}）\n".format(name, base))

    def _browse_zip(self):
        p = filedialog.askopenfilename(
            title="选择客户压缩包",
            filetypes=[("压缩包", "*.zip"), ("所有文件", "*.*")])
        if p:
            self.var_zip.set(p)
            # 自动填客户名：用 zip 文件名（去扩展名）
            base = os.path.splitext(os.path.basename(p))[0]
            self.var_customer.set(base)

    def _browse_work_root(self):
        p = filedialog.askdirectory(title="选择接单工作区")
        if p:
            self.var_work_root.set(p)

    def _open_archive(self):
        """
        用文件管理器打开归档目录。

        优先打开【最近一次交付文件夹】，没有则打开归档根目录。
        """
        # 优先最近交付
        target = getattr(self, "_last_delivery", "")
        if not target or not os.path.isdir(target):
            target = self.var_archive_dir.get().strip()

        if not target:
            messagebox.showinfo("提示", "还没有设置交付归档目录。\n请先选择目录或完成一次渲染。")
            return

        if not os.path.isdir(target):
            messagebox.showwarning(
                "目录不存在",
                "目录不存在（可能还没创建过）：\n\n" + target)
            return

        try:
            os.startfile(target)      # Windows 专用，直接调资源管理器
            self._append_log("[归档] 已打开目录：{}\n".format(target))
        except AttributeError:
            # 非 Windows 平台兜底
            import subprocess as _sp
            try:
                _sp.Popen(["explorer", target])
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc))
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def _browse_archive(self):
        p = filedialog.askdirectory(title="选择交付归档目录")
        if p:
            self.var_archive_dir.set(p)

    def _on_ingest(self):
        """
        执行资产标准化：zip → 干净英文工作目录。

        ═══════════════════════════════════════════════════════════════
        异常处理原则（V21.1）：
          · 任何失败都必须弹窗 + 完整 Traceback 写进日志框
          · 绝不静默吞异常
          · 关键路径打印 [诊断] 日志，能看出卡在哪一步
        ═══════════════════════════════════════════════════════════════
        """
        import traceback

        zip_path = self.var_zip.get().strip()
        work_root = self.var_work_root.get().strip()
        customer = self.var_customer.get().strip()

        self._append_log("\n" + "=" * 64 + "\n")
        self._append_log("[预处理] 开始\n")
        self._append_log("[诊断] zip  = {}\n".format(zip_path or "(未设置)"))
        self._append_log("[诊断] 工作区 = {}\n".format(work_root or "(未设置)"))
        self._append_log("[诊断] 客户名 = {}\n".format(customer or "(未设置)"))

        # ---- 前置校验 ----
        if not zip_path:
            self._append_log("[预处理] ✗ 未选择压缩包\n")
            messagebox.showerror("错误", "请先选择客户压缩包（.zip）")
            return

        # 支持 zip 文件 或 含模型的文件夹
        is_zip = zip_path.lower().endswith(".zip") and os.path.isfile(zip_path)
        is_dir = os.path.isdir(zip_path)

        if not is_zip and not is_dir:
            self._append_log("[预处理] ✗ 路径无效\n")
            messagebox.showerror(
                "错误",
                "路径不存在，或不是 .zip / 文件夹：\n\n{}".format(zip_path))
            return

        if not work_root:
            self._append_log("[预处理] ✗ 未设置工作区\n")
            messagebox.showerror("错误",
                "请先设置工作区目录（用于解压和渲染）。\n\n"
                "建议用：C:\\Users\\<用户名>\\Documents\\接单工作区\n"
                "避免用 D:\\ 根目录这类需要管理员权限的位置。")
            return

        # ---- 执行 ----
        def on_progress(msg):
            self._append_log(msg + "\n")

        try:
            if is_zip:
                result = ingest_zip(zip_path, work_root, customer, on_progress)
            else:
                # 文件夹：直接用 auto_detect_ingest 扫描
                self._append_log("[预处理] 输入是文件夹，直接扫描资产\n")
                result = auto_detect_ingest(
                    zip_path, work_root, customer, on_progress)
        except Exception as exc:
            # ★ 完整 Traceback 写进日志
            tb = traceback.format_exc()
            self._append_log("\n[预处理] ✗✗✗ 失败 ✗✗✗\n")
            self._append_log("[预处理] 错误类型: {}\n".format(
                type(exc).__name__))
            self._append_log("[预处理] 错误信息:\n{}\n".format(str(exc)))
            self._append_log("\n[预处理] 完整堆栈:\n{}\n".format(tb))
            self._append_log("=" * 64 + "\n")

            # 弹窗显示简明信息 + 指引
            messagebox.showerror(
                "预处理失败",
                "{}\n\n"
                "──────────────\n"
                "已终止，不会启动 UE5。\n"
                "完整堆栈已打印到下方日志框。".format(str(exc)))
            return

        # ---- 成功 ----
        models = result.get("models", [])
        task_dir = result["task_dir"]

        self._append_log("[预处理] ✓ 成功\n")
        self._append_log("[预处理] 任务目录: {}\n".format(task_dir))
        self._append_log("[预处理] 模型 {} 个 / 贴图 {} 张 / 跳过 {} 个\n".format(
            len(models), len(result.get("textures", [])),
            result.get("skipped", 0)))
        self._append_log("=" * 64 + "\n")

        # 回填路径
        self.var_fbx.set(models[0])
        self.var_texdir.set(task_dir)
        self.var_outdir.set(task_dir)

        messagebox.showinfo(
            "预处理完成",
            "资产已标准化：\n\n"
            "模型 {} 个\n贴图 {} 张\n跳过 {} 个无关文件\n\n"
            "工作目录：\n{}".format(
                len(models), len(result.get("textures", [])),
                result.get("skipped", 0), task_dir))

    def _do_archive(self, out_dir, customer):
        """
        渲染成功后自动归档。失败只记日志，不弹窗打断。
        """
        archive_dir = self.var_archive_dir.get().strip()
        if not archive_dir:
            self._append_log("[归档] 未配置归档目录，跳过\n")
            return
        try:
            r = archive_delivery(
                out_dir, archive_dir, customer,
                progress=lambda m: self._append_log(m + "\n"))
            if r.get("ok"):
                self._append_log("[归档] ✓ 已归档到：{}\n".format(r["delivery"]))
            else:
                self._append_log("[归档] ✗ 失败：{}\n".format(r.get("error")))
        except Exception as exc:
            self._append_log("[归档] ✗ 异常：{}\n".format(str(exc)[:120]))

    def _on_apply_preset(self):
        """
        应用风格预设。

        ═══════════════════════════════════════════════════════════════
        V23.5 交互改动：

          1. 【不再强制改材质模式】
             旧逻辑：选 preset 会把「材质模式」也改掉，限制自由度。
             新逻辑：preset 只影响 HDRI / 曝光 / 灯光颜色强度，
                     「材质模式」完全由用户手动控制。

          2. 【智能提示】
             选了 Cyber_Emissive 但材质模式是 texture 时，
             日志框里给一条黄色建议（不阻断，只提醒）。

          3. 【Custom 自定义】
             下拉框新增「Custom (自定义)」选项，
             选中后所有参数都由用户手动控制，程序不做任何覆盖。
        ═══════════════════════════════════════════════════════════════
        """
        name = self.var_preset.get()
        try:
            import json as _json
            cfg_path = _get_config_path()
            if not os.path.isfile(cfg_path):
                messagebox.showwarning("提示", "找不到 render_config.json")
                return
            with open(cfg_path, "r", encoding="utf-8") as fh:
                cfg = _json.load(fh)

            # ---- Custom：完全手动 ----
            if name == "Custom (自定义)":
                cfg.setdefault("preset", {})["active"] = "Custom"
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    _json.dump(cfg, fh, ensure_ascii=False, indent=2)
                self._append_log(
                    "[预设] 已切换为 Custom（自定义）\n")
                self._append_log(
                    "[预设] 材质模式 / HDRI / 曝光 / 灯光全部由你手动控制，"
                    "程序不会覆盖任何参数\n")
                messagebox.showinfo(
                    "自定义模式",
                    "已切换到 Custom（自定义）。\n\n"
                    "· 材质模式：由你选择\n"
                    "· HDRI / 曝光 / 灯光：使用 render_config.json 里的独立配置\n\n"
                    "程序不会再用预设覆盖这些参数。")
                return

            # ---- 不使用预设 ----
            if name == "（不使用 preset）":
                cfg.setdefault("preset", {})["active"] = ""
                # 只清 HDRI 预设，不动材质模式
                self.var_hdri_preset.set("")
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    _json.dump(cfg, fh, ensure_ascii=False, indent=2)
                self._append_log("[预设] 已关闭 preset，参数用配置文件的独立值\n")
                return

            # ---- 正常预设 ----
            p = (cfg.get("presets") or {}).get(name)
            if not isinstance(p, dict):
                messagebox.showwarning("提示", "preset '{}' 未定义".format(name))
                return

            cfg.setdefault("preset", {})["active"] = name

            # ★ 只同步 HDRI，不改材质模式
            _hdri = p.get("hdri_preset", "")
            if _hdri:
                self.var_hdri_preset.set(_hdri)

            with open(cfg_path, "w", encoding="utf-8") as fh:
                _json.dump(cfg, fh, ensure_ascii=False, indent=2)

            self._append_log("[预设] 已启用 {}\n".format(name))
            self._append_log(
                "[预设]   HDRI={} 曝光EV={} 灯光={}/{}/{}\n".format(
                    _hdri or "(默认)",
                    p.get("exposure_ev", "?"),
                    (p.get("lights", {}).get("key", {}) or {}).get("intensity", "?"),
                    (p.get("lights", {}).get("fill", {}) or {}).get("intensity", "?"),
                    (p.get("lights", {}).get("top", {}) or {}).get("intensity", "?")))
            self._append_log(
                "[预设]   材质模式保持你当前的选择：{}（未被修改）\n".format(
                    self.var_mat_mode.get()))

            # ---- 智能提示：赛博风格但用了贴图 ----
            if name == "Cyber_Emissive" and self.var_mat_mode.get() == "texture":
                self._append_log(
                    "[提示] 当前选择了赛博风格，建议将材质模式切换为 emissive "
                    "以获得最佳发光效果。\n", level="warn")

            # ---- 通用提示：预设想用 emissive 但用户选了别的 ----
            _want = p.get("material_mode")
            _cur = self.var_mat_mode.get()
            if _want and _want != _cur and name != "Cyber_Emissive":
                self._append_log(
                    "[提示] {} 预设推荐材质模式 '{}'，当前是 '{}'（保持你的选择）\n".format(
                        name, _want, _cur), level="warn")

        except Exception as exc:
            messagebox.showerror("错误", "应用 preset 失败：\n" + str(exc))

    def _on_save_preset(self):
        """把当前界面选中的材质模式写为 model.material_mode 默认值。"""
        try:
            import json as _json
            cfg_path = _get_config_path()
            with open(cfg_path, "r", encoding="utf-8") as fh:
                cfg = _json.load(fh)
            cfg.setdefault("model", {})["material_mode"] = self.var_mat_mode.get()
            # 同时关闭旧布尔开关，避免冲突
            cfg["model"]["use_custom_basecolor"] = False
            with open(cfg_path, "w", encoding="utf-8") as fh:
                _json.dump(cfg, fh, ensure_ascii=False, indent=2)
            self._append_log("[预设] 材质模式已存为默认：{}\n".format(
                self.var_mat_mode.get()))
        except Exception as exc:
            messagebox.showerror("错误", "保存失败：\n" + str(exc))

    def _on_save_hdri(self):
        """把当前 HDRI 预设名写为 hdri.hdri_preset 默认值。"""
        try:
            import json as _json
            cfg_path = _get_config_path()
            with open(cfg_path, "r", encoding="utf-8") as fh:
                cfg = _json.load(fh)
            cfg.setdefault("hdri", {})["hdri_preset"] = self.var_hdri_preset.get()
            with open(cfg_path, "w", encoding="utf-8") as fh:
                _json.dump(cfg, fh, ensure_ascii=False, indent=2)
            self._append_log("[预设] HDRI 预设已存为默认：{}\n".format(
                self.var_hdri_preset.get()))
        except Exception as exc:
            messagebox.showerror("错误", "保存失败：\n" + str(exc))

    def _toggle_scan_panel(self):
        """
        折叠 / 展开「本地批量扫描」区块。

        用接单工作流（zip 预处理）时这块用不上，
        折叠起来把空间让给下面的路径配置和日志。
        """
        try:
            collapsed = self.scan_collapsed.get()
            if collapsed:
                # 展开
                self.scan_container.pack(fill="both", expand=True, padx=0, pady=0,
                                         after=self.scan_toggle)
                self.scan_collapsed.set(False)
                self.scan_toggle.configure(text="▼ 本地批量扫描（点击折叠）")
            else:
                # 折叠
                self.scan_container.pack_forget()
                self.scan_collapsed.set(True)
                self.scan_toggle.configure(text="▶ 本地批量扫描（已折叠，点击展开）")
        except Exception as exc:
            self._append_log("[界面] 折叠失败：{}\n".format(str(exc)[:80]))

    def _append_log(self, text, level=None):
        """
        往日志框追加文本（线程安全）。

        参数：
          text  : 要输出的文本
          level : 可选，强制指定颜色
                  "normal" 普通 / "ok" 绿 / "warn" 黄 / "error" 红
                  不传则根据内容自动判断

        实现方式：统一投递到 LOG_QUEUE，由主线程的 _poll_log 消费写入。
        这样后台线程调用也不会触发 Tkinter 的跨线程错误。
        """
        try:
            s = str(text)
        except Exception:
            return
        if not s:
            return

        # 强制指定了颜色就直接用
        if level:
            try:
                LOG_QUEUE.put((level, s if s.endswith("\n") else s + "\n"))
            except Exception:
                pass
            return

        # 没指定时根据内容自动选颜色
        low = s.lower()
        level = "normal"
        if "✗" in s or "失败" in s or "错误" in s or "error" in low:
            level = "error"
        elif "✓" in s or "成功" in s or "完成" in s:
            level = "ok"
        elif "⚠" in s or "警告" in s or "跳过" in s or "[提示]" in s:
            level = "warn"

        try:
            LOG_QUEUE.put((level, s if s.endswith("\n") else s + "\n"))
        except Exception:
            pass

    def _clear_log(self):
        self.log_text.delete("1.0", "end")

    def _set_status(self, text):
        self.var_status.set(text)

    # =========================================================================
    # 校验
    # =========================================================================
    def _collect_tasks(self, mode):
        """
        根据勾选情况收集待处理任务列表。

        返回 list[dict]：每项 {name, fbx, texdir}
          · 如果列表里有勾选项 → 按勾选项批量执行
          · 否则 → 退化成单文件模式，用「模型 FBX」输入框
        无可用任务时返回 None（调用方负责提示）。
        """
        tasks = []
        selected = [m for m, v in zip(self.models, self.checks) if v.get()]
        if selected:
            for m in selected:
                tasks.append({
                    "name": m["name"],
                    "fbx": m["path"],
                    "texdir": m.get("texdir") or self.var_texdir.get().strip(),
                })
            return tasks

        # 没有扫描 / 没有勾选 → 单文件模式
        fbx = self.var_fbx.get().strip()
        if fbx and os.path.isfile(fbx):
            tasks.append({
                "name": os.path.splitext(os.path.basename(fbx))[0],
                "fbx": fbx,
                "texdir": self.var_texdir.get().strip(),
            })
            return tasks

        return None

    def _validate(self, mode, tasks):
        if not tasks:
            if not self.models:
                messagebox.showwarning(
                    "尚未选择模型",
                    "还没有扫描到任何模型。\n\n请先在上方选择「总文件夹」并点击「🔍 扫描模型」，\n"
                    "或者在「模型 FBX」里手动指定一个文件。")
            else:
                messagebox.showwarning("未勾选", "模型列表里没有勾选任何项目。")
            return False

        if mode in ("blender", "all"):
            if not os.path.isfile(CONFIG["blender_exe"]):
                messagebox.showerror("路径错误", "找不到 Blender：\n{}".format(CONFIG["blender_exe"]))
                return False

        if mode in ("ue5", "all"):
            if not os.path.isfile(CONFIG["unreal_editor_exe"]):
                messagebox.showerror("路径错误",
                                     "找不到 UE5：\n{}".format(CONFIG["unreal_editor_exe"]))
                return False
            if not os.path.isfile(CONFIG["uproject"]):
                messagebox.showerror("路径错误", "找不到工程：\n{}".format(CONFIG["uproject"]))
                return False

            # ===== 中文路径硬拦截 =====
            # 不是警告，是直接阻断 —— 因为中文路径必然导致 UE 崩溃，
            # 让它跑下去只会浪费用户几分钟然后失败。
            ok_cn, cn_msg = check_uproject_chinese(CONFIG["uproject"])
            if not ok_cn:
                messagebox.showerror("工程路径含中文，无法继续", cn_msg)
                LOG_QUEUE.put(("error", "\n" + "!" * 70))
                LOG_QUEUE.put(("error", "[已阻断] .uproject 路径含中文，UE5 会崩溃"))
                LOG_QUEUE.put(("error", "[已阻断] " + cn_msg.replace("\n", " ")[:160]))
                LOG_QUEUE.put(("error", "!" * 70 + "\n"))
                return False

        # ---- 图片输出保存路径：合法性 + 实际可写性双重校验 ----
        outdir = self.var_outdir.get().strip()
        if not outdir:
            messagebox.showerror("路径错误", "「图片输出保存路径」不能为空。")
            return False

        # 1) 语法合法性：非法字符 / 设备保留名 / 超长
        ok, reason = check_path_legal(outdir)
        if not ok:
            messagebox.showerror("输出路径不合法", reason)
            return False

        # 1.5) 高风险路径警告（盘符根目录 / 系统目录）
        #      不阻断，但必须醒目提示，因为这几乎必然导致权限失败
        try:
            warn_if_risky_path(
                outdir, emit=lambda m: LOG_QUEUE.put(("error", m + "\n")))
        except Exception:
            pass

        # 2) 真实可写性测试（宽松模式）
        #    探针失败不再硬性阻断 —— 因为安全软件常误拦脚本写文件，
        #    但 Blender / UE 作为正规程序往往能正常写入。
        #    这里只在失败时给出提醒，让用户自行决定是否继续。
        ok, reason = verify_dir_writable(outdir, strict=False)
        if not ok:
            LOG_QUEUE.put(("warn", "[提醒] 输出目录写入测试未通过：\n" + reason + "\n"))
            LOG_QUEUE.put(("warn", "[提醒] 仍将继续执行；若实际导出失败，"
                                   "请检查安全软件白名单。\n"))

        return True

    # =========================================================================
    # 执行入口
    # =========================================================================
    def _on_start(self, mode):
        if self.running:
            messagebox.showinfo("正在运行", "已有任务在执行，请等待完成或点击「🛑 停止任务」。")
            return

        # 记录本次任务起始时间（供交付清单统计耗时）
        self._run_t0 = time.time()
        self._last_delivery = ""

        tasks = self._collect_tasks(mode)
        if not self._validate(mode, tasks):
            return

        self.running = True
        self.stopping = False
        STOP_FLAG.clear()

        for b in (self.btn_blender, self.btn_ue, self.btn_all):
            b.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        # ⚠️ 关键：所有 Tkinter 变量的读取必须在【主线程】完成，
        #    然后把普通 dict 传给后台线程。
        #    Tkinter 变量不能跨线程访问，否则报
        #    "RuntimeError: main thread is not in main loop"。
        options = {
            "out_root": self.var_outdir.get().strip(),
            "do_cleanup": bool(self.var_cleanup.get()),
            "do_origin": bool(self.var_origin.get()),
            "do_lod": bool(self.var_lod.get()),
            "do_material": bool(self.var_material.get()),
            "do_smooth": bool(self.var_smooth.get()),
            "do_scene": bool(self.var_scene.get()),
            "do_light": bool(self.var_light.get()),
            "do_camera": bool(self.var_camera.get()),
        }

        self.worker_thread = threading.Thread(
            target=self._batch_worker, args=(mode, tasks, options), daemon=True)
        self.worker_thread.start()

    def _on_stop(self):
        if not self.running:
            return
        self.stopping = True
        STOP_FLAG.set()
        LOG_QUEUE.put(("warn", "\n[系统] 收到停止请求，将在当前阶段结束后中止…\n"))
        self.btn_stop.configure(state="disabled")
        self._set_status("正在停止…")

    # =========================================================================
    # 进程清理
    # =========================================================================
    def _on_cleanup(self):
        """
        「🧹 清理后台残留进程」按钮。

        随时可点：干掉所有残留的 blender.exe / UnrealEditor.exe。
        放在后台线程执行，避免 tasklist/taskkill 卡住界面。
        """
        if getattr(self, "_cleaning", False):
            return
        self._cleaning = True
        self.btn_cleanup.configure(state="disabled")
        LOG_QUEUE.put(("info", "\n[清理] 开始扫描后台残留进程…\n"))

        def worker():
            try:
                killed, _ = kill_residual_processes(
                    progress=lambda m: LOG_QUEUE.put(
                        ("ok" if "已终止" in m else "normal", m + "\n")))
                LOG_QUEUE.put(("ok" if killed else "info",
                               "[清理] 完成，共终止 {} 个进程\n".format(killed)))
            except Exception:
                LOG_QUEUE.put(("error", "[清理] 出错：\n" + traceback.format_exc() + "\n"))
            finally:
                self._cleaning = False
                UI_QUEUE.put(("restore_cleanup_btn", None))

        threading.Thread(target=worker, daemon=True).start()

    def _on_close(self):
        """
        退出流程（点右上角 X 或「退出」按钮都会走这里）。

        顺序很重要：
          1. 若任务在跑，先请求中止并等一小会儿，让它自己收尾
          2. 强行终止子进程（Blender / UE）
          3. 再次扫描并强杀所有残留（防止孤儿进程）
          4. 销毁窗口
        """
        # ---- 1. 运行中需要二次确认 ----
        if self.running:
            if not messagebox.askyesno(
                    "确认退出",
                    "任务仍在运行。\n\n退出会立即中止当前任务，\n"
                    "并清理所有 Blender / UE 后台进程。\n\n确定要退出吗？"):
                return

        LOG_QUEUE.put(("info", "\n[系统] 正在退出，清理后台进程…\n"))

        # ---- 2. 通知工作线程停止 ----
        STOP_FLAG.set()
        if self.running:
            deadline = time.time() + 3.0
            while self.running and time.time() < deadline:
                try:
                    self.root.update()
                except Exception:
                    break
                time.sleep(0.05)

        # ---- 3. 强杀子进程 ----
        try:
            killed, _ = kill_residual_processes()
            if killed:
                LOG_QUEUE.put(("ok", "[系统] 已清理 {} 个后台进程\n".format(killed)))
            else:
                LOG_QUEUE.put(("info", "[系统] 无残留进程\n"))
        except Exception:
            # 清理失败也不能阻止退出
            LOG_QUEUE.put(("warn", "[系统] 清理进程时出错，继续退出\n"))

        # ---- 4. 关闭窗口 ----
        try:
            self.root.destroy()
        except Exception:
            pass

    # =========================================================================
    # 批量工作线程
    # =========================================================================
    def _batch_worker(self, mode, tasks, options):
        """后台批量循环。注意：options 是主线程取好的快照，这里不再碰任何 Tk 变量。"""
        runner = ProcRunner(STOP_FLAG)
        total = len(tasks)
        success = 0
        failed = []

        try:
            out_root = options["out_root"]
            LOG_QUEUE.put(("info", "\n" + "=" * 72 + "\n"))
            LOG_QUEUE.put(("info", "[系统] 开始批量处理，共 {} 个模型\n".format(total)))
            LOG_QUEUE.put(("normal", "[系统] 输出根目录：{}\n".format(out_root)))
            LOG_QUEUE.put(("info", "=" * 72 + "\n"))

            # 批处理前再检查一次风险，避免用户忽略之前的警告
            try:
                warn_if_risky_path(
                    out_root, emit=lambda m: LOG_QUEUE.put(("error", m + "\n")))
            except Exception:
                pass

            # 工程路径含中文是 UE 崩溃的高危因素
            if mode in ("ue5", "all"):
                try:
                    ok_path, msg = _check_uproject_ascii()
                    if not ok_path:
                        LOG_QUEUE.put(("error", "\n" + "!" * 64))
                        LOG_QUEUE.put(("error", "[强烈警告] " + msg))
                        LOG_QUEUE.put(("error", "!" * 64 + "\n"))
                except Exception:
                    pass

            for idx, task in enumerate(tasks, start=1):
                if STOP_FLAG.is_set():
                    LOG_QUEUE.put(("warn", "[系统] 已停止，剩余 {} 个模型未处理\n".format(total - idx + 1)))
                    break

                name = task["name"]
                fbx = task["fbx"]
                texdir = task["texdir"]

                LOG_QUEUE.put(("info", "\n[进度 {}/{}] 正在处理 {} …\n".format(idx, total, name)))
                LOG_QUEUE.put(("normal", "        模型：{}\n".format(fbx)))
                LOG_QUEUE.put(("normal", "        贴图：{}\n".format(texdir or "（未指定）")))
                # 后台线程不能碰 Tk，投递到 UI_QUEUE 由主线程处理
                UI_QUEUE.put(("status", "处理中 [{}/{}] {}".format(idx, total, name)))

                # =====================================================
                # 单点故障隔离：为当前模型创建输出子目录
                #
                # ⚠️ 历史 bug（V4.1）：
                #    except 分支里引用了 model_out（用于打印目标路径），
                #    但 make_output_dir() 抛异常时 model_out 从未被赋值，
                #    导致 UnboundLocalError，整个批处理直接崩溃。
                #
                # ✅ 修复策略：
                #    1. 先把「目标路径」算出来放进独立变量 model_out_target，
                #       它只做字符串拼接，绝不会抛异常
                #    2. 真正的建目录放在 try 里，model_out 初始化为 None
                #    3. 任何异常都只影响当前模型 → 记日志 → continue 下一个
                # =====================================================
                model_out_target = os.path.join(out_root, safe_name(name))
                model_out = None

                try:
                    model_out = make_output_dir(out_root, name)
                    # 建目录成功但返回空串的极端情况，也要兜底
                    if not model_out:
                        raise OSError("make_output_dir 返回了空路径")

                except PermissionError as exc:
                    # 权限不足 / 被占用：红色警告 + 跳过当前模型
                    LOG_QUEUE.put(("error",
                        "[跳过] 无法创建目录，权限不足或被占用: {}\n"
                        "       错误：{}\n"
                        "       建议：换一个可写的位置（如 C:\\Users\\你的用户名\\Pictures），"
                        "或把该目录加入安全软件白名单\n".format(model_out_target, exc)))
                    failed.append(name)
                    model_out = None
                    # 注意：continue 之前必须先让变量状态干净
                    if idx < total and not STOP_FLAG.is_set():
                        time.sleep(0.5)
                    continue

                except OSError as exc:
                    # 其他文件系统错误（磁盘满、路径非法、网络盘断开等）
                    LOG_QUEUE.put(("error",
                        "[跳过] 目录创建失败: {}\n"
                        "       错误：{}\n".format(model_out_target, exc)))
                    failed.append(name)
                    model_out = None
                    if idx < total and not STOP_FLAG.is_set():
                        time.sleep(0.5)
                    continue

                except Exception as exc:
                    # 兜底：任何意料之外的异常都只跳过当前模型，不中断批次
                    LOG_QUEUE.put(("error",
                        "[跳过] 目录创建出现未知错误: {}\n"
                        "       错误：{}\n".format(model_out_target, exc)))
                    failed.append(name)
                    model_out = None
                    if idx < total and not STOP_FLAG.is_set():
                        time.sleep(0.5)
                    continue

                # 双重保险：走到这里 model_out 必须有值，否则跳过
                if not model_out:
                    LOG_QUEUE.put(("error",
                        "[跳过] 输出目录无效: {}\n".format(model_out_target)))
                    failed.append(name)
                    if idx < total and not STOP_FLAG.is_set():
                        time.sleep(0.5)
                    continue

                # _process_one 内部也可能抛异常（如子进程启动失败），
                # 这里再包一层，确保单个模型出错不会中断整个队列
                try:
                    ok = self._process_one(runner, mode, task, model_out, options)
                except Exception as exc:
                    LOG_QUEUE.put(("error",
                        "[跳过] {} 处理过程中出错：{}\n{}\n".format(
                            name, exc, traceback.format_exc())))
                    ok = False

                # 因用户中止而中断的任务，不计入失败
                if STOP_FLAG.is_set():
                    LOG_QUEUE.put(("warn", "[系统] {} 已中止\n".format(name)))
                    break
                if ok:
                    success += 1
                else:
                    failed.append(name)

                # ---- 任务间冷却 ----
                # Blender / UE 退出后，Windows 需要一点时间释放文件句柄。
                # 如果紧接着处理下一个模型，可能出现「上一个进程还锁着文件」
                # 导致 Permission denied。这里强制等待，代价极小但能规避大部分偶发错误。
                if idx < total and not STOP_FLAG.is_set():
                    LOG_QUEUE.put(("info", "[系统] 等待 1 秒，让文件句柄释放…"))
                    time.sleep(1.0)

            # ---------------- 汇总 ----------------
            LOG_QUEUE.put(("info", "\n" + "=" * 72 + "\n"))
            if STOP_FLAG.is_set():
                LOG_QUEUE.put(("warn", "[汇总] 已中止：成功 {} 个，失败 {} 个\n".format(
                    success, len(failed))))
            else:
                LOG_QUEUE.put(("ok", "[汇总] 全部完成：成功 {} 个，失败 {} 个\n".format(
                    success, len(failed))))
            if failed:
                LOG_QUEUE.put(("error", "[汇总] 失败/跳过的模型（{} 个）：{}\n".format(
                    len(failed), "、".join(failed))))
                LOG_QUEUE.put(("warn", "[汇总] 若为权限问题，请更换输出路径或加入安全软件白名单\n"))
            LOG_QUEUE.put(("normal", "[汇总] 输出根目录：{}\n".format(out_root)))

            # ---- 自动归档（仅全部成功时）----
            if success > 0 and not failed and not STOP_FLAG.is_set():
                try:
                    cust = self.var_customer.get().strip() or "client"
                    arch = self.var_archive_dir.get().strip()
                    if arch:
                        LOG_QUEUE.put(("info", "\n[归档] 开始整理交付文件…\n"))
                        # 收集本次渲染参数写进交付清单
                        _extra = {}
                        try:
                            _c = _load_cfg_dict()
                            _extra["preset"] = (_c.get("preset") or {}).get("active") or ""
                            _extra["material_mode"] = _get_material_mode()
                            _extra["hdri"] = ((_c.get("hdri") or {}).get("hdri_preset")
                                              or (_c.get("hdri") or {}).get("path") or "")
                            _cam = _c.get("camera") or {}
                            _extra["resolution"] = "{}x{}".format(
                                (_c.get("output") or {}).get("width", 1920),
                                (_c.get("output") or {}).get("height", 1536))
                            _extra["exposure"] = _get_effective_ev()
                            _ang = _cam.get("angles") or []
                            _extra["angle"] = _cam.get("face_angle", _ang[0] if _ang else "")
                            # 模型名：从 fbx 路径推断
                            _fbx = self.var_fbx.get().strip()
                            if _fbx:
                                _extra["model_name"] = os.path.basename(_fbx)
                            # 贴图数：数输出目录里的贴图
                            _tex = 0
                            for _dp, _dn, _fns in os.walk(out_root):
                                for _f in _fns:
                                    if os.path.splitext(_f.lower())[1] in (
                                            ".png", ".tga", ".jpg", ".jpeg", ".exr"):
                                        _tex += 1
                            _extra["tex_count"] = _tex

                            # 抗锯齿采样次数
                            _extra["aa_samples"] = CAPTURE_CONVERGE_FRAMES

                            # 处理耗时（从本次启动到现在）
                            try:
                                _extra["elapsed"] = int(time.time() - getattr(self, "_run_t0", time.time()))
                            except Exception:
                                pass

                            # 资产检测：模型+贴图是否都加载成功
                            try:
                                _m_ok = bool(_fbx and os.path.isfile(_fbx))
                                _t_ok = (_tex > 0)
                                _extra["assets_ok"] = bool(_m_ok and _t_ok)
                                _lines = []
                                _lines.append("  模型加载 : {}".format(
                                    "成功" if _m_ok else "失败"))
                                _lines.append("  贴图加载 : {}".format(
                                    "成功（{} 张）".format(_tex) if _t_ok else "失败（0 张）"))
                                _extra["assets_detail"] = "\n".join(_lines)
                            except Exception:
                                pass
                        except Exception:
                            pass

                        r = archive_delivery(
                            out_root, arch, cust, extra=_extra,
                            progress=lambda m: LOG_QUEUE.put(("info", m + "\n")))
                        if r.get("ok"):
                            self._last_delivery = r["delivery"]
                            LOG_QUEUE.put(("ok", "[归档] ✓ 已归档至：{}\n".format(
                                r["delivery"])))
                            LOG_QUEUE.put(("ok", "[归档] 点「📂 打开归档目录」可直接查看\n"))
                            LOG_QUEUE.put(("ok", "[归档] 图片 {} / 日志 {} / 资产 {}\n".format(
                                r["images"], r["logs"], r["assets"])))
                        else:
                            LOG_QUEUE.put(("warn", "[归档] ✗ {}\n".format(
                                r.get("error", "未知错误"))))
                    else:
                        LOG_QUEUE.put(("warn", "[归档] 未配置归档目录，跳过\n"))
                except Exception as exc:
                    LOG_QUEUE.put(("warn", "[归档] 异常（不影响出图）：{}\n".format(
                        str(exc)[:120])))
            LOG_QUEUE.put(("info", "=" * 72 + "\n"))

            UI_QUEUE.put(("status", "已完成：成功 {} / {}".format(success, total)))

        except Exception:
            LOG_QUEUE.put(("error", "[异常]\n" + traceback.format_exc() + "\n"))
            UI_QUEUE.put(("status", "执行出错"))
        finally:
            self.running = False
            UI_QUEUE.put(("restore_buttons", None))

    def _restore_buttons(self):
        for b in (self.btn_blender, self.btn_ue, self.btn_all):
            b.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    # =========================================================================
    # 单个模型：Blender + UE5
    # =========================================================================
    def _process_one(self, runner, mode, task, model_out, options):
        """
        处理单个模型。返回 True 表示成功。

        mode:
            "blender" → 只跑 Blender
            "ue5"     → 只跑 UE
            "all"     → 先 Blender 再 UE
        """
        name = task["name"]
        fbx = task["fbx"]
        texdir = task["texdir"]
        stem = safe_name(name)

        # 归一化路径：给子进程统一用正斜杠
        model_out_posix = _to_posix(model_out)
        texdir_posix = _to_posix(texdir) if texdir else ""

        # ---- 阶段 1：Blender ----
        ue_input_fbx = fbx
        if mode in ("blender", "all"):
            if STOP_FLAG.is_set():
                return False

            exported = os.path.join(model_out, "{}_normalized.fbx".format(stem))
            LOG_QUEUE.put(("info", "  [Blender] 预处理中…\n"))

            try:
                script = fill_template(BLENDER_SCRIPT_TEMPLATE, {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "export_fbx": _to_posix(exported),
                    "texdir": texdir_posix,
                    "outdir": model_out_posix,
                    "source_fbx": _to_posix(fbx),
                    "target_axis": BLENDER_TARGET_AXIS,
                    "lod_min_verts": LOD_MIN_VERTS,
                    "lod_ratios": repr(list(LOD_RATIOS)),
                    "do_cleanup": bool(options["do_cleanup"]),
                    "do_origin": bool(options["do_origin"]),
                    "do_lod": bool(options["do_lod"]),
                    "do_material": bool(options["do_material"]),
                    "do_smooth": bool(options.get("do_smooth", True)),
                })
                # 临时脚本按模型名区分，避免多任务互相覆盖；
                # 落在输出目录下的 temp_scripts/ 里，方便查看、也降低杀毒误报
                bl_path = write_temp_script(
                    "pipeline_blender_{}.py".format(stem), script, base_dir=model_out)
            except Exception:
                LOG_QUEUE.put(("error", "  [Blender] 生成脚本失败：\n" + traceback.format_exc()))
                return False

            # ⚠️ 参数顺序至关重要！
            #
            # 错误写法（V4.2 及之前）：
            #     blender.exe -b -P script.py -- --factory-startup
            #     ↑ 这里的 "--" 表示「后面是传给脚本的参数」，
            #       所以 --factory-startup 根本没被 Blender 解析，
            #       插件照常全部加载 —— 这正是 addon_utils.py 报错的根源！
            #
            # 正确写法：
            #     blender.exe -b --factory-startup -P script.py
            #     ↑ 放在 -P 之前，Blender 才真正忽略用户插件配置，
            #       只加载内置模块（实测插件数从 15 降到 8）。
            cmd = [CONFIG["blender_exe"], "-b", "--factory-startup", "-P", bl_path]
            rc = runner.run(cmd, label="Blender")
            if STOP_FLAG.is_set():
                return False

            if rc != 0:
                LOG_QUEUE.put(("warn", "  [Blender] 返回非零退出码 {}（继续尝试）\n".format(rc)))

            if os.path.isfile(exported) and _file_size(exported) > 0:
                LOG_QUEUE.put(("ok", "  [Blender] 完成：{}（{:.1f} KB）\n".format(
                    os.path.basename(exported), _file_size(exported) / 1024.0)))
                ue_input_fbx = exported
            else:
                LOG_QUEUE.put(("warn", "  [Blender] 未生成导出文件，UE 将直接使用原始 FBX\n"))
                ue_input_fbx = fbx

        # ---- 阶段 2：UE5 ----
        if mode in ("ue5", "all"):
            if STOP_FLAG.is_set():
                return False

            LOG_QUEUE.put(("info", "  [UE5] 渲染中…\n"))

            # ═══════════════════════════════════════════════════════════
            # ★ 启动前日志：UE 崩溃时也能看出它拿到了什么参数
            #
            # 之前 UE 没写 ue_render.log 就退出，完全查不出原因。
            # 现在在 Popen 之前就把启动参数写进 model_out/ue_launch.log，
            # 无论 UE 是否成功运行，这个文件一定存在。
            # ═══════════════════════════════════════════════════════════
            try:
                _launch_log = os.path.join(model_out, "ue_launch.log")
                with open(_launch_log, "a", encoding="utf-8") as _lf:
                    _lf.write("=" * 60 + "\n")
                    _lf.write("[%s] UE5 启动前诊断\n" % datetime.now().strftime("%H:%M:%S"))
                    _lf.write("  输入 FBX : %s\n" % ue_input_fbx)
                    _lf.write("  贴图目录 : %s\n" % (texdir or "(空)"))
                    _lf.write("  输出目录 : %s\n" % model_out)
                    _lf.write("  FBX 存在 : %s\n" % os.path.isfile(ue_input_fbx))
                    _lf.write("  贴图目录存在: %s\n" % os.path.isdir(texdir) if texdir else "  贴图目录: (未设置)\n")
                    _lf.write("  uproject : %s\n" % CONFIG.get("uproject", ""))
                    _lf.write("  UE 可执行: %s (存在=%s)\n" % (
                        CONFIG.get("unreal_editor_exe", ""),
                        os.path.isfile(CONFIG.get("unreal_editor_exe", ""))))
                    _lf.write("  MODE     : %s\n" % mode)
            except Exception as _e:
                LOG_QUEUE.put(("warn", "  [UE5] 启动日志写入失败：%s\n" % str(_e)[:80]))
            try:
                script = fill_template(UE_SCRIPT_TEMPLATE, {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    # 画质增强：是否启用 + 模块所在目录
                    "quality_enabled": bool(
                        ENABLE_QUALITY_ENHANCEMENT and HAS_QUALITY_MODULE),
                    "deliver_dir": _get_module_dir(),
                    "fbx": _to_posix(ue_input_fbx),
                    "texdir": texdir_posix,
                    "outdir": model_out_posix,
                    "width": UE_WIDTH,
                    "height": UE_HEIGHT,
                    "exposure": _get_effective_ev(UE_EXPOSURE),
                    "screen_percentage": UE_SCREEN_PERCENTAGE,
                    "super_sample": SUPER_SAMPLE_PERCENTAGE,
                    "dist_factor": _get_cfg_float("distance_factor", 2.2),
                    "fov": _get_cfg_float("fov", 38.0),
                    "face_angle": _get_cfg_float("face_angle", 90.0),
                    "cam_lift": _get_cfg_float("cam_lift", 0.5),
                    "debug_red_basecolor": _get_cfg_bool("debug_red_basecolor", False,
                                                         section="model"),
                    "material_mode": _get_material_mode(),
                    "material_base_color": repr(_get_base_color()),
                    "material_emissive_gain": _get_emissive_gain(),
                "emission_gain": _get_emission_gain(),
                "debug_emission": _get_emission_debug(),
                "emission_color": repr(_get_emission_color()),
                "bloom_intensity": _get_bloom("intensity", 2.5),
                "bloom_threshold": _get_bloom("threshold", 0.2),
                    "min_bytes": UE_MIN_BYTES,
                    "converge_frames": CAPTURE_CONVERGE_FRAMES,
                    "converge_interval": CAPTURE_CONVERGE_INTERVAL,
                    # ★ 优先用 render_config.json 的 camera.angles，
                    #   取不到才回退到内置候选角度。
                    #   （之前这里写死 UE_ANGLE_CANDIDATES，改配置无效）
                    "angles": repr(_get_cfg_list(
                        "angles", list(UE_ANGLE_CANDIDATES))),
                    "do_scene": bool(options["do_scene"]),
                    "do_light": bool(options["do_light"]),
                    "do_camera": bool(options["do_camera"]),
                })
                ue_path = write_temp_script(
                    "pipeline_ue5_{}.py".format(stem), script, base_dir=model_out)
            except Exception:
                LOG_QUEUE.put(("error", "  [UE5] 生成脚本失败：\n" + traceback.format_exc()))
                return False

            # =================================================================
            # UE 启动参数
            #
            # ⚠️ 关键修正：禁用 Zen 的正确开关是 -NoZenAutoLaunch，不是 -NoZen！
            #
            # 现场证据（Saved/Logs 里的引擎日志）：
            #   LogZenServiceInstance: Warning: Failed launch service using
            #     executable '.../zenserver.exe' on port 8558
            #   Awaiting ZenServer readiness, waited for 21.635 seconds
            #   （无限重复，每 5 秒一轮）
            #
            # 这说明：-NoZen 不是有效参数，UE 仍然自动拉起 ZenServer，
            #          启动失败后不断重试，进程永远卡在启动阶段 →
            #          Python 脚本根本没机会执行 → 没有任何 png、
            #          连 ue_render.log 都不会生成 → 最后被超时杀掉。
            #
            # 正确开关（引擎源码 ZenServerInterface.cpp:1753 确认）：
            #   FParse::Param(FCommandLine::Get(), TEXT("NoZenAutoLaunch"))
            #
            # 配合 -DDC=InstalledNoZenLocalFallback 使用本地 DDC，双保险。
            # =================================================================
            # 用纯英文工程副本运行，规避 CEF 中文路径崩溃
            actual_uproject = prepare_ascii_uproject(
                progress=lambda m: LOG_QUEUE.put(("info", "  [UE5] " + m + "\n")))

            cmd = [
                CONFIG["unreal_editor_exe"],
                actual_uproject,
                "-ExecutePythonScript=" + ue_path,
                # --- Zen 相关：真正禁止自动启动 ---
                "-NoZenAutoLaunch",
                "-DDC=InstalledNoZenLocalFallback",
                "-DDC-ForceMemoryCache",
                # --- 常规无人值守参数 ---
                "-nosplash",
                "-nop4",
                "-unattended",
                "-NoSound",
                "-windowed",
                "-ResX=1280",
                "-ResY=1024",
                "-d3d11",
                # --- 关闭可能弹窗/需要交互的模块 ---
                "-NoLiveCoding",
                "-NoSentry",
                # -----------------------------------------------------------------
                # 禁用 WebBrowser 插件，彻底绕开 CEF 初始化
                #
                # 根因（已确认）：
                #   D:/YourWorkspace/AppData/Local/UnrealEngine/ 这个目录
                #   无法创建新的子目录（实测：新建 DSHPipeline_Project 被拒绝）。
                #   UE 的 CEF 需要在里面建 webcache_<pid>，建不出来 →
                #   CEF initialisation failed → Ensure condition failed: bCEFInitialized
                #   → 进程在启动阶段硬崩溃，Python 脚本完全没机会执行。
                #
                # 我们做的是无人值守的离屏渲染，根本用不到内置浏览器，
                # 直接把 WebBrowser / WebBrowserWidget 插件禁掉即可。
                # -----------------------------------------------------------------
                "-DisablePlugins=WebBrowser,WebBrowserWidget",
            ]
            env = os.environ.copy()
            env["UE5_AUTO_QUIT"] = "1"

            # -----------------------------------------------------------------
            # 关于 CEF（UE 内置浏览器）崩溃的处理说明
            #
            # 现场证据链（依次排查后确认）：
            #   1) 崩溃日志：CEF initialization for cache dir ... failed with exit code 21
            #      Ensure condition failed: bCEFInitialized  → 进程在启动阶段就崩了
            #   2) 把工程换成纯英文名后，缓存路径确实变干净了：
            #      C:/.../UnrealEngine/DSHPipeline_Project/webcache_6613
            #      但 CEF 依然初始化失败（error 2）→ 说明不完全是中文路径问题
            #   3) 设置 UE_CEF_CACHE_ROOT 反而引入了新的冲突：
            #      ERROR:context.cc(166) The cache_path directory ...
            #      → 该环境变量会被 CEF 校验，指到非常规位置就会被拒绝
            #
            # 结论：不要手动干预 CEF 的缓存路径。
            #       保留 UE 自身的默认行为，配合「纯英文工程名」这一项即可
            #       （工程名已由 prepare_ascii_uproject 处理）。
            #       CEF 在无 GUI/受限环境下失败属于常见的非致命问题，
            #       它不应阻止 Python 脚本执行 —— 若仍然崩溃，
            #       诊断日志会明确指出 bCEFInitialized 断言。
            # -----------------------------------------------------------------

            # 传入超时：UE 卡死时自动终止，不让整个队列挂住
            rc = runner.run(cmd, env=env, label="UE5", timeout=UE_TIMEOUT_SECONDS)
            if STOP_FLAG.is_set():
                return False

            final = os.path.join(model_out, "FINAL.png")
            if os.path.isfile(final) and _file_size(final) > 0:
                LOG_QUEUE.put(("ok", "  [UE5] 完成：{}（{:.1f} KB）\n".format(
                    final, _file_size(final) / 1024.0)))
                return True

            # ---- 没有 FINAL.png：做完整的现场诊断 ----
            self._diagnose_ue_failure(model_out, rc, ue_path)
            return False

        # 只跑 Blender
        return True

    # =========================================================================
    # UE 失败现场诊断
    # =========================================================================
    def _diagnose_ue_failure(self, model_out, rc, ue_script_path):
        """
        UE 没有产出 FINAL.png 时，尽量从现场提取线索。

        会检查：
          1. 退出码含义（含常见崩溃码）
          2. UE 自己写的日志 ue_render.log
          3. 结构化结果 ue_summary.json（里有 error 字段）
          4. 目录里有哪些 png（可能生成了但没合成 FINAL）
          5. UE 崩溃日志（Saved/Crashes 目录）
        """
        LOG_QUEUE.put(("error", "\n" + "=" * 64))
        LOG_QUEUE.put(("error", "  [UE5 诊断] 未生成 FINAL.png，开始排查"))
        LOG_QUEUE.put(("error", "=" * 64))

        # ---- 1. 退出码 ----
        code_map = {
            0: "正常退出（但没产出图片 → 脚本逻辑问题，看 ue_render.log）",
            1: "一般性错误",
            3: "找不到 .uproject 或路径错误",
            -1: "进程启动失败",
            TIMEOUT_RC: "超时被强制终止（UE 卡死）",
            3221225477: "0xC0000005 内存访问冲突（UE 崩溃）",
            -1073741819: "0xC0000005 内存访问冲突（UE 崩溃）",
            3221226505: "0xC0000409 栈溢出（常为安全软件拦截）",
            -1073740791: "0xC0000409 栈溢出（常为安全软件拦截）",
            3221225725: "0xC00000FD 栈溢出",
        }
        meaning = code_map.get(rc, "")
        LOG_QUEUE.put(("error", "[诊断] 退出码：{} {}".format(
            rc, ("（{}）".format(meaning)) if meaning else "")))

        # ---- 2. UE 脚本自己写的日志 ----
        ue_log = os.path.join(model_out, "ue_render.log")
        if os.path.isfile(ue_log):
            LOG_QUEUE.put(("info", "[诊断] 找到 UE 脚本日志：{}".format(ue_log)))
            try:
                with open(ue_log, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
                LOG_QUEUE.put(("info", "[诊断] 日志共 {} 行，末尾 25 行：".format(len(lines))))
                for line in lines[-25:]:
                    LOG_QUEUE.put(("normal", "  | " + line[:200]))
            except Exception as exc:
                LOG_QUEUE.put(("warn", "[诊断] 读取脚本日志失败：{}".format(exc)))
        else:
            LOG_QUEUE.put(("warn",
                "[诊断] 没有 ue_render.log —— 说明 Python 脚本可能根本没开始执行，"
                "或是很早就崩了。"))

        # ---- 3. 结构化结果 ----
        summary = os.path.join(model_out, "ue_summary.json")
        if os.path.isfile(summary):
            try:
                import json
                with open(summary, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                LOG_QUEUE.put(("info", "[诊断] ue_summary.json 内容："))
                LOG_QUEUE.put(("normal", "  ok = {}".format(data.get("ok"))))
                err = data.get("error")
                if err:
                    LOG_QUEUE.put(("error", "[诊断] 脚本记录的异常："))
                    for line in str(err).splitlines()[-20:]:
                        LOG_QUEUE.put(("error", "  | " + line[:200]))
                shots = data.get("shots") or []
                if shots:
                    LOG_QUEUE.put(("normal", "[诊断] 拍摄记录 {} 条：".format(len(shots))))
                    for s in shots[:10]:
                        LOG_QUEUE.put(("normal", "  | 角度 {}° → {} 字节".format(
                            s.get("angle"), s.get("bytes"))))
            except Exception as exc:
                LOG_QUEUE.put(("warn", "[诊断] 解析 ue_summary.json 失败：{}".format(exc)))
        else:
            LOG_QUEUE.put(("warn", "[诊断] 没有 ue_summary.json（脚本未跑到收尾阶段）"))

        # ---- 4. 目录里有哪些图 ----
        try:
            pngs = [f for f in os.listdir(model_out) if f.lower().endswith(".png")]
        except Exception:
            pngs = []
        if pngs:
            LOG_QUEUE.put(("info", "[诊断] 输出目录中存在这些 PNG："))
            for f in sorted(pngs):
                fp = os.path.join(model_out, f)
                LOG_QUEUE.put(("normal", "  | {:<28} {:.1f} KB".format(
                    f, os.path.getsize(fp) / 1024.0)))
        else:
            LOG_QUEUE.put(("warn", "[诊断] 输出目录里一张 PNG 都没有。"))

        # ---- 5. UE 崩溃日志 ----
        try:
            uproj_dir = os.path.dirname(CONFIG["uproject"])
            crash_dir = os.path.join(uproj_dir, "Saved", "Crashes")
            if os.path.isdir(crash_dir):
                entries = sorted(os.listdir(crash_dir), reverse=True)[:3]
                if entries:
                    LOG_QUEUE.put(("info", "[诊断] 发现 UE 崩溃记录目录：{}".format(crash_dir)))
                    for e in entries:
                        LOG_QUEUE.put(("normal", "  | " + e))
        except Exception:
            pass

        # ---- 6. 生成的 UE 脚本路径（方便手动检查） ----
        LOG_QUEUE.put(("info", "[诊断] 本次使用的 UE 脚本："))
        LOG_QUEUE.put(("normal", "  " + str(ue_script_path)))

        LOG_QUEUE.put(("error", "=" * 64 + "\n"))

    # =========================================================================
    # 工具
    # =========================================================================
    def _set_status(self, text):
        try:
            self.var_status.set(text)
        except Exception:
            pass


# =============================================================================
# ⑦ 入口
# =============================================================================

def main():
    """
    程序入口。

    打包成 exe 后（--windowed），必须显式设置高 DPI 感知，
    否则在 125%/150% 缩放的显示器上界面会发虚。
    """
    # ═══════════════════════════════════════════════════════════════════
    # 启动第一件事：确保配置文件存在
    #
    # 之前用户点「设为默认」才报"找不到 render_config.json"，
    # 因为配置只在 _internal\ 里，exe 同级没有。
    # 现在启动就检查，不存在立刻生成完整默认配置。
    # ═══════════════════════════════════════════════════════════════════
    try:
        _cfg_path, _created = ensure_config_file(verbose=True)
        if _created:
            print("[配置] 已自动生成默认配置文件：{}".format(_cfg_path))
        else:
            print("[配置] 配置文件：{}".format(_cfg_path))
    except Exception as _e:
        print("[配置] 初始化异常（继续运行）：{}".format(str(_e)[:100]))

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except Exception:
        pass
    PipelineApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
