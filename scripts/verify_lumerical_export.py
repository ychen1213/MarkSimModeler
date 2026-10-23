"""Read back generated LSF in a new hidden FDTD session; never runs a solver.

Requires an existing licensed Lumerical install with lumapi. The supplied export
is executed twice in this script's private session. Only the output directory is
written, never the input bundle, exported files, or a user's open project.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
np = None  # Imported inside the reporting boundary so a missing dependency is recorded.


def json_value(value):
    if isinstance(value, np.ndarray):
        if np.iscomplexobj(value):
            return {'real': value.real.tolist(), 'imag': value.imag.tolist()}
        return value.tolist()
    if isinstance(value, np.generic): return value.item()
    raise TypeError(type(value).__name__)


def verify_objects(session, manifest, group, stl=False):
    """Check count, SI dimensions, material binding, and overlap priority."""
    result = []
    expected = manifest['stl'] if stl else [manifest['background_object'], *manifest['objects']]
    session.groupscope('::model::' + group)
    session.selectall()
    count = int(session.getnumber())
    session.unselectall()
    session.groupscope('::model')
    assert count == len(expected), (count, len(expected))
    by_material = {m['source_name']:m for m in manifest['materials']}
    for obj in expected:
        full = '::model::' + group + '::' + obj['object_name']
        centre = np.array([float(session.getnamed(full, axis)) for axis in 'xyz'])
        if stl or obj.get('primitive') in ('addpoly','addplanarsolid'):
            vertices = np.asarray(session.getnamed(full, 'vertices'))
            if vertices.shape[1] == 2:
                xy = vertices + centre[:2]
                halfz = float(session.getnamed(full,'z span'))/2
                bounds = np.array([[xy[:,0].min(),xy[:,0].max()],
                                   [xy[:,1].min(),xy[:,1].max()],
                                   [centre[2]-halfz,centre[2]+halfz]])
            else:
                xyz = vertices + centre
                bounds = np.array([xyz.min(axis=0),xyz.max(axis=0)]).T
        else:
            span=np.array([float(session.getnamed(full,axis+' span')) for axis in 'xyz'])
            bounds=np.array([centre-span/2,centre+span/2]).T
        goal = np.asarray(obj['bounds_um'])*1e-6
        np.testing.assert_allclose(bounds, goal, rtol=1e-9, atol=2e-15)
        record = by_material[obj['material']]
        material = record.get('builtin_name') if record['mode']=='builtin' else group+'_'+record['suffix']
        actual_material = session.getnamed(full, 'material')
        assert actual_material == material, (actual_material, material)
        mesh_order = int(session.getnamed(full, 'mesh order'))
        assert mesh_order == obj['mesh_order']
        result.append({'object_name':obj['object_name'], 'bounds_m':bounds,
                       'expected_bounds_m':goal, 'material':actual_material,
                       'mesh_order':mesh_order, 'passed':True})
    return {'count':count, 'objects':result}


def verify_materials(session, manifest, group):
    result = []
    for mat in manifest['materials']:
        if mat['mode'] != 'sampled_nk': continue
        name = group+'_'+mat['suffix']
        actual = np.asarray(session.getmaterial(name, 'sampled data'))
        expected = np.column_stack([mat['frequency_hz'], np.asarray(mat['epsilon_real'])+1j*np.asarray(mat['epsilon_imag'])])
        np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-12)
        lo = float(session.getmaterial(name, 'wavelength min'))
        hi = float(session.getmaterial(name, 'wavelength max'))
        np.testing.assert_allclose([lo,hi], np.asarray(mat['wavelength_range_nm'])*1e-9, rtol=1e-12)
        result.append({'material':name, 'sampled_data':actual, 'fit_range_m':[lo,hi], 'passed':True})
    return result


def execute(session, export, entry, output, tag, manifest):
    """Execute unmodified bytes via eval and compare independently read values."""
    code = (export/entry).read_text(encoding='utf-8-sig')
    # STL entry is intentionally relative to its export folder.
    session.cd(str(export).replace('\\','/'))
    start = time.monotonic()
    session.eval(code)
    group = str(session.getv('mm_group'))
    result = {'entry':str(export/entry), 'group':group,
              'script_sha256':hashlib.sha256((export/entry).read_bytes()).hexdigest(),
              'geometry':verify_objects(session, manifest, group, stl=(entry=='import_stl.lsf')),
              'materials':verify_materials(session, manifest, group)}
    session.save(str(output/(tag+'.fsp')))
    result.update(passed=True, elapsed_seconds=time.monotonic()-start)
    return result


def voxel_example(output, repo):
    sys.path.insert(0, str(repo/'src'))
    from markmodel.lumerical import export_lumerical
    from marksim.voxel import create_voxel_model, geometry_manifest
    labels = np.zeros((3,3,3), dtype=np.uint16)
    labels[0,0,0] = labels[1,0,0] = labels[1,1,0] = labels[1,1,1] = 1
    vox = create_voxel_model(labels, [2.,3.,4.], [.1,.2,.3], {0:'air',1:'absorber'})
    materials = {'schema_version':'1.0','materials':{
        'air': {'wavelength_nm':[400.,600.,1000.], 'n':[1.,1.,1.], 'k':[0.,0.,0.], 'k_status':'provided','assume_lossless':False},
        'absorber': {'wavelength_nm':[400.,600.,1000.], 'n':[.2,.3,.4], 'k':[3.,3.,3.], 'k_status':'provided','assume_lossless':False}}}
    model = {'geometry':geometry_manifest(vox,'voxels.h5'), 'voxels':vox,'materials':materials,
             'source':{'kind':'synthetic_runtime_geometry_test', 'not_measurement':True}}
    out = output/'voxel_export'
    export_lumerical(model,out)
    return out


def main():
    global np
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--install',type=Path,required=True)
    p.add_argument('--export',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--repo',type=Path,help='Also generate and validate a 3D voxel export using this source checkout')
    args=p.parse_args()
    args.install=args.install.resolve()
    args.export=args.export.resolve()
    args.output=args.output.resolve()
    if args.repo: args.repo=args.repo.resolve()
    args.output.mkdir(parents=True,exist_ok=False)
    sys.path.insert(0,str(args.install/'api'/'python'))
    manifest=json.loads((args.export/'lumerical_manifest.json').read_text(encoding='utf-8'))
    report={'install':str(args.install),'python':sys.executable,'target_version':manifest['target_version'],
            'target_runtime_verified':False, 'target_version_match':'not asserted',
            'runtime_verified_scope':'Only the installed runtime_version reported below; target 2023 R2.1 compatibility is not asserted',
            'scope':'Actual geometry/material construction and readback, no FDTD solve or fit accuracy validation',
            'cases':[], 'passed':False}
    report_path=args.output/'runtime_validation.json'
    def save():report_path.write_text(json.dumps(report,default=json_value,ensure_ascii=False,indent=2),encoding='utf-8')
    def run_case(session,export,entry,tag,meta):
        print('CASE_START',tag,flush=True)
        try:
            result=execute(session,export,entry,args.output,tag,meta)
        except Exception as ex:
            result={'passed':False,'error':repr(ex),'traceback':traceback.format_exc()}
        result['name']=tag
        report['cases'].append(result);save()
        print('CASE_END',tag,result['passed'], result.get('error',''),flush=True)
        return result
    try:
        import numpy as np
        import lumapi
        print('OPEN_INDEPENDENT_HIDDEN_FDTD',flush=True)
        with lumapi.FDTD(hide=True) as session:
            session.eval('probe_version=version;')
            report['runtime_version']=session.getv('probe_version');save()
            a=run_case(session,args.export,'main.lsf','main_first',manifest)
            b=run_case(session,args.export,'main.lsf','main_second',manifest)
            report['rerun_names_distinct']=bool(a.get('group') and a.get('group')!=b.get('group'))
            if a.get('passed') and b.get('passed'):
                report['first_group_after_rerun']=verify_objects(session,manifest,a['group'])
            session.newproject()
            if (args.export/'import_stl.lsf').is_file():
                run_case(session,args.export,'import_stl.lsf','stl',manifest)
            if args.repo:
                voxel=voxel_example(args.output,args.repo)
                voxel_manifest=json.loads((voxel/'lumerical_manifest.json').read_text(encoding='utf-8'))
                session.newproject()
                run_case(session,voxel,'main.lsf','voxel_planar',voxel_manifest)
                session.newproject()
                run_case(session,voxel,'import_stl.lsf','voxel_stl',voxel_manifest)
            report['passed']=all(c['passed'] for c in report['cases']) and report['rerun_names_distinct']
    except Exception as ex:
        report['session_error']=repr(ex);report['traceback']=traceback.format_exc()
    finally:save()
    print('REPORT',report_path,'PASSED',report['passed'],flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__': raise SystemExit(main())
