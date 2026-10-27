# Blender 到 UE5 自动化渲染管线：开发踩坑与避坑指南

> **项目**：Pipeline_Tool —— 面向独立开发者与小型工作室的 3D 资产批处理与渲染管线
> **技术栈**：Python 3.11 / Unreal Engine 5.8 / Blender 5.2 / Tkinter / Subprocess
> **代码规模**：主程序 10,070 行，核心模块 2,768 行（ue_render_core 2,214 + render_config 554）
> **开发周期**：两周密集攻坚，27 个版本迭代
> **文档作者**：Pipeline TD @ 自动化渲染工作室

---

## 写在前面：这份文档为什么值得读

这不是一份"我用了什么库"的教程。

这是一份**故障档案**。里面每一条坑，都是真实发生的、花了几个小时到十几个小时才定位的、有些至今没有完全解决的。我保留了失败的部分，因为**能说清楚哪里没搞明白，比假装什么都懂更有价值**。

文末有 5 条建议，如果只看那个，也能省下你几天时间。

---

## 一、环境与系统层面的"暗雷"

这一章的所有问题，都有一个共同特征：**报错信息和你真正的问题毫无关系**。你会以为在调 A，实际是 B 出了问题。

### 1.1 中文路径导致 UE 启动即闪退

**【现象】**

```
启动 UnrealEditor.exe，进程存活 3-5 秒后退出。
没有窗口，没有日志，Windows 事件查看器里只有一条无意义的
应用程序错误。改用 UnrealEditor-Cmd.exe 则完全没有输出。
```

**【排查思路】**

第一反应是参数写错了。于是做了三板斧排查：

1. 手动用同样的参数启动编辑器 → **能正常打开**
2. 用脚本启动 → 闪退
3. 差异在哪？脚本用 `subprocess.Popen`，且工作目录、参数都一致

把参数逐条删掉再试，发现**删到只剩工程路径时仍然闪退**。这时才意识到问题不在参数，而在**路径本身**。

`D:\Unreal Projects\MyProject\MyProject.uproject` —— 中文。

UE5 的编辑器启动时会初始化 CEF（Chromium Embedded Framework）用于 Web 相关的编辑器面板。**CEF 的缓存目录要求纯 ASCII 路径**，一旦遇到中文，CEF 初始化失败 → 触发未捕获异常 → 引擎在 Python 脚本执行前就退出了。

这就是为什么连日志都没有：**Python 解释器还没被唤起，进程就死了。**

**【最终解决方案】**

生成一个**英文影子工程**（ASCII Shadow Project），每次运行前自动创建：

```python
def prepare_ascii_uproject():
    """
    把中文路径的工程复制成一个纯英文路径的影子工程。

    关键点：
      · .uproject 文件名改成 ASCII
      · 工程所在目录也要 ASCII
      · Content 用 junction 链接，避免复制几十 GB 资源
      · CEF 缓存目录显式指定到 ASCII 路径
    """
    src = CONFIG["uproject"]                    # MyProject.uproject

    if not has_chinese(src):
        return src                              # 本身就是英文，直接用

    shadow_dir = _ensure_ascii_cef_dir(src)     # D:\UE_ASCII_Projects\Proj_3\
    shadow = os.path.join(shadow_dir, "DSHPipeline_Project.uproject")

    if not os.path.isfile(shadow):
        shutil.copy2(src, shadow)
        # Content / Config 用 junction，不占空间
        _link_dir(os.path.join(os.path.dirname(src), "Content"),
                  os.path.join(shadow_dir, "Content"))

    return shadow
```

**验证方式**：影子工程的 CEF 缓存目录是 `D:\UE_ASCII_Projects\...\webcache_6613`，启动日志不再出现 `CEF initialization failed`。

> **⚠️ 注意**：`-DisablePlugins=WebBrowser,WebBrowserWidget` 也能缓解，但**不能根治**。WebBrowser 插件在网络请求、OAuth 等场景仍会被拉起。

---

### 1.2 路径含空格导致参数截断

**【现象】**

```
日志里 UE 的启动参数是：
  -ExecutePythonScript=D:/YourWorkspace/deep_seek

但实际路径是：
  D:/YourWorkspace/deep seek/temp_scripts/pipeline_ue5.py
```

参数在空格处被切断了。

**【排查思路】**

这是 `subprocess` 与 Windows 命令行解析的老问题。当参数通过列表传给 `subprocess.Popen` 时，Python 会正确处理引号；但当路径被拼进一个**字符串命令行**时，空格会被 shell 当作分隔符。

**真正的原因是**：我在两处用了不同的构建方式——一处用列表，一处用字符串拼接。字符串拼接那处漏了引号。

**【最终解决方案】**

统一用列表传参，**永远不用字符串拼命令行**：

```python
# ❌ 错误：字符串拼接
cmd = "%s %s -ExecutePythonScript=%s" % (exe, proj, script)
subprocess.Popen(cmd, shell=True)     # ← shell 会吃掉空格

# ✅ 正确：列表传参
cmd = [
    exe,
    uproject,                          # 自动加引号
    "-ExecutePythonScript=" + script,  # 自动加引号
    "-NoZenAutoLaunch",
]
subprocess.Popen(cmd, shell=False)
```

