"""Apply only audited code/docs/tests to an exact MarkSim source baseline.

Standard library only; no administrator rights or package installation needed.
This patch never traverses/copies user projects or results. Every target is
checked before any write. Unknown source modifications require manual merging.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parent


def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def contained(root, name, *, allowed=None):
    if not isinstance(name,str) or not name or "\\" in name:
        raise ValueError('Relative POSIX path required')
    parts=PurePosixPath(name)
    if parts.is_absolute() or PureWindowsPath(name).drive or '..' in parts.parts or name!=parts.as_posix():
        raise ValueError('Unsafe patch path: '+name)
    if allowed and parts.parts[0] not in allowed:
        raise ValueError('Patch may change src/tests/docs only: '+name)
    value=(root/name).resolve()
    if not value.is_relative_to(root.resolve()): raise ValueError('Patch path escapes target: '+name)
    if allowed:
        # A link inside src must not redirect writes into user projects, even
        # when that destination is still inside the same repository root.
        if value != root.resolve().joinpath(*parts.parts):
            raise ValueError('Linked patch target requires manual merge: '+name)
        if value.relative_to(root.resolve()).parts[0] not in allowed:
            raise ValueError('Resolved patch target is outside src/tests/docs: '+name)
    return value


def inspect(target, edition):
    manifest=json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version')!='1.0': raise ValueError('Unsupported patch manifest')
    if not target.is_dir() or not (target/'src/marksim/service.py').is_file():
        raise ValueError('Target must be the existing simulator source root with src/marksim/service.py')
    entries=[item for item in manifest['files'] if item['edition']==edition]
    if not entries: raise ValueError('No files for selected edition')
    plan=[];seen=set()
    # Preflight every payload and every target before creating even a backup.
    for item in entries:
        path=contained(target,item['relative_path'],allowed={'src','tests','docs'})
        if str(path).casefold() in seen: raise ValueError('Duplicate patch target')
        seen.add(str(path).casefold())
        source=contained(ROOT,item['payload'])
        if not source.is_file() or sha256(source)!=item['after_sha256']:
            raise ValueError('Patch payload hash mismatch: '+item['payload'])
        if path.exists() and not path.is_file(): raise ValueError('Target is not a regular file: '+str(path))
        actual=sha256(path) if path.is_file() else None
        if actual==item['after_sha256']:
            state='already_applied'
        elif actual==item['before_sha256']:
            state='pending'
        else:
            raise ValueError('Unrecognized local changes or different source version; no files changed. Manual merge required: '+str(path))
        plan.append({**item,'path':path,'source':source,'actual_sha256':actual,'state':state})
    return manifest,plan


def install(target, edition, *, check=False):
    target=Path(target).resolve()
    manifest,plan=inspect(target,edition)
    pending=[item for item in plan if item['state']=='pending']
    summary={'edition':edition,'target':str(target),'checked_files':len(plan),'pending_files':len(pending),
             'already_applied':len(plan)-len(pending),'status':'checked' if check else 'already_applied'}
    if check or not pending: return summary
    backup=contained(target,'code_adapter_backups/'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8])
    backup.mkdir(parents=True,exist_ok=False)
    archive=[]
    for item in pending:
        path=item['path'];saved=backup/'files'/item['relative_path']
        if path.is_file():
            saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,saved)
            if sha256(saved)!=item['actual_sha256']: raise ValueError('Source changed during backup: '+str(path))
        archive.append({'relative_path':item['relative_path'],'before_sha256':item['actual_sha256'],
                        'after_sha256':item['after_sha256'],'was_present':path.is_file()})
    record={'patch_id':manifest['patch_id'],'edition':edition,'target':str(target),'status':'backed_up','files':archive}
    record_path=backup/'manifest.json'
    record_path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
    written=[]
    try:
        # Recheck the entire target set after backup and before the first write.
        for item in plan:
            actual=sha256(item['path']) if item['path'].is_file() else None
            if actual!=item['actual_sha256']: raise ValueError('Target changed during preflight; retry after reviewing: '+str(item['path']))
        for item in pending:
            path=item['path'];path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_name(path.name+'.adapter-'+uuid.uuid4().hex+'.tmp')
            try:
                shutil.copy2(item['source'],temporary)
                if sha256(temporary)!=item['after_sha256']: raise ValueError('Staged payload hash mismatch')
                actual=sha256(path) if path.is_file() else None
                if actual!=item['actual_sha256']: raise ValueError('Target changed while applying patch: '+str(path))
                temporary.replace(path);written.append(item)
            finally:
                if temporary.is_file(): temporary.unlink()
        record['status']='applied'
    except Exception as exc:
        rollback=[]
        for item in reversed(written):
            path=item['path']
            # Never overwrite a user's concurrent edit during rollback.
            if not path.is_file() or sha256(path)!=item['after_sha256']:
                rollback.append({'path':str(path),'status':'concurrent change; restore manually from backup'});continue
            if item['actual_sha256'] is None: path.unlink()
            else: shutil.copy2(backup/'files'/item['relative_path'],path)
            rollback.append({'path':str(path),'status':'restored'})
        record.update(status='failed',error=str(exc),rollback=rollback)
        raise RuntimeError(f'Patch failed; backup and recovery details: {backup}; {exc}') from exc
    finally:
        record_path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
    summary.update(status='applied',backup_dir=str(backup))
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',required=True,help='Existing simulator source root, not a user simulation project')
    parser.add_argument('--edition',required=True,choices=('MarkSim','MarkSimFDTD'))
    parser.add_argument('--check',action='store_true',help='Validate all baselines and payloads without writing any target files')
    args=parser.parse_args(argv)
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf8',errors='replace')
    try:
        print(json.dumps(install(args.target,args.edition,check=args.check),ensure_ascii=False,indent=2));return 0
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
