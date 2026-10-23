"""Auditable optical material library for isotropic, nonmagnetic materials.

Wavelengths are vacuum wavelengths in nm.  The convention is n_tilde=n+i*k
with k>=0 for absorption, and relative epsilon=(n+i*k)**2 (mu_r=1).
No table or Sellmeier model is extrapolated beyond its declared range.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np


DEFAULT_LIBRARY = Path(__file__).resolve().parents[1] / "materials" / "library.json"


class MaterialLibrary:
    def __init__(self, path=None, definitions=None):
        self.path = Path(path).resolve() if path is not None else DEFAULT_LIBRARY
        self.base_dir = self.path.parent
        if definitions is None:
            with self.path.open(encoding="utf-8-sig") as stream:
                definitions = json.load(stream)
        self.document = copy.deepcopy(definitions)
        if self.document.get("schema_version", 1) != 1:
            raise ValueError("Unsupported material-library schema_version")
        self.materials = self.document.get("materials", {})
        if not self.materials:
            raise ValueError("The material library is empty")
        self._tables = {}
        for name in self.materials:
            self.validate_material(name)

    @property
    def names(self):
        return list(self.materials)

    def metadata(self, name):
        self.validate_material(name)
        return copy.deepcopy(self.materials[name])

    def validate_material(self, name):
        if name not in self.materials:
            raise ValueError(f"Unknown material {name!r}; available: {', '.join(self.names)}")
        spec = self.materials[name]
        model = spec.get("model")
        if model == "constant":
            if not np.isfinite(spec.get("n", np.nan)) or spec["n"] <= 0:
                raise ValueError(f"{name}: constant n must be finite and >0")
            if not np.isfinite(spec.get("k", 0)) or spec.get("k", 0) < 0:
                raise ValueError(f"{name}: k must be finite and >=0")
        elif model == "tabulated":
            self._table(name)
        elif model == "sellmeier":
            b = np.asarray(spec.get("B", []), dtype=float)
            c = np.asarray(spec.get("C_um2", []), dtype=float)
            if not b.size or b.shape != c.shape or not np.all(np.isfinite([b, c])):
                raise ValueError(f"{name}: Sellmeier B and C_um2 must be equal finite lists")
            if "valid_range_nm" not in spec:
                raise ValueError(f"{name}: Sellmeier requires valid_range_nm")
        elif model == "meep_dispersion":
            if not np.isfinite(spec.get("epsilon_inf", np.nan)) or spec["epsilon_inf"] <= 0:
                raise ValueError(f"{name}: epsilon_inf must be positive")
            if "valid_range_nm" not in spec:
                raise ValueError(f"{name}: an explicit dispersion model needs valid_range_nm")
            for pole in spec.get("electric_susceptibilities", []):
                values = [pole.get("frequency_um_inv", np.nan), pole.get("gamma_um_inv", np.nan), pole.get("sigma", np.nan)]
                if pole.get("kind") not in ("lorentz", "drude") or not np.all(np.isfinite(values)):
                    raise ValueError(f"{name}: invalid Drude/Lorentz pole")
                if values[0] <= 0 or values[1] < 0 or values[2] < 0:
                    raise ValueError(f"{name}: passive pole requires frequency>0, gamma>=0, sigma>=0")
        else:
            raise ValueError(f"{name}: unsupported material model {model!r}")
        interval = spec.get("valid_range_nm")
        if interval is not None:
            if len(interval) != 2 or not np.all(np.isfinite(interval)) or not 0 < interval[0] < interval[1]:
                raise ValueError(f"{name}: invalid valid_range_nm")
        return spec

    def _table(self, name):
        if name in self._tables:
            return self._tables[name]
        spec = self.materials[name]
        if "data" in spec:
            data = np.asarray(spec["data"], dtype=float)
        else:
            source = self.base_dir / spec.get("file", "")
            if not source.is_file():
                raise ValueError(f"{name}: material CSV does not exist: {source}")
            table = np.genfromtxt(source, delimiter=",", names=True, encoding="utf-8-sig")
            if table.dtype.names is None or not {"wavelength_nm", "n", "k"}.issubset(table.dtype.names):
                raise ValueError(f"{name}: CSV needs wavelength_nm,n,k columns")
            data = np.column_stack([np.atleast_1d(table[key]) for key in ("wavelength_nm", "n", "k")])
        if data.ndim != 2 or data.shape[1] != 3 or data.shape[0] < 2:
            raise ValueError(f"{name}: a table requires at least two wavelength,n,k rows")
        if not np.all(np.isfinite(data)) or np.any(np.diff(data[:, 0]) <= 0):
            raise ValueError(f"{name}: tabulated wavelengths must be finite and strictly increasing")
        if np.any(data[:, 0] <= 0) or np.any(data[:, 1:] < 0):
            raise ValueError(f"{name}: require wavelength>0 and passive n,k>=0")
        self._tables[name] = data
        return data

    def evaluate(self, name, wavelengths_nm):
        spec = self.validate_material(name)
        wavelengths = np.atleast_1d(np.asarray(wavelengths_nm, dtype=float))
        if not np.all(np.isfinite(wavelengths)) or np.any(wavelengths <= 0):
            raise ValueError("Vacuum wavelength_nm must be finite and >0")
        interval = spec.get("valid_range_nm")
        if interval and (np.any(wavelengths < interval[0]) or np.any(wavelengths > interval[1])):
            raise ValueError(f"{name}: wavelength outside valid range {interval} nm; extrapolation is disabled")
        model = spec["model"]
        if model == "constant":
            n = np.full_like(wavelengths, float(spec["n"]))
            k = np.full_like(wavelengths, float(spec.get("k", 0)))
        elif model == "tabulated":
            table = self._table(name)
            if np.any(wavelengths < table[0, 0]) or np.any(wavelengths > table[-1, 0]):
                raise ValueError(f"{name}: wavelength outside tabulated range [{table[0, 0]}, {table[-1, 0]}] nm; extrapolation is disabled")
            n = np.interp(wavelengths, table[:, 0], table[:, 1])
            k = np.interp(wavelengths, table[:, 0], table[:, 2])
        elif model == "sellmeier":
            wave_um2 = (wavelengths / 1000) ** 2
            epsilon = np.ones_like(wavelengths)
            for b, c in zip(spec["B"], spec["C_um2"]):
                denominator = wave_um2 - c
                if np.any(np.isclose(denominator, 0, atol=1e-14, rtol=0)):
                    raise ValueError(f"{name}: wavelength is at a Sellmeier pole")
                epsilon += b * wave_um2 / denominator
            if np.any(epsilon <= 0) or not np.all(np.isfinite(epsilon)):
                raise ValueError(f"{name}: invalid transparent Sellmeier evaluation")
            n, k = np.sqrt(epsilon), np.zeros_like(wavelengths)
        else:
            f = 1000 / wavelengths
            eps = np.full_like(wavelengths, spec["epsilon_inf"], dtype=complex)
            for pole in spec.get("electric_susceptibilities", []):
                f0, gamma, sigma = (pole[key] for key in ("frequency_um_inv", "gamma_um_inv", "sigma"))
                denominator = -f * f - 1j * gamma * f
                if pole["kind"] == "lorentz":
                    denominator += f0 * f0
                eps += sigma * f0 * f0 / denominator
            nk = np.sqrt(eps.astype(complex))
            n, k = nk.real, nk.imag
        epsilon = (n + 1j * k) ** 2
        if not np.all(np.isfinite(epsilon)):
            raise ValueError(f"{name}: non-finite permittivity (check poles and wavelength)")
        return {"wavelength_nm": wavelengths, "n": n, "k": k,
                "epsilon_real": epsilon.real, "epsilon_imag": epsilon.imag}

    def snapshot(self):
        """A portable JSON document including tabulated values and provenance."""
        document = copy.deepcopy(self.document)
        for name, spec in document["materials"].items():
            if spec["model"] == "tabulated":
                spec["data"] = self._table(name).tolist()
                spec.pop("file", None)
        return document

    def meep_spec(self, name, wavelength_nm, broadband=False):
        """JSON-ready isotropic Medium parameters in length units of 1 um."""
        spec = self.validate_material(name)
        optical = self.evaluate(name, wavelength_nm)
        if spec["model"] == "meep_dispersion":
            return {"mode": "dispersive", "epsilon_inf": spec["epsilon_inf"],
                    "electric_susceptibilities": copy.deepcopy(spec.get("electric_susceptibilities", [])),
                    "valid_range_nm": spec["valid_range_nm"]}
        if broadband and spec["model"] != "constant":
            raise ValueError(f"{name}: broadband Meep requires an explicit fitted Drude/Lorentz model; {spec['model']} n,k data are not a time-domain dispersion model")
        er, ei = float(optical["epsilon_real"][0]), float(optical["epsilon_imag"][0])
        if er <= 0:
            raise ValueError(f"{name}: epsilon_real={er:g} <=0; use a stable explicit Drude/Lorentz model for Meep")
        if broadband and ei != 0:
            raise ValueError(f"{name}: constant absorbing n,k cannot be represented as broadband constant complex epsilon in Meep; supply a Drude/Lorentz model")
        conductivity = 2 * np.pi * (1000 / wavelength_nm) * ei / er
        return {"mode": "narrowband", "epsilon_real": er,
                "D_conductivity": float(conductivity), "reference_wavelength_nm": float(wavelength_nm)}
