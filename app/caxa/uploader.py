"""CAXA operations: upload a PDF, create/link parts, read results back.

Ground rules (user requirements, 2026-11-05):

* the PDF that is uploaded is always the copy staged from the file the user
  selected - never a file that merely happens to have the target name;
* ``borrow`` only links parts that already exist; it never creates them;
* a new ``own`` child keeps the parent the user chose; an explicitly requested
  parent is never silently replaced by another candidate;
* a Part that was created but could not be linked is *not* a normal success;
* the document class reported by CAXA is checked; a fallback that produced a
  different class ends as "待核实/部分完成" and is not sent to the workflow.
"""

from __future__ import annotations

import os

import drawing_bundle
import relations
import staging
from .bridge import CaxaBridge, CaxaError

DRAWING_2D = 0
DRAWING_CAPP = 2

#: Verified class IDs: 1002 Drawing (2D), 1003 CappDocument (process doc).
CLASS_ID_DRAWING = 1002
CLASS_ID_CAPP = 1003
CLASS_NAMES_DRAWING = ("drawing",)
CLASS_NAMES_CAPP = ("cappdocument",)

CLASS_ID_BY_KIND = {
    "component": CLASS_ID_DRAWING,
    "machine_drawing": CLASS_ID_DRAWING,
    "process_doc": CLASS_ID_CAPP,
}

STATUS_UPLOADED = "已上传"
STATUS_PARTIAL = "部分完成"
STATUS_PENDING = "待核实"
STATUS_FAILED = "失败"


class MachineMissing(Exception):
    def __init__(self, machine):
        super().__init__("整机 %s 在 CAXA 中不存在" % machine)
        self.machine = machine


class PublishedDrawingError(CaxaError):
    def __init__(self, draw_name: str):
        super().__init__("失败：图纸已发布，无法追加版次（%s）" % draw_name)
        self.draw_name = draw_name


class UploadStop(Exception):
    pass


class UnverifiedResult(CaxaError):
    """The write was attempted but its result could not be confirmed."""

    def __init__(self, message, **kwargs):
        super().__init__(message, **kwargs)
        self.response_lost = True


def _candidate_parent_codes(code: str) -> list[str]:
    normalized = str(code or "").strip().upper()
    parents = []
    current = normalized
    while current:
        if "-" in current:
            current = current.rsplit("-", 1)[0]
        elif "." in current:
            current = current.rsplit(".", 1)[0]
        else:
            break
        if current and current not in parents:
            parents.append(current)
    return parents


def normalize_class_name(value) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def classify_document(result: dict) -> tuple[int | None, bool]:
    """Return (class_id, verified) from a helper upload result."""
    class_id = result.get("class_id")
    try:
        class_id = int(class_id) if class_id is not None else None
    except (TypeError, ValueError):
        class_id = None
    name = normalize_class_name(result.get("class_name") or result.get("low_class"))
    if class_id is None:
        if name in CLASS_NAMES_DRAWING:
            class_id = CLASS_ID_DRAWING
        elif name in CLASS_NAMES_CAPP:
            class_id = CLASS_ID_CAPP
    return class_id, class_id is not None


def expected_class_id(kind: str, drawing_type: int) -> int:
    if drawing_type == DRAWING_CAPP or kind == "process_doc":
        return CLASS_ID_CAPP
    return CLASS_ID_DRAWING


