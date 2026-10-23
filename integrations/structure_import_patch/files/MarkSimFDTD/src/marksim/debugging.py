"""Independent read-only checks for project, materials, geometry and FDTD output.

Each stage records input/source hashes and real tracebacks. This module never
starts the solver; use the explicit fdtd command to run electromagnetic physics.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
from pathlib import Path
import platform
import sys
import time
import traceback
import uuid

from .schemas import canonical_hash, file_hash, jsonable, read_json, write_json, versioned

STAGES = ("project", "materials", "geometry", "scattering")
ROOT = Path(__file__).resolve().parents[2]
STAGE_MODULES = {
    "project": ("schemas.py",),
    "materials": ("materials.py", "service.py", "schemas.py"),
    "geometry": ("geometry.py", "schemas.py"),
    "scattering": ("scattering.py", "schemas.py"),
}


class StageInputError(ValueError):
    """Add a project field and filename without discarding the original cause."""

    def __init__(self, field, source, message):
        self.field, self.source = field, str(source) if source else None
        super().__init__(f"{field}: {message}")


def _checked(field, source, operation):
    try:
        return operation()
    except StageInputError:
        raise
    except Exception as exc:
        raise StageInputError(field, source, str(exc)) from exc


class DiagnosticContext:
    """Lazy documents keep one broken stage from blocking unrelated stages."""

    def __init__(self, project, scattering, report_dir):
        path = Path(project).resolve() if project else None
        self.project_dir = path.parent if path and (path.is_file() or path.name == "project.yaml") else path
        self.scattering = Path(scattering).resolve() if scattering else None
        self.report_dir = report_dir
        self.sources = {}

    def source(self, path, field):
        path = Path(path).resolve()
        if not path.is_file():
            raise StageInputError(field, path, "Input file does not exist")
        key = str(path)
        if key not in self.sources:
            self.sources[key] = {"path": key, "sha256": file_hash(path), "bytes": path.stat().st_size,
                                 "input_fields": []}
        if field not in self.sources[key]["input_fields"]:
            self.sources[key]["input_fields"].append(field)
        return path

    def document(self, path, field):
        path = self.source(path, field)
        return _checked(field, path, lambda: versioned(read_json(path), field))

    def project(self):
        if self.project_dir is None:
            raise StageInputError("--project", None, "This stage requires a project directory")
        return self.document(self.project_dir / "project.yaml", "project")

    def project_document(self, field):
        project = self.project()
        path = _checked(f"project.{field}", self.project_dir / "project.yaml",
                        lambda: self.project_dir / project[field])
        return self.document(path, field), path


def check_project(ctx):
    project = ctx.project()
    inputs = {}
    for field in ("materials", "process", "simulation"):
        _, path = ctx.project_document(field)
        inputs[field] = str(path.resolve())
    from .schemas import simulation_config
    simulation, path = ctx.project_document("simulation")
    simulation = _checked("simulation", path, lambda: simulation_config(simulation))
    return {"name": project.get("name"), "input_files": inputs,
            "backend": simulation["backend"], "lambda_vacuum_nm": simulation["lambda_vacuum_nm"],
            "scope": "File/schema/config check; no FDTD environment probe or solver run"}


def check_materials(ctx):
    from .materials import evaluate_library
    from .service import refresh_material_sources
    library, path = ctx.project_document("materials")
    from .structure_import import is_imported, imported_model
    from .service import load_project
    if is_imported(ctx.project()):
        data = load_project(ctx.project_dir); model = imported_model(data)
        values = evaluate_library(model["materials"], data["simulation"]["lambda_vacuum_nm"])
        return {"scope":"Verified immutable material snapshot; raw source paths are provenance only", "materials":values,
                "lambda_vacuum_nm":data["simulation"]["lambda_vacuum_nm"],"library_hash":canonical_hash(model["materials"]),"warnings":[]}
    simulation, _ = ctx.project_document("simulation")
    evaluated, failures, warnings, refreshed = {}, [], [], {}
    for name, spec in library.get("materials", {}).items():
        source_text = str(spec.get("source", ""))
        source = Path(source_text)
        basename = Path(source_text.replace("\\", "/")).name
        local = ctx.project_dir / "materials" / basename if basename else None
        relative = ctx.project_dir / source
        if local is not None and local.is_file():
            source = local
        elif relative.is_file():
            source = relative
        if source.is_file():
            ctx.source(source, f"materials.{name}.source")
        else:
            warnings.append(f"{name}: original TXT unavailable; checking embedded table snapshot")
        try:
            single = refresh_material_sources({**library, "materials": {name: spec}}, project_dir=ctx.project_dir)
            spec = single["materials"][name]
            refreshed[name] = spec
            evaluated[name] = evaluate_library({**library, "materials": {name: spec}},
                                               simulation["lambda_vacuum_nm"])[name]
        except Exception as exc:
            failures.append((name, exc, source if source.is_file() else path))
    if not library.get("materials"):
        raise StageInputError("materials.materials", path, "Material library is empty")
    if failures:
        first_name, first_error, first_source = failures[0]
        message = "\n".join(f"materials.{name}: {error}" for name, error, _ in failures)
        raise StageInputError(f"materials.{first_name}", first_source, message) from first_error
    return {"scope": "All library materials at the selected wavelength, including unused entries",
            "lambda_vacuum_nm": simulation["lambda_vacuum_nm"], "materials": evaluated,
            "library_hash": canonical_hash({**library, "materials": refreshed}), "warnings": warnings}


def check_geometry(ctx):
    from .geometry import build_geometry
    from .schemas import validate_geometry
    from .structure_import import is_imported, imported_model
    if is_imported(ctx.project()):
        from .service import load_project
        model = imported_model(load_project(ctx.project_dir)); geometry = model["geometry"]
        ctx.source(model["manifest_path"], "geometry_input.manifest")
        for name, member in model["files"].items(): ctx.source(member, "geometry_input."+name)
        return {"representation":geometry["representation"], "units":geometry["units"],
            "cell_bounds_um":geometry["cell_bounds_um"],"geometry_hash":canonical_hash(geometry),
            "scope":"Verified imported snapshot; process not rebuilt; not a solver compatibility check"}
    recipe, path = ctx.project_document("process")
    for i, step in enumerate(recipe.get("steps", [])):
        if step.get("enabled", True) and step.get("mask", {}).get("gds_path"):
            ctx.source(ctx.project_dir / step["mask"]["gds_path"], f"process.steps[{i}].mask.gds_path")
    geometry = _checked("process.steps / cell_bounds_um / period_x_um", path,
                        lambda: validate_geometry(build_geometry(recipe, base_dir=ctx.project_dir)))
    return {"representation": geometry["representation"], "units": geometry["units"],
            "period_x_um": geometry["period_x_um"], "cell_bounds_um": geometry["cell_bounds_um"],
            "solids": len(geometry["solids"]), "materials": sorted({geometry["background_material"]} |
                                                       {s["material"] for s in geometry["solids"]}),
            "invariance_evidence": geometry.get("invariance_evidence"),
            "geometry_input_hash": geometry.get("input_hash"),
            "geometry_hash": canonical_hash(geometry),
            "scope": "Actual in-memory GDS/process build; no project snapshots written; not an FDTD check"}


def _scattering(ctx, path=None, field="--scattering"):
    from .scattering import load_scattering, validate_scattering
    path = path or ctx.scattering
    if path is None:
        raise StageInputError(field, None, "Select an existing scattering.h5")
    path = ctx.source(path, field)
    result = _checked(field, path, lambda: validate_scattering(load_scattering(path), require_corrected=True))
    meta = result["metadata"]
    if meta.get("generation_method") != "native_fdtd":
        raise StageInputError(f"{field}.metadata.generation_method", path,
                              "Production diagnostics require saved native_fdtd output; test fixtures are rejected")
    if meta.get("time_converged") is not True:
        raise StageInputError(f"{field}.metadata.time_converged", path,
                              "DFT is not time-converged; extend the original solve before accepting time-converged results")
    return result


def check_scattering(ctx):
    result = _scattering(ctx)
    meta = result["metadata"]
    return {"orders": len(result["orders_mn"]), "metadata": meta,
            "total_reflected_power_by_input": result["eta_by_input"].sum(axis=0).tolist(),
            "scope": "Saved array/basis/normalization contract; does not rerun FDTD or certify grid convergence"}


CHECKS = dict(zip(STAGES, (check_project, check_materials, check_geometry, check_scattering)))


def diagnose(*, stage="all", project=None, scattering=None, output=None):
    """Return and save an auditable report, including failures and traceback.

    ``output`` is a report *parent*: every call creates a new child, never
    overwriting previous evidence. ``all`` checks available inputs; explicit
    individual stages report missing required inputs as failures.
    """
    if stage not in (*STAGES, "all"):
        raise ValueError(f"Unknown stage {stage!r}; choose {', '.join(STAGES)} or all")
    started = time.perf_counter()
    stamp = datetime.now(timezone.utc)
    parent = Path(output).resolve() if output else ROOT / "outputs" / "diagnostics"
    directory = parent / (stamp.strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    ctx = DiagnosticContext(project, scattering, directory)
    versions = {"python": platform.python_version(), "python_executable": sys.executable,
                "platform": platform.platform(), "solver": "native_fdtd; no FDTD started by this diagnostic"}
    for name in ("numpy", "scipy", "h5py", "gdstk", "matplotlib", "fdtd"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "not installed in this interpreter"
    report = {"schema_version": "1.0", "diagnostic_version": "stage-debug-1.0", "status": "running",
              "generation_method": "local_stage_diagnostic", "report_dir": str(directory),
              "started_at_utc": stamp.isoformat(),
              "units": {"geometry": "um", "wavelength": "nm", "phase": "rad", "elapsed": "seconds"},
              "software_versions": versions, "stages": [], "code_sha256": {}}
    selected = STAGES if stage == "all" else (stage,)
    for name in selected:
        entry = {"stage": name}
        if stage == "all" and ((name in STAGES[:3] and not project) or
                               (name in STAGES[3:] and not scattering)):
            entry.update(status="skipped", reason="No project supplied" if name in STAGES[:3] else "No scattering supplied")
            report["stages"].append(entry)
            continue
        stage_started = time.perf_counter()
        for module in ("debugging.py", *STAGE_MODULES[name]):
            report["code_sha256"][module] = file_hash(Path(__file__).with_name(module))
        try:
            entry.update(status="passed", details=CHECKS[name](ctx))
        except Exception as exc:
            trace = directory / f"{name}_traceback.txt"
            trace.write_text(traceback.format_exc(), encoding="utf-8")
            entry.update(status="failed", error={"type": type(exc).__name__, "message": str(exc),
                         "input_field": getattr(exc, "field", name), "source_file": getattr(exc, "source", None),
                         "traceback_file": str(trace)})
        entry["elapsed_seconds"] = time.perf_counter() - stage_started
        report["stages"].append(entry)
    report["sources"] = list(ctx.sources.values())
    active = [item for item in report["stages"] if item["status"] != "skipped"]
    report["status"] = "passed" if active and all(item["status"] == "passed" for item in active) else "failed"
    if not active:
        report["error"] = "No stage was checked. Supply --project and/or --scattering."
    report["elapsed_seconds"] = time.perf_counter() - started
    write_json(directory / "report.json", report)
    lines = [f"MarkSim diagnostic: {report['status']}", f"Output: {directory}",
             "No FDTD was launched. All source files were opened read-only."]
    for item in report["stages"]:
        lines.append(f"[{item['status']}] {item['stage']}: " + item.get("error", {}).get("message", item.get("reason", "OK")))
    (directory / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonable(report)


def add_arguments(parser):
    """Use on either a standalone parser or the main CLI's debug subparser."""
    parser.add_argument("--stage", choices=(*STAGES, "all"), default="all")
    parser.add_argument("--project", help="Project directory or project.yaml")
    parser.add_argument("--scattering", help="Existing, calibrated scattering.h5")
    parser.add_argument("--output", help="Report parent; a unique child directory is always created")
    return parser


def run_from_args(args):
    try:
        report = diagnose(stage=args.stage, project=args.project, scattering=args.scattering,
                          output=args.output)
    except Exception as exc:
        print(f"Diagnostic could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="MarkSim independent stage checks; never launches the FDTD solver")
    return run_from_args(add_arguments(parser).parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
