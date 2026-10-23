# 共同接口（开发约定）

新程序命名 `MarkSimModeler`，独立源码，Python 3.11/3.12；复用新旧版相同的几何核心，不调用求解器。

## 内存模型

`model = {geometry: dict, materials: MaterialLibrary, voxels: dict | None, source: dict}`。
geometry 沿用 schema 1.0，units=um，parametric_solids 或 voxel_labels；voxels 为含 material_id 数组的已验证模型。
材料沿用 schema 1.0 / materials[name] = wavelength_nm,n,k,k_status,assume_lossless,source 等。
建模与导出允许负实部介电常数；自家求解器适配独立拒绝不支持的材料。

## 自家交换目录

`structure.json`:
```
{"schema_version":"1.0", "format":"marksim-structure-bundle",
 "geometry_file":"geometry.json", "material_library_file":"materials.json",
 "files_sha256":{"geometry.json":"...", "materials.json":"...", "voxels.h5":"..."},
 "units":"um", "coordinate_convention":"x=scan,y=stripe,z=up", "provenance":{}}
```
parametric 不含 voxels.h5；voxel geometry.json 用相对 voxel_file 指向同目录H5。
目录以结构包传递，所有引用必须是包内相对路径且在校验清单内，拒绝路径越界和文件哈希不符。
材料以包内快照为准，不跟随历史 source 绝对路径重新读旧TXT。

## 根代理负责的API

`markmodel.service.create_project(folder, mark_type='AH53', parameters=None)` -> project data
`load_project(folder)` -> {folder, project, process, materials}
`save_project(folder, process, materials)` -> revision record; saves explicit edits only
`build_project(folder, process=None, materials=None, previews=True)` -> {model, output_dir, ...}
`load_model(source, material_library=None)` -> model (bundle, geometry JSON/H5/NPZ)
`preview_model(model, output_dir)` -> paths
`repeat_model(model, count_x=1, count_y=1, stripe_length_um=None, *, finite=False)` -> new model; finite=True includes an explicit finite 1x1 object, no implicit periodic boundary
`reconstruct(config, output_dir)` -> {model, output_dir,...} (existing bridge config)
`voxelize(model, spacing_um, output_dir)` -> model
`markmodel.exchange.write_bundle(model, output_dir)` -> {manifest, directory,...}
`read_bundle(path)` -> model
`markmodel.mesh.solid_meshes(model, max_triangles=2000000)` -> list[{name,material,vertices_um:ndarray(N,3),triangles:ndarray(M,3),source_step_id?}]
`write_binary_stl(mesh,path)` -> report
`validate_mesh(mesh)` -> volume,bounds,closed etc (raises on invalid topology)

## Lumerical导出代理API

`markmodel.lumerical.export_lumerical(model, output_dir, *, export_stl=True, material_mapping=None, wavelength_range_nm=None, max_triangles=2000000)` -> paths/report.
Default coordinate mapping identity physical x,y,z. Geometry only: no light source/monitor/FDTD region, no deleteall; use newly named structure group and local materials. No source or existing project overwrite.
LSF basic rect/poly/planarsolid or STL routes, visible notes on target2023R2.1 not runtime-tested. Sampled material: frequency Hz and complex relative epsilon=(n+ik)^2. No lossless fallback for unknown k. STL manifest records um scale and material/object association.

## GUI代理

新UI `markmodel.ui`，复用样式/已有工艺表单作为实现参考，不加载FDTD。支持工程/材料、Mark/GDS/工艺、几何、SEM/TEM/体素、导出与报告。
调用上列服务；未实现入口必须明确禁用。脚本入口由根代理写CLI `run_modeler.py`，GUI代理负责startup/setup/依赖说明（不安装/复制旧venv）。

## 现有软件适配代理

两版增加bundle导入服务/CLI/GUI，导入后新建工程，不覆盖原工程；求解读取导入快照，绝不悄悄重建旧process。
项目可采用 `geometry_input:{kind:'structure_bundle',path:'inputs/structure/structure.json'}`；所有状态绑定/保存/另存为/扫描需保持正确输入源。
未支持体素/真3D可读和预览，但求解明确拒绝；不自动降维，不新增物理算法。