**另外**：`subprocess` 在 Windows 上有个额外的坑——含空格的路径如果**以反斜杠结尾**，会转义后面的引号：

```
"C:\My Dir\" → 引号被转义，参数还没结束
```

解决：路径末尾不要带 `\`，或用 `os.path.normpath()` 规范化。

---

### 1.3 WinError 5：权限拒绝的三重陷阱

**【现象】**

写入失败，报 `[WinError 5] 拒绝访问`。但路径明明是当前用户自己的目录。

**【排查思路】**

这个错误有**三种完全不同的成因**，必须区分：

| 成因 | 现象 | 判断方法 |
|---|---|---|
| **系统保护目录** | 写 `C:\` 根目录、`D:\` 根目录失败 | 路径长度 ≤ 4 |
| **UE 资源锁** | 写 `Content\*.uasset` 失败，但目录可写 | 只对特定文件失败 |
| **沙箱/策略限制** | 任意目录都失败，但同级目录正常 | 进程继承的策略 |

我的项目中三种都遇到过：

- **C 盘根目录**：`default_dir` 配成 `C:\` 时，UE 保存资产失败
- **UE 资源锁**：主工程里的 `M_Body.uasset` 被编辑器占用，`save_asset()` 静默返回 `False`
- **沙箱**：工具运行在受限环境时，只有工作区可写

**【最终解决方案】**

写了三级探测函数，**在动手前先测试**：

```python
def probe_writable(path, label="目录"):
    """
    用真实写入测试目录可写性，而不是检查权限位。

    为什么不能只查 os.access：
      Windows 的 ACL + 沙箱策略 组合下，os.access 会误判为可写。
      唯一可靠的判断是【真的写一个文件试试】。
    """
    if not path:
        return False, "路径为空"

    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".dsh_write_probe_%d" % os.getpid())
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("probe")
        os.remove(probe)
        return True, "可写"
    except PermissionError:
        return False, "拒绝访问（可能是系统保护目录或被占用）"
    except OSError as e:
        return False, "写入失败：%s" % e
