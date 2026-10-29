# -*- coding: utf-8 -*-
r"""
Pipeline_Tool Lite —— 3D 资产批处理与渲染演示版

============================================================
这是 Pipeline_Tool 的【开源演示版】，用于学习和研究。

Lite 版功能：
  - zip / fbx 输入，自动解压（含中文乱码修复）
  - 文件名安全化
  - Blender 基础预处理：减面生成 LOD1-3
  - Blender EEVEE 渲染，单角度出图
  - 简单归档

完整版额外提供：
  - 精细三灯配比与逐角度重排
  - 精确曝光控制与 HDRI 调校
  - 锈色染色材质链路
  - 多套商业风格预设
  - 多角度环绕渲染
  - 收件箱批量扫描
  - 自动交付清单
  - UE5 渲染后端

============================================================

许可证：个人学习与商业限制许可（见 LICENSE）
未经作者 MJMJMJ 书面授权，禁止商业使用。
"""

import json
import os
import shutil
import subprocess
import sys
import zipfile

APP_NAME = "Pipeline_Tool Lite"
APP_VERSION = "1.0.0-lite"

# 通用配置：不含针对性调优参数
DEFAULT_CONFIG = {
    "_说明": "Lite 版配置。完整版提供更精细的渲染参数。",
    "blender_exe": r"D:\blender.exe",
    "output": {"width": 1920, "height": 1024},
    "camera": {"fov": 50.0, "distance_factor": 2.5, "angles": [90]},
    "exposure": {"ev": -4.0},
    "lights": {"intensity": 50.0},
    "lod": {"ratios": [0.5, 0.25, 0.10], "min_verts": 100},
}

CONFIG_FILENAME = "lite_config.json"


def log(msg):
    print("[Lite] " + str(msg), flush=True)


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    return os.path.join(app_dir(), CONFIG_FILENAME)


def load_config():
    p = config_path()
    if not os.path.isfile(p):
        try:
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_CONFIG, fh, ensure_ascii=False, indent=2)
            log("已生成默认配置：%s" % p)
        except OSError as e:
            log("写入配置失败：%s" % e)
        return dict(DEFAULT_CONFIG)

    try:
        # 用 utf-8-sig 读取：兼容带 BOM 的配置文件
        # （Windows 记事本、PowerShell Set-Content 都可能写入 BOM，
        #   用 utf-8 读会报 "Unexpected UTF-8 BOM"）
        with open(p, "r", encoding="utf-8-sig") as fh:
            user = json.load(fh)
    except (OSError, ValueError) as e:
        log("配置读取失败：%s，使用默认值" % e)
        return dict(DEFAULT_CONFIG)

    cfg = dict(DEFAULT_CONFIG)
    for k, v in user.items():
        cfg[k] = v
    return cfg


def sanitize_name(name, fallback="asset"):
    """去掉 Windows 非法字符，限制长度。"""
    if not name:
        return fallback
    s = str(name).strip()
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "_")
    s = s.strip(" .")
    return (s[:60] or fallback)


def fix_zip_name(info):
    """修复 zip 内中文乱码（cp437 -> gbk）。"""
    try:
        raw = info.filename
        if not isinstance(raw, str):
            return raw
        return raw.encode("cp437").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def ingest_zip(zip_path, work_root):
    """解压 zip 到任务目录。返回 (ok, task_dir, fbx_list)。"""
    if not os.path.isfile(zip_path):
        log("zip 不存在：%s" % zip_path)
        return False, "", []

    stem = sanitize_name(os.path.splitext(os.path.basename(zip_path))[0])
    task_dir = os.path.join(work_root, stem)
    try:
        os.makedirs(task_dir, exist_ok=True)
    except OSError as e:
        log("创建任务目录失败：%s" % e)
        return False, "", []

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                name = fix_zip_name(info)
                if name.endswith("/"):
                    continue
                safe = name.replace("\\", "/").lstrip("/")
                if ".." in safe.split("/"):
                    log("跳过可疑路径：%s" % name)
                    continue
                parts = [sanitize_name(p) for p in safe.split("/") if p]
                if not parts:
                    continue
                target = os.path.join(task_dir, *parts)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as e:
        log("zip 损坏：%s" % e)
        return False, task_dir, []
    except OSError as e:
        log("解压失败：%s" % e)
        return False, task_dir, []

    fbx = []
    for dp, dns, fns in os.walk(task_dir):
        for fn in fns:
            if fn.lower().endswith(".fbx"):
                fbx.append(os.path.join(dp, fn))

    log("解压完成：%s（%d 个 FBX）" % (task_dir, len(fbx)))
    return True, task_dir, fbx


def fill(tpl, vals):
    out = tpl
    for k, v in vals.items():
        out = out.replace("@" + k + "@", str(v))
    return out


def run_proc(cmd, tag="Proc"):
    try:
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, encoding="utf-8", errors="replace")
    except OSError as e:
        log("[%s] 启动失败：%s" % (tag, e))
        return False

    for line in p.stdout:
        line = line.rstrip()
        if any(k in line for k in ("[Prep]", "[Render]", "Error")):
            log("[%s] %s" % (tag, line))
    p.wait()
    return p.returncode == 0


