"""CAXA-only upload orchestration used by the distributable uploader.

One journal task = one content batch (server + login + the exact PDFs).  The
phases (upload / classify+relate / workflow) are planned separately, so adding
a relation or sending the workflow later never re-uploads confirmed PDFs.

Order of operations is deliberate:

1. validate the rows that will be executed (content hash first);
2. find/restore the task and refuse to continue while any operation is
   uncertain (orphan running rows included);
3. take the single-run lock;
4. classify own/borrow from the live tree (or restore recorded decisions) and
   validate the *effective* graph before any write;
5. persist those decisions;
6. upload only unconfirmed items; then execute only unconfirmed relations;
7. start the workflow only when every required upload/relation is confirmed.
"""

from __future__ import annotations

import hashlib
import json
import os

import drawing_bundle
import journal as journal_module
import relations as relations_module
import staging as staging_module
from caxa.bridge import CaxaBridge, CaxaError
from caxa.uploader import (
    CLASS_ID_CAPP,
    CLASS_ID_DRAWING,
    CaxaUploader,
    MachineMissing,
    PublishedDrawingError,
    UnverifiedResult,
)

ITEM_PENDING = "待上传"
ITEM_DONE = "已上传"
ITEM_FAILED = "失败"
ITEM_UNCERTAIN = "待核实"
ITEM_PARTIAL = "部分完成"
ITEM_SKIPPED = "已跳过"

WORKFLOW_STARTED = "已发起"
WORKFLOW_PENDING = "待核实"
WORKFLOW_BLOCKED = "已阻止"
WORKFLOW_FAILED = "失败"


class PendingVerificationError(CaxaError):
    """A previous attempt has an unknown outcome; automatic replay is blocked."""

    def __init__(self, message, pending=None):
        super().__init__(message, stage="pending_verification", response_lost=True)
        self.pending = pending or []


def _is_executed(item: dict) -> bool:
    if not item.get("upload_sel", True):
        return False
    return str(item.get("status") or "") not in (ITEM_SKIPPED,)


def _selected_items(manifest: dict) -> list[dict]:
    return [item for item in manifest.get("items") or [] if isinstance(item, dict) and _is_executed(item)]


def _upload_order(items: list[dict], relations: list[dict] | None = None) -> list[dict]:
    """Place a PDF that is a parent of another in-batch PDF first."""
    remaining = list(items)
    ordered = []
    rel_parents: dict[str, set[str]] = {}
    for relation in relations or []:
        parent = drawing_bundle.normalize_code(relation.get("parent_code"))
        child = drawing_bundle.normalize_code(relation.get("child_code"))
        if parent and child:
            rel_parents.setdefault(child, set()).add(parent)
    while remaining:
        pending_codes = {drawing_bundle.normalize_code(item.get("code")) for item in remaining}
        ready = []
        for item in remaining:
            code = drawing_bundle.normalize_code(item.get("code"))
            parents = set(rel_parents.get(code, set()))
            legacy_parent = drawing_bundle.normalize_code(item.get("parent_code"))
            if legacy_parent:
                parents.add(legacy_parent)
            if not (parents & pending_codes) or all(parent == code for parent in parents):
                ready.append(item)
        if not ready:
            ordered.extend(remaining)
            break
        ordered.extend(ready)
        ready_ids = {id(item) for item in ready}
        remaining = [item for item in remaining if id(item) not in ready_ids]
    return ordered


def find_person_id(bridge: CaxaBridge, name: str) -> int:
    result = bridge.call(
        {
            "cmd": "query_objects",
            "class_id": 1008,
            "limit": 20,
            "where_property": "name",
            "where_value": name,
            "where_operator": "equal",
            "properties": ["name"],
        },
        timeout=120,
    )
    items = [item for item in result.get("items") or [] if (item.get("name") or "") == name]
    if len(items) != 1:
        raise CaxaError("人员 %s 精确匹配数为 %d" % (name, len(items)))
    return int(items[0]["object_id"])


def refine_relations_from_tree(
    uploader: CaxaUploader,
    relations: list[dict],
    part_exists_cache: dict[str, bool] | None = None,
    log=print,
    *,
    force: bool = False,
) -> dict[str, bool]:
    """Decide own/borrow from the live CAXA design tree.

    Child absent -> ``own`` (Reference=False); child present -> ``borrow``.
    Manual choices win.  A previous automatic decision is kept unless
    ``force`` (an explicit re-check) is requested; this keeps retries stable.
    """
    cache = part_exists_cache if part_exists_cache is not None else {}
    for rel in relations:
        parent = drawing_bundle.normalize_code(rel.get("parent_code"))
        child = drawing_bundle.normalize_code(rel.get("child_code"))
        if not parent or not child:
            continue
        if rel.get("manual_override"):
            if child not in cache:
                cache[child] = uploader.find_part(child) is not None
            rel["tree_exists"] = cache[child]
            continue
        if rel.get("auto_decided") and not force:
            continue
        if child not in cache:
            cache[child] = uploader.find_part(child) is not None
        exists_in_tree = cache[child]
        rel["tree_exists"] = exists_in_tree
        new_type = "borrow" if exists_in_tree else "own"
        if rel.get("link_type") != new_type:
            rel["link_type"] = new_type
            log(
                "设计树预检：%s ← %s 判定为【%s】（树中%s）"
                % (
                    parent,
                    child,
                    "借用" if exists_in_tree else "归属子件",
                    "已存在" if exists_in_tree else "不存在",
                )
            )
        rel["auto_decided"] = True
    return cache


# ---------------------------------------------------------------------------
# plan identity
# ---------------------------------------------------------------------------


