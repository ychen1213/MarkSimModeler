import copy
import json
import shutil
from pathlib import Path
import numpy as np
import pytest
from markmodel import service
from markmodel.exchange import write_bundle,read_bundle
from markmodel.materials import evaluate
from marksim.schemas import read_json,write_json


@pytest.mark.parametrize('family,count',[('AH32',2),('AH53',3),('AH74',4)])
def test_real_gds_build_portable_bundle(family,count,tmp_path):
    data=service.create_project(tmp_path/'project',family)
    result=service.build_project(data['folder'],previews=False)
    assert len(result['model']['geometry']['solids'])==count+1
    assert not (data['folder']/'simulation.yaml').exists()
    pack=write_bundle(result['model'],tmp_path/'export')
    moved=tmp_path/'moved with spaces';shutil.copytree(pack['directory'],moved)
    loaded=read_bundle(moved)
    assert loaded['geometry']==result['model']['geometry']
    assert 'source' not in loaded['materials']['materials']['RIDGE_DEMO']
    assert 'source' in result['model']['materials']['materials']['RIDGE_DEMO']
    with pytest.raises(FileExistsError):write_bundle(result['model'],moved)
    (moved/'geometry.json').write_text('{}')
    with pytest.raises(ValueError,match='checksum'):read_bundle(moved)


def test_bundle_rejects_unhashed_or_external_inputs(tmp_path):
    data=service.create_project(tmp_path/'project')
    model=service.build_project(data['folder'],previews=False)['model']
    write_bundle(model,tmp_path/'export')
    manifest=tmp_path/'export/structure.json';spec=read_json(manifest)
    spec['geometry_file']='../project/process.yaml';write_json(manifest,spec)
    with pytest.raises(ValueError,match='Unhashed'):read_bundle(manifest)
    spec['files_sha256']['../project/process.yaml']='bad';write_json(manifest,spec)
    with pytest.raises(ValueError,match='inside'):read_bundle(manifest)


def test_materials_allow_metal_without_solver_fallback():
    lib=service.demo_materials();lib['materials']['RIDGE_DEMO']['n']=[.5]*3;lib['materials']['RIDGE_DEMO']['k']=[3.]*3
    assert evaluate(lib,643)['RIDGE_DEMO']['epsilon_real']<0
    lib['materials']['RIDGE_DEMO']['k']=None
    with pytest.raises(ValueError,match='unknown'):evaluate(lib,643)
    lib['materials']['RIDGE_DEMO']['assume_lossless']=True
    assert evaluate(lib,643)['RIDGE_DEMO']['k']==0
    with pytest.raises(ValueError,match='outside'):evaluate(lib,1200)


def test_finite_replication_preserves_dimensions_and_original(tmp_path):
    service.create_project(tmp_path/'project')
    model=service.build_project(tmp_path/'project',previews=False)['model'];original=copy.deepcopy(model)
    repeated=service.repeat_model(model,3,2,10.)
    assert model==original
    g=repeated['geometry'];assert not g['invariant_y'] and g['periodic_x'] is False
    assert np.allclose(g['cell_bounds_um'][:2],[[-24,24],[-10,10]])
    assert len(g['solids'])==24
    from marksim.geometry import geometry_volume_by_material
    before=geometry_volume_by_material(model['geometry']);after=geometry_volume_by_material(g)
    for name in before:assert np.isclose(after[name],before[name]*30)


def test_voxel_round_trip_retains_cell_origin_and_material_ids(tmp_path):
    from marksim.voxel import create_voxel_model,geometry_manifest
    labels=np.zeros((3,4,5),np.uint16);labels[1:,1:3,1:4]=2
    v=create_voxel_model(labels,[.1,.2,-.3],[.02,.03,.04],{0:'AIR_DEMO',2:'RIDGE_DEMO'})
    model={'geometry':geometry_manifest(v,'voxels.h5'),'voxels':v,'materials':service.demo_materials()}
    write_bundle(model,tmp_path/'voxels');loaded=read_bundle(tmp_path/'voxels')
    assert np.array_equal(loaded['voxels']['material_id'],labels)
    assert loaded['voxels']['origin_um']==[.1,.2,-.3]
    assert loaded['voxels']['spacing_um']==[.02,.03,.04]


def test_edit_mark_and_save_preserves_revision(tmp_path):
    data=service.create_project(tmp_path/'project')
    original=read_json(data['folder']/'process.yaml')
    recipe=service.apply_mark_parameters(data['folder'],original,{'mark_type':'AH32','mother_period_um':12.,'cd_um':2.,'height_um':.2})
    saved=service.save_project(data['folder'],recipe,data['materials'])
    assert read_json(Path(saved['revision_dir'])/'process.yaml')==original
    built=service.build_project(data['folder'],previews=False)
    assert built['model']['geometry']['period_x_um']==12
    assert len(built['model']['geometry']['solids'])==3


def test_explicit_single_finite_unit_and_voxel_bundle_keep_boundary_semantics(tmp_path):
    from marksim.voxel import create_voxel_model,geometry_manifest
    service.create_project(tmp_path/'project')
    periodic=service.build_project(tmp_path/'project',previews=False)['model']
    finite=service.repeat_model(periodic,finite=True)
    assert finite['geometry']['finite_array']['count_x']==1
    assert finite['geometry']['periodic_x'] is False
    assert finite['geometry']['substrate_extend_bottom'] is False
    assert 'finite_array' not in periodic['geometry']
    v=create_voxel_model(np.ones((2,2,2),np.uint16),[0,0,0],[.1,.1,.1],{0:'AIR_DEMO',1:'RIDGE_DEMO'})
    model={'geometry':geometry_manifest(v,'old.h5'),'voxels':v,'materials':service.demo_materials()}
    finite=service.repeat_model(model,2,1)
    write_bundle(finite,tmp_path/'finite_voxels')
    loaded=read_bundle(tmp_path/'finite_voxels')
    assert loaded['geometry']['finite_array']==finite['geometry']['finite_array']
    assert loaded['geometry']['periodic_x'] is False