def load_script(name):
    """读取同目录下的 blender 脚本模板。"""
    p = os.path.join(app_dir(), name)
    if not os.path.isfile(p):
        log("缺少脚本：%s" % name)
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read()


def preprocess(blender, fbx, task_dir, cfg):
    tpl = load_script("blender_prep.py")
    if not tpl:
        return False

    lod_dir = os.path.join(task_dir, "lod")
    os.makedirs(lod_dir, exist_ok=True)

    script = fill(tpl, {
        "src_fbx": fbx.replace("\\", "/"),
        "dst_dir": lod_dir.replace("\\", "/"),
        "lod_ratios": tuple(cfg["lod"]["ratios"]),
        "min_verts": cfg["lod"]["min_verts"],
    })
    sp = os.path.join(task_dir, "_prep.py")
    with open(sp, "w", encoding="utf-8") as fh:
        fh.write(script)

    log("Blender 预处理中…")
    return run_proc([blender, "-b", "--factory-startup", "-P", sp], "Prep")


def render(blender, fbx, texdir, task_dir, cfg):
    tpl = load_script("blender_render.py")
    if not tpl:
        return False

    out_dir = os.path.join(task_dir, "output")
    os.makedirs(out_dir, exist_ok=True)

    script = fill(tpl, {
        "fbx": fbx.replace("\\", "/"),
        "texdir": (texdir or "").replace("\\", "/"),
        "outdir": out_dir.replace("\\", "/"),
        "width": cfg["output"]["width"],
        "height": cfg["output"]["height"],
        "angles": list(cfg["camera"]["angles"]),
        "fov": cfg["camera"]["fov"],
        "dist_factor": cfg["camera"]["distance_factor"],
        "exposure": cfg["exposure"]["ev"],
        "light_intensity": cfg["lights"]["intensity"],
    })
    sp = os.path.join(task_dir, "_render.py")
    with open(sp, "w", encoding="utf-8") as fh:
        fh.write(script)

    log("Blender 渲染中…")
    return run_proc([blender, "-b", "--factory-startup", "-P", sp], "Render")


def process(input_path, work_root=None):
    cfg = load_config()
    blender = cfg.get("blender_exe", "")

    if not os.path.isfile(blender):
        log("找不到 Blender：%s" % blender)
        log("请修改 %s 里的 blender_exe" % config_path())
        return False

    work_root = work_root or os.path.join(app_dir(), "work")
    os.makedirs(work_root, exist_ok=True)

    input_path = os.path.abspath(input_path)
    if not os.path.isfile(input_path):
        log("输入不存在：%s" % input_path)
        return False

    ext = os.path.splitext(input_path)[1].lower()
    task_dir = work_root
    fbx_list = []

    if ext == ".zip":
        ok, task_dir, fbx_list = ingest_zip(input_path, work_root)
        if not ok or not fbx_list:
            log("zip 处理失败或没有 FBX")
            return False
    elif ext == ".fbx":
        stem = sanitize_name(os.path.splitext(os.path.basename(input_path))[0])
        task_dir = os.path.join(work_root, stem)
        os.makedirs(task_dir, exist_ok=True)
        fbx_list = [input_path]
    else:
        log("不支持的格式：%s（只支持 .fbx / .zip）" % ext)
        return False

    texdir = ""
    for cand in ("Textures", "textures", "TEX"):
        p = os.path.join(os.path.dirname(fbx_list[0]), cand)
        if os.path.isdir(p):
            texdir = p
            break
    log("贴图目录：%s" % (texdir or "（未找到）"))

    fbx = fbx_list[0]
    ok1 = preprocess(blender, fbx, task_dir, cfg)
    log("预处理：%s" % ("成功" if ok1 else "失败或跳过"))

    ok2 = render(blender, fbx, texdir, task_dir, cfg)
    log("渲染：%s" % ("成功" if ok2 else "失败"))

    out = os.path.join(task_dir, "output")
    if os.path.isdir(out):
        pngs = [f for f in os.listdir(out) if f.lower().endswith(".png")]
        log("")
        log("完成！产物在：%s" % out)
        log("共 %d 张图" % len(pngs))
        return len(pngs) > 0

    return False


def main():
    log("=" * 60)
    log("%s  v%s" % (APP_NAME, APP_VERSION))
    log("=" * 60)
    log("")
    log("用法：python Pipeline_Tool_Lite.py <模型.fbx 或 压缩包.zip>")
    log("配置：%s" % config_path())
    log("")

    if not os.path.isfile(config_path()):
        load_config()
        log("已生成默认配置，请检查 blender_exe 后重新运行")
        return 0

    if len(sys.argv) < 2:
        log("错误：缺少输入文件参数")
        return 1

    return 0 if process(sys.argv[1]) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("用户中断")
        sys.exit(130)
    except Exception as exc:
        log("异常：%s: %s" % (type(exc).__name__, exc))
        import traceback
        traceback.print_exc()
        sys.exit(1)
