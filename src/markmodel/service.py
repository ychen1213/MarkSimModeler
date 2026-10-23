"""Shared modeling operations for GUI and CLI; no electromagnetic solver import."""
from __future__ import annotations
import copy
from datetime import datetime,timezone
from pathlib import Path
import re
import uuid
import numpy as np
from marksim.schemas import read_json,write_json,file_hash
from marksim.geometry import build_geometry,save_geometry,preview_geometry
from marksim.voxel import (load_voxels,save_voxels,geometry_manifest,import_legacy_npz,
                           preview_voxels,voxelize_parametric,create_voxel_model)
from .materials import validate_library
from .exchange import read_bundle,validate_model,safe_member

ROOT=Path(__file__).resolve().parents[2]


def unique_folder(parent,prefix):
    label=re.sub(r'[^\w.-]+','_',str(prefix)).strip('._')[:60] or 'output'
    result=Path(parent)/(label+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:8])
    result.mkdir(parents=True,exist_ok=False)
    return result


def demo_materials():
    return {'schema_version':'1.0','generation_method':'explicit_synthetic_demo','materials':{
        name:{'wavelength_nm':[400.,643.,1000.],'n':[n]*3,'k':[0.]*3,'k_status':'provided',
              'assume_lossless':False,'source':'synthetic constant-index example, not measured data',
              'valid_range_nm':[400.,1000.]}
        for name,n in [('AIR_DEMO',1.),('SUBSTRATE_DEMO',1.5),('RIDGE_DEMO',2.)]}}


def create_project(folder,mark_type='AH53',parameters=None):
    from marksim.marks import default_mark_config,normalize_mark_config,write_mark_gds
    out=Path(folder).resolve()
    if out.exists():raise FileExistsError(f'Existing project preserved: {out}')
    cfg=normalize_mark_config({**default_mark_config(mark_type),**(parameters or {}),'mark_type':mark_type})
    out.mkdir(parents=True)
    binding=write_mark_gds(out/'inputs'/f'{mark_type}.gds',cfg)
    period=cfg['mother_period_um']; height=cfg['height_um']
    recipe={'schema_version':'1.0','period_x_um':period,'cell_bounds_um':[[-period/2,period/2],[-1,1],[-2,max(1.,height)]],
            'invariant_y':True,'background_material':'AIR_DEMO','substrate_material':'SUBSTRATE_DEMO',
            'substrate_extend_bottom':True,'mark':{**cfg,'target_step_id':'ridge','template_mask':binding},
            'steps':[{'id':'substrate','name':'衬底 DEMO','operation':'create_substrate','material':'SUBSTRATE_DEMO',
                      'z_min_um':-2.,'z_max_um':0.,'enabled':True,'sidewall_angle_deg':90},
                     {'id':'ridge','name':f'{mark_type} 光栅','operation':'add','material':'RIDGE_DEMO',
                      'mask':{'gds_path':f'inputs/{mark_type}.gds','cell':binding['cell'],'layer':1,'datatype':0},
                      'mask_polarity':'positive','z_min_um':0.,'thickness_um':height,'cd_bias_um':0.,
                      'delta_x_um':0.,'delta_y_um':0.,'rotation_deg':0.,'enabled':True,'sidewall_angle_deg':90}]}
    write_json(out/'process.yaml',recipe)
    write_json(out/'materials/library.json',demo_materials())
    write_json(out/'project.yaml',{'schema_version':'1.0','kind':'marksim-modeler','name':f'{mark_type}_MODELER_DEMO',
                                  'process':'process.yaml','materials':'materials/library.json',
                                  'description':'Synthetic modeling example; no solver configuration or optical predictions'})
    return load_project(out)


