"""Shared CLI/GUI orchestration; editing optics never launches FDTD."""
from __future__ import annotations
import copy
from datetime import datetime,timezone
from pathlib import Path
import uuid
import re
import numpy as np
from .schemas import read_json,write_json,canonical_hash,simulation_config,versioned
from .geometry import build_geometry,save_geometry,preview_geometry
from .materials import evaluate_library,import_material_txt
from .jobs import run_task,run_queue,load_runtime
from .scattering import load_scattering
from .optics import run_optics,save_scan,save_scan_csv,plot_scan
from .metrics import compute_metrics,save_metrics
from .plots import plot_scattering,plot_fields
from .paths import resolve_input_path

ROOT=Path(__file__).resolve().parents[2]

def unique_folder(parent,prefix):
    # Project names are display text, not paths. Keep new runs inside the
    # explicitly chosen parent even if a name contains slashes or '..'.
    prefix=re.sub(r'[^\w.-]+','_',str(prefix),flags=re.UNICODE).strip('._')[:80] or 'project'
    path=Path(parent)/(prefix+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:6])
    path.mkdir(parents=True,exist_ok=False)
    return path

def load_project(folder):
    folder=resolve_input_path(folder).resolve()
    if folder.is_file(): folder=folder.parent
    project=versioned(read_json(folder/'project.yaml'),'Project')
    data={'folder':folder,'project':project,
            'process':read_json(resolve_input_path(project['process'],base_dir=folder)),
            'materials':read_json(resolve_input_path(project['materials'],base_dir=folder)),
            'simulation':simulation_config(read_json(resolve_input_path(project['simulation'],base_dir=folder))),
            'optics':read_json(resolve_input_path(project['optics'],base_dir=folder))}
    from .structure_import import is_imported, imported_model
    if project.get('geometry_input') is not None and not is_imported(data):
        raise ValueError('Unsupported geometry_input kind; refusing implicit recipe fallback')
    if data['process'].get('model_version') == 'imported-structure-1.0' and not is_imported(data):
        raise ValueError('Imported process is descriptive only; geometry_input binding is required')
    if is_imported(data): imported_model(data)
    return data

def refresh_material_sources(library, project_dir=None):
    """Re-read known raw TXT sources so editing a material invalidates physics.

    Project-local materials/<source basename> takes precedence over a source
    absolute path, so a moved/copied project never follows its previous copy.
    Portable imported snapshots remain usable if every source is absent; their
    exact numeric contents and original source hash stay in the task.
    """
    library=copy.deepcopy(library)
    for name,spec in list(library['materials'].items()):
        source_text=str(spec.get('source',''))
        source=resolve_input_path(source_text)
        if project_dir is not None:
            root=Path(project_dir).resolve()
            # Normalize slash separators for provenance saved on another OS.
            basename=Path(source_text.replace('\\','/')).name
            local=root/'materials'/basename if basename else None
            relative=root/source if not source.is_absolute() else None
            if local is not None and local.is_file():
                source=local
            elif relative is not None and relative.is_file():
                source=relative
        if source.is_file() and spec.get('import'):
            options=spec['import']
            library['materials'][name]=import_material_txt(source,
                wavelength_unit=options['wavelength_unit'],columns=options['columns'],
                assume_lossless=spec.get('assume_lossless',False))
    library['input_hash']=canonical_hash(library['materials'])
    return library

