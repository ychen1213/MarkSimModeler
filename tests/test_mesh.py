import struct
import numpy as np
import pytest
from markmodel.mesh import prism_mesh,validate_mesh,solid_meshes,write_binary_stl
from markmodel.service import demo_materials
from marksim.voxel import create_voxel_model,geometry_manifest


def test_concave_prism_closed_volume_and_stl(tmp_path):
    s={'name':'L shape','material':'RIDGE_DEMO','polygon_xy_um':[[0,0],[2,0],[2,1],[1,1],[1,2],[0,2]],'z_min_um':-.3,'z_max_um':.2}
    mesh=prism_mesh(s);report=validate_mesh(mesh)
    assert report['volume_um3']==pytest.approx(1.5)
    path=tmp_path/'shape.stl';write_binary_stl(mesh,path)
    raw=path.read_bytes();n=struct.unpack('<I',raw[80:84])[0]
    assert len(raw)==84+50*n
    with pytest.raises(FileExistsError):write_binary_stl(mesh,path)
    broken={**mesh,'triangles':mesh['triangles'][:-1]}
    with pytest.raises(ValueError,match='open'):validate_mesh(broken)


def test_voxel_surfaces_contain_all_materials_and_hole_volume():
    # A two-cell shell keeps the material corner separate from the inner void.
    # A one-cell shell here pinches at one vertex and is rightly non-manifold.
    ids=np.ones((6,6,6),np.uint16);ids[2:4,2:4,2:4]=0
    ids[0,0,0]=2
    v=create_voxel_model(ids,[-.1,-.2,-.3],[.1,.2,.3],{0:'AIR_DEMO',1:'SUBSTRATE_DEMO',2:'RIDGE_DEMO'})
    m={'geometry':geometry_manifest(v,'voxels.h5'),'voxels':v,'materials':demo_materials()}
    meshes=solid_meshes(m)
    assert set(mesh['material'] for mesh in meshes)=={'SUBSTRATE_DEMO','RIDGE_DEMO'}
    for name,label in [('SUBSTRATE_DEMO',1),('RIDGE_DEMO',2)]:
        actual=sum(validate_mesh(mesh)['volume_um3'] for mesh in meshes if mesh['material']==name)
        assert actual==pytest.approx(np.count_nonzero(ids==label)*.006)
    with pytest.raises(ValueError,match='max_triangles'):solid_meshes(m,max_triangles=3)


def test_disconnected_voxel_bodies_are_separate_closed_objects():
    ids=np.zeros((3,3,3),np.uint16);ids[0,0,0]=2;ids[1,1,1]=2
    v=create_voxel_model(ids,[0,0,0],[1,1,1],{0:'AIR_DEMO',2:'RIDGE_DEMO'})
    meshes=solid_meshes({'geometry':geometry_manifest(v,'voxels.h5'),'voxels':v,'materials':demo_materials()})
    assert len(meshes)==2
    assert sum(validate_mesh(m)['volume_um3'] for m in meshes)==pytest.approx(2)
