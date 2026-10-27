# Debug Log：UE5 材质渲染排查与 Blender 引擎接入

> 记录时间：2026-10-26 ~ 2026-10-27
> 项目：Pipeline_Tool（UE5 / Blender 自动化渲染管线）
> 涉及版本：v2.5.0 → v2.7.1

---

## 摘要

在为 StoneGolem 模型接入 PBR 贴图渲染的过程中，遇到了一个持续复现的现象：

**UE5 用 SceneCapture2D 出图时，材质 BaseColor 无论连接什么贴图或节点链，渲染结果都完全不变（出图字节数恒为 1173227）。**

排查过程中修复了 9 个真实的代码缺陷（贴图导入、通道绑定、节点连接 API 误用等），但核心现象始终未消除。最终通过接入 Blender EEVEE 渲染引擎解决了业务需求。

本文记录完整排查过程、已确认的缺陷、以及**尚未验证的推测**。

---

## 1. 现象描述

### 1.1 初始表现

| 配置 | 预期 | 实际 |
|---|---|---|
| `material_mode = "texture"` | 显示 PBR 贴图（锈迹、龟裂纹理） | 灰白石膏 |
| `material_mode = "dark_metal"` | 深色金属 | 灰白 |
| 锈色叠加节点 | 橙红锈迹 | 无变化 |

### 1.2 关键量化指标

用**出图文件的字节数**作为判断依据（PNG 无损，内容相同则字节数必然相同）：

| BaseColor 连接方式 | 出图字节数 |
| --- | --- |
| Albedo 贴图（直连） | 1173227 |
| Albedo → 灰度 → 锈色 → Lerp | 1173227 |
| Albedo → Lerp（Alpha=1.0，B=纯红） | 1173227 |
| **纯红常量（直连）** | **846093** |

**只有「常量直连」时字节数才变化。任何经过中间节点的路径，字节数完全不变。**

---

## 2. 已确认并修复的缺陷（9 处）

以下每一处都经过了实测验证，不是推测。

### 2.1 贴图被静默导入成 32×32 占位图

**症状**：UE 里查询贴图尺寸，全部是 `32x32`。日志只写「导入贴图 42 张」，不报错。

**根因**：`AssetImportTask` 未指定 `factory`。StoneGolem 的 TGA 是 4096×4096 32bpp 未压缩，单文件 64 MB。UE 靠扩展名推断导入器时，在这个体积下推断失败，创建了 32×32 占位贴图。

**修复**：

```python
task = unreal.AssetImportTask()
task.filename = path
task.destination_path = dest
# ★ 必须显式指定
task.factory = unreal.TextureFactory()
```

**验证**：修复后删除重导 42 张，全部恢复 4096×4096。

**教训**：`AssetImportTask` 对贴图必须指定 factory，否则失败是静默的。

---

### 2.2 Metallic / Roughness 数据在 Alpha 通道

**症状**：金属度恒为 0，模型呈陶土质感。

**探针方法**：用 Blender 逐像素统计 4 张 4096×4096 TGA：

```
T_StoneGolem_Body_Metallic.tga
    R=0  G=0  B=0              ← RGB 全零
    A: min 0.000  avg 0.225  max 0.733   ← 数据在 Alpha

T_StoneGolem_Body_Roughness.tga
    R=G=B: min 0.271  avg 0.775  max 1.000  ← 数据在灰度 RGB
    A=0                          ← 这张 Alpha 反而是空的
```

**根因**：旧代码固定读 R/G 通道。`Body_Metallic` 的 R 通道是 0，金属度永远为 0。

**修复**：改为「Alpha 优先，RGB 回退」，并且在 alpha 与 rgb 都可能有值的场景用 `clamp(A + R)` 兼容。

---

### 2.3 `connect_material_expressions` 静默返回 False

这是本项目中**最具隐蔽性的 API 陷阱**。

**症状**：节点创建成功、日志显示「已连接」，但材质无效果。

**探针方法**：在空白工程里单独测试：

```python
r = mel.connect_material_expressions(src, "R", lerp, "Alpha")
print(r)   # False  —— 连接失败，但不抛异常
```

**根因**：UE 的 `connect_material_expressions` 在通道名/端口名不匹配时**返回 False 而不抛异常**。旧代码调用后从不检查返回值。

**修复**：封装 `conn()` 包装函数，每次连接都检查返回值并记录日志。