```

对于 **UE 资源锁**，光探测目录不够，还要检查**目标文件**：

```python
def is_file_locked(path):
    """尝试以追加模式打开，失败说明被占用。"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "a"):
            return False
    except (PermissionError, OSError):
        return True
```

> **实战教训**：遇到 `save_asset -> False` 时，**不要重试同一条路径**。UE 不会释放已加载的资产。正确做法是换一个新路径（带时间戳），或者提示用户手动删除。

---

### 1.4 临时地图垃圾导致工程启动报错

**【现象】**

```
弹窗：「地图加载失败」
内容浏览器里堆积了 174 个 .umap 文件
```

**【排查思路】**

我的代码每次渲染都 `new_level()` 创建一个临时关卡，路径是：

```python
map_path = ROOT + "/Map_" + 当前时间戳     # /Game/DSH_Golem/Map_143052
```

跑了几十次之后，`/Game/DSH_Golem/` 下堆了 174 个 `.umap`。

结果 UE 启动时，**把 `Materials/M_Body` 误当成地图文件去加载**（同名索引扫描时有概率命中错误文件），于是弹窗"地图加载失败"。

**【最终解决方案】**

1. **固定路径 + 覆盖**，而不是每次新建：

```python
map_path = "/Game/DSH_Temp/Map"      # 永远是同一个，每次覆盖
les.new_level(map_path)
```

2. **临时关卡和资产目录完全隔离**：

```
/Game/DSH_Temp/        ← 临时关卡，可随时清空
/Game/DSH_Golem/       ← 资产，不能混入 .umap
```

3. **启动时清理**：扫一遍临时目录，删掉所有 `.umap`。

> **教训**：凡是"每次运行都新建"的东西，一定要有对应的清理策略。否则它会成为定时炸弹。

---

### 1.5 文件被 UE 编辑器占用，Python 无法保存

**【现象】**

```python
mat = assets.create_asset("M_Body", "/Game/DSH_Golem/Materials",
                          unreal.Material, unreal.MaterialFactoryNew())
print(mat)                    # <Material ...>  ← 创建成功
ok = EditorAssetLibrary.save_loaded_asset(mat)
print(ok)                     # False          ← 保存失败
print(os.path.exists(path))   # True           ← 但文件已存在（旧版本）
```

**创建返回成功，保存返回 False，磁盘上留着旧文件。** 三次操作三个不同的结果，极具迷惑性。

**【排查思路】**

1. 检查是不是权限问题 → 目录可写 ✓
2. 检查是不是路径问题 → 纯英文 ✓
3. 检查文件时间戳 → **没有更新！**

关键发现：**`save_loaded_asset` 返回 `False`，但磁盘上有个同名的旧文件。** 说明 UE 加载了旧资产并持有引用，新数据写不进去。

在编辑器里手动打开 `M_Body`，发现它的修改时间停在几小时前——**编辑器一直持有这个资产的内存对象，磁盘文件处于被锁状态**。

**【最终解决方案】**

三层策略：

```python
def new_mat(path):
    """创建或复用材质资产，三级降级。"""

    # 方案 1：资产已存在 → 加载并清空节点复用
    #   ⚠️ 必须复用同一个对象，否则模型引用的还是旧材质
    if EditorAssetLibrary.does_asset_exist(path):
        mat = EditorAssetLibrary.load_asset(path)
        for expr in MaterialEditingLibrary.get_material_expressions(mat):
            MaterialEditingLibrary.delete_material_expression(mat, expr)
        return mat

    # 方案 2：不存在 → 创建
    mat = create_asset(...)
    if mat:
        return mat

    # 方案 3：创建失败 → 换时间戳路径
    return create_asset(name + "_" + timestamp, ...)
```

**为什么方案 1 是"清空节点"而不是"删除资产重建"**：

删除资产后重建，模型的 `StaticMeshComponent` 仍指向旧对象，新材质不会生效。**必须复用同一个 UObject。**

> **教训**：UE 的资产系统里，`create_asset` / `save_asset` / 磁盘文件 是三个独立的状态。任何一个撒谎，整体就不可信。

---

## 二、跨软件资产流转的黑洞

Blender 和 UE5 对同一份数据的理解，差异比想象中大得多。

### 2.1 减面后法线翻转：模型看起来像碎玻璃

**【问题本质】**

Blender 使用 **OpenGL** 坐标系（Y 轴向上为正），DirectX 使用 **DirectX** 坐标系（Y 轴向下为正）。

法线贴图的 G（绿）通道方向相反。

**如果不翻转**，本该凸起的表面会渲染成凹陷，视觉上就是"碎玻璃/融化"感。

**【代码层面的修复策略】**

**不要在手写材质节点里做 `G *= -1`** —— 这会破坏法线的归一化，导致光照计算错误。

正确做法是在**贴图资产**上设置：

```python
# ✅ 正确：在资产层面翻转
normal_tex = EditorAssetLibrary.load_asset(tex_path)
if not normal_tex.get_editor_property("flip_green_channel"):
    normal_tex.set_editor_property("flip_green_channel", True)
    EditorAssetLibrary.save_loaded_asset(normal_tex)
```

同时，采样器类型也要对：

```python
# 法线贴图必须用 SAMPLERTYPE_NORMAL
# 用 COLOR 会把法线数据当颜色做 sRGB 解码，结果完全错误
tex_node.sampler_type = unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL
```

---

### 2.2 组合图通道错乱：Metallic+Gloss 的数据在 Alpha 里

**【问题本质】**

这个坑极深。贴图文件名叫 `Metallic+Gloss`，直觉上会认为：

- R 通道 = Metallic
- G 通道 = Gloss

**实际用 Blender 逐像素统计后**：

```
T_StoneGolem_Body_Metallic.tga
    R = 0.000   G = 0.000   B = 0.000     ← RGB 全零！
    A = 0.000 ~ 0.733  (均值 0.225)       ← 数据在 Alpha

T_StoneGolem_Body_Roughness.tga
    R = G = B = 0.271 ~ 1.000 (均值 0.775) ← 数据在灰度 RGB
    A = 0.000                              ← 这张 Alpha 反而是空的
```

**同一个模型，两张贴图的数据位置完全不同。**

**【代码层面的修复策略】**

不能"一刀切"认定数据在某个通道，必须**实测每张贴图**，然后写兼容逻辑：

```python
def _pick_channel(src, primary, fallback):
    """
    取 primary 通道，为 0 时回退 fallback。

    实现：clamp(primary + fallback)
    因为实测中两张图的通道互斥（一张 A 有值则 R 必为 0），
    相加结果恰好等于真实值。若都有值，Clamp 保证不超 1.0。
    """
    a = mask(src, primary)
    b = mask(src, fallback)
    return clamp(add(a, b))
```

**探针脚本**（关键工具，建议每个项目都写一个）：

```python
import bpy
img = bpy.data.images.load(texture_path)
px = list(img.pixels[:])
w, h = img.size
n = w * h
step = max(1, n // 200000)      # 采样 20 万像素就够了

for ch, name in enumerate("RGBA"):
    vals = [px[i * 4 + ch] for i in range(0, n, step)]
    nonzero = sum(1 for v in vals if v > 0.01) / len(vals)
    print("%s: min=%.3f avg=%.3f max=%.3f nz=%.0f%%" % (
        name, min(vals), sum(vals)/len(vals), max(vals), nonzero*100))
```

**输出长这样，一眼就能看出数据在哪**：

```
R: min=0.000 avg=0.000 max=0.000 nz=0%     ← 空的
G: min=0.000 avg=0.000 max=0.000 nz=0%     ← 空的
B: min=0.000 avg=0.000 max=0.000 nz=0%     ← 空的
A: min=0.000 avg=0.225 max=0.733 nz=97%    ← 数据在这
```

---

### 2.3 sRGB 色彩空间导致"灰泥人"

**【问题本质】**

**Albedo 是颜色数据**，需要 sRGB 解码（显示器 gamma 2.2）。
**Roughness / Metallic / AO 是数值数据**，绝不能做 sRGB 解码。

如果数值贴图被当成 sRGB 处理：

```
原始值 0.77（粗糙度）
→ sRGB 解码 → 0.55
→ 材质被压平，看起来像灰泥
```

**【代码层面的修复策略】**

按文件名强制设置：

```python
COLOR_KEYWORDS = ("albedo", "basecolor", "base_color", "diffuse")

for name, tex in textures.items():
    is_color = any(k in name.lower() for k in COLOR_KEYWORDS)
    tex.set_editor_property("srgb", is_color)
    # 数值贴图还要设置压缩格式，否则会有色带
    if not is_color:
        tex.set_editor_property("compression_settings",
                                unreal.TextureCompressionSettings.TC_MASKS)
```

材质节点里也要对应：

```python
# 颜色贴图 → SAMPLERTYPE_COLOR（自动 sRGB 解码）
albedo.sampler_type = unreal.MaterialSamplerType.SAMPLERTYPE_COLOR

# 数值贴图 → SAMPLERTYPE_LINEAR_COLOR（不做解码）
roughness.sampler_type = unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR
metallic.sampler_type  = unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR
```

> **踩坑记录**：`SAMPLERTYPE_LINEAR_COLOR` 中间是**下划线**，不是 `SAMPLERTYPE_LINEARCOLOR`。写错了会 `AttributeError`，但因为是枚举查找，报错信息是 `'MaterialSamplerType' has no attribute`，很容易误以为枚举名整体错了。

---

### 2.4 4K 贴图导入变 32×32 占位图

**【问题本质】**

**这是本次开发中最隐蔽的一个 bug。**

`AssetImportTask` 如果不指定 `factory`，UE 会通过文件扩展名推断导入器。对于 **4096×4096、32bpp、64 MB 的未压缩 TGA**，推断失败时 UE **不会报错**，而是创建一个 32×32 的默认占位贴图。

日志只会写：

```
导入贴图 42 张
```

看起来一切正常。

**【代码层面的修复策略】**

```python
task = unreal.AssetImportTask()
task.filename = path
task.destination_path = "/Game/DSH_Golem/Textures"
task.automated = True
task.save = True
task.replace_existing = True

# ★ 关键：必须显式指定 factory
task.factory = unreal.TextureFactory()

# 同时扩展名列表要完整，.tif/.exr/.hdr 都要覆盖
```

**并且要加尺寸校验**，让失败变得可见：

```python
bad_sizes = []
for name, tex in textures.items():
    w = tex.blueprint_get_size_x()
    h = tex.blueprint_get_size_y()
    if w <= 32 and h <= 32:
        bad_sizes.append((tex.get_name(), w, h))

if bad_sizes:
    log("!" * 62)
    log("[贴图][错误] %d 张贴图是占位图大小，导入失败！" % len(bad_sizes))
    for n, w, h in bad_sizes:
        log("[贴图][错误]   %s  %dx%d" % (n, w, h))
    log("!" * 62)
```

> **教训**：**32×32 是 UE 的默认占位贴图尺寸**。看到这个数字，就是导入失败了。

---

## 三、渲染与画质的"极限拉扯"

### 3.1 SceneCapture 满屏狗牙：TSR 需要时间收敛

**【原因分析】**

UE5 默认使用 **TSR**（Temporal Super Resolution，时域超分抗锯齿）。原理是**跨帧累积**亚像素信息。

`SceneCapture2D.capture_scene()` 是**即时抓一帧**——TSR 的历史缓冲根本没有时间累积，于是输出锯齿严重的原始帧。

**【如何通过代码进行参数接管】**

**方案一：多次 capture 让 TSR 收敛**

```python
# 连续抓 3 帧，每帧间隔 0.4 秒，让 TSR 累积历史
for i in range(CONVERGE_FRAMES):
    capture.capture_scene()
    time.sleep(CONVERGE_INTERVAL)
```

**方案二：截图阶段强制 2 倍超采样**

```python
# 平时 100%，截图时切到 200%（渲染 4 倍像素再降采样）
set_cvar("r.ScreenPercentage", 200)
capture.capture_scene()
set_cvar("r.ScreenPercentage", 100)      # 截完立刻恢复，避免影响性能
```

**方案三：画质锁死（15 项 CVar）**

```python
QUALITY_CVARS = {
    "r.DepthOfFieldQuality": 0,              # 关 DoF，主体绝对清晰
    "r.MotionBlurQuality": 0,                # 关运动模糊
    "r.DefaultFeature.MotionBlur": 0,
    "r.AntiAliasingMethod": 4,               # TSR
    "r.TSR.History.SampleCount": 8,          # 限制历史样本，减少拖影
    "r.TSR.ShadingRejection.Flickering": 0,
    "r.Streaming.MipBias": -2,               # 强制最高精度 mip
    "r.MaxAnisotropy": 16,                   # UE 上限
    "r.Streaming.PoolSize": 0,               # 不限流式池
    "r.DynamicRes.OperationMode": 0,         # 关动态分辨率
    "r.Upscale.Quality": 5,                  # 最高质量放大滤波
    "r.ShadowQuality": 5,
    "r.Shadow.MaxResolution": 2048,
    "r.Shadow.CSM.MaxCascades": 4,
    "r.DefaultFeature.AntiAliasing": 4,
}
for name, val in QUALITY_CVARS.items():
    unreal.SystemLibrary.execute_console_command(None, "%s %s" % (name, val))
```

**结果**：15/15 项设置成功，锯齿明显改善。

---

### 3.2 背景板与地平线穿帮

**【原因分析】**

初版用一个巨大的 Cube 当背景墙。问题在于：

1. Cube 是**实体**，会遮挡环境光 → 场景变暗
2. Cube 的**边缘**和地面交接处会露出地平线
3. 加一个 **Plane** 当地面 → 地平线更明显，像"摄影棚穿帮"

我一度用"把墙放大 10 倍"来解决，结果墙变成 1000 米，光照还是不对。

**【如何通过代码进行参数接管】**

**最终方案：完全删掉地面，用一个包裹全场景的球体做渐变背景。**

```python
def create_background_wall(center, size, camera_loc, bg_cfg):
    """
    球形渐变背景。

    为什么用球而不是方形墙：
      · 球从任何角度看都是完美背景，不会露出边界
      · 用 WorldPosition.Z 驱动渐变，天然无接缝
      · 材质 UNLIT + bReverseCulling，不参与光照计算
    """
    radius = size * max(dist_factor * 3.0, 12.0)

    sphere = ss.spawn_actor_from_class(
        unreal.StaticMeshActor, center, unreal.Rotator(0, 0, 0))
    mesh = EditorAssetLibrary.load_asset("/Engine/BasicShapes/Sphere")
    sphere.static_mesh_component.set_static_mesh(mesh)
    sphere.set_actor_scale3d(unreal.Vector(radius, radius, radius))
    sphere.set_actor_label("DSH_BgBall")

    # 渐变材质：WorldPosition.Z → Lerp(bottom, top)
    mat = build_gradient_material(bg_cfg["gradient_bottom"],
                                  bg_cfg["gradient_top"])
    # ★ 双面渲染，相机在球内也能看到
    mat.set_editor_property("two_sided", True)
    sphere.static_mesh_component.set_material(0, mat)
```

**渐变材质节点图**：

```
WorldPosition ──► ComponentMask(Z) ──► Divide(radius) ──► Clamp
                                                              │
                                                          Lerp(Bottom, Top)
                                                              │
                                                          Emissive
```

`bReverseCulling = True` + `ShadingModel = UNLIT`，这样：

- 相机在球体内部能看到背面
- 材质不参与光照，纯粹是自发光背景，不会照亮模型

---

### 3.3 自动打光过曝 / 死黑

**【原因分析】**

初版用固定强度的三盏灯。问题：

- 模型尺寸差异大（有的 1 米，有的 200 单位）→ 固定亮度对不同模型完全不可用
- 主光过强 → 高光死白，细节全丢
- 没有补光 → 背面死黑

**【如何通过代码进行参数接管】**

**按模型尺寸缩放灯光强度**：

```python
# 光强与模型尺寸的平方成正比（物理正确：距离翻倍，光强 1/4）
key_intensity = cfg["lights"]["key"]["intensity"] * (size / 100.0) ** 2
```

**灯光随相机角度重排**：

```python
def reposition_lights(center, size, shot_angle_deg):
    """
    每换一个拍摄角度，重新布置三盏灯。

    为什么必须每帧重排：
      固定灯光时，转到背面拍就是全黑。
      重排后每个角度都是"主光从相机侧打来"，主体始终有光。
    """
    for role in ("key", "fill", "top"):
        light = find_actor("DSH_Light_" + role)
        offset = rotate_offset(cfg_offset[role], shot_angle_deg)
        light.set_actor_location(center + offset * size)
```

**HDRI 环境光作为基础照明**：

```python
def setup_hdri_environment(hdri_path, hdri_cfg, ss):
    """
    加载 HDRI 作为环境光。

    坑：.exr 直接导入得到的是 Texture2D，不是 TextureCube。
        需要转成 Cubemap 才能被 SkyLight 使用。
    """
    cube = import_as_cubemap(hdri_path)     # 内部处理 TextureCube
    skylight = spawn(SkyLight)
    skylight.get_component().set_editor_property("cubemap", cube)
    skylight.get_component().set_editor_property("intensity", hdri_cfg["intensity"])
```

---

### 3.4 硬编码导致 JSON 配置失效

**【原因分析】**

这是**架构层面**的坑，比单个 bug 更危险。

现象：改了 `render_config.json` 里的相机距离，渲染结果完全不变。

排查发现：代码里写死了 `DIST_FACTOR = 2.2`，JSON 里的值被传给了一个**局部变量**，但实际使用的是**全局常量**：

```python
# 模板里
DIST_FACTOR = 2.2                        # ← 硬编码

# 主程序里
values = {"dist_factor": _get_cfg_float("distance_factor", 2.2)}
script = fill_template(UE_SCRIPT_TEMPLATE, values)
# 但模板里的 DIST_FACTOR 根本没被替换！
```

**【如何通过代码进行参数接管】**

**1. 模板化 —— 所有参数用占位符**

```python
UE_SCRIPT_TEMPLATE = r'''
# 相机距离系数（由主程序从 render_config.json 传入）
# ⚠️ 这里原来硬编码 2.2，导致改配置无效、模型永远偏小
DIST_FACTOR = @dist_factor@
...
'''
```

**2. 为什么用 `@key@` 而不是 `str.format`**

```python
def fill_template(template, values):
    """
    用 @占位符@ 语法替换模板参数。

    为什么不用 str.format：
      生成的脚本内部包含大量 Python 字面花括号（字典、格式串、lambda），
      用 .format 会把它们当成占位符，报 "Replacement index out of range"。
      改用 @key@ 这种不会和 Python 语法冲突的标记，最稳。
    """
    out = template
    for key, val in values.items():
        out = out.replace("@" + key + "@", str(val))

    # ★ 检查有没有漏替换的占位符
    leftover = re.findall(r"@([A-Za-z_][A-Za-z0-9_]*)@", out)
    if leftover:
        raise RuntimeError("模板占位符未填充：%s" % sorted(set(leftover)))
    return out
```

**3. 配置文件的多副本问题**

后期发现：`render_config.json` 同时存在于

```
dist\Pipeline_Tool\render_config.json
dist\Pipeline_Tool\_internal\render_config.json
```

改了一处，程序读的是另一处。

**解决**：写一个同步脚本，改配置时**校验所有副本内容一致**。

---

## 四、核心故障：材质在 SceneCapture 中不生效

> **⚠️ 阅读前必读**
>
> 这一章记录的是一个**未完全解决**的问题。
>
> 我最初判断"UE5 的 Lerp 节点求值有 Bug"，但这个结论**没有经过充分验证**。
> 下面如实记录现象、排查过程、已确认的修复、以及**尚未验证的推测**。
>
> 如果要在简历里写这件事，请按"工程排查能力"来写，不要写成"发现引擎 Bug"。

### 4.1 现象

给 StoneGolem 接入 PBR 贴图后，UE5 出图始终是灰白石膏。

**关键量化指标**（用出图 PNG 的字节数判断，无损格式内容相同则字节必然相同）：

| BaseColor 连接方式 | 出图字节数 |
| --- | --- |
| Albedo 贴图（直连） | 1173227 |
| Albedo → 灰度 → 锈色 → Lerp | 1173227 |
| Albedo → Lerp（Alpha=1.0，B=纯红）| 1173227 |
| **纯红常量（直连）** | **846093** |

**只有「常量直连」时字节数才变化。任何经过中间节点的路径，字节数完全不变。**

日志显示一切正常：

```
[材质] Body 锈色叠加已启用：强度=1.00 颜色=(1.00,0.00,0.00)
[材质] Body BaseColor <- MaterialExpressionLinearInterpolate
[材质] Body Emission 已关闭
连接失败：0 处
```

### 4.2 已确认并修复的缺陷（9 处）

这些不是推测，每一处都有实测验证。

#### 缺陷 1：贴图导入成 32×32 占位图

见 §2.4。修复后删除重导 42 张，全部恢复 4096×4096。

#### 缺陷 2：Metallic/Roughness 数据在 Alpha 通道

见 §2.2。

#### 缺陷 3：`connect_material_expressions` 静默失败

**这是最具隐蔽性的 API 陷阱。**

```python
result = mel.connect_material_expressions(src, "R", lerp, "Alpha")
print(result)     # False   ← 连接失败，但不抛异常
```

如果通道名或端口名不匹配，UE **返回 False 而不抛异常**。旧代码从不检查返回值。

**修复**：封装检查函数。

```python
def conn(src, src_out, dst, dst_in):
    """连接两个材质节点，并检查返回值。"""
    try:
        ok = mel.connect_material_expressions(src, src_out, dst, dst_in)
    except Exception as e:
        log("✗ 连接异常 %s" % e)
        return False
    if not ok:
        log("✗ 连接失败：%s'%s' -> %s" % (
            type(src).__name__, src_out or "(默认)", dst_in))
    return ok
```

#### 缺陷 4：输出通道名规则

在 UE 里**逐个节点实测** `src_out` 参数，得到的规则表：

| 节点类型 | 可用的 src_out |
| --- | --- |
| `Constant3Vector` | `""` / R / G / B |
| `VectorParameter` | `""` / RGB / R / G / B / A |
| `TextureSampleParameter2D` | `""` / RGB / R / G / B / A |
| `Constant` | **仅 `""`** |
| `ScalarParameter` | **仅 `""`** |
| `Desaturation` | **仅 `""`** |
| `Multiply` / `Add` / `Lerp` / `ComponentMask` / `Clamp` | **仅 `""`** |

**规律**：计算类节点一律只能用 `""`。只有常量、参数、采样这类叶子节点才有 RGB/R/G/B 分口。

#### 缺陷 5：输入端口名 `"Input"` 不存在

实测结果：

```
Constant3Vector -> Desaturation.'Input'  = False
Constant3Vector -> Desaturation.'Color'  = False
Constant3Vector -> Desaturation.''       = True    ← 默认端口才对

ComponentMask -> ...'Input'              = False
ComponentMask -> ...''                   = True

Clamp         -> ...'Input'              = False
Clamp         -> ...''                   = True
```

**7 处 `"Input"` 全部改为 `""`**，代码里 `"Input"` 用法清零。

#### 缺陷 6：自发光开关被忽略

```python
# ❌ 旧代码：只看贴图是否存在
elif _emi_tex is not None:
    connect_emissive(...)
```

`emission.enabled = false` 根本没有被读取。自发光叠加在 BaseColor 之上，把颜色冲淡。

**修复**：

```python
elif not EMISSION_ENABLED:
    log("Emission 已关闭（emission.enabled=false），跳过")
```

验证：修复后日志出现 `Emission 已关闭`，`[DIAG] Emissive输入=None`。

#### 缺陷 7-9：锈色算法问题

| 问题 | 分析 | 修复 |
| --- | --- | --- |
| 乘法混合导致变暗 | `0.30 × (0.95, 0.32, 0.12) = (0.285, 0.096, 0.036)`，亮度从 0.30 掉到 0.14 | 改为灰度染色：`Albedo → 灰度 → × 锈色 → × 提亮` |
| 遮罩方向反了 | 用 `1 - Roughness`，而粗糙度均值 0.775，取反后 0.225，几乎不上色 | 直接用 `Roughness` |
| 配置文件被 GUI 覆盖 | 「应用预设」按钮写回 `preset.active`，其 `exposure_ev` 优先级更高 | 清空 `preset.active`，同步所有配置副本 |

### 4.3 尚未验证的推测

**以下全部是推测，没有任何验证，不能作为结论。**

| 推测 | 验证方法 | 状态 |
| --- | --- | --- |
| 材质未真正挂载到 MeshComponent | 渲染前查 `mesh_comp.get_material(0)` 的实际返回值 | ❌ 未做 |
| SceneCapture 使用独立渲染世界 | 改用普通 Camera + Movie Render Queue 渲染同一材质对比 | ❌ 未做 |
| 节点图未编译完成 | 等待若干帧后再 capture | ❌ 未做 |
| Lerp 节点求值异常 | 单独测试 `Lerp(常量A, 常量B, 1.0)` 是否输出 B | ❌ 未做 |

**在排除前三条之前，不能说这是 UE 引擎的 Bug。**

### 4.4 工程决策：切换渲染后端

在 UE 路径持续无法定位的情况下，用**同一套贴图**在 Blender Cycles 中测试：

**一次成功** —— 锈褐色、龟裂纹理、金属高光完整呈现。

这反向证明了：**贴图、通道绑定、材质逻辑本身都是正确的**，问题局限于 UE 的渲染路径。

于是做了一个架构决策：**把 Blender 接入为第二个渲染后端**。

**性能实测**（1920×1536，4 个角度）：

| 引擎 | 首张 | 后续每张 | 4 角度总计 |
| --- | --- | --- | --- |
| Cycles CPU 64 samples | 24.01 s | 24.00 s | 96 s |
| EEVEE 64 TAA | 14.60 s | 0.60 s | 16.4 s |
| **Workbench（白模）** | 1.59 s | **0.24 s** | **2.55 s** |

**最终架构**：

```
                    ┌─ plaster（白模） → BLENDER_WORKBENCH（0.24 s/张）
render_engine = "blender" ─┼─ 其他材质        → BLENDER_EEVEE   （0.6 s/张）
                    └─ 需要光追        → CYCLES（可选）
```

**已知限制**：EEVEE 首张有约 15 秒着色器编译开销，无法消除（试过 `--factory-startup` 开关都不影响）。所以「贴图模式 4 角度 = 16.4 秒」，只有白模能进 3 秒。

> **架构层面的收获**：当一条技术路径的成本不可控时，**换路径**往往比继续钻更划算。这需要进行成本核算，不应被当成认输。

---

## 五、给其他开发者的 5 条避坑建议

### 建议 1：不要相信「静默失败」，所有 API 返回值都要检查

这个项目里最耗时的 bug，全部来自**不报错但是错的** API：

| API | 失败表现 |
| --- | --- |
| `connect_material_expressions()` | 返回 `False`，不抛异常 |
| `create_asset()` + `save_loaded_asset()` | 创建成功，保存返回 `False` |
| `AssetImportTask` 缺 factory | 生成 32×32 占位图，日志写「导入成功」 |
| `set_material()` | 返回 `True`，但实际未生效 |

**实践**：

```python
# ❌ 危险
mel.connect_material_expressions(a, "R", b, "A")

# ✅ 安全
if not mel.connect_material_expressions(a, "R", b, "A"):
    log("✗ 连接失败：%s -> %s" % (type(a).__name__, "A"))
```

**判断标准**：任何"设置了应该生效"的操作，都要能**查询回来验证**。查不回来，就等于没做。

---

### 建议 2：`except: pass` 是技术债的高利贷

我的代码里有 **321 个 `except` 块、487 处日志调用**。这个比例不是巧合——**每个 `except: pass` 都曾经掩盖过一个真实的 bug**。

最典型的例子：

```python
# 这段代码掩盖了「端口名写错」整整 6 个版本
for k, v in props.items():
    try:
        node.set_editor_property(k, v)
    except Exception:
        pass          # ← 属性赋值失败，静默跳过
```

**实践**：

```python
try:
    node.set_editor_property(k, v)
except Exception as e:
    log("✗ 属性 %s = %r 赋值失败：%s" % (k, v, str(e)[:80]))
    # 要么记录，要么重试，要么降级 —— 但不能只是 pass
```

**例外情况**：如果是「可选功能，失败不影响主流程」，可以 pass，但要满足两个条件：

1. 代码里写清楚为什么可以忽略
2. 在**聚合层面**有兜底报告（"本次运行 N 项可选功能失败"）

---

### 建议 3：用「可观测的量化指标」代替「看起来对」

这个项目最关键的一个工具是：**看出图 PNG 的字节数**。

```
1173227 = 灰白图
846093  = 变暗的图
```

PNG 是无损格式，**内容完全相同则字节数必然相同**。所以字节数可以精确判断「这次渲染和上次是否完全一致」——比肉眼看图可靠一万倍。

我第一次意识到问题严重，就是在发现**连续 6 个版本的出图字节数完全没变**之后——那说明我改的一切都没有生效。

**实践**：

- 渲染管线：**出图字节数 / 图像哈希**
- 材质系统：**材质参数的实际值**（`get_editor_property` 查回来）
- 贴图导入：**贴图尺寸**（32×32 = 失败）
- 配置系统：**实际读取到的值**（打印出来，不靠推断）

---

### 建议 4：无头/批处理模式下，不要过度依赖引擎的「即时效果」

这条是血泪教训。

| 依赖的东西 | 无头模式下的表现 |
| --- | --- |
| TSR 时域抗锯齿 | 单帧抓取没有历史累积 → 满屏锯齿 |
| `new_level()` | 主工程里可能直接失败 |
| 材质编译 | 可能异步，抓帧时还没编译完 |
| 自发光 / 后处理 | 可能不参与 SceneCapture 的某些通路 |
| CEF 相关功能 | 无头模式下直接崩溃 |

**实践**：

- 关键视觉效果**必须用「每帧独立可算」的方式实现**（比如自发光 GLSL、几何体变形），不要依赖跨帧状态
- 批处理前**先用单次运行验证**，不要一开始就批量
- 每个关键环节都要有**独立的验证点**，不要串成一长条流水线等最后出图

---

### 建议 5：把「坑」变成「文档」，把「猜测」变成「待验证」

我在开发过程中维护了两份文档：

1. **`Debug_Log.md`** —— 记录已确认的修复 + 未验证的推测
2. **代码注释** —— 每个反直觉的地方都写清楚「为什么这么写」

代码注释的例子：

```python
# ═══════════════════════════════════════════════════════════════════
# ★ 实测发现：Metallic 数据存在【不同通道】里
#
#   Body_Metallic.tga    R:0   G:0   B:0   A:57   ← 值在 Alpha！
#   Armor_Metallic.tga   R:112 G:112 B:112 A:64
#   Weapon_Metallic.tga  R:135 G:135 B:135 A:80
#
# 旧代码只读 R 通道 → Body 拿到 0 → 金属度永远为 0 → 陶土感。
#
# 现在：ComponentMask 分别取 R 和 A，再用 Add 合并。
# ═══════════════════════════════════════════════════════════════════
```

**为什么这很重要**：

- 三个月后的你会忘记为什么这么写
- 接手的人会"优化"掉看起来冗余的代码，然后重新踩坑
- **面试官看到这种注释，会知道你真的调过**

**最重要的是**：文档里要区分「**已确认**」和「**待验证**」。

我在 §4.3 里明确写了"以下是推测，不能作为结论"。这不是示弱——**能清楚划分「我确认了什么」和「我猜了什么」，是工程能力的体现**。反过来，如果把推测当成结论写进文档，一旦被追问细节，就会全盘崩塌。

---

## 附录：项目数据

| 指标 | 数量 |
| --- | --- |
| 主程序代码 | 10,070 行 |
| 核心模块 | 2,768 行 |
| 版本迭代 | 27 个（v2.4.0 → v2.8.0）|
| `except` 块 | 321 |
| 日志调用 | 487 |
| 环境防护函数 | 16 |
| 已确认修复的缺陷 | 9 处（材质链） + 6 处（环境层） |
| 未解决的缺陷 | 1 处（UE SceneCapture 材质不生效，已通过 Blender 后端绕过） |

---

## 结语

写这份文档时，我反复提醒自己一件事：

**技术文档的价值，在于它能不能让读者少踩一个坑。**

而不是它能不能让作者显得很厉害。

这个项目里真正有价值的东西，不是"我实现了多少功能"，而是：

- 我用**字节数**当指标，发现了"改了但没变"
- 我写了**探针脚本**，把"凭直觉猜通道"变成"实测统计"
- 我**记录了未解决的部分**，而不是假装全都搞定了

如果你在这个文档里学到了什么，希望你学到的是这些方法。

---

*本文档基于 Pipeline_Tool v2.8.0 开发过程整理*
*最后更新：2026-10-27*
