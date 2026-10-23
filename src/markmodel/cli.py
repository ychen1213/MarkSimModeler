"""Separately runnable geometry, interchange and Lumerical export stages."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from marksim.schemas import jsonable
from . import service
from .exchange import write_bundle


def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser(description='MarkSimModeler: editable geometry + materials -> structure bundle / Lumerical LSF / STL')
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('gui');p.add_argument('--project')
    p=commands.add_parser('new');p.add_argument('--type',choices=['AH32','AH53','AH74'],default='AH53');p.add_argument('--output',required=True)
    p.add_argument('--period-um',type=float);p.add_argument('--cd-nm',type=float);p.add_argument('--height-nm',type=float)
    p=commands.add_parser('build');p.add_argument('--project',required=True)
    p=commands.add_parser('inspect');p.add_argument('--source',required=True);p.add_argument('--materials')
    for action in ('bundle','lumerical'):
        p=commands.add_parser(action);selection=p.add_mutually_exclusive_group(required=True)
        selection.add_argument('--project');selection.add_argument('--source')
        p.add_argument('--materials');p.add_argument('--output',required=True)
        p.add_argument('--count-x',type=int,default=1);p.add_argument('--count-y',type=int,default=1)
        p.add_argument('--stripe-length-um',type=float)
        p.add_argument('--finite',action='store_true',help='Explicit finite object, including a 1 x 1 array')
        if action=='lumerical':
            p.add_argument('--no-stl',action='store_true');p.add_argument('--band-nm',nargs=2,type=float)
    p=commands.add_parser('reconstruct');p.add_argument('--config',required=True);p.add_argument('--materials');p.add_argument('--output',required=True)
    p=commands.add_parser('voxelize');p.add_argument('--source',required=True);p.add_argument('--materials');p.add_argument('--spacing-um',nargs=3,type=float,required=True);p.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='gui':
            from .ui import launch
            launch(args.project);return 0
        if args.command=='new':
            params={}
            for arg,key,factor in [('period_um','mother_period_um',1),('cd_nm','cd_um',.001),('height_nm','height_um',.001)]:
                value=getattr(args,arg)
                if value is not None:params[key]=value*factor
            if 'mother_period_um' in params and 'cd_um' not in params:
                params['cd_um']=params['mother_period_um']/(2*{'AH32':3,'AH53':5,'AH74':7}[args.type])
            r=service.create_project(args.output,args.type,params);result={'project':str(r['folder'])}
        elif args.command=='build':
            r=service.build_project(args.project);result={k:v for k,v in r.items() if k!='model'}
        elif args.command in ('bundle','lumerical'):
            model=(service.build_project(args.project,previews=False)['model'] if args.project
                   else service.load_model(args.source,args.materials))
            model=service.repeat_model(model,args.count_x,args.count_y,args.stripe_length_um,finite=args.finite)
            if args.command=='bundle':result=write_bundle(model,args.output)
            else:
                from .lumerical import export_lumerical
                exported=export_lumerical(model,args.output,export_stl=not args.no_stl,wavelength_range_nm=args.band_nm)
                result={key:value for key,value in exported.items() if key!='report'}
                result['target_runtime_verified']=False
        elif args.command=='inspect':
            model=service.load_model(args.source,args.materials)
            result={'representation':model['geometry']['representation'],'bounds_um':model['geometry']['cell_bounds_um'],
                    'materials':list(model['materials']['materials']),'source':model['source']}
        elif args.command=='voxelize':
            model=service.voxelize(service.load_model(args.source,args.materials),args.spacing_um,args.output)
            result={'output':args.output,'shape_xyz':model['voxels']['shape_xyz']}
        else:
            from marksim.schemas import read_json
            r=service.reconstruct(args.config,args.output,materials=read_json(args.materials) if args.materials else None,log=print)
            result={'output_dir':r['output_dir'],'representation':r['model']['geometry']['representation']}
        print(json.dumps(jsonable(result),ensure_ascii=False,indent=2));return 0
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr);return 1
