# Lumerical 几何与材料导出

本模块创建可编辑的几何和材料，目标为 **Lumerical 2023 R2.1**。它不执行 FDTD，也不创建光源、监视器、求解区域或边界条件。目标版本的 IDE 执行目前**未验证**；Python 中的文件、材料和网格测试不能替代目标版本验证。本机已知安装为 2025 R2，前期许可证会话存在失败记录，本次没有尝试启动或修改许可设置。

## 输出内容与使用

| 文件 | 用途 |
| --- | --- |
| `main.lsf` | 首选入口：自包含的几何与材料生成脚本，无原电脑绝对路径依赖 |
| `materials.lsf` | `main.lsf` 中材料段的阅读摘录；依赖主脚本分配的名称，不单独执行 |
| `materials_snapshot.json` | 完整原始材料表与来源快照，不会重新读取原 TXT 路径 |
| `materials/*.csv` | 实际导出的波长、n、k、频率和复相对介电常数，便于核对 |
| `stl/*.stl` | 可选辅助格式，每个对象独立文件，保留其材料关联 |
| `import_stl.lsf` | 可选替代入口：导入 STL 并恢复材料、缩放和重叠优先级 |
| `lumerical_manifest.json` | 单位、坐标、对象、材料、来源、哈希、验证范围和限制 |
| `README.txt` | 跟随导出目录移动的操作说明 |

在 Lumerical 中新建空工程，或先保存现有工程。手动进入 Layout 模式，用 Script File Editor 打开并运行 `main.lsf`。脚本检测到 Analysis 模式会停止，不会自动切换并清空现有结果。

新对象位于 `MSM_*` 结构组中。检查尺寸和材料拟合，补充仿真设置，再由用户在 IDE 中另存为 `.fsp`。本工具不直接生成 `.fsp`，也不将其他格式改后缀冒充工程文件。

再次运行会新建组和本地材料。名称检查同时覆盖已有组以及前次失败可能遗留的材料，不会把旧材料静默当成新材料。再次导入后，用户应明确禁用不需要的旧组，避免多套结构重叠参与计算。名称隔离本身不解决物理重叠。

`import_stl.lsf` 是 `main.lsf` 的替代路线。运行时将 Lumerical 工作目录设为导出目录；脚本首先检查所有 STL 文件，再创建材料与结构。不应把两条路线的对象同时用于同一次仿真。

## 坐标与几何

物理坐标保持 `x=扫描方向，y=条纹长度方向，z=向上`，不使用旧二维求解器的内部轴置换。建模数据单位为 µm；`main.lsf` 中的坐标乘 `1e-6` 转为米。STL 坐标数值仍为 µm，导入脚本明确使用 `stlimport(path, 1e-6, ...)`。