def prepare_project(folder,*,process=None,simulation=None,materials=None,save_artifacts=True,log=None):
    data=load_project(folder); root=data['folder']
    recipe=copy.deepcopy(process if process is not None else data['process'])
    sim=simulation_config(simulation if simulation is not None else data['simulation'])
    from .structure_import import is_imported, imported_model, assert_solver_scope
    artifacts=unique_folder(root/'geometry','build') if save_artifacts else None
    if is_imported(data):
        # Explicit snapshot branch: never rebuild the descriptive process stub.
        selected_data={**data, 'process':recipe, 'materials':materials if materials is not None else data['materials']}
        model=imported_model(selected_data)
        geometry=copy.deepcopy(model['geometry']); library=copy.deepcopy(model['materials'])
        assert_solver_scope(geometry)
        if log: log('Reading verified imported structure/material snapshots; original sources are not re-read')
    else:
        library=refresh_material_sources(materials if materials is not None else data['materials'],project_dir=root)
        if log: log('Reading actual GDS and rebuilding material geometry')
        geometry=build_geometry(recipe,base_dir=root,snapshots_dir=artifacts/'steps' if artifacts else None)
    from .result_inspection import geometry_optical_warnings
    geometry.setdefault('warnings', []).extend(geometry_optical_warnings(geometry))
    if log:
        for warning in geometry.get('warnings', []):
            log('几何说明：'+warning)
    geometry['is_demo']='DEMO' in data['project'].get('name','').upper()
    used={geometry['background_material']}|{s['material'] for s in geometry['solids']}
    missing=used-set(library['materials'])
    if missing: raise ValueError(f'Missing material definitions: {sorted(missing)}')
    selected={**library,'materials':{n:library['materials'][n] for n in sorted(used)}}
    optical=evaluate_library(selected,sim['lambda_vacuum_nm'])
    task={'schema_version':'1.0','geometry':geometry,'materials':optical,'simulation':sim,
          'material_library_snapshot':selected,'material_library_hash':canonical_hash(selected)}
    if is_imported(data): task['geometry_input_snapshot']={'manifest_sha256':model['manifest_sha256'], 'files_sha256':model['manifest']['files_sha256']}
    from .meep_backend import validate_task
    _,_,cells=validate_task(task)  # module itself does not import Meep
    if artifacts:
        save_geometry(geometry,artifacts/'geometry.json')
        write_json(artifacts/'task.json',task)
        write_json(artifacts/'materials_used.json',optical)
        preview_geometry(geometry,artifacts)
    source_time=10/(1000/sim['lambda_vacuum_nm']*sim['fwidth_fraction'])
    step_count=(2*(source_time+min(40.,sim['after_source_time'])+sim['tail_time'])+
                2*(source_time+sim['after_source_time']+sim['tail_time']))*2*sim['resolution_per_um']
    estimate=cells*step_count*1.2e-8
    return {'task':task,'artifact_dir':str(artifacts) if artifacts else None,
            'estimated_cells':cells,'estimated_memory_mb':100+cells*400/1024**2,
            'estimated_wall_seconds_range':[.5*estimate,2.5*estimate],
            'estimate_note':'Empirical single-process range on this machine; load/DFT/material complexity changes runtime',
            'data':data}

def run_fdtd(folder,*,runtime=None,prepared=None,cancel_event=None,log=None,run_root=None,use_cache=True):
    prepared=prepared or prepare_project(folder,log=log)
    from .structure_import import is_imported, imported_model
    current=load_project(folder)
    if is_imported(current):
        model=imported_model(current)
        expected={'manifest_sha256':model['manifest_sha256'],'files_sha256':model['manifest']['files_sha256']}
        if prepared['task'].get('geometry_input_snapshot') != expected:
            raise ValueError('Prepared task does not match this imported structure; validate the current project again')
    runtime=runtime or load_runtime(ROOT/'config/runtime.json')
    target=Path(run_root) if run_root else resolve_input_path(folder)/'runs'
    result=run_task(prepared['task'],target,runtime,cancel_event=cancel_event,log=log,use_cache=use_cache)
    if result['status']=='succeeded':
        d=Path(result['run_dir']); scattering=load_scattering(d/'scattering.h5')
        plot_scattering(scattering,d/'figures'); plot_fields(d/'fields.h5',d/'figures')
        result['numerical_status']=scattering['metadata']['status']
    return result

