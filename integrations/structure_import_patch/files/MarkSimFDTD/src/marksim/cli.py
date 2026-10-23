from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from .schemas import jsonable,read_json

def main(argv=None):
    # Redirected Windows stdout may use GBK; grid diagnostics contain µ and λ.
    # Keep direct script runs and captured logs UTF-8 as well as the GUI worker.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser=argparse.ArgumentParser(description='MarkSimFDTD source edition: geometry -> native Windows FDTD -> calibrated diffraction')
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('demo'); p.add_argument('--output',required=True)
    commands.add_parser('marks',help='List implemented mark templates and reserved entries')
    p=commands.add_parser('new-mark',help='Create a new AH32/AH53/AH74 parametric demo project')
    p.add_argument('--type',choices=('AH32','AH53','AH74'),required=True)
    p.add_argument('--output',required=True)
    pitch=p.add_mutually_exclusive_group()
    pitch.add_argument('--mother-pitch-um',type=float,help='Full FDTD mother period, um')
    pitch.add_argument('--pitch-um',type=float,help='Internal stripe centre pitch, um; mother period is h times this')
    p.add_argument('--cd-nm',type=float); p.add_argument('--height-nm',type=float)
    for name in ('geometry','validate','fdtd','run'):
        p=commands.add_parser(name); p.add_argument('--project',required=True)
        if name in ('fdtd','run'): p.add_argument('--runtime')
    p=commands.add_parser('scan'); p.add_argument('--project',required=True); p.add_argument('--parameter',required=True)
    p.add_argument('--values',nargs='+',type=float); p.add_argument('--start',type=float); p.add_argument('--stop',type=float); p.add_argument('--num-points',type=int)
    p.add_argument('--runtime')
    p=commands.add_parser('gui'); p.add_argument('--project')
    p.add_argument('--page',default='project',choices=('project','process','geometry','reconstruction','simulation','jobs','results'))
    from .debugging import add_arguments, run_from_args
    p=commands.add_parser('debug',help='Diagnose one stage; original inputs are preserved')
    add_arguments(p)
    p=commands.add_parser('dataflow',help='Explain source -> reference subtraction -> calibrated diffraction from saved results')
    p.add_argument('--scattering'); p.add_argument('--output')
    p.add_argument('--order',type=int)
    p=commands.add_parser('reconstruct'); p.add_argument('--config',required=True); p.add_argument('--output',required=True); p.add_argument('--legacy-root')
    p=commands.add_parser('voxelize'); p.add_argument('--geometry',required=True); p.add_argument('--spacing-um',nargs=3,type=float,required=True); p.add_argument('--output',required=True)
    p=commands.add_parser('import-structure',help='Copy verified modeler geometry/material snapshot into a new simulation project')
    p.add_argument('--source',required=True); p.add_argument('--output',required=True)
    p=commands.add_parser('import-voxel'); p.add_argument('--npz',required=True); p.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='debug': return run_from_args(args)
        if args.command=='marks':
            from .marks import catalog
            print(json.dumps(catalog(),ensure_ascii=False,indent=2)); return 0
        if args.command=='new-mark':
            from .marks import create_mark_project,default_mark_config,normalize_mark_config
            config=default_mark_config(args.type)
            h=normalize_mark_config(config)['target_order']
            if args.mother_pitch_um is not None: config['mother_period_um']=args.mother_pitch_um
            if args.pitch_um is not None: config['mother_period_um']=h*args.pitch_um
            config['cd_um']=args.cd_nm/1000 if args.cd_nm is not None else config['mother_period_um']/(2*h)
            if args.height_nm is not None: config['height_um']=args.height_nm/1000
            result=create_mark_project(args.output,args.type,parameters=config)
            print(json.dumps({'project_dir':result['project_dir'],'mark':result['geometry']['mark']},ensure_ascii=False,indent=2))
            return 0
        if args.command=='dataflow':
            from .dataflow_report import export_dataflow
            result=export_dataflow(args.scattering,output_parent=args.output,sample_order=args.order)
            print(json.dumps({key:value for key,value in result.items() if key!='report'},ensure_ascii=False,indent=2))
            return 0
        from . import service
        from .jobs import load_runtime
        runtime=load_runtime(args.runtime) if getattr(args,'runtime',None) else None
        if args.command=='demo':
            from .examples import create_ah53_project
            result={'project_dir':create_ah53_project(args.output)['project_dir']}
        elif args.command in ('geometry','validate'):
            prepared=service.prepare_project(args.project,save_artifacts=args.command=='geometry',log=print)
            result={k:prepared[k] for k in ('artifact_dir','estimated_cells','estimated_memory_mb','estimated_wall_seconds_range','estimate_note')}
            result['valid']=True
        elif args.command=='fdtd': result=service.run_fdtd(args.project,runtime=runtime,log=print)
        elif args.command=='run': result=service.run_full(args.project,runtime=runtime,log=print)
        elif args.command=='scan':
            import numpy as np
            values=args.values
            if values is None:
                if args.start is None or args.stop is None or args.num_points is None or args.num_points<1:
                    parser.error('Provide --values or --start/--stop/--num-points')
                values=np.linspace(args.start,args.stop,args.num_points)
            result=service.scan_parameter(args.project,args.parameter,values,runtime=runtime,log=print)
        elif args.command=='reconstruct':
            result=service.reconstruct_images(args.config,args.output,legacy_root=args.legacy_root,log=print)
        elif args.command=='voxelize':
            result=service.convert_voxel_geometry(args.geometry,args.output,spacing_um=args.spacing_um)
        elif args.command=='import-structure':
            result=service.import_structure(args.source,args.output)
        elif args.command=='import-voxel':
            result=service.convert_voxel_geometry(args.npz,args.output,legacy_npz=True)
        else:
            from .ui import launch
            launch(args.project,page=args.page); return 0
        print(json.dumps(jsonable(result),ensure_ascii=False,indent=2,allow_nan=False))
        if result.get('status') in ('failed','cancelled') or result.get('fdtd',{}).get('status') in ('failed','cancelled'): return 1
        return 0
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr); return 1

if __name__=='__main__': sys.exit(main())