---

### 2.4 输出通道名规则（实测表）

在 UE 里逐个节点测试 `src_out` 参数，得到：

| 节点类型 | 可用的 src_out |
| --- | --- |
| `Constant3Vector` | `""` / R / G / B |
| `VectorParameter` | `""` / RGB / R / G / B / A |
| `TextureSampleParameter2D` | `""` / RGB / R / G / B / A |
| `Constant` | **仅 `""`** |
| `ScalarParameter` | **仅 `""`** |
| `Desaturation` | **仅 `""`** |
| `Multiply` / `Add` / `Lerp` / `ComponentMask` / `Clamp` | **仅 `""`** |

**规律**：计算类节点（Multiply/Add/Lerp/Desaturation/ComponentMask/Clamp）一律只能用 `""`。只有常量、参数、采样这类叶子节点才有 RGB/R/G/B 分口。

---

### 2.5 输入端口名 `"Input"` 不存在

**症状**：`ComponentMask`、`Clamp`、`Desaturation` 的输入全部连不上。

**实测**：

```
Constant3Vector -> Desaturation.'Input'  = False
Constant3Vector -> Desaturation.'Color'  = False
Constant3Vector -> Desaturation.''       = True    ← 默认端口才对
ComponentMask   -> ...'Input'            = False
ComponentMask   -> ...''                 = True
Clamp           -> ...'Input'            = False
Clamp           -> ...''                 = True
```

**修复**：7 处 `"Input"` 全部改为 `""`。代码中 `"Input"` 用法已清零。

---

### 2.6 自发光开关被忽略

**症状**：配置 `emission.enabled = false`，但日志输出「Emission 已连接」。

**根因**：

```python
elif _emi_tex is not None:      # 只看贴图是否存在
```

完全没检查 `enabled` 配置。自发光会叠加在 BaseColor 之上，把材质颜色冲淡。

**修复**：

```python
elif not EMISSION_ENABLED:
    log("Emission 已关闭，跳过")
```

**验证**：修复后日志出现「Emission 已关闭」，`[DIAG] Emissive输入=None`。

---

### 2.7 锈色用乘法混合导致变暗

**症状**：加了锈色反而整体变暗，不是变红。

**数值分析**：

```
Albedo 均值 ≈ 0.30
× RustColor (0.95, 0.32, 0.12)
= (0.285, 0.096, 0.036)
→ 亮度从 0.30 掉到 0.14，三个通道全被压暗
```

**修复**：改为「灰度染色」——先把 Albedo 转灰度保留明暗，再乘锈色，最后提亮补偿。

```
gray    = dot(Albedo, (0.3, 0.59, 0.11))
rust_bc = RustColor × gray × 2.4
BaseColor = lerp(Albedo, rust_bc, Roughness × Strength)
```

---

### 2.8 遮罩方向写反

**症状**：锈色「已启用」但完全看不出。

**根因**：初版用 `1 - Roughness` 当遮罩。而 `Body_Roughness` 均值 0.775（石头很粗糙），取反后是 0.225，几乎不上色。

**修复**：直接用 `Roughness`（越粗糙越容易生锈）。

---

### 2.9 配置文件被 GUI 覆盖

**症状**：改好的 `render_config.json` 每次运行后变回旧值。

**根因**：GUI 的「风格预设 → 应用」按钮会写回配置：

```python
def _on_apply_preset(self):
    cfg.setdefault("preset", {})["active"] = name
    json.dump(cfg, fh, ...)
```

而 `preset` 的 `exposure_ev` 优先级高于配置文件，导致曝光被覆盖。

**修复**：清空 `preset.active`，并在同步脚本里统一检查多个位置的配置副本。

---

## 3. 核心现象：仍未定位

修复上述 9 处缺陷后，日志显示：

```
[材质] Body 锈色叠加已启用：强度=1.00 颜色=(1.00,0.00,0.00)
[材质] Body BaseColor <- MaterialExpressionLinearInterpolate
[材质] Body Emission 已关闭（emission.enabled=false），跳过
连接失败：0 处
角度 90°  1173227 字节        ← 仍然完全没变
```

`Lerp(A, B, Alpha=1.0)` 在数学上应完全等于 `B`（纯红），但出图与不加任何节点时字节数完全相同。

### 3.1 已排除的可能

