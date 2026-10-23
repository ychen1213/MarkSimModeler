# Existing simulator structure-import adapter validation

Date: 2026-10-22, local Windows ordinary-user Python 3.12 environment. Interpreter: `D:/工作/MarkSimFDTD/.venv/Scripts/python.exe`. The two `marksim` packages were tested in separate processes with explicit source paths; neither imports the new `markmodel` package.

## Changes

Both `D:/工作/MarkSim` and `D:/工作/MarkSimFDTD` now provide CLI `import-structure --source BUNDLE --output NEW_PROJECT` and GUI **导入结构用于仿真…**. Imports copy verified files into a new self-contained project. Imported geometry and material tables are immutable snapshots. Wavelength and simulation settings remain editable. Geometry/material changes require a new export/import; only wavelength scans are accepted. `prepare_project` loads the verified snapshot rather than rebuilding the descriptive process stub. Source material TXT paths are provenance only. Full file and manifest hashes bind preparation, GUI state and result caches. Unsupported/removed input bindings cannot fall back silently to recipe mode.

Save-as preserves relative bundle paths and omits old runs/queues/state. Invalid drafts cannot switch input sources or modify frozen geometry/materials. Switching to a recipe restores editable controls before recovering pending draft text. Old MarkSim keeps a valid compatibility optics document, but import and imported-project `run` do not execute optical readout. Existing explicit optical-only use remains available.

All 18 modified existing source files were copied before editing with SHA256 in `outputs/adapter_backups/20261022/manifest.json`. Complete 24-file patch inventory (18 modifications, 6 additions across both programs) and final hashes: `outputs/adapter_changed_files.json`.

## Actual test results

| Edition | New integration tests | Existing targeted regression |
|---|---|---|
| MarkSimFDTD | 16 passed in 21.75 s | 29 passed in 24.18 s |
| MarkSim (Meep) | 16 passed in 21.82 s | 82 passed in 85.21 s |

JUnit reports are under `outputs/adapter_native_hardened.xml`, `adapter_meep_hardened.xml`, `adapter_native_regression.xml`, `adapter_meep_regression.xml`. Initial 9/10 pass reports are retained; the one initial failure was an overly strict test comparison between GUI strings `650` and `650.0`, corrected to compare numerical value. No production exception was suppressed.

Integration tests cover real GDS-generated structure snapshots; preparation without recipe rebuilding; frozen material sources even if historical TXT changes; wavelength interpolation and hash invalidation; denied geometry/material edits and scans; traversal/hash tampering; finite/true-3D rejection; relocation; diagnostic consistency; actual CLI import and preparation; real Tk import, preview, save/draft/save-as, read-only guards, restored recipe editing and pending text; actual HDF5 voxel preview and solver rejection; invalid/missing input-mode refusal.

## Actual new-modeler interchange

- Real modeler `outputs/validation/AH53_structure/structure.json`: both programs imported and prepared four solids. Native estimated 51,205 cells, old Meep API validation 50,688 cells; these are different backend estimates, not simulation results. Reports: `outputs/validation/native_import_AH53/adapter_validation.json` and `meep_import_AH53/adapter_validation.json`.
- Real modeler synthetic SEM/TEM reconstruction bundle `outputs/validation/sem_tem_structure/structure.json`: both real Tk GUIs loaded the exact HDF5 labels and displayed voxel cross sections. Both preparation paths explicitly rejected it because current solvers require y-invariant parametric periodic_2d solids. Reports: `outputs/validation/native_import_sem_tem/adapter_validation.json` and `meep_import_sem_tem/adapter_validation.json`.

No Meep/native FDTD physical solve was run for this import-only change; no fake scattering matrix was generated. Existing solver restrictions remain in force. True 3D/voxel/finite array input is readable and previewable, but not silently flattened for current periodic_2d backends. Solver material restrictions and monitor/source positions still require validation after import. This change does not establish Lumerical 2023 R2.1 runtime compatibility.


## Transferable source patch

`integrations/structure_import_patch/` contains only the 24 audited changed/new source, test and documentation files, plus the manifest, README and standard-library-only Python application script. Full baseline and payload SHA256 checks happen before writing; unknown local changes, new-file collisions, escaped/linked paths are refused. `--check` does not modify the target. Every real write saves previous files and hashes into a unique `code_adapter_backups/` directory. Already-applied files are idempotent. Failures attempt rollback and preserve the recovery report; concurrent user edits are not overwritten.

`tests/test_adapter_patch.py`: 6 passed in 0.36 s. Tests exercise preflight-only behavior, complete application, backup, idempotence, unchanged user data, unknown source changes/new-file conflicts/corrupt payload refusal, write-failure rollback and path restriction. Actual copies of both original production baselines were also checked/applied/reapplied; all final hashes matched and user-data sentinels were unchanged. Evidence: `outputs/patch_validation/report.json` and `outputs/adapter_patch_final.xml`.

After review, both import readers also rebuild canonical voxel metadata from HDF5 labels/coordinates and compare every required geometry manifest field (including background, units and axis convention); degenerate zero-area parametric polygons are rejected. Four added cases verify these checks. The patched code still does not run a physical solve on import.
