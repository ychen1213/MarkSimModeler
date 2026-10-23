# MarkSimModeler · 独立三维建模与导出

从 Mark/GDS 与简化几何工艺，或 SEM/TEM 重建，得到带材料信息的三维结构。输出自家软件结构包、Lumerical 形状生成脚本及辅助 STL。全部为可编辑 Python 源码，不包含电磁求解器、后端光路或封装 EXE。

## 启动

本机双击 **启动MarkSimModeler.bat**。本机配置复用已核验的 Python 环境，没有复制或重装原环境。

v0.1.1 修复了系统 PATH 只有 Windows 商店 Python 占位入口时无法双击启动的问题。启动脚本现在先读本机解释器配置，错误输出写入 `logs/startup_*.log`；无需重新安装环境。

v0.2.0 完成一轮真实用户流程排查：修复模板尺寸未应用、工艺参数被重置、配置/预览串工程等问题；加入材料导入预览、图像配准与材料来源控制、后台保存、错误记录和小窗口滚动。[详细问题与修复记录](docs/UX_AUDIT_v0.2.0.md)

迁移到公司电脑：复制整个源码目录（或解压源码 ZIP），在普通用户可写目录运行 `setup.bat`，再运行启动脚本。需要现有 64 位 Python 3.11/3.12（含 Tk）和联网安装 wheel 的权限；不需要管理员、Meep、WSL、Docker、Lumerical API。

已有全部依赖时，直接运行：

```powershell
python scripts/check_environment.py
python run_modeler.py gui
```

已有特定环境可设置 `config/local_runtime.json`，格式见 [GUI 与环境说明](docs/gui.md)。该文件仅用于本机，不进入源码 ZIP。所有导出默认选择新文件夹，不覆盖已有导出。

## 第一次使用

1. 新建 AH32、AH53 或 AH74 工程。演示材料名称含 `DEMO`，代表合成常数折射率，不是实测材料。
2. 在“工程与材料”导入 n/k 表，或显式添加常数材料；在“Mark / GDS / 工艺”设置母周期、CD、图层和工艺步骤。
3. 添加薄膜时选择填平或包覆模型。包覆的顶部与侧壁厚度分别设置；这属于几何工艺近似。
4. 点击“生成 / 更新结构”，核对逐步骤颜色、图例、截面与交互 3D。
5. 在“导出与报告”选择当前单元或有限阵列，然后导出。

SEM/TEM 入口可直接运行已知真值合成演示，也可打开随附标注工具，对真实图设置标尺、ROI 和边缘辅助后载入配置。单截面外推有 2.5D 假设；目前桥接材料角色是背景、衬底、条纹，不能自动识别任意多材料。显式多材料 H5/NPZ 体素可导入。

## 两种主要输出

| 输出 | 用法 | 保留的信息 |
|---|---|---|
| 自家结构包 `structure.json` | 把整个目录交给已适配的 MarkSim / MarkSimFDTD，选择“导入结构” | 多材料几何、光谱表、体素、单位、坐标与校验和 |
| Lumerical `main.lsf` | 在 Lumerical Layout 模式运行脚本 | 原生多边形挤出/三维面片、材料与独立结构组 |
| 辅助 `stl/*.stl` + `import_stl.lsf` | 在导出目录执行 STL 导入脚本 | 封闭三角面、配套材料映射与 µm→m 缩放 |

原生 LSF 为主交付。STL 自身没有单位和光学材料，因此必须连同清单使用。每个材料实体单独导出，包括衬底和薄膜；不把整个多材料对象合成单一材料 STL。

LSF 不设置仿真域、边界、源、监视器，不删除旧对象，不保存覆盖工程。运行后可在 Lumerical 自行另存 `.fsp`。**本机没有目标 2023 R2.1：其运行与材料拟合尚未在目标版本验证。** 静态脚本/几何测试通过不等于目标软件执行通过。

已有两版软件在本机已增加结构导入。导入新建仿真工程，实际采用冻结结构与材料；修改几何需回建模器后重新导入。现有二维周期求解器会拒绝有限阵列、任意三维体素及其他超出范围的输入，不会自动降维。[迁移适配说明](integrations/README.md)

## 按阶段运行和修改

```powershell
python run_modeler.py new --type AH53 --output my_projects/AH53
python run_modeler.py build --project my_projects/AH53
python run_modeler.py bundle --project my_projects/AH53 --output exports/AH53_structure
python run_modeler.py lumerical --source exports/AH53_structure --output exports/AH53_lumerical
python run_modeler.py reconstruct --config examples/sem_tem_bridge_demo.json --output exports/sem_tem
```

每个示例输出路径须不存在。有限阵列示例：在 `bundle` 或 `lumerical` 命令增加 `--finite --count-x 3 --stripe-length-um 10`。有限 1×1 也必须显式带 `--finite`；未指定时保留母周期含义。

结构包可整体移动，几何中的坐标单位为 µm；内部 LSF 使用 m。材料表是波长 nm 与 n/k；Lumerical 使用频率 Hz 与复相对介电常数 `εr=(n+ik)²`，导出器负责转换。缺少 k 必须显式选择无损近似，超出表格范围不外推。

- [数据流、坐标与材料](docs/data_flow.md)
- [模块地图和分段调试](docs/development.md)
- [实际验收记录与限制](docs/validation.md)
- [Lumerical 导出详细说明](docs/lumerical_export.md)
- [环境核验](docs/environment_report.md)

## 验证

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

测试包含真实 GDS、几何布尔操作、材料、可搬移结构包、封闭 STL、体素、原生 Tk 表单与导出。GUI 测试需要桌面会话。合成数据仅用作验证几何与重建，不作为 Meep/Lumerical 仿真结果。