def load_project(folder):
    folder=Path(folder).resolve()
    if folder.is_file():folder=folder.parent
    if not (folder/'project.yaml').is_file():
        raise ValueError('请选择包含 project.yaml 的工程文件夹；单独结构包请从「SEM / TEM 与体素」加载。')
    project=read_json(folder/'project.yaml')
    if project.get('schema_version')!='1.0':raise ValueError('Unsupported project schema')
    # Existing MarkSim inputs can also be opened for modeling; no solver is loaded.
    process_path=safe_member(folder,project['process'])
    material_path=safe_member(folder,project['materials'])
    if process_path==material_path or process_path==folder/'project.yaml' or material_path==folder/'project.yaml':
        raise ValueError('Project input files must be distinct from each other and project.yaml')
    process=read_json(process_path);materials=read_json(material_path)
    validate_library(materials)
    if project.get('geometry_input',{}).get('kind')=='structure_bundle':
        raise ValueError('这是冻结结构的仿真工程。请从「SEM / TEM 与体素」加载其 inputs/structure/structure.json；不要编辑占位工艺。')
    if not isinstance(process,dict) or not isinstance(process.get('steps'),list):
        raise ValueError('工艺文件需要 steps 步骤列表。')
    ids=[]
    for step in process['steps']:
        if not isinstance(step,dict) or not isinstance(step.get('id'),str) or not step['id'].strip():
            raise ValueError('每个工艺步骤都需要非空 ID。')
        if step.get('operation') not in ('create_substrate','add','etch','create_film','delete'):
            raise ValueError(f"步骤 {step['id']} 的操作不受当前建模器支持：{step.get('operation')}")
        ids.append(step['id'])
    if len(set(ids))!=len(ids):raise ValueError('工艺步骤 ID 重复，请使用不同 ID，避免图例与模板绑定到错误步骤。')
    return {'folder':folder,'project':project,'process':process,'materials':materials}


def save_project(folder,process,materials):
    data=load_project(folder);validate_library(materials)
    # Validate before touching either of the explicit editable inputs.
    geometry=build_geometry(process,base_dir=data['folder'])
    validate_model({'geometry':geometry,'materials':materials,'voxels':None})
    if process==data['process'] and materials==data['materials']:
        return {'revision_dir':None,'unchanged':True}
    revision=unique_folder(data['folder']/'revisions','before_save')
    write_json(revision/'process.yaml',data['process']);write_json(revision/'materials.json',data['materials'])
    write_json(data['folder']/data['project']['process'],process)
    write_json(data['folder']/data['project']['materials'],materials)
    return {'revision_dir':str(revision)}


def apply_mark_parameters(folder,process,config,target_step_id=None):
    from marksim.marks import apply_mark_to_project
    data={'folder':Path(folder).resolve(),'process':copy.deepcopy(process),'project':{},'simulation':{}}
    return apply_mark_to_project(data,config,target_step_id=target_step_id)['process']


def preview_model(model,output_dir):
    validate_model(model)
    return (preview_geometry(model['geometry'],output_dir) if model['geometry']['representation']=='parametric_solids'
            else preview_voxels(model['voxels'],output_dir))


def build_project(folder,process=None,materials=None,previews=True):
    data=load_project(folder)
    recipe=copy.deepcopy(process if process is not None else data['process'])
    # Imported tables become editable project data. Re-read a TXT only when the
    # user imports it again; a build must never silently override a saved edit.
    library=validate_library(copy.deepcopy(materials if materials is not None else data['materials']))
    output=unique_folder(data['folder']/'builds','geometry')
    g=build_geometry(recipe,base_dir=data['folder'],snapshots_dir=output/'steps')
    model=validate_model({'geometry':g,'materials':library,'voxels':None,
                         'source':{'kind':'gds_and_geometric_process','project':str(data['folder']),
                                   'recipe':recipe,'not_process_physics':True}})
    save_geometry(g,output/'geometry.json');write_json(output/'materials.json',library)
    images=preview_model(model,output) if previews else {}
    return {'model':model,'output_dir':str(output),'previews':images}