| 假设 | 排除依据 |
| --- | --- |
| 贴图导入失败 | 4096×4096，质检 OK |
| 通道数据错误 | Blender 用同一套贴图出图正常 |
| 节点连接失败 | `conn()` 检查后失败数为 0 |
| 端口名错误 | 逐个节点实测确认 |
| 自发光干扰 | 修复开关后仍不变 |
| 贴图资产损坏 | 删除重导后 4096×4096 |
| 材质未保存 | `save_loaded_asset -> True`，磁盘文件时间戳更新 |
| 曝光 / 灯光参数 | 与配置一致 |

### 3.2 尚未验证的推测

以下都是**推测，没有验证**，不能作为结论：

1. **材质未真正挂载到 MeshComponent**
   `set_material()` 在某条路径上可能没有生效。可验证方式：在渲染前查询 `get_material(0)` 的实际返回值。

2. **SceneCapture 使用独立渲染世界**
   `SceneCapture2D` 有自己的临时场景，材质改动可能未同步。可验证方式：改用普通 Camera + `Movie Render Queue` 渲染同一材质对比。

3. **节点图未被编译**
   虽然 `recompile_material()` 返回成功，但编译可能异步。可验证方式：等待若干帧后再 capture。

4. **Lerp 节点求值异常**
   这是我最初的猜测，但**没有任何直接证据**。要证实需要：单独测试 `Lerp(常量A, 常量B, 1.0)` 是否输出 B，且排除上面 3 种可能。

**在没有排除 1-3 之前，不能说这是 UE 引擎 Bug。**

---

## 4. 解决方案：接入 Blender EEVEE

### 4.1 决策依据

用同一套贴图在 Blender Cycles 中渲染，**一次成功**：

- 锈褐色、龟裂纹理、金属高光完整呈现
- 与 UE 渲染结果（灰白）形成明确对比

说明贴图、通道、材质逻辑都是正确的，问题局限于 UE 的渲染路径。

### 4.2 性能优化

初始用 Cycles，速度无法接受，做了引擎对比测试（1920×1536，4 角度）：

| 引擎 | 首张 | 后续每张 | 4 角度总计 |
| --- | --- | --- | --- |
| Cycles CPU 64 samples | 24.01 s | 24.00 s | 96 s |
| EEVEE 64 TAA | 14.60 s | 0.60 s | 16.4 s |
| **Workbench（白模）** | 1.59 s | **0.24 s** | **2.55 s** |

**最终方案**：

- 白模（`plaster`）→ `BLENDER_WORKBENCH`，0.24 秒/张
- 其他材质 → `BLENDER_EEVEE` / `BLENDER_EEVEE_NEXT`，0.6 秒/张
- Cycles 保留为可选（需要光追时）

### 4.3 已知限制

**EEVEE 首张固定耗时 ~15 秒用于着色器编译**，无法消除：

- 尝试 `--factory-startup` 与不用它，都是 14.9 秒
- Blender 5.2 未暴露着色器缓存控制接口

因此「贴图模式 4 角度 = 16.4 秒」，而「白模 4 角度 = 2.55 秒」。

---

## 5. 踩坑清单（可复用）

| 坑 | 教训 |
| --- | --- |
| UE 的 `connect_material_expressions` 静默失败 | **所有 API 返回值都要检查**，`except: pass` 是灾难 |
| 计算类节点只能用 `""` 通道 | 通道名不能凭直觉写，要实测 |
| `"Input"` 端口名不存在 | UE 材质节点的默认端口就是 `""` |
| `AssetImportTask` 不指定 factory 会静默失败 | 大文件尤其危险 |
| 配置有多个副本 | 改配置要同步所有位置并校验 |
| 出图字节数是最灵敏的回归指标 | 无损 PNG 内容相同则字节必然相同 |
| 「日志说成功了」不等于「真的成功」 | 要看**实质效果**，不看日志措辞 |

---

## 6. 结论

- **已解决**：贴图导入、通道绑定、端口配置、自发光干扰等 9 处缺陷
- **已绕过**：UE 渲染不出材质的问题，通过接入 Blender EEVEE 规避
- **未解决**：UE SceneCapture 与该材质的兼容性问题，根因待查

如果后续要继续排查 UE 侧，建议按 3.2 的验证顺序逐条排除，而不是直接归因于引擎 Bug。
