# 源码地图与分段调试

所有按钮与 CLI 使用同一服务；不需要重新打包即可修改 Python。先关闭旧窗口，再运行入口，便会加载修改后的代码。

| 改什么 | 文件 |
|---|---|
| CLI 和异常返回 | `run_modeler.py`、`src/markmodel/cli.py` |
| 项目、工艺建模、体素、阵列 | `src/markmodel/service.py` |
| 材料表与 ε 计算 | `src/markmodel/materials.py` |
| 结构包 schema/完整性 | `src/markmodel/exchange.py` |
| 凹多边形、三维体素面、STL | `src/markmodel/mesh.py` |
| LSF、光谱表与导出清单 | `src/markmodel/lumerical.py` |
| 窗口与异步任务 | `src/markmodel/ui/app.py` |
| 工艺/材料/图像/导出表单 | `src/markmodel/ui/{process,materials,transfer}.py` |
| 样式和通用控件 | `src/markmodel/ui/widgets.py` |
| 复用的 GDS、薄膜、预览、体素核心 | `src/marksim/`（不含求解器） |
| SEM/TEM 标注与重建 | `3D reconstruction/gui.py`、`3D reconstruction/grating3d/` |
| 两个老程序的导入补丁 | `integrations/` |

## 可独立运行的边界

1. `new` 只生成工程、GDS、工艺与材料，便于核对模板尺寸。
2. `build` 输出几何和每步快照，可检查某个工艺是否改变了预期区域。
3. `bundle` 与 `inspect` 校验冻结结构，可以脱离原工程搬移读取。
4. `lumerical --source ...` 不重建工艺，直接对确定的输入进行转换，适合对照坐标与材料。
5. `reconstruct` 只做图像到体素，输出可再次进入 `bundle/lumerical`。

调试测试：`python -m pytest tests/test_mesh.py -q` 等。GUI 同步入口 `build_now()` / `export_now()` 与交互按钮共享服务；按钮任务在后台线程运行，Tk 更新回到主线程。

## 修改约束

- 不把合成几何或静态校验记作仿真结果；不要自动增加源或光路。
- 原始 n/k 表在导入时读取。保存后的材料表是可编辑权威数据，build 不重读原 TXT；重新读取必须显式重新导入。
- 结构包必须同时验证哈希和语义。体素 JSON 的单位、背景、范围与 H5 必须一致。
- 有限阵列必须保留显式标志；不能被旧二维求解器当作无限周期单元。
- µm→m 只在 LSF 或 STL 导入处转换一次。不要把显示单位变更带入物理坐标。
- 修改建模核心需运行对应几何/材料回归；导出修改还需检查闭合、方向、体积和 round-trip。

STL 使用 float32，远离原点的小结构可能精度不足。导出器要求顶点漂移小于最短网格边的0.1%，体积变化小于0.1%；超过时拒绝，可选不含 STL 的原生 LSF，不能悄悄挪动坐标或缩小结构。当前检测不是任意外来 CAD 的全面自交检测器。

源代码迁移通过 `scripts/build_source_archive.py`。不带 `.venv`、缓存、用户工程/输出、本机绝对路径配置。原程序补丁核对基线后才写文件，版本不同需人工合并。
