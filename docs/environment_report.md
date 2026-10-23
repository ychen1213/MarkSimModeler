# 环境核验（2026-10-22）

- 新建 `D:\工作\MarkSimModeler`，现有项目不是Git仓库，修改适配前保存可追溯备份。
- 复用已核验 `MarkSimFDTD/.venv/Scripts/python.exe`（Python3.12 x64）及NumPy/SciPy/h5py/Matplotlib/gdstk/Plotly/Pillow/Tk；不重装、不复制虚拟环境。
- 新工具只建模与导出，不需要fdtd、Meep、WSL或Lumerical API。
- 本机只读核验Lumerical为2025R2，未发现2023R2.1；目标版生成脚本需在目标安装验收，不能将新版或静态检查标为2023成功。
- 复用的12个建模模块与原始哈希记录在reused_sources.json，SEM/TEM源码单独复制到新目录。

本次读取原MarkSim开发要求；本次已确认的新范围覆盖旧文档中的FDTD/光路开发要求。原始工程和测量数据只读。