参数化结构以 `addpoly` 创建：XY 平面多边形沿 z 拉伸，保留凹轮廓、位置和厚度。现有工艺内核已将孔洞等布尔结果分解成简单实体，导出器不会把有孔轮廓错误填满。[官方 Polygon 说明](https://optics.ansys.com/hc/en-us/articles/360034901493-Polygon-Simulation-Object)

体素结构按照材料 ID 提取暴露面，用 `addplanarsolid` 创建封闭三角面片实体；不同连通体分别导出。材料 ID 不插值、不平滑、不强行压平成二维。检查包括每条边恰有两个相邻面、方向一致、正体积及体积与体素标签一致。非流形表面会拒绝导出。面片数超过上限时要求用户主动调整重建网格，程序不自行降低精度。[官方 planar solid 接口](https://optics.ansys.com/hc/en-us/articles/360034924113-addplanarsolid-Script-command)

辅助 STL 采用二进制格式，每个前景对象一份，附有限背景盒。STL 不包含材料、单位和 mesh order，因此必须配合 manifest 或导入脚本，不能把合并后的单个 STL 当成完整多材料模型。[官方 STL 格式说明](https://optics.ansys.com/hc/en-us/articles/360034901953-STL-import-Simulation-object)、[STL 导入命令](https://optics.ansys.com/hc/en-us/articles/360034924733-stlimport-Script-command)

### 背景、衬底与有限阵列

为了保留非真空背景，导出额外创建覆盖 `cell_bounds_um` 的有限背景盒，`mesh order=3`；前景实体设为 `mesh order=1`，因此在重叠区域前景优先。manifest 使用 `role=background_enclosure` 区分该盒与真实工艺实体。背景盒体积与前景体积不能相加作为总材料体积。

背景盒外的介质由目标工程设置决定，本脚本不修改全局背景。衬底同样只导出有限建模深度，源数据中的 `substrate_extend_bottom=true` 作为警告保留，不会在未定义求解区域时擅自延伸。若要模拟半无限衬底，应在 Lumerical 中将衬底按实际求解域延伸进入相应 PML。

母周期与沿 y 均匀等信息保存在 manifest 中。导出周期单元并不等于设置了周期边界；有限阵列则导出用户已确认的重复结果和实际边缘。

## 材料与频率数据

默认采用包内材料快照，不按同名材料自动替换成 Lumerical 库条目。对于每个使用的材料，以完整 n/k 表构造：

```text
真空波长 λ [nm]
    → f [Hz] = 299792458 / (λ × 1e-9)
    → εr(f) = (n(λ) + i k(λ))²
    → ε′ = n² − k²，ε″ = 2nk
    → Sampled data 的两列 [频率 Hz, 复相对介电常数]
```

这里的 ε 是无量纲的**相对介电常数**，不是绝对介电常数 ε₀εr，也不是电导率。脚本使用 `addmaterial("Sampled data")` 和 `setmaterial(...,"sampled data",table)`，表按频率升序排列。[官方 sampled material 接口](https://optics.ansys.com/hc/en-us/articles/360034409654-setmaterial-Script-command)

可以指定 `wavelength_range_nm=[最短波长, 最长波长]`。波段必须完整处于每个导出材料的原始数据范围内；端点在线性 n/k 表内插值，随后再平方计算 ε。不会外推，不接受单个频点作为宽带材料，不把单频空间 n/k 导入当成宽带材料的默认实现。

本模块允许 ε′ 为负的金属光学表，不经过旧求解器的窄带电导率适配。该能力表示可以建模与导出，不代表自家两个求解器都支持这些材料。

缺失 k 默认阻止导出。只有库中已明确保存 `assume_lossless=true` 才采用 k=0，并在脚本与 manifest 标为用户选择的近似。`"true"` 字符串不会当成布尔真值。含 NaN、无效波长、非递增波长、负 k 或非有限 ε 的表会被拒绝。

### 拟合仍要核对

原始表的精确保留不等于时域色散模型已经拟合成功。Lumerical 会对 Sampled data 进行色散拟合；脚本把拟合波段显式设为实际导出表的范围。用户应在 Material Explorer 检查该波段内的 n/k 或 ε 拟合，必要时调整系数与容差。跨软件比较时，应同时核对材料数据与实际拟合曲线。[官方材料导入与拟合说明](https://optics.ansys.com/hc/en-us/articles/360034915093-Creating-new-sampled-data-materials-in-FDTD)

显式库映射示例：

```python
material_mapping = {"my_gold": "Au (Gold) - Johnson and Christy"}
```

这种映射表示用户主动选用 Lumerical 内置材料；导出器不保证它与包内原始表相同。脚本运行前检查指定名称是否存在，不存在则停止。导出记录同时保留映射与原始材料表。

## 修改代码与分段验证

实现位于 `src/markmodel/lumerical.py`，没有导入 `lumapi`，也没有启动外部程序：

- `sampled_material`：独立检查 n/k、插值及 ε 换算。
- `_preamble`：Layout/材料/文件预检与新名称分配。
- `_material_lines`：生成完整光谱材料段。
- `_polygon_lines` / `_planar_lines`：分别生成拉伸多边形和真正三维面片。
- `export_lumerical`：组织目录、STL、材料快照、清单与哈希。

公开入口：

```python
from markmodel.lumerical import export_lumerical

result = export_lumerical(
    model,
    "outputs/new_export",
    export_stl=True,
    wavelength_range_nm=[500, 800],  # 可省略，省略时导出各材料完整表
    max_triangles=2_000_000,
)
print(result["main_lsf"])
```

目录必须不存在或为空，已有输出一律保留。输入模型保持不变；不修改源 TXT、GDS、图像或目标 `.fsp`。

## 已执行的验证

2026-10-22，本机复用已有 Python 3.12 环境执行：

```text
python -m pytest tests/test_lumerical_export.py -q
12 passed in 1.75s
```

覆盖光谱换算和金属 ε′<0、波段内插值/外推拒绝、缺 k 的明确选择、错误单频/无效波段、显式库映射与脚本注入拒绝、凹多边形的物理位置及体积、STL 单位与闭合体积、真实三维体素面片和体积守恒、有限背景优先级、输出文件保护及哈希。

另将新工程 `examples/AH53_DEMO` 已生成的真实几何快照通过 `load_model → export_lumerical` 导出到 `outputs/lumerical_validation_AH53`：4 个前景对象、1 个背景盒、5 个 STL、3 个材料表；没有运行求解器，`target_runtime_verified=false`。

**未完成的验证**：Lumerical 2023 R2.1 中逐条执行脚本、重复运行、STL IDE 回读、Material Explorer 拟合质量，以及在该版本保存 `.fsp` 后重新加载。本次没有把这些项目标记为通过。
