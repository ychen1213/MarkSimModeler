# 实际验收记录（2026-10-22）

使用既有 `D:/工作/MarkSimFDTD/.venv/Scripts/python.exe`，Python 3.12.13 x64。没有重装 Python 或复制虚拟环境。新建工具位于 `D:/工作/MarkSimModeler`。

## 分阶段结果

| 阶段 | 实际完成及验证 |
|---|---|
| P0 独立建模与结构包 | AH32/53/74 真 GDS → 布尔几何 → 材料快照 → 哈希结构包；新目录搬移读取通过 |
| P1 Lumerical 与 STL | 参数结构、凹多边形、多材料体素，封闭/朝向/顶点流形/体积与 STL 精度检查；实际文件已生成 |
| P2 老程序导入 | 两版真实导入新工具 AH53 包并通过 prepare；真实体素包经 Tk 预览，并明确拒绝二维求解 |
| P3 GUI | 五页 Tk 窗口；新建、编辑、保存、GDS、材料、生成、有限阵列、SEM/TEM 和导出实际测试 |
| P4 迁移与回归 | 全套 61 项通过，26.02 秒；源码补丁实际应用/幂等/回滚/数据保持验证通过；源码解压冒烟见后附记录 |

全套命令：`python -m pytest tests -q --junitxml=outputs/modeler_all_tests.xml`。XML 是实际运行记录。另两版适配：原生版16项新增+29项既有回归通过；Meep版16项新增+82项既有回归通过。[适配完整记录](adapter_validation.md)

## 实际生成物

以下路径相对于开发目录，原始运行结果留在本机 `outputs/validation/`；分发源码不打包历史输出，可用 CLI 复现。

| 示例 | 结果 |
|---|---|
| `AH53_structure` / `AH53_lumerical` | 4个前景实体，包含衬底；5个STL（含背景盒） |
| `coated_structure` / `coated_lumerical` | 300nm光栅 + 100nm顶部/50nm侧壁包覆，4种材料；17个前景分片，18个STL |
| `sem_tem_demo` / `sem_tem_structure` / `sem_tem_lumerical` | 合成图实际边缘提取与重建；138×66×34体素；9个前景连通实体，95,248个前景三角面；10个STL |

最后一例经新增顶点流形检查再次读取并通过。解析网格保留整单元阶梯，不对材料标签做浮点插值或自动平滑。GUI截图为程序真实客户区：`outputs/gui_review_20261022T150700_0101ea2c/`。

## 审查中发现并修复的问题

- 材料草稿不再被原 TXT 重新读取覆盖；导入独立几何优先其相邻材料快照，不以当前工程同名材料替代。
- 体素清单的单位、背景、轴、范围、标签和 H5 必须一致，不能只依赖文件哈希。
- 大坐标处修改条纹长度使用绝对坐标容差；STL 同时检查拓扑、实际 float32 顶点与体积误差。
- 拒绝单顶点夹点的非流形表面。原“合法孔洞”测试夹具实际也有夹点，改为内外表面明确隔开的孔洞；原夹点另设拒绝测试。
- 工程输入文件必须位于工程内，互不别名；外部图像与 GDS 只作显式输入。
- GUI删除步骤的 z 上界、停用模板绑定、异步快照过期与图像配置材料相对路径已修复。

有失败的初始测试记录保留在开发输出中，未压制生产异常、放宽物理精度或伪造执行结果。

## 明确未验证 / 未实现

1. **Lumerical 2023 R2.1 本机未安装，本次未在 Lumerical 执行 LSF。** 清单始终为 `target_runtime_verified=false`；材料拟合需在目标 Material Explorer 检查。当前本机2025安装也不作为目标版本通过的证据。
2. **本次没有运行 Meep 或原生 FDTD 物理求解。** 修改的是结构导入与准备，求解器内核未改；没有新散射矩阵、Jones矩阵或光路KPI。
3. 公司电脑本身尚未运行安装测试；源码可由已有普通用户Python安装依赖，但公司网络/执行策略不能在此电脑替代验证。
4. 单SEM+单TEM重建仍有截面外推假设；图像桥接目前是背景/衬底/条纹三角色，不自动推断任意材料。
5. GDS工艺为几何近似，原生参数模型目前是竖直挤出；任意倾斜/圆角可由显式三维体素承载，但没有新增完整工艺物理仿真、任意CAD导入或光学张量材料。
6. `.fsp` 由 Lumerical 运行脚本后保存，本工具不直接写。有限背景与衬底的仿真域/PML处理由接收端明确设置。

