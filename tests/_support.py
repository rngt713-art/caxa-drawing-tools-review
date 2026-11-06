"""Shared offline fixtures. Never starts CAXA, a helper EXE, or a server."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
import contextlib

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import drawing_bundle  # noqa: E402
import journal as journal_module  # noqa: E402
import relations as relations_module  # noqa: E402
import upload_core  # noqa: E402
from caxa.bridge import CaxaError  # noqa: E402

ACCOUNT = {"ip": "offline.invalid", "port": 1, "user": "tester", "password": "unused"}

#: All test scratch data stays inside the workspace (the OS temp dir is not
#: always writable in this environment).
BASE_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")


@contextlib.contextmanager
def temp_dir(prefix="case_"):
    os.makedirs(BASE_TMP, exist_ok=True)
    # os.mkdir (not tempfile.mkdtemp): mkdtemp applies a 0o700 DACL that the
    # sandbox token cannot extend into a subdirectory in this environment.
    path = os.path.join(BASE_TMP, "%s%s" % (prefix, uuid.uuid4().hex[:10]))
    os.makedirs(path)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def write_pdf(directory, name, content=b"%PDF-1.4 test"):
    path = os.path.join(str(directory), name)
    with open(path, "wb") as handle:
        handle.write(content)
    digest = hashlib.sha256(content).hexdigest()
    return path, digest, len(content)


def make_item(directory, code, name="", kind="component", machine="", content=None,
              filename=None):
    content = content or ("%PDF " + code).encode("utf-8")
    filename = filename or drawing_bundle.expected_drawing_filename(code, name)
    path, digest, size = write_pdf(directory, filename, content)
    return {
        "code": code,
        "name": name,
        "kind": kind,
        "machine": machine,
        "file_path": path,
        "file": "drawings/" + filename,
        "sha256": digest,
        "size": size,
        "upload_sel": True,
        "status": "待上传",
    }


def make_manifest(items, relations=None, **extra):
    manifest = {
        "schema": "caxa-drawing-upload-bundle",
        "schema_version": 1,
        "items": items,
        "assemblies": [],
        "manual_borrows": [],
        "relations": relations or [],
    }
    manifest.update(extra)
    return manifest


class TempDir:
    def __enter__(self):
        self._td = tempfile.TemporaryDirectory()
        return self._td.name

    def __exit__(self, *exc):
        self._td.cleanup()


def journal_path(name="journal.sqlite3"):
    base = os.path.join(BASE_TMP, "journal_" + uuid.uuid4().hex[:10])
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, name)


class FakeBridge:
    """Records commands; behaves like the Python bridge surface used by core."""

    def __init__(self):
        self.calls = []
        self.workflows = []
        self.auto_confirm_calls = []
        self.login_count = 0
        self.stopped = 0

    def login(self, ip, port, user, password):
        self.login_count += 1
        self.calls.append(("login", user))
        return {"ok": True, "logged_in": True}

    def stop(self):
        self.stopped += 1

    def set_auto_confirm(self, enabled, mode="", request_id=""):
        self.auto_confirm_calls.append((enabled, mode, request_id))
        return {"ok": True, "enabled": enabled}

    def start_workflow(self, drawing_ids, ip, port, user_id, **kwargs):
        self.workflows.append(list(drawing_ids))
        process_id = 900 + len(self.workflows)
        return {
            "ok": True,
            "process_instance_id": process_id,
            "started": True,
            "process_state": "2",
            "process_start_time": "offline",
            "state_verified": True,
            "documents_verified": True,
            "linked_document_ids": list(drawing_ids),
        }

    def call(self, cmd, timeout=240.0, idempotent=None):
        self.calls.append((cmd.get("cmd"), dict(cmd)))
        name = cmd.get("cmd")
        if name == "query_parts":
            return {"ok": True, "items": []}
        if name == "part_drawings":
            return {"ok": True, "drawings": []}
        if name == "part_info":
            return {"ok": True, "children": [], "link_endpoints": []}
        return {"ok": True}


class FakeUploader:
    """Stand-in for CaxaUploader used by upload_core tests."""

    instances = []

    def __init__(self, bridge, log=print, on_duplicate="append_iteration",
                 staging_dir=None, auto_confirm="", **kwargs):
        self.bridge = bridge
        self.log = log
        self.on_duplicate = on_duplicate
        self.staging = staging_dir
        self.borrow_actions = []
        self.uploads = 0
        self.relations = []
        self.closed = 0
        FakeUploader.instances.append(self)

    def upload_item(self, item, batch_codes, on_machine_missing=None,
                    explicit_parent="", relation_type="", staged=None):
        self.uploads += 1
        behaviour = getattr(self, "upload_behaviour", None)
        if behaviour:
            return behaviour(item, relation_type=relation_type)
        return {
            "object_id": 100 + self.uploads,
            "class_name": "Drawing",
            "class_id": 1002,
            "class_verified": True,
            "method": "attach",
            "file_name": os.path.basename(item.get("file_path") or ""),
            "target": "零件 %s 图纸" % item.get("code"),
            "caxa_path": "path",
            "created_new_part": False,
        }

    def link_relation(self, parent, child, link_type, **kwargs):
        behaviour = getattr(self, "relation_behaviour", None)
        if behaviour:
            row = behaviour(parent, child, link_type)
        else:
            row = {
                "machine": parent,
                "code": child,
                "link_type": link_type,
                "status": "已借用" if link_type != "own" else "已归属",
                "note": "",
            }
        self.relations.append(row)
        self.borrow_actions.append(row)
        return row

    def close(self):
        self.closed += 1


def patch_core(fake_bridge=None, fake_uploader_cls=FakeUploader):
    """Context manager patching upload_core's CAXA factories."""
    import contextlib
    from unittest.mock import patch

    @contextlib.contextmanager
    def _ctx():
        bridge = fake_bridge or FakeBridge()
        FakeUploader.instances = []
        with patch.object(upload_core, "CaxaBridge", lambda *a, **k: bridge), \
                patch.object(upload_core, "CaxaUploader", fake_uploader_cls), \
                patch.object(upload_core, "find_person_id", return_value=10):
            yield bridge

    return _ctx()


def run_upload(manifest, journal_file, bridge=None, uploader_cls=FakeUploader, **kwargs):
    options = dict(
        on_duplicate="append_iteration",
        start_workflow=False,
        log=lambda *a: None,
        journal_path=journal_file,
        staging_root=os.path.join(BASE_TMP, "staging_" + uuid.uuid4().hex[:8]),
    )
    options.update(kwargs)
    with patch_core(bridge, uploader_cls) as fake_bridge:
        result = upload_core.upload_bundle(manifest, ACCOUNT, **options)
    return result, fake_bridge


def open_journal(path):
    return journal_module.TaskJournal(path)


def relation(parent, child, link_type="own", **extra):
    entry = {
        "parent_code": parent,
        "child_code": child,
        "link_type": link_type,
        "source": "manual",
        "manual_override": False,
        "link_status": "",
    }
    entry.update(extra)
    return entry