def load_model(source,material_library=None):
    path=Path(source).resolve()
    if path.is_dir() or path.name=='structure.json':
        return read_bundle(path)
    if path.suffix.lower()=='.json':
        document=read_json(path)
        if isinstance(document,dict) and document.get('format')=='marksim-structure-bundle':
            return read_bundle(path)
    library=read_json(material_library) if isinstance(material_library,(str,Path)) else material_library
    if library is None:
        adjacent=path.parent/'materials.json'
        if adjacent.exists():library=read_json(adjacent)
    if library is None:raise ValueError('Geometry does not contain optical tables; select the material library explicitly')
    voxels=None
    if path.suffix.lower() in ('.h5','.hdf5','.npz'):
        voxels=import_legacy_npz(path) if path.suffix.lower()=='.npz' else load_voxels(path)
        g=geometry_manifest(voxels,'voxels.h5')
    else:
        g=read_json(path)
        if g.get('format')=='marksim-structure-bundle':return read_bundle(path)
        if g.get('representation')=='voxel_labels':
            voxels=load_voxels(path.parent/g['voxel_file'])
    return validate_model({'geometry':g,'materials':copy.deepcopy(library),'voxels':voxels,
                          'source':{'kind':'explicit_geometry_import','path':str(path),'sha256':file_hash(path)}})


def repeat_model(model,count_x=1,count_y=1,stripe_length_um=None,*,finite=False):
    """Explicit finite replication; preserve original without inventing boundaries.

    Identity arguments leave the original periodic interpretation unchanged,
    unless finite=True explicitly requests a single finite unit.
    A requested finite array/length clears the y-invariance declaration so the
    existing periodic 2D solvers cannot mistake an edge-containing mark for an
    infinite grating. Changing a length is allowed only for verified full-y
    rectangles; general polygons/voxels cannot be stretched silently.
    """
    validate_model(model)
    if any(type(n) is not int or n<1 or n>1000 for n in (count_x,count_y)):
        raise ValueError('Repeat counts must be integers in 1..1000')
    if count_x*count_y>10000:raise ValueError('Too many finite repeats; limit is 10000')
    result=copy.deepcopy(model)
    if count_x==count_y==1 and stripe_length_um is None and not finite:return result
    g=result['geometry'];bounds=np.asarray(g['cell_bounds_um'],float)
    old_period=float(bounds[0,1]-bounds[0,0]); y_period=float(bounds[1,1]-bounds[1,0])
    finite={'count_x':count_x,'count_y':count_y,'source_period_x_um':old_period,'source_period_y_um':y_period}
    if g['representation']=='parametric_solids':
        solids=copy.deepcopy(g['solids'])
        if stripe_length_um is not None:
            from marksim.geometry import invariance_evidence
            if not invariance_evidence(g)['periodic_2d_eligible']:
                raise ValueError('Length override requires verified full-y rectangles; do not stretch arbitrary 3D shapes')
            length=float(stripe_length_um)
            if not np.isfinite(length) or length<=0:raise ValueError('Stripe length must be positive')
            center=bounds[1].mean();old=bounds[1].copy();bounds[1]=[center-length/2,center+length/2]
            for solid in solids:
                p=np.asarray(solid['polygon_xy_um'],float)
                p[:,1]=np.where(np.isclose(p[:,1],old[0],rtol=0,atol=1e-8),bounds[1,0],bounds[1,1]);solid['polygon_xy_um']=p.tolist()
            y_period=length;finite['stripe_length_um']=length
        if len(solids)*count_x*count_y>100000:raise ValueError('Finite model exceeds 100000 material pieces')
        repeated=[]
        for ix in range(count_x):
            for iy in range(count_y):
                offset=np.array([(ix-(count_x-1)/2)*old_period,(iy-(count_y-1)/2)*y_period])
                for solid in solids:
                    s=copy.deepcopy(solid);s['name']=f'{s.get("name","solid")}_x{ix}_y{iy}'
                    s['polygon_xy_um']=(np.asarray(s['polygon_xy_um'])+offset).tolist();repeated.append(s)
        bounds[0]+=np.array([-1,1])*(count_x-1)*old_period/2
        bounds[1]+=np.array([-1,1])*(count_y-1)*y_period/2
        g.update(solids=repeated,cell_bounds_um=bounds.tolist(),period_x_um=count_x*old_period,
                 period_y_um=count_y*y_period,invariant_y=False,periodic_x=False,finite_array=finite,
                 substrate_extend_bottom=False)
        g.pop('mark',None);g.pop('invariance_evidence',None)
    else:
        if stripe_length_um is not None:raise ValueError('Voxel length must not be rescaled; change the reconstructed domain explicitly')
        v=result['voxels']
        if v['material_id'].size*count_x*count_y>2000000:raise ValueError('Finite voxel model exceeds 2000000 cells')
        origin=np.asarray(v['origin_um'])-np.array([(count_x-1)*old_period/2,(count_y-1)*y_period/2,0])
        tiled=create_voxel_model(np.tile(v['material_id'],(count_x,count_y,1)),origin,v['spacing_um'],v['material_map'],
                    background_id=v['background_id'],provenance={**v['provenance'],'finite_array':finite})
        result['voxels']=tiled;g=geometry_manifest(tiled,'voxels.h5');result['geometry']=g
        g.update(periodic_x=False,finite_array=finite)
    result['source']={**result.get('source',{}),'finite_array':finite,'boundary_condition':'finite geometry only; no automatic electromagnetic boundaries'}
    return validate_model(result)