## 源码迁移实测

源码归档解压到 `outputs/source_archive_smoke/MarkSimModeler` 后，119个归档文件SHA256全部匹配，确认没有虚拟环境、EXE或本机解释器配置。清除进程PYTHONPATH后，在新目录依次运行环境检查、新建AH53、生成结构包、导出LSF/STL，全部返回0；最后新目录全套 **61 passed in 73.58s**。

测试仍复用本机已核验解释器，不等同于在公司电脑安装成功。v0.1.0交付归档仅增加本段和验证记录，可执行代码、测试与示例输入均与已测试归档一致。证据：[迁移记录](validation_artifacts/source_archive_smoke.json)、[解压后的测试XML](validation_artifacts/archive_tests.xml)、[工作目录测试XML](validation_artifacts/modeler_all_tests.xml)。

## v0.1.1 双击启动修复

用户反馈 v0.1.0 双击仅显示“请按任意键继续”后关闭。实际环境为 PATH 只有0字节 WindowsApps Python占位入口、没有py命令，但本机配置中的既有Python可用。旧bat先依赖PATH启动Python才能读取配置，形成自举缺陷。前述61项与解压测试采用显式Python路径，没有覆盖这个失败路径。

修复为bat先通过系统PowerShell读取配置，再选择Python；配置、工程虚拟环境、py和真实PATH解释器按顺序解析，跳过商店占位入口。启动日志持久保留，不安装或替换环境。真实bat的 `--check-only` 已返回0；`--smoke-test` 已成功创建真实Tk与五页并正常结束，日志分别为 `logs/startup_20261022T152948251_42712.log`、`logs/startup_20261022T153002731_2332.log`。

正常无参数bat入口也已实际启动，主窗口“MarkSim Structure Modeler · 独立源码版”响应正常（本次进程8880，启动日志 `logs/startup_20261022T153313506_46472.log`）。启动专项8项回归全部通过：覆盖中文空格目录、仅商店占位PATH、配置优先、环境变量路径、诊断参数转发、错误日志与退出码保留；见 `docs/validation_artifacts/launcher_tests_v0.1.1.xml`。本次仅修改启动路径，不重跑无关电磁仿真。

## v0.2.0 使用体验与错误恢复（2026-10-23）

沿用同一解释器、依赖和已有启动配置，未重装环境。按实际用户顺序检查材料、Mark/GDS工艺、保存/切换、后台建模、预览、SEM/TEM/体素、导出及异常恢复，完成33项问题与体验改进，详细见 [排查记录](UX_AUDIT_v0.2.0.md)。全部复现使用新建临时工程，未修改用户现有项目，未关闭旧GUI。

最终完整执行 `python -X utf8 -m pytest tests -q --junitxml=outputs/ux_audit/all_tests_final.xml`：**131 passed in 238.32s**，退出码0。涵盖真实Tk、真实GDS/多材料几何、合成图边缘提取/体素、结构包及LSF/STL文件输出。新增回归覆盖待应用表单、材料单位、模态窗口、保存失败保护、忙态选择拦截、销毁控件、小窗口底栏、配置独立保存往返与防覆盖。

实际记录：[最终完整JUnit](validation_artifacts/ux_all_tests_v0.2.0.xml)。本轮没有运行Meep或Lumerical求解，没有改变目标版本未验证的结论。

## v0.2.1 Lumerical脚本缺陷修复（2026-10-23）

用户实际运行发现 `main.lsf` 第29行语法错误。已确认旧测试只验证数据，并用 `.rstrip(';')` 掩盖了LSF矩阵最后一行多余分号。修复材料、多边形和三维顶点矩阵输出，材料阅读摘录改为 `.txt`。

新增回归对修复前代码实际失败，对修复版通过；最终全套 **139 passed in 97.11s**。[JUnit](validation_artifacts/all_tests_v0.2.1.xml)

本次另在真实 **Lumerical 2025 R2 / 8.34.4175** 独立隐藏会话中完成主脚本两次、STL、三维体素面片及体素STL，共 **5/5** 执行与对象/材料回读通过。无FDTD求解、无目标2023 R2.1执行，未改用户原工程和原导出。[实际回读](validation_artifacts/lumerical_runtime_v0.2.1.json)、[根因与复用验证工具](LUMERICAL_FIX_v0.2.1.md)