def optics_only(scattering_file,config,*,reference_scattering=None,known_shift_um=None,branch_center_um=0.,output_parent=None):
    """Independent saved-file calculation; no call to solver or geometry builder."""
    scattering_file=resolve_input_path(scattering_file)
    scattering=load_scattering(scattering_file)
    if reference_scattering is None:
        reference_scattering=config.get('reference_scattering')
    if config.get('reference_mode')=='same_mark_unshifted':
        if reference_scattering is not None:
            raise ValueError('Choose same_mark_unshifted or a reference file, not both')
        reference_scattering=scattering_file
    elif config.get('reference_mode') not in (None,'external','none'):
        raise ValueError('Unsupported reference_mode')
    if config.get('reference_mode')=='none' and reference_scattering is not None:
        raise ValueError('reference_mode=none conflicts with a reference scattering file')
    if config.get('reference_mode')=='external' and not reference_scattering:
        raise ValueError('reference_mode=external requires an actual reference scattering file')
    if not scattering['metadata'].get('time_converged',False):
        raise ValueError('DFT not time-converged: extend simulation before final optical readout')
    scan=run_optics(scattering,config)
    # Persist the executed preset/defaults, not an incomplete caller request.
    # Legacy inputs stay byte-for-byte compatible at the scientific contract.
    if config.get('readout_profile'):
        config=copy.deepcopy(scan['metadata']['config'])
    reference=None
    if reference_scattering:
        reference_config={**config,'rigid_shift_um':0.}
        ref=load_scattering(resolve_input_path(reference_scattering))
        if not ref['metadata'].get('time_converged',False): raise ValueError('Reference DFT not time-converged')
        reference=run_optics(ref,reference_config)
    metrics=compute_metrics(scan,reference,known_shift_um,branch_center_um=branch_center_um)
    parent=Path(output_parent) if output_parent else Path(scattering_file).parent/'optics'
    out=unique_folder(parent,'readout')
    write_json(out/'optics_config.json',config)
    save_scan(out/'scan.h5',scan); save_scan_csv(out/'scan.csv',scan); plot_scan(out/'scan.png',scan)
    if reference is not None: save_scan(out/'reference_scan.h5',reference)
    save_metrics(out/'metrics.json',metrics)
    write_json(out/'manifest.json',{'schema_version':'1.0','status':'succeeded','generation_method':'calibrated_meep_to_ideal_optics',
        'scattering_file':str(Path(scattering_file).resolve()),'input_scattering_hash':scan['metadata']['input_scattering_hash'],
        'reference_scattering':str(reference_scattering) if reference_scattering else None,
        'optics_config':config,'known_shift_um':known_shift_um})
    return {'output_dir':str(out),'metrics':metrics,'scan':scan}

def run_full(folder,*,runtime=None,cancel_event=None,log=None):
    data=load_project(folder)
    run=run_fdtd(folder,runtime=runtime,cancel_event=cancel_event,log=log)
    if run['status']!='succeeded': return {'fdtd':run,'optics':None}
    if cancel_event is not None and cancel_event.is_set(): return {'fdtd':run,'optics':None}
    from .structure_import import is_imported
    if is_imported(data): return {'fdtd':run,'optics':None}
    config=copy.deepcopy(data['optics'])
    if config.get('reference_scattering') and not Path(config['reference_scattering']).is_absolute():
        config['reference_scattering']=str(data['folder']/config['reference_scattering'])
    optical=optics_only(Path(run['run_dir'])/'scattering.h5',config)
    return {'fdtd':run,'optics':{'output_dir':optical['output_dir'],'metrics':optical['metrics']}}

def _set_scan_value(data,path,value):
    from .structure_import import is_imported, READ_ONLY
    if is_imported(data) and path != 'simulation.lambda_vacuum_nm': raise ValueError(READ_ONLY)
    if path=='simulation.lambda_vacuum_nm': data['simulation']['lambda_vacuum_nm']=float(value); return
    fields=path.split('.')
    supported={'thickness_um','etch_depth_um','cd_bias_um','delta_x_um','delta_y_um'}
    if len(fields)!=4 or fields[:2]!=['process','steps'] or fields[3] not in supported:
        raise ValueError('Scan supports simulation.lambda_vacuum_nm or process.steps.INDEX.'+','.join(sorted(supported)))
    index=int(fields[2]); step=data['process']['steps'][index]
    if fields[3]=='thickness_um' and 'z_max_um' in step:
        raise ValueError('Thickness scan requires thickness_um representation, not explicit z_max_um')
    step[fields[3]]=float(value)

