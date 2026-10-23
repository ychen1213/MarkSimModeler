# 导入结构功能源码补丁

本机 `D:/工作/MarkSim` 与 `D:/工作/MarkSimFDTD` 已应用本补丁。此目录用于迁移到公司电脑的同版本源码；不是安装器，不安装依赖或修改系统。

先退出正在使用该源码的 GUI/脚本。用已有 Python 执行：

```powershell
python apply_patch.py --edition MarkSimFDTD --target "D:\software\MarkSimFDTD" --check
python apply_patch.py --edition MarkSimFDTD --target "D:\software\MarkSimFDTD"
```

原 Meep 版将 `--edition` 改为 `MarkSim`，`--target` 指向其源码根目录（包含 `src/marksim/service.py`），不要指向某个用户仿真工程。

- `--check` 仅验证，不写任何目标文件。
- 全部原文件及补丁内容先核对 SHA256；仅接受已记录的旧基线或完整的新文件内容。未知修改、新增文件同名冲突会在写入前拒绝，需人工合并，不能强制覆盖。
- 仅列出的 `src/`、`tests/`、`docs/` 文件可写；用户工程、原始数据、结果、环境和依赖不复制也不修改。
- 写前保存原文件及哈希至目标根目录 `code_adapter_backups/<UTC时间_随机码>/`；失败时尝试恢复已写文件，保留恢复日志，对并发用户修改不强行覆盖。
- 已全部应用时返回 `already_applied`，不再写文件或生成备份；混合已应用与未应用文件会逐项核验。
- 公司旧代码若与记录基线不一致，请让 AI/开发人员按 `files/` 中源码人工合并，再运行适配测试，不能直接替换整个目录。

`manifest.json` 含每个源文件的原始/最终 SHA256。`files/MarkSim/` 和 `files/MarkSimFDTD/` 是各自版本的源码补丁。应用后重启 GUI，在工程页使用“导入结构用于仿真…”；详见应用后的 `docs/structure_import.md`。