def voxelize(model,spacing_um,output_dir):
    validate_model(model)
    if model['geometry']['representation']!='parametric_solids':raise ValueError('Voxelize requires parametric solids; existing labels are not resampled implicitly')
    out=Path(output_dir).resolve()
    if out.exists():raise FileExistsError(f'Existing voxel output preserved: {out}')
    v=voxelize_parametric(model['geometry'],spacing_um)
    out.mkdir(parents=True);save_voxels(v,out/'voxels.h5');g=geometry_manifest(v,'voxels.h5')
    write_json(out/'geometry.json',g);write_json(out/'materials.json',model['materials'])
    result=validate_model({'geometry':g,'materials':copy.deepcopy(model['materials']),'voxels':v,
                          'source':{'kind':'explicit_voxel_conversion','original':model.get('source',{})}})
    preview_model(result,out)
    return result


def reconstruct(config,output_dir,*,materials=None,log=None,cancel_event=None):
    from marksim.reconstruction import reconstruct_legacy
    path=Path(config).resolve() if isinstance(config,(str,Path)) else None
    spec=read_json(path) if path else copy.deepcopy(config)
    base=path.parent if path else ROOT
    legacy=copy.deepcopy(spec['legacy_config'])
    if isinstance(legacy,str):legacy=str((base/legacy).resolve())
    elif not legacy.get('generate_demo',False):
        for key in ('sem_image','tem_image'):
            if legacy.get(key):legacy[key]=str((base/legacy[key]).resolve())
    result=reconstruct_legacy(legacy,ROOT/'3D reconstruction',output_dir,material_map=spec['material_map'],
        tem_section=spec['tem_section'],registration=spec.get('registration'),spacing_um=spec.get('spacing_um',[.02]*3),
        log=log,cancel_event=cancel_event)
    library=materials
    if library is None and spec.get('materials'):
        library=read_json(base/spec['materials'])
    if library is None and isinstance(legacy,dict) and legacy.get('generate_demo'):
        library=demo_materials()
    if library is None:raise ValueError('Real reconstruction needs an explicit optical material library; labels alone do not determine n,k')
    model=load_model(Path(output_dir)/'geometry.json',library)
    write_json(Path(output_dir)/'materials.json',library)
    return {**result,'model':model,'output_dir':str(Path(output_dir).resolve())}
