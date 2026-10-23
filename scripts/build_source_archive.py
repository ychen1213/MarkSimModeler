"""Build an editable source ZIP; never bundle environments or local user data."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
INCLUDE={'.gitignore','README.md','run_modeler.py','requirements.txt','requirements-dev.txt',
         'setup.bat','启动MarkSimModeler.bat','src','tests','scripts','docs','examples',
         'config','integrations','3D reconstruction'}
EXCLUDE={'.venv','__pycache__','.pytest_cache','outputs','dist','builds','revisions',
         'local_runtime.json','code_adapter_backups'}


def build(output=None):
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    output=Path(output) if output else ROOT/'dist'/f'MarkSimModeler_source_{stamp}.zip'
    output=output.resolve()
    if output.exists():raise FileExistsError(f'Existing archive preserved: {output}')
    files=[]
    for path in sorted(ROOT.rglob('*')):
        relative=path.relative_to(ROOT)
        if relative.parts[0] not in INCLUDE or any(part in EXCLUDE for part in relative.parts):continue
        if not path.is_file() or path.suffix in ('.pyc','.pyo'):continue
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError(f'Linked source outside package: {relative}')
        files.append((path,relative.as_posix()))
    inventory={name:hashlib.sha256(path.read_bytes()).hexdigest() for path,name in files}
    manifest={'format':'marksim-modeler-source','created_utc':stamp,'file_count':len(files),
              'excluded':'virtual environments, caches, outputs, user builds/revisions, local interpreter settings',
              'files_sha256':inventory}
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=7) as archive:
        for path,name in files:archive.write(path,'MarkSimModeler/'+name)
        archive.writestr('MarkSimModeler/SOURCE_MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    return {'archive':str(output),'file_count':len(files),'bytes':output.stat().st_size,
            'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output');args=parser.parse_args()
    print(json.dumps(build(args.output),ensure_ascii=False,indent=2))