def scan_parameter(folder,path,values,*,runtime=None,cancel_event=None,log=None):
    folder=resolve_input_path(folder)
    values=np.asarray(values,float)
    if values.ndim!=1 or not len(values) or not np.isfinite(values).all(): raise ValueError('Scan values must be a finite nonempty array')
    runtime=runtime or load_runtime(ROOT/'config/runtime.json')
    data=load_project(folder); tasks=[]
    for value in values:
        modified=copy.deepcopy(data); _set_scan_value(modified,path,value)
        tasks.append(prepare_project(folder,process=modified['process'],simulation=modified['simulation'],
                                     save_artifacts=False)['task'])
    queue_dir=unique_folder(Path(folder)/'queues','scan')
    description={'schema_version':'1.0','parameter_path':path,'values':values.tolist(),'status':'pending',
                 'task_count':len(tasks),'tasks':[{'input_hash':canonical_hash(t),'simulation':t['simulation']} for t in tasks]}
    write_json(queue_dir/'queue.json',description)
    results=run_queue(tasks,Path(folder)/'runs',runtime,cancel_event=cancel_event,log=log)
    description.update(status='cancelled' if len(results)<len(tasks) or any(r['status']=='cancelled' for r in results)
                       else ('succeeded' if all(r['status']=='succeeded' for r in results) else 'failed'),results=results)
    write_json(queue_dir/'queue.json',description)
    return {'queue_dir':str(queue_dir),**description}


def reconstruct_images(config, output_dir, *, legacy_root=None, log=None, cancel_event=None):
    """Parallel image entrance; saves label geometry, never launches the Jones solver."""
    from .reconstruction import reconstruct_legacy
    if isinstance(config, (str, Path)): config = resolve_input_path(config)
    base = Path(config).resolve().parent if isinstance(config, (str, Path)) else None
    spec = read_json(config) if base is not None else copy.deepcopy(config)
    versioned(spec, 'SEM/TEM bridge configuration')
    allowed = {'schema_version', 'legacy_config', 'material_map', 'tem_section', 'registration', 'spacing_um', 'note'}
    unknown = set(spec) - allowed
    if unknown: raise ValueError(f'Unknown reconstruction fields: {sorted(unknown)}')
    legacy = copy.deepcopy(spec['legacy_config'])
    if isinstance(legacy, str):
        legacy = str(resolve_input_path(legacy, base_dir=base))
    if isinstance(legacy, dict):
        for key in ('sem_image', 'tem_image'):
            if legacy.get(key): legacy[key] = str(resolve_input_path(legacy[key], base_dir=base))
    return reconstruct_legacy(legacy, resolve_input_path(legacy_root) if legacy_root else ROOT/'3D reconstruction', output_dir,
        material_map=spec['material_map'], tem_section=spec['tem_section'], registration=spec.get('registration'),
        spacing_um=spec.get('spacing_um', [.02,.02,.02]), log=log, cancel_event=cancel_event)


def convert_voxel_geometry(source, output_dir, *, spacing_um=None, legacy_npz=False):
    from .voxel import voxelize_parametric, import_legacy_npz, save_voxels, geometry_manifest, preview_voxels
    source=resolve_input_path(source)
    out=Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()): raise FileExistsError('Voxel conversion requires a new or empty output directory')
    if legacy_npz:
        model=import_legacy_npz(source)
    else:
        if spacing_um is None: raise ValueError('Choose explicit xyz spacing_um')
        model=voxelize_parametric(read_json(source),spacing_um)
    out.mkdir(parents=True,exist_ok=True)
    save_voxels(model,out/'voxels.h5')
    write_json(out/'geometry.json',geometry_manifest(model,'voxels.h5'))
    preview=preview_voxels(model,out)
    return {'geometry_file':str(out/'geometry.json'),'voxels_file':str(out/'voxels.h5'),
            'preview_files':preview,'shape_xyz':model['shape_xyz'],
            'solver_scope':'Interchange/preview; full Jones backend does not accept voxels in this MVP'}


def import_structure(source, output, *, previews=True):
    """Import a verified structure into a new project without running physics."""
    from .structure_import import import_project
    return import_project(source, output, legacy_optics=True, previews=previews)
