"""Optical tables for geometry exchange, independent of any FDTD limitation."""
from __future__ import annotations
import copy
import numpy as np


def validate_library(library, *, require_known_k=False):
    if library.get('schema_version') != '1.0' or not library.get('materials'):
        raise ValueError('Material library requires schema 1.0 and named optical tables')
    for name, spec in library['materials'].items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError('Material names must be nonempty strings')
        w, n = np.asarray(spec.get('wavelength_nm'), float), np.asarray(spec.get('n'), float)
        if (w.ndim != 1 or len(w) < 2 or n.shape != w.shape or not np.isfinite([w,n]).all()
                or np.any(w <= 0) or np.any(n <= 0) or np.any(np.diff(w) <= 0)):
            raise ValueError(f'{name}: finite increasing wavelength_nm and positive n are required')
        if type(spec.get('assume_lossless', False)) is not bool:
            raise ValueError(f'{name}: assume_lossless must be explicitly boolean')
        if spec.get('k') is None:
            if require_known_k and not spec.get('assume_lossless', False):
                raise ValueError(f'{name}: k unknown; explicitly confirm the lossless approximation or supply k')
        else:
            k = np.asarray(spec['k'], float)
            if k.shape != w.shape or not np.isfinite(k).all() or np.any(k < 0):
                raise ValueError(f'{name}: passive finite k table required')
    return library


def evaluate(library, wavelength_nm):
    """Interpolate n/k, never extrapolate; negative real epsilon is valid here."""
    validate_library(library, require_known_k=True)
    wavelength = float(wavelength_nm)
    if not np.isfinite(wavelength) or wavelength <= 0:
        raise ValueError('Wavelength must be positive and finite')
    result = {}
    for name, spec in library['materials'].items():
        w = np.asarray(spec['wavelength_nm'])
        if not w[0] <= wavelength <= w[-1]:
            raise ValueError(f'{name}: wavelength outside material data; extrapolation disabled')
        n = float(np.interp(wavelength, w, spec['n']))
        k = float(np.interp(wavelength, w, spec['k'])) if spec.get('k') is not None else 0.
        eps = complex(n,k)**2
        result[name] = dict(n=n,k=k,epsilon_real=eps.real,epsilon_imag=eps.imag,
                            lambda_vacuum_nm=wavelength,lossless_assumption=spec.get('k') is None)
    return result


def freeze_library(library):
    """Preserve provenance but prohibit readers following historical TXT paths."""
    result = copy.deepcopy(validate_library(library))
    for spec in result['materials'].values():
        if 'source' in spec:
            spec['original_source'] = spec.pop('source')
        if 'import' in spec:
            spec['original_import'] = spec.pop('import')
    result['snapshot_policy'] = 'embedded numeric tables; external source paths are provenance only'
    return result