class CaxaUploader:
    def __init__(self, bridge: CaxaBridge, log=print, on_duplicate: str = "append_iteration",
                 staging_dir: staging.TaskStaging | None = None, auto_confirm: str = ""):
        self.bridge = bridge
        self.log = log
        self.on_duplicate = on_duplicate
        self.staging = staging_dir
        self.auto_confirm = auto_confirm or ""
        self.borrow_actions = []  # [{machine, code, link_type, status, note}]
        self._owns_staging = False

    # ---------- basic queries ----------
    def find_product(self, identity: str):
        identity = identity.upper()
        r = self.bridge.call({"cmd": "find_product", "keyword": identity, "limit": 20})
        found = r.get("found", [])
        for p in found:
            if (p.get("identity") or "").upper() == identity:
                return p
        for p in found:
            if (p.get("name") or "").strip().upper() == identity:
                return p
        for p in found:
            if identity in (p.get("name") or "").upper():
                return p
        self.log("未找到整机产品 %s，候选：%s" % (
            identity, [(x.get("identity"), x.get("name")) for x in found[:10]]))
        return None

    def find_part(self, identity: str):
        r = self.bridge.call({"cmd": "query_parts", "keyword": identity})
        for it in r.get("items") or []:
            if (it.get("identity") or "").upper() == identity.upper():
                return it
        return None

    def part_drawings(self, part_id: int, strict: bool = False):
        """Read the drawing list.  ``None`` = unknown (query failed)."""
        try:
            r = self.bridge.call({"cmd": "part_drawings", "part_id": int(part_id)})
        except CaxaError:
            if strict:
                raise
            return None
        drawings = r.get("drawings")
        if drawings is None:
            return [] if not strict else None
        return list(drawings)

    def part_info(self, part_id: int) -> dict:
        """Raw part_info; raises when the helper itself failed."""
        return self.bridge.call({"cmd": "part_info", "part_id": int(part_id)})

    def part_children(self, part_id: int) -> list[dict]:
        """Read child links (id, reference); raises when this is unknown."""
        return list(self.part_info(part_id).get("children") or [])

    def machine_part(self, product_id, identity: str):
        try:
            return self.bridge.call(
                {"cmd": "product_root_part", "product_id": product_id, "identity": identity}
            )
        except CaxaError:
            return None

    def create_part(self, identity: str, name: str = ""):
        r = self.bridge.call({"cmd": "create_part", "identity_no": identity, "name": name})
        return {"object_id": r["object_id"], "identity": r["identity"]}

    def link_part(self, parent_id, child_id, reference: bool, product_link: bool = False):
        return self.bridge.call({
            "cmd": "link_part",
            "parent_id": parent_id,
            "child_id": child_id,
            "reference": reference,
            "product_link": product_link,
        })

    def _find_matching_drawing(self, drawings: list[dict], name: str, draw_no: str) -> dict | None:
        target_name = (name or "").strip().upper()
        target_no = (draw_no or "").strip().upper()
        for dr in drawings:
            dr_name = str(dr.get("name") or "").strip().upper()
            dr_no = str(dr.get("draw_no") or "").strip().upper()
            if (target_name and dr_name == target_name) or (target_no and dr_no == target_no):
                return dr
        return None

    def attach_file(self, part_id, file_path, drawing_type, name, draw_no="",
                    on_duplicate: str = "", kind: str = ""):
        effective_dup = on_duplicate or self.on_duplicate
        before_drawings = self.part_drawings(part_id)
        if before_drawings is None:
            raise UnverifiedResult(
                "上传前无法读取该零件的图纸列表，无法确认是否会重复/追加",
                stage="pre_upload_query",
            )
        existing_dr = self._find_matching_drawing(before_drawings, name, draw_no)
        if existing_dr is not None and effective_dup == "append_iteration":
            state_text = str(existing_dr.get("state") or "").strip()
            if "发布" in state_text:
                raise PublishedDrawingError(existing_dr.get("name") or name or draw_no)

        # Auto-confirm is enabled for exactly this upload command only, and the
        # helper further restricts it to windows owned by its own process while
        # the upload call is active.
        auto_enabled = False
        if (
            self.auto_confirm == "append_iteration"
            and effective_dup == "append_iteration"
            and hasattr(self.bridge, "set_auto_confirm")
        ):
            try:
                self.bridge.set_auto_confirm(
                    True,
                    mode="append_iteration",
                    request_id=getattr(self.staging, "task_id", "") or "",
                )
                auto_enabled = True
            except Exception as ex:
                self.log("未能启用重复对话框自动确认（改为人工确认）：%s" % ex)
        try:
            r = self.bridge.call({
                "cmd": "upload",
                "parent_kind": "part",
                "parent_id": part_id,
                "file_path": file_path,
                "draw_no": draw_no,
                "name": name,
                "drawing_type": drawing_type,
                "on_duplicate": effective_dup,
            })
        finally:
            if auto_enabled:
                try:
                    self.bridge.set_auto_confirm(False, mode="append_iteration")
                except Exception:
                    pass
        if not r.get("attached"):
            # The helper may have written before returning attached=false.
            raise UnverifiedResult(
                "图纸挂接未确认：helper 返回 attached=false（可能已写入）",
                stage="upload_verify",
                result=dict(r),
                partial=bool(r.get("object_id")),
            )

        method = str(r.get("method") or "")
        # 1) new_version must prove that the file was really replaced.
        diag = r.get("diag") or {}
        if method == "new_version" and diag.get("new_version_copy_failed"):
            raise UnverifiedResult(
                "新增版次未确认：覆盖检出文件失败（未检入）",
                stage="new_version",
                result=dict(r),
                partial=True,
            )
        if method == "append_iteration_fallback_reuse":
            # Policy asked for a new iteration; CAXA silently reused.  Verify.
            pass

        # 2) Every append path (not only fallback_reuse) needs real evidence
        #    that the iteration/history increased for an existing drawing.
        if existing_dr is not None and effective_dup == "append_iteration":
            after_drawings = self.part_drawings(part_id)
            if after_drawings is None:
                raise UnverifiedResult(
                    "追加新版次结果无法确认：回读图纸列表失败",
                    stage="upload_verify",
                    result=dict(r),
                    partial=True,
                )
            after_dr = self._find_matching_drawing(after_drawings, name, draw_no)
            if after_dr is None:
                raise UnverifiedResult(
                    "追加新版次结果无法确认：回读不到该图纸",
                    stage="upload_verify",
                    result=dict(r),
                    partial=True,
                )
            before_cnt = int(existing_dr.get("history_count") or 0)
            after_cnt = int(after_dr.get("history_count") or 0)
            before_itr = str(existing_dr.get("iterator_no") or "")
            after_itr = str(after_dr.get("iterator_no") or "")
            if after_cnt <= before_cnt and after_itr == before_itr:
                raise UnverifiedResult(
                    "追加新版次结果无法确认：版次未增加（当前版次 %s）"
                    % (after_itr or "未知"),
                    stage="upload_verify",
                    result=dict(r),
                    partial=True,
                )
            r["reused"] = False
            r["new_version"] = True
            r["method"] = "append_iteration"
            r["iterator_no"] = after_itr
        elif method == "reuse" and effective_dup == "append_iteration":
            raise UnverifiedResult(
                "重复策略要求追加版次，但 CAXA 返回复用旧文档，未获版次证据",
                stage="upload_verify",
                result=dict(r),
            )

        class_id, verified = classify_document(r)
        expected = expected_class_id(kind, drawing_type)
        r["class_id"] = class_id
        r["class_verified"] = bool(verified and class_id == expected)
        if not r["class_verified"]:
            raise UnverifiedResult(
                "上传返回的文档类型不是预期类型（实际 %s / 期望 %s）"
                % (
                    r.get("class_name") or r.get("low_class") or r.get("class_id") or "未知",
                    "Drawing" if expected == CLASS_ID_DRAWING else "CappDocument",
                ),
                stage="upload_verify",
                result=r,
            )

        extra = []
        if r.get("reused"):
            extra.append("复用已有文档")
        if r.get("new_version"):
            extra.append("新增版本（迭代）")
        if (r.get("diag") or {}).get("append_iteration_requested") and not r.get("reused"):
            extra.append("重复框确认追加新版次")
        if r.get("method"):
            extra.append("方式 " + str(r.get("method")))
        self.log("  挂接成功（文档类 %s%s）" % (
            r.get("class_name") or r.get("low_class") or "?",
            ("，" + "，".join(extra)) if extra else ""))
        return r

    # ---------- business orchestration ----------
    def ensure_machine(self, machine: str, part_name: str = "", create_part: bool = True):
        """整机（产品）与整机零件必须存在；产品缺失抛 MachineMissing。

        A created-but-unlinked machine part carries ``link_failed`` so callers
        do not treat it as a normal success.
        """
        product = self.find_product(machine)
        if product is None:
            raise MachineMissing(machine)
        part = self.machine_part(product["object_id"], machine)
        if part is None:
            if not create_part:
                raise CaxaError("整机 %s 存在但缺少整机零件节点" % machine)
            part = self.create_part(machine, part_name)
            part = dict(part)
            part["created_new_part"] = True
            try:
                self.link_part(
                    product["object_id"], part["object_id"],
                    reference=False, product_link=True,
                )
                part["link_failed"] = False
            except CaxaError as ex:
                part["link_failed"] = True
                part["link_error"] = str(ex)
                self.log("整机零件已建但挂接失败：%s" % ex)
        else:
            part = dict(part)
            part["link_failed"] = False
        return product, part

    def resolve_parent_part(self, code: str, batch_codes: set, explicit_parent: str = ""):
        """Return (parent_id, parent_in_batch, parent_identity).

        An explicitly requested parent is authoritative: when it is missing the
        caller must fail - guessing another parent is forbidden.
        """
        code = drawing_bundle.normalize_code(code)
        if explicit_parent:
            ep = drawing_bundle.normalize_code(explicit_parent)
            if ep and ep != code:
                found = self.find_part(ep)
                if found is not None:
                    return found["object_id"], False, ep
                return None, ep in batch_codes, ep
            return None, False, ""
        for parent in _candidate_parent_codes(code):
            found = self.find_part(parent)
            if found is not None:
                return found["object_id"], False, parent
            if parent in batch_codes:
                return None, True, parent
        return None, False, ""

    def _stage_item(self, item, file_name: str | None):
        if self.staging is None:
            self.staging = staging.TaskStaging(log=self.log)
            self._owns_staging = True
        staged = self.staging.stage(item, file_name=file_name)
        self.staging.verify(staged)
        return staged

    def upload_item(self, item, batch_codes: set, on_machine_missing,
                    explicit_parent: str = "", relation_type: str = "",
                    staged: staging.StagedFile | None = None):
        """Upload one item; the payload always comes from a staged copy."""
        code = drawing_bundle.normalize_code(item["code"])
        name = str(item.get("name") or "").strip()
        machine = drawing_bundle.normalize_code(item.get("machine"))
        kind = str(item.get("kind") or "")
        relation_type = relations.normalize_link_type(relation_type)
        if not os.path.exists(item.get("file_path") or item.get("page_pdf") or ""):
            raise CaxaError("文件不存在: %s" % (item.get("file_path") or item.get("page_pdf")))

        drawing_type = DRAWING_CAPP if kind == "process_doc" else DRAWING_2D
        file_name = drawing_bundle.expected_drawing_filename(code, name)
        if kind in ("machine_drawing", "process_doc", "component"):
            file_name = os.path.basename(file_name)
        if staged is None:
            staged = self._stage_item(item, file_name)
        else:
            file_name = staged.file_name

        product = None
        machine_part = None
        if machine:
            # Borrow never creates a Part (neither machine root nor child).
            may_create = kind != "process_doc" and relation_type != "borrow"
            try:
                product, machine_part = self.ensure_machine(
                    machine,
                    part_name=name if kind == "machine_drawing" else "",
                    create_part=may_create,
                )
            except MachineMissing:
                if kind in ("process_doc", "machine_drawing"):
                    raise
                self.log("整机 %s 不存在，组件照常上传、跳过整机借用" % machine)
                product, machine_part = None, None
            except CaxaError as ex:
                if kind in ("process_doc", "machine_drawing"):
                    raise
                self.log("整机零件不可用（%s），组件照常上传、跳过整机借用" % ex)
                product, machine_part = None, None

        if kind == "process_doc":
            if machine_part is None:
                raise CaxaError("工艺文档缺少整机代码或整机零件")
            if machine_part.get("link_failed"):
                raise CaxaError(
                    "整机零件已创建但未能挂接到产品 %s（%s），本次不上传工艺文档"
                    % (machine, machine_part.get("link_error") or "未知原因"),
                    stage="ensure_machine",
                    partial=True,
                )
            part_id = machine_part.get("object_id") or machine_part.get("part_id")
            result = self.attach_file(
                part_id, staged.path, drawing_type, file_name, draw_no=code, kind=kind
            )
            caxa_path = "产品 %s（%s）→ 整机零件 %s → 文档 %s" % (
                machine, (product or {}).get("name", ""), machine, file_name)
            return {
                "target": "整机 %s 工艺文档" % machine,
                "file_name": file_name,
                "caxa_path": caxa_path,
                "staged_sha256": staged.sha256,
                "status": STATUS_UPLOADED,
                **result,
            }

        if kind == "machine_drawing":
            if machine_part is None:
                raise CaxaError(
                    "整机 %s 存在但缺少整机零件节点，未上传整机图纸" % machine,
                    stage="ensure_machine",
                )
            if machine_part.get("link_failed"):
                raise CaxaError(
                    "整机零件已创建但未能挂接到产品 %s（%s），本次不上传图纸"
                    % (machine, machine_part.get("link_error") or "未知原因"),
                    stage="ensure_machine",
                    partial=True,
                    result={
                        "created_part_id": machine_part.get("object_id"),
                        "part_id": machine_part.get("object_id"),
                    },
                )
            part_id = machine_part.get("object_id") or machine_part.get("part_id")
            result = self.attach_file(
                part_id, staged.path, drawing_type, file_name, draw_no=code, kind=kind
            )
            caxa_path = "产品 %s（%s）→ 整机零件 %s → 文档 %s" % (
                machine, (product or {}).get("name", ""), machine, file_name)
            return {
                "target": "整机 %s 图纸" % machine,
                "file_name": file_name,
                "caxa_path": caxa_path,
                "staged_sha256": staged.sha256,
                "status": STATUS_UPLOADED,
                **result,
            }

        # component
        parent_id, parent_in_batch, parent_identity = self.resolve_parent_part(
            code, batch_codes, explicit_parent=explicit_parent
        )
        if parent_id is None and parent_identity:
            raise CaxaError(
                "指定的父级 %s 在 CAXA 中不存在（不会改用其他父级）" % parent_identity,
                stage="resolve_parent",
            )
        if parent_id is None and parent_in_batch:
            raise CaxaError("父级 %s 在本批图纸中，请先上传父级" % parent_identity)

        part = self.find_part(code)
        created_new_part = False
        link_reference = None
        if part is None:
            if relation_type == "borrow":
                raise CaxaError(
                    "借用只能链接已存在的零件：%s 在 CAXA 中不存在，未创建" % code,
                    stage="borrow_missing_child",
                )
            if parent_id is None:
                raise CaxaError(
                    "找不到父节点: %s（没有可用的归属父级）" % code,
                    stage="resolve_parent",
                )
            part = self.create_part(code, name)
            created_new_part = True
            try:
                self.link_part(parent_id, part["object_id"], reference=False)
                link_reference = False
            except CaxaError as ex:
                raise CaxaError(
                    "零件 %s 已创建但归属挂接失败（%s），需要人工处理" % (code, ex),
                    stage="link_new_part",
                    partial=True,
                    result={
                        "created_part_id": part.get("object_id"),
                        "part_id": part.get("object_id"),
                        "parent_id": parent_id,
                    },
                ) from ex
        else:
            parent_id = parent_id or part.get("parent_id")

        result = self.attach_file(
            part["object_id"], staged.path, drawing_type, file_name, draw_no=code, kind=kind
        )
        # Everything below is display-only: a failed lookup must not turn a
        # confirmed upload into a replayable failure.
        parent_name = ""
        prod = None
        try:
            if parent_identity:
                pp = self.find_part(parent_identity)
                parent_name = (pp or {}).get("name", "") or ""
            prod = self.find_product(
                parent_identity[:2] if len(parent_identity) >= 2 else "04"
            )
        except Exception as ex:
            self.log("上传已成功，但补充显示信息查询失败（不影响结果）：%s" % ex)
        caxa_path = "产品 %s（%s）→ 零件 %s（%s）→ 零件 %s（%s）→ 文档 %s" % (
            (prod or {}).get("identity", ""), (prod or {}).get("name", ""),
            parent_identity, parent_name, code, name, file_name)
        return {
            "target": "零件 %s 图纸" % code,
            "file_name": file_name,
            "caxa_path": caxa_path,
            "created_new_part": created_new_part,
            "linked_parent_identity": parent_identity if created_new_part else "",
            "link_reference": link_reference,
            "staged_sha256": staged.sha256,
            "status": STATUS_UPLOADED,
            **result,
        }

    # ---------- relations ----------
    @staticmethod
    def _as_bool(value):
        """Never interpret the string 'false' as True."""
        if value is None or value == "":
            return None
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("true", "1", "yes"):
                return True
            if text in ("false", "0", "no"):
                return False
            return None
        return bool(value)

    def _child_link_state(self, parent_id, child_id) -> tuple[bool, bool | None]:
        """Return (exists, reference) or raise CaxaError when it is unknown.

        ``part_info`` exposes both ``children`` and ``link_endpoints``; either
        can be incomplete.  A reported ``children_error`` without usable
        endpoint evidence means UNKNOWN - never "no link".
        """
        info = self.part_info(parent_id)
        children = info.get("children")
        endpoints = info.get("link_endpoints") or []
        children_error = info.get("children_error")
        matched_reference = None
        matched = False
        for child in children or []:
            candidate = (
                child.get("sub_id")
                if child.get("sub_id") is not None
                else child.get("sub_object_id")
            )
            if candidate is None:
                role = child.get("roleB") or {}
                candidate = role.get("object_id")
            if candidate is None:
                continue
            try:
                if int(candidate) != int(child_id):
                    continue
            except (TypeError, ValueError):
                continue
            matched = True
            matched_reference = self._as_bool(child.get("reference"))
            break
        if not matched:
            for endpoint in endpoints or []:
                role_a = endpoint.get("roleA") or {}
                role_b = endpoint.get("roleB") or {}
                ids = [role_a.get("object_id"), role_b.get("object_id")]
                try:
                    if int(child_id) not in {int(v) for v in ids if v is not None}:
                        continue
                except (TypeError, ValueError):
                    continue
                matched = True
                matched_reference = self._as_bool(endpoint.get("reference"))
                break
        if matched:
            return True, matched_reference
        if children_error:
            raise CaxaError(
                "无法确认 %s 的现有子件关系（part_info children 失败：%s）"
                % (parent_id, children_error),
                stage="part_info",
                result={"part_info": {"children_error": children_error}},
            )
        return False, None

    def link_relation(self, parent_code: str, child_code: str, link_type: str,
                      *, create_parent: bool = False) -> dict:
        """Link one existing child to one existing parent; never creates parts."""
        parent_code = drawing_bundle.normalize_code(parent_code)
        child_code = drawing_bundle.normalize_code(child_code)
        link_type = relations.normalize_link_type(link_type) or "borrow"
        is_reference = link_type != "own"
        row = {
            "machine": parent_code,
            "code": child_code,
            "link_type": link_type,
            "status": "",
            "note": "",
        }
        if not parent_code or not child_code or parent_code == child_code:
            row["status"] = "失败"
            row["note"] = "父子图号无效"
            return row

        parent = self.find_part(parent_code)
        if parent is None:
            row["status"] = "缺父"
            row["note"] = "CAXA 中不存在父级 %s（不新建）" % parent_code
            self.log("关系跳过：父级 %s 在 CAXA 中不存在（不新建）" % parent_code)
            return row
        child = self.find_part(child_code)
        if child is None:
            row["status"] = "缺子"
            row["note"] = "CAXA 中不存在子件 %s（借用不新建）" % child_code
            self.log("关系跳过：%s ← %s（节点不存在）" % (parent_code, child_code))
            return row

        parent_id = parent.get("object_id") or parent.get("part_id")
        child_id = child.get("object_id") or child.get("part_id")
        exists, current_reference = self._child_link_state(parent_id, child_id)
        if exists:
            if current_reference is None:
                row["status"] = "待核实"
                row["note"] = "已存在链接，但无法确认其是归属还是借用"
            elif current_reference != is_reference:
                row["status"] = "冲突"
                row["note"] = (
                    "已存在其他类型的链接（当前 reference=%s，请求 reference=%s），未自动改写"
                    % (current_reference, is_reference)
                )
            else:
                row["status"] = "已存在"
                row["note"] = "链接已存在且类型一致"
            self.log(
                "关系检查：%s ← %s = %s" % (parent_code, child_code, row["status"])
            )
            return row

        try:
            self.link_part(parent_id, child_id, reference=is_reference)
        except CaxaError as ex:
            row["status"] = "待核实" if getattr(ex, "uncertain", False) else "失败"
            row["note"] = str(ex)
            self.log("关系建立失败 %s ← %s：%s" % (parent_code, child_code, ex))
            return row

        # Read the link back; only a verified link counts as success.
        exists_after, reference_after = self._child_link_state(parent_id, child_id)
        if not exists_after:
            row["status"] = "待核实"
            row["note"] = "已调用建立关系，但回读未确认到链接"
            return row
        if reference_after is None:
            row["status"] = "待核实"
            row["note"] = "回读到了链接，但无法确认其是归属还是借用"
            return row
        if reference_after != is_reference:
            row["status"] = "待核实"
            row["note"] = "回读的链接类型为 reference=%s，与请求 %s 不一致" % (
                reference_after, is_reference,
            )
            return row
        row["status"] = "已归属" if link_type == "own" else "已借用"
        self.log("%s：%s ← %s" % (row["status"], parent_code, child_code))
        return row

    def borrow_under(self, parent_code: str, codes: list,
                     skip_codes: set | None = None,
                     link_type: str = "borrow") -> int:
        """Compatibility wrapper around :meth:`link_relation`."""
        skip_codes = {drawing_bundle.normalize_code(c) for c in (skip_codes or set())}
        linked = 0
        for raw_code in codes:
            code = drawing_bundle.normalize_code(raw_code)
            if not code or code in skip_codes:
                continue
            row = self.link_relation(parent_code, code, link_type)
            self.borrow_actions.append(row)
            if row["status"] in ("已归属", "已借用"):
                linked += 1
        return linked

    def borrow_missing_components(self, machine: str, codes: list, batch_codes: set,
                                  uploaded_pairs: set) -> int:
        """兼容接口：Z03 整机总装图明细表借用（03.GPZ 钢瓶组不借用）。"""
        machine = drawing_bundle.normalize_code(machine)
        codes = [
            drawing_bundle.normalize_code(c)
            for c in (codes or [])
            if not drawing_bundle.normalize_code(c).startswith("03.GPZ.")
        ]
        if not machine or not codes or machine.startswith("03.GPZ."):
            return 0
        skip = set(batch_codes or set()) | {
            child for (m, child) in (uploaded_pairs or set()) if m == machine
        }
        return self.borrow_under(machine, codes, skip_codes=skip, link_type="borrow")

    def close(self) -> None:
        if self._owns_staging and self.staging is not None:
            self.staging.close()
            self.staging = None
