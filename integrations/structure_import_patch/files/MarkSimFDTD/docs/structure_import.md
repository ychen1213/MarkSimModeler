# 从 MarkSimModeler 导入结构

项目与材料快照是求解输入，不是仅用于预览的附加图片。本入口不运行 FDTD 或光路计算。

## GUI

工程页选择 **导入结构用于仿真…**，选择结构包内 `structure.json`，再选择新工程父目录。程序将已校验文件复制到新工程 `inputs/structure/`；原有工程不覆盖。几何页自动显示这份快照。

工艺与材料显示为只读。需要修改 pitch/CD、工艺或材料 n/k 时，请回到建模工具修改并导出新包，再导入为新工程。仿真页允许修改波长、网格、光源/监视器坐标和收敛参数。初始仿真设置是本程序通用默认值，不保证适合任意输入结构，必须再次验证。

## 脚本

```powershell
python run_marksim.py import-structure --source D:\your_bundle\structure.json --output D:\new_simulation_project
python run_marksim.py validate --project D:\new_simulation_project
python run_marksim.py fdtd --project D:\new_simulation_project
```

`--output` 只能是新的或空目录。也可从 Python 调用 `marksim.service.import_structure(source, output)`。

## 校验与可移植性

- 包必须有单位 `um`、明确的 x=scan/y=stripe/z=up 坐标约定及全部所引用文件的 SHA256。
- 文件引用必须位于包内；禁止绝对路径、路径越界和符号链接逃逸。
- 新工程固定 manifest 的 SHA256。以后验证、准备求解、GUI 输入绑定与独立诊断都会重新核验；任何内容变动都会阻止把旧准备结果作为当前输入。
- 材料表使用包内数值快照，历史 `source` 路径只作来源说明，不自动重新读取旧 TXT；直接改写工程材料库也会明确拒绝。
- 改波长会重新插值材料并改变任务/结果绑定。只允许扫描 `simulation.lambda_vacuum_nm`，导入几何不允许工艺参数扫描。
- 另存为复制整个输入包，保持相对路径；不复制原结果、队列与旧 GUI 状态。草稿不得改变输入源、几何和材料。

## 求解边界

可以读取及预览真实三维和体素结构，但两版当前 Jones/FDTD 主后端均要求沿 y 均匀的二维周期模型。三维变化、体素及明确标记的有限阵列会拒绝求解，不自动降维或改为周期边界。材料和几何是否适合当前求解器仍需 `validate` 检查。导入成功不等于通过物理求解验收。

旧 MarkSim 的 imported 工程保留 GUI 所需的兼容 optics 配置；导入不执行它，`run` 对导入工程也止于 FDTD。用户之后如需光路读出，必须主动使用已有独立入口。

## 修改位置

`src/marksim/structure_import.py` 负责包验证、固定快照、复制、预览与兼容工程；`service.py` 的 `prepare_project` 显式区分 recipe 与 structure_bundle。GUI 的工程、工艺、材料、几何页面与 controller 处理入口和只读状态。测试 `tests/test_structure_import.py` 使用真实几何准备、命令行及 Tk 窗口，不伪造 FDTD 输出。
