"""Source patch guard tests; no simulator or scientific output involved."""
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

SCRIPT=Path(__file__).resolve().parents[1]/"integrations/structure_import_patch/apply_patch.py"


@pytest.fixture
def patch(tmp_path):
    spec=importlib.util.spec_from_file_location("source_adapter_patch",SCRIPT)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.ROOT=tmp_path/"patch";module.ROOT.mkdir()
    target=tmp_path/"simulator";(target/"src/marksim").mkdir(parents=True)
    (target/"src/marksim/service.py").write_bytes(b"original service")
    (target/"user_project").mkdir();(target/"user_project/data.bin").write_bytes(b"user data unchanged")
    records=[]
    for name,before,after in [("src/marksim/service.py",b"original service",b"new service"),("docs/import.md",None,b"new documentation")]:
        source=module.ROOT/"files"/name;source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(after)
        records.append({"edition":"MarkSimFDTD","relative_path":name,"payload":"files/"+name,
                        "before_sha256":hashlib.sha256(before).hexdigest() if before else None,"after_sha256":hashlib.sha256(after).hexdigest()})
    (module.ROOT/"manifest.json").write_text(json.dumps({"schema_version":"1.0","patch_id":"test-source-only","files":records}),encoding="utf8")
    return module,target


def test_preflight_apply_backup_idempotence_and_user_data(patch):
    module,target=patch
    assert module.install(target,"MarkSimFDTD",check=True)["pending_files"]==2
    assert not (target/"code_adapter_backups").exists()
    result=module.install(target,"MarkSimFDTD")
    assert result["status"]=="applied"
    backup=Path(result["backup_dir"])
    assert (backup/"files/src/marksim/service.py").read_bytes()==b"original service"
    assert (target/"src/marksim/service.py").read_bytes()==b"new service"
    assert (target/"user_project/data.bin").read_bytes()==b"user data unchanged"
    assert module.install(target,"MarkSimFDTD")["status"]=="already_applied"
    assert len(list((target/"code_adapter_backups").iterdir()))==1


@pytest.mark.parametrize("conflict",["modified_baseline","new_file_conflict","payload_tamper"])
def test_all_files_checked_before_any_write(patch,conflict):
    module,target=patch
    if conflict=="modified_baseline": (target/"src/marksim/service.py").write_bytes(b"user modifications")
    elif conflict=="new_file_conflict":
        (target/"docs").mkdir();(target/"docs/import.md").write_bytes(b"user document")
    else: (module.ROOT/"files/docs/import.md").write_bytes(b"corrupt patch")
    original=(target/"src/marksim/service.py").read_bytes()
    with pytest.raises(ValueError):module.install(target,"MarkSimFDTD")
    assert (target/"src/marksim/service.py").read_bytes()==original
    assert not (target/"code_adapter_backups").exists()


def test_failed_write_rolls_back_and_keeps_backup_record(patch,monkeypatch):
    module,target=patch
    copy=module.shutil.copy2
    def fail_second(source,destination,*args,**kwargs):
        if "import.md.adapter-" in str(destination): raise OSError("test disk-write failure")
        return copy(source,destination,*args,**kwargs)
    monkeypatch.setattr(module.shutil,"copy2",fail_second)
    with pytest.raises(RuntimeError,match="backup and recovery"):module.install(target,"MarkSimFDTD")
    assert (target/"src/marksim/service.py").read_bytes()==b"original service"
    assert not (target/"docs/import.md").exists()
    record=json.loads(next((target/"code_adapter_backups").glob("*/manifest.json")).read_text(encoding="utf8"))
    assert record["status"]=="failed" and record["rollback"][0]["status"]=="restored"


def test_only_src_tests_docs_and_contained_paths_allowed(patch):
    module,target=patch
    manifest_path=module.ROOT/"manifest.json"
    manifest=json.loads(manifest_path.read_text(encoding="utf8"));manifest["files"][1]["relative_path"]="../user_data.txt"
    manifest_path.write_text(json.dumps(manifest),encoding="utf8")
    with pytest.raises(ValueError,match="Unsafe patch path"):module.install(target,"MarkSimFDTD")
    assert (target/"src/marksim/service.py").read_bytes()==b"original service"