def _item_plan_entry(item: dict) -> dict:
    """Fields that define *content*; status/selection/workflow are not part."""
    return {
        "code": drawing_bundle.normalize_code(item.get("code")),
        "name": str(item.get("name") or ""),
        "kind": str(item.get("kind") or ""),
        "machine": drawing_bundle.normalize_code(item.get("machine")),
        "sha256": str(item.get("sha256") or ""),
        "size": item.get("size"),
    }


def content_digest(manifest: dict, account: dict) -> str:
    payload = {
        "server": str(account.get("ip") or ""),
        "port": int(account.get("port") or 0),
        "user": str(account.get("user") or ""),
        "items": sorted(
            (_item_plan_entry(item) for item in manifest.get("items") or []
             if isinstance(item, dict)),
            key=lambda entry: (entry["code"], entry["sha256"]),
        ),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan_digest(manifest: dict, account: dict, **_ignored) -> str:
    """Backwards-compatible name: the batch identity is the content only."""
    return content_digest(manifest, account)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _result_row(item: dict, status: str, detail: str = "", **extra) -> dict:
    row = {
        "code": drawing_bundle.normalize_code(item.get("code")),
        "name": str(item.get("name") or ""),
        "file": os.path.basename(str(item.get("file_path") or "")),
        "status": status,
        "detail": detail,
    }
    row.update(extra)
    return row


def _record_value(record: dict | None, key: str, default=""):
    if not record:
        return default
    value = record.get(key)
    return default if value is None else value


def _is_drawing_row(record: dict | None) -> bool:
    if not record:
        return False
    try:
        if int(record.get("class_id") or 0) == CLASS_ID_DRAWING:
            return True
    except (TypeError, ValueError):
        pass
    name = "".join(ch for ch in str(record.get("class_name") or "").lower() if ch.isalnum())
    return name == "drawing"


def _confirmed_drawing_ids(confirmed: dict[str, dict], codes: set[str]) -> list[int]:
    ids: set[int] = set()
    for code, record in (confirmed or {}).items():
        if code not in codes or not _is_drawing_row(record):
            continue
        remote = record.get("remote_id")
        try:
            ids.add(int(remote))
        except (TypeError, ValueError):
            continue
    return sorted(ids)


def _workflow_from_record(record: dict) -> dict:
    """Restore the workflow result from the stored JSON detail."""
    payload = {}
    detail = record.get("detail") or ""
    try:
        payload = json.loads(detail) if detail else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    drawing_ids = []
    for value in payload.get("drawing_ids") or []:
        try:
            drawing_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return {
        "status": WORKFLOW_STARTED,
        "process_instance_id": record.get("remote_id") or None,
        "drawing_ids": drawing_ids,
        "started": bool(payload.get("started", True)),
        "restored": True,
    }


def content_lock_key(digest: str, account: dict) -> str:
    """One lock per batch content + login, covering lookup/create/writes."""
    seed = "%s|%s|%s|%s" % (
        digest,
        account.get("ip") or "",
        int(account.get("port") or 0),
        account.get("user") or "",
    )
    return "content-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _pending_error(pending: list[dict]) -> PendingVerificationError:
    detail = "；".join(
        "%s %s(%s)" % (row["kind"], row["key"], row["status"]) for row in pending[:8]
    )
    return PendingVerificationError(
        "本批次存在结果待核实/中断的操作，已阻止自动重放，请先用核实命令处理：%s"
        % detail,
        pending=pending,
    )


def _phase_blockers(failures, pending_codes, relation_failures, relation_pending,
                    drawing_ids) -> list[str]:
    blockers = []
    if failures:
        blockers.append("%d 项图纸上传失败" % len(failures))
    if pending_codes:
        blockers.append("%d 项上传结果待核实" % len(pending_codes))
    if relation_failures:
        blockers.append("%d 条必需关系未成功" % relation_failures)
    if relation_pending:
        blockers.append("%d 条关系结果待核实" % relation_pending)
    if not drawing_ids:
        blockers.append("没有已验证的 Drawing 可关联")
    return blockers


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


def upload_bundle(manifest: dict, account: dict, *, on_duplicate: str = "append_iteration",
                  borrow_missing: bool = True, start_workflow: bool = False,
                  workflow_template_id: int = 1,
                  workflow_template_instance_id: int = 31,
                  workflow_process_def_id: str = "2",
                  workflow_activity_def_id: str = "4",
                  performer_name: str = "审核人",
                  log=print, progress=None, journal=None, journal_path: str | None = None,
                  force_new_task: bool = False, auto_confirm: str = "",
                  staging_root: str | None = None, recheck_tree: bool = False,
                  bundle_path: str = "") -> dict:
    items = list(manifest.get("items") or [])
    relations = drawing_bundle.normalize_manifest_relations(manifest, log=log)
    selected = _selected_items(manifest)
    selected_codes = {drawing_bundle.normalize_code(item.get("code")) for item in selected}

    # 1) validate only the rows that will be executed (structure is checked by
    #    the bundle validator for every row).
    for item in selected:
        problems = drawing_bundle.item_problems(item, check_content=True)
        if problems:
            raise CaxaError(
                "第 %s 项校验失败：%s" % (item.get("code") or "?", "；".join(problems)),
                stage="plan_validate",
            )
    bundle_errors = drawing_bundle.validate_bundle(manifest)
    if bundle_errors:
        raise CaxaError(
            "上传包校验失败：\n" + "\n".join(bundle_errors[:20]), stage="plan_validate"
        )

    digest = content_digest(manifest, account)
    owns_journal = journal is None
    journal = journal or journal_module.TaskJournal(journal_path)
    bridge = None
    uploader = None
    task = None
    lock = None
    content_lock = None
    try:
        # Single-run guard must cover lookup/create as well as the writes,
        # otherwise two processes can create two tasks for the same batch.
        try:
            content_lock = journal.run_lock(content_lock_key(digest, account))
            content_lock.__enter__()
        except journal_module.JournalBusy as exc:
            raise PendingVerificationError(str(exc)) from exc
        existing = journal.find_task(
            digest=digest,
            server=str(account.get("ip") or ""),
            port=int(account.get("port") or 0),
            user=str(account.get("user") or ""),
        )
        # Any attempt of this batch with an unknown outcome blocks replay,
        # including older attempts and even an explicit "new task".
        for previous in journal.tasks_for_digest(
            digest=digest,
            server=str(account.get("ip") or ""),
            port=int(account.get("port") or 0),
            user=str(account.get("user") or ""),
        ):
            pending = journal.pending_operations(previous["task_id"])
            if pending:
                raise _pending_error(pending)
        if existing and not force_new_task:
            task = existing
            log("继续本批次任务：%s（状态 %s）" % (task["task_id"], task["status"]))
        else:
            task = journal.create_task(
                digest=digest,
                server=str(account.get("ip") or ""),
                port=int(account.get("port") or 0),
                user=str(account.get("user") or ""),
                bundle_path=bundle_path,
                note="force_new_task" if force_new_task else "",
            )
        task_id = task["task_id"]
        try:
            lock = journal.run_lock(task_id)
            lock.__enter__()
        except journal_module.JournalBusy as exc:
            raise PendingVerificationError(str(exc)) from exc

        confirmed = journal.confirmed_uploads(task_id)
        decisions = journal.relation_decisions(task_id)
        confirmed_relations = journal.confirmed_relations(task_id)
        stored_workflow = journal.workflow(task_id)

        # Reconcile "已上传" claims against this machine's journal only.
        for item in items:
            if not isinstance(item, dict):
                continue
            code = drawing_bundle.normalize_code(item.get("code"))
            if item.get("status") == ITEM_DONE and code not in confirmed:
                item["status"] = ITEM_PENDING
                item.pop("drawing_id", None)
                item.pop("class_name", None)
                item.pop("class_id", None)
                item["note"] = "包内的“已上传”状态缺少本机记录，已重新纳入上传"

        todo = [
            item for item in selected
            if drawing_bundle.normalize_code(item.get("code")) not in confirmed
        ]
        relations_pending: list[dict] = []
        for rel in relations:
            key = (
                drawing_bundle.normalize_code(rel.get("parent_code")),
                drawing_bundle.normalize_code(rel.get("child_code")),
            )
            record = confirmed_relations.get(key)
            if record and str(record.get("link_type") or "") == str(rel.get("link_type") or ""):
                rel["link_status"] = "已存在"
                continue
            relations_pending.append(rel)

        workflow_missing = bool(start_workflow) and not (
            stored_workflow and stored_workflow["status"] == journal_module.OP_OK
        )
        needs_connection = bool(todo or relations_pending or workflow_missing)

        # 2) connect only when there is work (or classification needs the tree)
        if needs_connection:
            bridge = CaxaBridge()
            log("登录 CAXA：%s@%s:%s" % (account["user"], account["ip"], account["port"]))
            bridge.login(
                account["ip"], int(account["port"]), account["user"], account["password"]
            )
            uploader = CaxaUploader(
                bridge,
                log=log,
                on_duplicate=on_duplicate,
                staging_dir=staging_module.TaskStaging(task_id, root=staging_root, log=log),
                auto_confirm=auto_confirm,
            )
            user_id = find_person_id(bridge, account["user"])
            log("当前 CAXA 人员：%s（Person %s）" % (account["user"], user_id))
        else:
            user_id = None

        # 3) classify AFTER the task is known, BEFORE any write; restore the
        #    classification recorded by this task if present.
        pending_keys = {
            (
                drawing_bundle.normalize_code(rel.get("parent_code")),
                drawing_bundle.normalize_code(rel.get("child_code")),
            )
            for rel in relations_pending
        }
        if relations:
            cache: dict[str, bool] = {}
            for rel in relations:
                key = (
                    drawing_bundle.normalize_code(rel.get("parent_code")),
                    drawing_bundle.normalize_code(rel.get("child_code")),
                )
                if key not in pending_keys:
                    continue
                if rel.get("manual_override"):
                    # The human choice is authoritative; no tree query needed.
                    rel["auto_decided"] = False
                    continue
                if key in decisions and not recheck_tree:
                    rel["link_type"] = decisions[key]
                    rel["auto_decided"] = True
                    continue
                # First run of this task: ask the server, do not trust a value
                # imported in the package.
                rel["auto_decided"] = False
                if uploader is None:
                    raise CaxaError(
                        "需要连接 CAXA 以判定关系类型（own/borrow）", stage="plan_relations"
                    )
                if key[1] not in cache:
                    cache[key[1]] = uploader.find_part(key[1]) is not None
                rel["tree_exists"] = cache[key[1]]
                rel["link_type"] = "borrow" if cache[key[1]] else "own"
                rel["auto_decided"] = True

            analysis = relations_module.analyze_relations(relations)
            if analysis["issues"]:
                raise CaxaError(
                    "关系存在冲突，请先修正：\n"
                    + "\n".join(issue["message"] for issue in analysis["issues"]),
                    stage="plan_relations",
                )
            # 4) persist decisions before ANY remote write
            for rel in relations:
                if rel.get("manual_override"):
                    continue
                journal.record_operation(
                    task_id,
                    "decision",
                    journal_module.relation_key(
                        rel.get("parent_code"), rel.get("child_code")
                    ),
                    status=journal_module.OP_OK,
                    phase="classify",
                    parent_code=drawing_bundle.normalize_code(rel.get("parent_code")),
                    child_code=drawing_bundle.normalize_code(rel.get("child_code")),
                    link_type=str(rel.get("link_type") or ""),
                )

        batch_codes = {
            drawing_bundle.normalize_code(item.get("code")) for item in items if item.get("code")
        }
        own_parent_map: dict[str, str] = {}
        relation_by_child: dict[str, dict] = {}
        for rel in relations:
            if rel.get("link_type") == "own":
                own_parent_map[drawing_bundle.normalize_code(rel.get("child_code"))] = (
                    drawing_bundle.normalize_code(rel.get("parent_code"))
                )
            relation_by_child.setdefault(
                drawing_bundle.normalize_code(rel.get("child_code")), rel
            )

        journal.set_task_stage(task_id, "upload")
        upload_results: list[dict] = []
        failures: list[dict] = []
        uncertain: list[dict] = []
        pending_upload_codes: set[str] = set()

        for item in selected:
            code = drawing_bundle.normalize_code(item.get("code"))
            record = confirmed.get(code)
            if record:
                item["status"] = ITEM_DONE
                item["drawing_id"] = int(record["remote_id"]) if record.get("remote_id") else None
                item["class_name"] = record.get("class_name") or ""
                item["class_id"] = record.get("class_id") or ""
                item["iterator_no"] = record.get("iterator_no") or ""
                item["note"] = "本任务已确认上传，未重复写入"
                upload_results.append(
                    _result_row(
                        item, ITEM_DONE, "本任务已确认上传",
                        drawing_id=item.get("drawing_id"),
                        class_name=item.get("class_name"),
                    )
                )

        ordered = sorted(
            todo, key=lambda item: 0 if item.get("kind") == "machine_drawing" else 1
        )
        ordered = _upload_order(ordered, relations)
        total = len(ordered)
        for index, item in enumerate(ordered):
            code = drawing_bundle.normalize_code(item.get("code"))
            if progress:
                progress(index, total, "正在上传 %s" % (code or "未命名"))
            journal.set_task_stage(task_id, "upload:%s" % code)
            journal.record_operation(
                task_id, "upload", code, status=journal_module.OP_RUNNING,
                phase="upload", item_code=code,
            )
            try:
                explicit_parent = own_parent_map.get(code) or drawing_bundle.normalize_code(
                    item.get("parent_code")
                )
                rel = relation_by_child.get(code) or {}
                result = uploader.upload_item(
                    item,
                    batch_codes,
                    on_machine_missing=None,
                    explicit_parent=explicit_parent,
                    relation_type=str(rel.get("link_type") or ""),
                )
                class_id = result.get("class_id")
                item["status"] = ITEM_DONE
                item["target"] = result.get("target", "")
                item["note"] = result.get("file_name", "")
                item["caxa_path"] = result.get("caxa_path", "")
                item["drawing_id"] = result.get("object_id")
                item["class_name"] = result.get("class_name")
                item["class_id"] = class_id
                item["iterator_no"] = result.get("iterator_no") or ""
                journal.record_operation(
                    task_id, "upload", code, status=journal_module.OP_OK,
                    phase="upload", item_code=code,
                    remote_id=result.get("object_id"),
                    class_id=class_id if class_id is not None else "",
                    iterator_no=str(result.get("iterator_no") or ""),
                    class_name=result.get("class_name") or "",
                    detail=str(result.get("file_name") or ""),
                )
                upload_results.append(
                    _result_row(
                        item, ITEM_DONE, item.get("caxa_path", ""),
                        drawing_id=result.get("object_id"),
                        class_name=result.get("class_name"),
                    )
                )
                if result.get("created_new_part") and result.get("linked_parent_identity"):
                    linked_parent = drawing_bundle.normalize_code(
                        result.get("linked_parent_identity")
                    )
                    for rel2 in relations:
                        if (
                            drawing_bundle.normalize_code(rel2.get("parent_code")) == linked_parent
                            and drawing_bundle.normalize_code(rel2.get("child_code")) == code
                        ):
                            rel2["link_status"] = "已归属"
                            journal.record_operation(
                                task_id, "relation",
                                journal_module.relation_key(linked_parent, code),
                                status=journal_module.OP_OK,
                                phase="relation", parent_code=linked_parent,
                                child_code=code, link_type="own",
                                detail="新建归属子件时已挂接",
                            )
                log("已上传 %s -> %s" % (code, item.get("target") or code))
            except PublishedDrawingError as exc:
                item["status"] = "失败：图纸已发布，无法追加版次"
                item["note"] = str(exc)
                failures.append(item)
                journal.record_operation(
                    task_id, "upload", code, status=journal_module.OP_FAILED,
                    phase="upload", item_code=code, error=str(exc),
                )
                log("上传阻止 %s：%s" % (code, exc))
            except MachineMissing as exc:
                item["status"] = "整机不存在"
                item["note"] = str(exc)
                failures.append(item)
                journal.record_operation(
                    task_id, "upload", code, status=journal_module.OP_FAILED,
                    phase="upload", item_code=code, error=str(exc),
                )
                log("整机不存在：%s" % exc)
            except (CaxaError, OSError, ValueError) as exc:
                uncertain_flag = bool(getattr(exc, "uncertain", False))
                partial = bool(getattr(exc, "partial", False))
                stage = getattr(exc, "stage", "") or ""
                error_result = getattr(exc, "result", {}) or {}
                if partial:
                    item["status"] = ITEM_PARTIAL
                elif uncertain_flag:
                    item["status"] = ITEM_UNCERTAIN
                else:
                    item["status"] = ITEM_FAILED
                item["note"] = str(exc)
                if uncertain_flag or partial:
                    uncertain.append(item)
                    pending_upload_codes.add(code)
                    journal.record_operation(
                        task_id, "upload", code, status=journal_module.OP_PENDING,
                        phase="upload:%s" % stage, item_code=code, error=str(exc),
                        remote_id=error_result.get("object_id")
                        or error_result.get("created_part_id") or "",
                        class_id=error_result.get("class_id") or "",
                        iterator_no=str(error_result.get("iterator_no") or ""),
                        detail=json.dumps(
                            {"partial": partial, "stage": stage,
                             "result": error_result},
                            ensure_ascii=False,
                        ),
                    )
                else:
                    failures.append(item)
                    journal.record_operation(
                        task_id, "upload", code, status=journal_module.OP_FAILED,
                        phase="upload:%s" % stage, item_code=code, error=str(exc),
                    )
                log("上传未完成 %s（%s）：%s" % (code, item["status"], exc))

        # ---- relations ----
        if progress:
            progress(total, total, "正在处理次级/借用关系")
        relation_rows: list[dict] = []
        relation_failures = 0
        relation_pending = 0
        relation_executed = 0
        for rel in relations:
            parent = drawing_bundle.normalize_code(rel.get("parent_code"))
            child = drawing_bundle.normalize_code(rel.get("child_code"))
            if not parent or not child:
                continue
            key_tuple = (parent, child)
            key = journal_module.relation_key(parent, child)
            link_type = str(rel.get("link_type") or "borrow")
            record = confirmed_relations.get(key_tuple)
            if record and str(record.get("link_type") or "") == link_type:
                rel["link_status"] = "已存在"
                continue
            if str(rel.get("link_status") or "") in relations_module.PENDING_STATUSES:
                relation_pending += 1
                relation_rows.append(
                    {
                        "parent_code": parent, "child_code": child,
                        "link_type": link_type, "status": "待核实",
                        "note": rel.get("note") or "上次结果待核实",
                    }
                )
                continue
            if child in pending_upload_codes:
                rel["link_status"] = "缺子"
                relation_failures += 1
                relation_rows.append(
                    {
                        "parent_code": parent, "child_code": child,
                        "link_type": link_type, "status": "缺子",
                        "note": "子件上传结果待核实，关系未执行",
                    }
                )
                journal.record_operation(
                    task_id, "relation", key, status=journal_module.OP_PENDING,
                    phase="relation", parent_code=parent, child_code=child,
                    link_type=link_type, error="子件上传结果待核实",
                )
                continue
            child_item = next(
                (it for it in items
                 if drawing_bundle.normalize_code(it.get("code")) == child),
                None,
            )
            child_failed = child_item is not None and child_item.get("status") in (
                ITEM_FAILED, ITEM_PARTIAL, ITEM_UNCERTAIN, "整机不存在",
                "失败：图纸已发布，无法追加版次", ITEM_SKIPPED,
            )
            if child_failed:
                rel["link_status"] = "缺子"
                relation_failures += 1
                relation_rows.append(
                    {
                        "parent_code": parent, "child_code": child,
                        "link_type": link_type, "status": "缺子",
                        "note": "子件上传失败，关系未执行",
                    }
                )
                journal.record_operation(
                    task_id, "relation", key, status=journal_module.OP_FAILED,
                    phase="relation", parent_code=parent, child_code=child,
                    link_type=link_type, error="子件上传失败",
                )
                continue
            if not borrow_missing:
                rel["link_status"] = "未执行"
                relation_failures += 1
                relation_rows.append(
                    {
                        "parent_code": parent, "child_code": child,
                        "link_type": link_type, "status": "未执行",
                        "note": "本次未启用关系执行（borrow_missing=false），必需关系未完成",
                    }
                )
                log("必须执行的关系未执行：%s ← %s" % (parent, child))
                continue
            journal.set_task_stage(task_id, "relation:%s" % key)
            journal.record_operation(
                task_id, "relation", key, status=journal_module.OP_RUNNING,
                phase="relation", parent_code=parent, child_code=child,
                link_type=link_type,
            )
            try:
                row = uploader.link_relation(parent, child, link_type)
            except CaxaError as exc:
                row = {
                    "parent_code": parent,
                    "child_code": child,
                    "link_type": link_type,
                    "status": "待核实" if getattr(exc, "uncertain", False) else "失败",
                    "note": str(exc),
                }
            row["parent_code"] = parent
            row["child_code"] = child
            row["link_type"] = link_type
            relation_rows.append(row)
            rel["link_status"] = row["status"]
            rel["note"] = row.get("note") or ""
            relation_executed += 1
            if row["status"] in ("已借用", "已归属", "已存在"):
                journal.record_operation(
                    task_id, "relation", key, status=journal_module.OP_OK,
                    phase="relation", parent_code=parent, child_code=child,
                    link_type=link_type, detail=row.get("note") or row["status"],
                )
            elif row["status"] == "待核实":
                relation_pending += 1
                journal.record_operation(
                    task_id, "relation", key, status=journal_module.OP_PENDING,
                    phase="relation", parent_code=parent, child_code=child,
                    link_type=link_type, error=row.get("note") or "",
                )
            elif row["status"] == "冲突":
                relation_failures += 1
                journal.record_operation(
                    task_id, "relation", key, status=journal_module.OP_FAILED,
                    phase="relation", parent_code=parent, child_code=child,
                    link_type=link_type, error=row.get("note") or "已有链接类型冲突",
                )
            else:
                relation_failures += 1
                journal.record_operation(
                    task_id, "relation", key, status=journal_module.OP_FAILED,
                    phase="relation", parent_code=parent, child_code=child,
                    link_type=link_type, error=row.get("note") or "",
                )

        # ---- workflow ----
        drawing_ids = _confirmed_drawing_ids(
            journal.confirmed_uploads(task_id), selected_codes
        )
        workflow_result: dict | None = None
        workflow_blocked = ""
        workflow_started_now = False
        if start_workflow:
            if stored_workflow and stored_workflow["status"] == journal_module.OP_OK:
                workflow_result = _workflow_from_record(stored_workflow)
                workflow_result["duplicate"] = True
                workflow_result["note"] = (
                    "本任务已发起过该流程，未重复创建；如审核人/模板需要变更，请人工处理"
                )
                log("流程已存在（本任务已发起）：%s" % workflow_result.get("process_instance_id"))
            elif stored_workflow and stored_workflow["status"] in (
                journal_module.OP_PENDING, journal_module.OP_RUNNING,
            ):
                workflow_blocked = "上次流程结果待核实，未重复创建"
                workflow_result = {
                    "status": WORKFLOW_PENDING,
                    "process_instance_id": stored_workflow.get("remote_id") or None,
                    "reason": workflow_blocked,
                    "drawing_ids": drawing_ids,
                }
                log("%s（流程ID %s）" % (workflow_blocked, workflow_result["process_instance_id"]))
            else:
                blockers = _phase_blockers(
                    failures, pending_upload_codes, relation_failures,
                    relation_pending, drawing_ids,
                )
                if blockers:
                    workflow_blocked = "已阻止发起流程：" + "、".join(blockers)
                    workflow_result = {
                        "status": WORKFLOW_BLOCKED,
                        "reason": workflow_blocked,
                        "drawing_ids": drawing_ids,
                    }
                    log(workflow_blocked)
                else:
                    if progress:
                        progress(total, total, "正在发起审核打印流程")
                    journal.set_task_stage(task_id, "workflow")
                    journal.record_workflow(
                        task_id, status=journal_module.OP_RUNNING, process_id=None,
                        stage="create", drawing_ids=drawing_ids,
                    )
                    try:
                        performer_id = find_person_id(bridge, performer_name)
                        raw = bridge.start_workflow(
                            drawing_ids,
                            account["ip"],
                            int(account["port"]),
                            user_id,
                            performer_id=performer_id,
                            template_id=workflow_template_id,
                            template_instance_id=workflow_template_instance_id,
                            process_def_id=workflow_process_def_id,
                            activity_def_id=workflow_activity_def_id,
                            execute=True,
                        )
                        process_id = raw.get("process_instance_id")
                        started = raw.get("started") is True
                        process_state = str(raw.get("process_state") or "")
                        state_verified = (
                            raw.get("state_verified") is True
                            and process_state == "2"
                        )
                        try:
                            positive = int(process_id or 0) > 0
                        except (TypeError, ValueError):
                            positive = False
                        # A successful start call is insufficient. Require a
                        # live state read (2 OPEN__RUNNING) and the exact
                        # document set read back from the process object.
                        link_ids = raw.get("linked_document_ids")
                        linkage_ok = False
                        if (
                            raw.get("documents_verified") is True
                            and isinstance(link_ids, list)
                        ):
                            try:
                                linkage_ok = (
                                    set(int(v) for v in link_ids)
                                    == set(int(v) for v in drawing_ids)
                                )
                            except (TypeError, ValueError):
                                linkage_ok = False
                        if not started or not positive or not state_verified or not linkage_ok:
                            workflow_result = {
                                "status": WORKFLOW_PENDING,
                                "process_instance_id": process_id,
                                "reason": (
                                    "流程返回未确认启动（started=%s, id=%s, "
                                    "state=%s, 状态回读=%s, 文档回读=%s/%s）"
                                    % (
                                        raw.get("started"),
                                        process_id,
                                        process_state or "未知",
                                        state_verified,
                                        len(link_ids) if isinstance(link_ids, list) else "未知",
                                        len(drawing_ids),
                                    )
                                ),
                                "drawing_ids": drawing_ids,
                            }
                            journal.record_workflow(
                                task_id, status=journal_module.OP_PENDING,
                                process_id=process_id, stage="started_unconfirmed",
                                drawing_ids=drawing_ids, started=False,
                                error=workflow_result["reason"],
                            )
                            log("流程启动未确认：%s" % workflow_result["reason"])
                        else:
                            workflow_result = {
                                "status": WORKFLOW_STARTED,
                                "process_instance_id": process_id,
                                "drawing_ids": drawing_ids,
                                "started": True,
                                "process_state": process_state,
                                "start_time": raw.get("process_start_time") or "",
                                "linked_document_ids": [int(v) for v in link_ids],
                            }
                            workflow_started_now = True
                            journal.record_workflow(
                                task_id, status=journal_module.OP_OK,
                                process_id=process_id, stage="started",
                                drawing_ids=drawing_ids, started=True,
                                detail="state=%s, linked_documents=%s"
                                % (process_state, len(link_ids)),
                            )
                            log("流程已发起：WFProcessInst %s" % process_id)
                    except CaxaError as exc:
                        process_id = getattr(exc, "created_process_instance_id", None)
                        uncertain_flag = bool(getattr(exc, "uncertain", False)) or bool(process_id)
                        workflow_result = {
                            "status": WORKFLOW_PENDING if uncertain_flag else WORKFLOW_FAILED,
                            "process_instance_id": process_id,
                            "reason": str(exc),
                            "stage": getattr(exc, "stage", ""),
                            "drawing_ids": drawing_ids,
                        }
                        journal.record_workflow(
                            task_id,
                            status=journal_module.OP_PENDING if uncertain_flag
                            else journal_module.OP_FAILED,
                            process_id=process_id,
                            stage=getattr(exc, "stage", "") or "workflow",
                            drawing_ids=drawing_ids,
                            error=str(exc),
                        )
                        log("流程未确认完成：%s（流程ID %s）" % (exc, process_id))

        uploaded_total = sum(1 for item in items if item.get("status") == ITEM_DONE)
        ok = (
            not failures
            and not pending_upload_codes
            and relation_failures == 0
            and relation_pending == 0
            and not workflow_blocked
            and (not start_workflow or (workflow_result or {}).get("status") == WORKFLOW_STARTED)
        )
        if pending_upload_codes or (
            workflow_result and workflow_result.get("status") == WORKFLOW_PENDING
        ):
            task_status = journal_module.TASK_PENDING
        elif ok:
            task_status = journal_module.TASK_DONE
        else:
            task_status = journal_module.TASK_FAILED
        journal.set_task_status(task_id, task_status, stage="finished")

        duplicate = not todo and not relation_executed and not workflow_started_now
        if duplicate and not ok:
            duplicate = False
        return {
            "ok": ok,
            "task_id": task_id,
            "duplicate": duplicate,
            "items": items,
            "results": upload_results,
            "uploaded": uploaded_total,
            "failed": len(failures),
            "uncertain": len(pending_upload_codes),
            "pending": [
                {"code": item.get("code"), "note": item.get("note")}
                for item in uncertain
            ],
            "relations": relation_rows,
            "relation_failures": relation_failures,
            "relation_pending": relation_pending,
            "borrow_rows": relation_rows,
            "borrow_total": sum(
                1 for row in relation_rows if row["status"] in ("已借用", "已归属")
            ),
            "borrow_skipped": sum(1 for row in relation_rows if row["status"] == "已存在"),
            "workflow": workflow_result,
            "workflow_blocked": workflow_blocked,
            "task_status": task_status,
            "drawing_ids": drawing_ids,
            "warnings": [
                warning
                for item in items
                for warning in drawing_bundle.filename_warnings(item)
            ],
        }
    except Exception as exc:
        if task is not None:
            try:
                pending_now = journal.pending_operations(task["task_id"])
            except Exception:
                pending_now = []
            status = (
                journal_module.TASK_PENDING
                if (pending_now or getattr(exc, "uncertain", False))
                else journal_module.TASK_FAILED
            )
            try:
                journal.set_task_status(task["task_id"], status, stage="aborted", note=str(exc))
            except Exception:
                pass
        raise
    finally:
        if lock is not None:
            try:
                lock.__exit__(None, None, None)
            except Exception:
                pass
        if content_lock is not None:
            try:
                content_lock.__exit__(None, None, None)
            except Exception:
                pass
        if uploader is not None:
            try:
                uploader.close()
            except Exception:
                pass
        if bridge is not None:
            bridge.stop()
        if owns_journal:
            journal.close()


# ---------------------------------------------------------------------------
# preview / pre-check / manual relations
# ---------------------------------------------------------------------------


def preview_bundle(manifest: dict) -> dict:
    selected = _selected_items(manifest)
    missing = [
        item for item in selected if not os.path.isfile(str(item.get("file_path") or ""))
    ]
    invalid = []
    fingerprints = []
    for item in selected:
        problems = drawing_bundle.item_problems(item, check_content=False)
        if problems:
            invalid.append((item, problems))
        fingerprints.extend(
            (item, problem) for problem in drawing_bundle.verify_fingerprint(item)
        )
    relations = drawing_bundle.normalize_manifest_relations(manifest)
    issues = relations_module.analyze_relations(relations)["issues"]
    return {
        "items": len(manifest.get("items") or []),
        "selected": len(selected),
        "missing_files": len(missing),
        "invalid_names": len(invalid),
        "invalid_details": [
            "%s：%s" % (item.get("code"), "；".join(problems)) for item, problems in invalid
        ],
        "fingerprint_errors": len(fingerprints),
        "fingerprint_details": [
            "%s：%s" % (item.get("code"), problem) for item, problem in fingerprints
        ],
        "relation_issues": issues,
        "ready": not missing and not invalid and not fingerprints and bool(selected),
    }


def precheck_with_caxa(manifest: dict, account: dict, log=print) -> dict:
    """Calibrate own/borrow against the live tree (explicit user action)."""
    relations = drawing_bundle.normalize_manifest_relations(manifest)
    bridge = None
    uploader = None
    try:
        bridge = CaxaBridge()
        log("预检连接 CAXA：%s@%s:%s" % (account["user"], account["ip"], account["port"]))
        bridge.login(
            account["ip"], int(account["port"]), account["user"], account["password"]
        )
        uploader = CaxaUploader(bridge, log=log)
        cache = refine_relations_from_tree(uploader, relations, log=log, force=True)
        analysis = relations_module.analyze_relations(relations)
        own_count = sum(1 for rel in relations if rel.get("link_type") == "own")
        borrow_count = sum(1 for rel in relations if rel.get("link_type") != "own")
        return {
            "checked_relations": len(relations),
            "own_count": own_count,
            "borrow_count": borrow_count,
            "tree_cache": cache,
            "issues": analysis["issues"],
        }
    finally:
        if uploader is not None:
            uploader.close()
        if bridge is not None:
            bridge.stop()


def apply_manual_borrows(manifest: dict, account: dict, log=print,
                         journal=None, journal_path: str | None = None) -> dict:
    """Execute the selected relations only: read-only lookups, no new parts."""
    relations = drawing_bundle.normalize_manifest_relations(manifest)
    batch_codes = {
        drawing_bundle.normalize_code(item.get("code"))
        for item in (manifest.get("items") or [])
        if item.get("code")
    }
    target_relations = [
        rel for rel in relations
        if drawing_bundle.normalize_code(rel.get("child_code")) not in batch_codes
        or rel.get("source") == "manual"
    ]
    if not target_relations:
        raise CaxaError("没有可执行的次级/借用条目")

    digest = hashlib.sha256(
        json.dumps(
            {
                "server": str(account.get("ip") or ""),
                "port": int(account.get("port") or 0),
                "user": str(account.get("user") or ""),
                "relations": sorted(
                    [
                        {
                            "parent": drawing_bundle.normalize_code(rel.get("parent_code")),
                            "child": drawing_bundle.normalize_code(rel.get("child_code")),
                            "link_type": (
                                rel.get("link_type") if rel.get("manual_override") else "auto"
                            ),
                        }
                        for rel in target_relations
                    ],
                    key=lambda entry: (entry["parent"], entry["child"]),
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    owns_journal = journal is None
    journal = journal or journal_module.TaskJournal(journal_path)
    bridge = None
    uploader = None
    task = None
    lock = None
    content_lock = None
    rows: list[dict] = []
    try:
        try:
            content_lock = journal.run_lock(content_lock_key(digest, account))
            content_lock.__enter__()
        except journal_module.JournalBusy as exc:
            raise PendingVerificationError(str(exc)) from exc
        existing = journal.find_task(
            digest=digest,
            server=str(account.get("ip") or ""),
            port=int(account.get("port") or 0),
            user=str(account.get("user") or ""),
        )
        for previous in journal.tasks_for_digest(
            digest=digest,
            server=str(account.get("ip") or ""),
            port=int(account.get("port") or 0),
            user=str(account.get("user") or ""),
        ):
            pending_previous = journal.pending_operations(previous["task_id"])
            if pending_previous:
                raise _pending_error(pending_previous)
        task = existing or journal.create_task(
            digest=digest,
            server=str(account.get("ip") or ""),
            port=int(account.get("port") or 0),
            user=str(account.get("user") or ""),
            note="manual_borrow",
        )
        try:
            lock = journal.run_lock(task["task_id"])
            lock.__enter__()
        except journal_module.JournalBusy as exc:
            raise PendingVerificationError(str(exc)) from exc

        bridge = CaxaBridge()
        log("登录 CAXA：%s@%s:%s" % (account["user"], account["ip"], account["port"]))
        bridge.login(
            account["ip"], int(account["port"]), account["user"], account["password"]
        )
        uploader = CaxaUploader(bridge, log=log, on_duplicate="reuse")
        refine_relations_from_tree(uploader, target_relations, log=log)
        analysis = relations_module.analyze_relations(target_relations)
        if analysis["issues"]:
            raise CaxaError(
                "关系存在冲突，请先修正：\n"
                + "\n".join(issue["message"] for issue in analysis["issues"]),
                stage="plan_relations",
            )
        confirmed = journal.confirmed_relations(task["task_id"])
        failures = 0
        pending = 0
        for rel in target_relations:
            parent = drawing_bundle.normalize_code(rel.get("parent_code"))
            child = drawing_bundle.normalize_code(rel.get("child_code"))
            if not parent or not child:
                continue
            link_type = str(rel.get("link_type") or "borrow")
            key_tuple = (parent, child)
            key = journal_module.relation_key(parent, child)
            record = confirmed.get(key_tuple)
            if record and str(record.get("link_type") or "") == link_type:
                rows.append(
                    {
                        "parent_code": parent, "child_code": child,
                        "link_type": link_type, "status": "已存在",
                        "note": "本任务已确认",
                    }
                )
                continue
            journal.record_operation(
                task["task_id"], "relation", key, status=journal_module.OP_RUNNING,
                phase="relation", parent_code=parent, child_code=child,
                link_type=link_type,
            )
            try:
                row = uploader.link_relation(parent, child, link_type)
            except CaxaError as exc:
                row = {
                    "parent_code": parent, "child_code": child,
                    "link_type": link_type,
                    "status": "待核实" if getattr(exc, "uncertain", False) else "失败",
                    "note": str(exc),
                }
            row["parent_code"] = parent
            row["child_code"] = child
            row["link_type"] = link_type
            rows.append(row)
            rel["link_status"] = row["status"]
            rel["note"] = row.get("note") or ""
            if row["status"] in ("已借用", "已归属", "已存在"):
                status = journal_module.OP_OK
            elif row["status"] == "待核实":
                status = journal_module.OP_PENDING
                pending += 1
            elif row["status"] in ("缺父", "缺子"):
                status = journal_module.OP_FAILED
                failures += 1
            else:
                status = journal_module.OP_FAILED
                failures += 1
            journal.record_operation(
                task["task_id"], "relation", key, status=status, phase="relation",
                parent_code=parent, child_code=child, link_type=link_type,
                detail=row.get("note") or row["status"],
                error=row.get("note") if status == journal_module.OP_FAILED else "",
            )
        journal.set_task_status(
            task["task_id"],
            journal_module.TASK_PENDING if pending
            else journal_module.TASK_FAILED if failures
            else journal_module.TASK_DONE,
            stage="finished",
        )
        return {
            "borrowed": sum(1 for row in rows if row["status"] in ("已借用", "已归属")),
            "skipped": sum(1 for row in rows if row["status"] == "已存在"),
            "missing": sum(1 for row in rows if row["status"] in ("缺父", "缺子")),
            "failed": failures,
            "pending": pending,
            "rows": rows,
            "task_id": task["task_id"],
            "ok": not failures and not pending,
        }
    finally:
        if lock is not None:
            try:
                lock.__exit__(None, None, None)
            except Exception:
                pass
        if content_lock is not None:
            try:
                content_lock.__exit__(None, None, None)
            except Exception:
                pass
        if uploader is not None:
            uploader.close()
        if bridge is not None:
            bridge.stop()
        if owns_journal:
            journal.close()
