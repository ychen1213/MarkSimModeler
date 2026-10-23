"""Portable, hashed geometry + optical material snapshots; never executable data."""
from __future__ import annotations
import copy
from pathlib import Path, PureWindowsPath
from marksim.schemas import read_json, write_json, file_hash
from marksim.geometry import validate_geometry
from marksim.voxel import validate_voxels, save_voxels, load_voxels, geometry_manifest, create_voxel_model
from .materials import validate_library, freeze_library

FORMAT = 'marksim-structure-bundle'


def validate_model(model):
    g = model['geometry']
    validate_library(model['materials'])
    if g.get('representation') == 'parametric_solids':
        validate_geometry(g)
        from .mesh import triangulate_polygon
        for solid in g['solids']:
            triangulate_polygon(solid['polygon_xy_um'])
        used = {g['background_material']} | {s['material'] for s in g['solids']}
    elif g.get('representation') == 'voxel_labels':
        v = validate_voxels(model['voxels'])
        canonical=create_voxel_model(v['material_id'],v['origin_um'],v['spacing_um'],v['material_map'],
                                     background_id=v['background_id'],provenance=v.get('provenance'))
        expected=geometry_manifest(canonical,g.get('voxel_file','voxels.h5'))
        for key,value in expected.items():
            if key=='voxel_file':continue
            if g.get(key) != value:
                raise ValueError(f'Geometry manifest and voxel data disagree: {key}')
        used = set(v['material_map'].values())
    else:
        raise ValueError('Unsupported geometry representation; no silent conversion')
    missing = used - set(model['materials']['materials'])
    if missing:
        raise ValueError(f'Missing optical material definitions: {sorted(missing)}')
    return model


def safe_member(root, value):
    """Require one bundle-relative regular file, including Windows path semantics."""
    if not isinstance(value,str) or not value or '\\' in value:
        raise ValueError('Bundle member must be a forward-slash relative path')
    p = Path(value)
    if p.is_absolute() or PureWindowsPath(value).drive or '..' in p.parts:
        raise ValueError('Bundle path must stay inside its own directory')
    result = (root / p).resolve()
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise ValueError(f'Bundle member missing or escapes directory: {value}')
    return result


def write_bundle(model, output_dir):
    validate_model(model)
    out = Path(output_dir).resolve()
    if out.exists():
        raise FileExistsError(f'Existing export preserved: {out}')
    out.mkdir(parents=True)
    geometry = copy.deepcopy(model['geometry'])
    files = ['geometry.json','materials.json']
    if geometry['representation'] == 'voxel_labels':
        save_voxels(model['voxels'], out/'voxels.h5')
        # The validated manifest already describes these exact labels. Keep
        # finite-array and reconstruction metadata while relocating only the
        # payload path; regenerating it would erase declared physical extent.
        geometry['voxel_file'] = 'voxels.h5'
        files.append('voxels.h5')
    write_json(out/'geometry.json', geometry)
    write_json(out/'materials.json', freeze_library(model['materials']))
    manifest = dict(schema_version='1.0',format=FORMAT,units='um',
                    coordinate_convention='x=scan,y=stripe,z=up',
                    geometry_file='geometry.json', material_library_file='materials.json',
                    files_sha256={name:file_hash(out/name) for name in files},
                    provenance=copy.deepcopy(model.get('source',{})))
    # Publish the entry point last. Partial directories are never complete bundles.
    write_json(out/'structure.json',manifest)
    return {'manifest':str(out/'structure.json'),'directory':str(out),'files':files}


def read_bundle(path):
    path = Path(path).resolve()
    if path.is_dir(): path = path/'structure.json'
    spec = read_json(path)
    if spec.get('schema_version') != '1.0' or spec.get('format') != FORMAT or spec.get('units') != 'um':
        raise ValueError('Not a supported MarkSim structure bundle')
    if spec.get('coordinate_convention') != 'x=scan,y=stripe,z=up':
        raise ValueError('Unrecognized physical coordinate convention')
    hashes = spec.get('files_sha256')
    if not isinstance(hashes,dict) or not hashes:
        raise ValueError('Missing bundle checksum inventory')
    files = {}
    for relative, digest in hashes.items():
        files[relative] = safe_member(path.parent,relative)
        if file_hash(files[relative]) != digest:
            raise ValueError(f'Bundle checksum mismatch: {relative}')
    for key in ('geometry_file','material_library_file'):
        if spec.get(key) not in files:
            raise ValueError(f'Unhashed required bundle input: {key}')
    g = read_json(files[spec['geometry_file']])
    voxels = None
    if g.get('representation') == 'voxel_labels':
        if g.get('voxel_file') not in files:
            raise ValueError('Voxel data not covered by bundle checksum')
        voxels = load_voxels(files[g['voxel_file']])
    return validate_model(dict(geometry=g, materials=read_json(files[spec['material_library_file']]),
                               voxels=voxels,source={'bundle':str(path),'manifest_sha256':file_hash(path),
                                                   'original':spec.get('provenance',{})}))
