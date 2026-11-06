"""Pure helpers for the Tk uploader.

The GUI itself is thin: these functions build the frozen snapshot that a worker
thread may read, merge results back, and decide what may be clicked while a task
runs.  Keeping them free of Tk objects makes the busy/snapshot rules testable.
"""

from __future__ import annotations

import copy
import json

import drawing_bundle

DEFAULT_DUPLICATE_POLICY = "append_iteration"
DUPLICATE_POLICIES = ("append_iteration", "reuse")
DUPLICATE_POLICY_LABELS = {
    "append_iteration": "追加新版次",
    "reuse": "已有则复用",
}
_DUPLICATE_LABEL_TO_POLICY = {
    label: policy for policy, label in DUPLICATE_POLICY_LABELS.items()
}

ITEM_DONE = "已上传"
ITEM_PENDING = "待上传"


class SettingsError(ValueError):
    pass


def duplicate_policy_label(value: str | None) -> str:
    policy = str(value or DEFAULT_DUPLICATE_POLICY)
    return DUPLICATE_POLICY_LABELS.get(policy, policy)


def duplicate_policy_value(label_or_value: str | None) -> str:
    text = str(label_or_value or DEFAULT_DUPLICATE_POLICY).strip()
    return _DUPLICATE_LABEL_TO_POLICY.get(text, text)


def snapshot_settings(
    *,
    ip: str,
    port,
    user: str,
    password: str,
    duplicate_policy: str,
    start_workflow: bool,
    performer: str = "",
) -> dict:
    """Validate GUI values once, on the UI thread, then freeze them."""
    ip_text = str(ip or "").strip()
    user_text = str(user or "").strip()
    if not ip_text:
        raise SettingsError("请填写 CAXA 服务器 IP")
    if not user_text:
        raise SettingsError("请填写 CAXA 用户名")
    if not password:
        raise SettingsError("请填写 CAXA 密码")
    try:
        port_int = int(str(port).strip())
    except (TypeError, ValueError):
        raise SettingsError("CAXA 端口必须是数字")
    if not 0 < port_int < 65536:
        raise SettingsError("CAXA 端口超出范围")
    policy = duplicate_policy_value(duplicate_policy)
    if policy not in DUPLICATE_POLICIES:
        raise SettingsError("未知的重复图号策略：%s" % policy)
    return {
        "ip": ip_text,
        "port": port_int,
        "user": user_text,
        "password": password,
        "duplicate_policy": policy,
        "start_workflow": bool(start_workflow),
        "performer": str(performer or "").strip(),
    }


def account_from(settings: dict) -> dict:
    return {
        "ip": settings["ip"],
        "port": int(settings["port"]),
        "user": settings["user"],
        "password": settings["password"],
    }


def identity_key(settings: dict) -> tuple[str, int, str]:
    return (str(settings["ip"]), int(settings["port"]), str(settings["user"]))


def snapshot_manifest(manifest: dict | None) -> dict:
    """Deep copy so the worker never touches objects the UI can still edit."""
    if not manifest:
        return {}
    return copy.deepcopy(json.loads(json.dumps(manifest, ensure_ascii=False)))


def apply_upload_result(items: list[dict], result_manifest: dict) -> int:
    """Copy per-item results from the frozen manifest back into the live list."""
    by_code = {
        drawing_bundle.normalize_code(item.get("code")): item
        for item in (result_manifest or {}).get("items") or []
    }
    merged = 0
    for item in items:
        code = drawing_bundle.normalize_code(item.get("code"))
        source = by_code.get(code)
        if source is None:
            continue
        for key in ("status", "note", "target", "caxa_path", "drawing_id", "class_name", "class_id"):
            if key in source:
                item[key] = source[key]
        merged += 1
    return merged


def apply_relation_result(relations: list[dict], result_manifest: dict) -> int:
    by_pair = {
        (
            drawing_bundle.normalize_code(rel.get("parent_code")),
            drawing_bundle.normalize_code(rel.get("child_code")),
        ): rel
        for rel in (result_manifest or {}).get("relations") or []
    }
    merged = 0
    for relation in relations:
        key = (
            drawing_bundle.normalize_code(relation.get("parent_code")),
            drawing_bundle.normalize_code(relation.get("child_code")),
        )
        source = by_pair.get(key)
        if source is None:
            continue
        for field in ("link_type", "link_status", "note", "manual_override", "auto_decided"):
            if field in source:
                relation[field] = source[field]
        merged += 1
    return merged


def invalidate_successes(items: list[dict], reason: str) -> int:
    """Another account/server or an edited item must not keep a success state."""
    changed = 0
    for item in items:
        if item.get("status") == ITEM_DONE or item.get("drawing_id"):
            item["status"] = ITEM_PENDING
            item["note"] = reason
            for key in ("drawing_id", "class_name", "class_id", "caxa_path", "target"):
                item.pop(key, None)
            changed += 1
    return changed


def invalidate_item(item: dict, reason: str = "条目已修改，需重新上传") -> bool:
    if item.get("status") == ITEM_DONE or item.get("drawing_id"):
        item["status"] = ITEM_PENDING
        item["note"] = reason
        for key in ("drawing_id", "class_name", "class_id", "caxa_path", "target"):
            item.pop(key, None)
        return True
    return False


def should_auto_confirm(policy: str) -> str:
    """Auto-confirm is only ever enabled for append_iteration during one request."""
    return "append_iteration" if str(policy or "") == "append_iteration" else ""


def can_start_upload(*, busy: bool, has_bundle: bool, selected: int, settings_ok: bool) -> tuple[bool, str]:
    if busy:
        return False, "当前任务尚未结束，请等待完成"
    if not has_bundle:
        return False, "请先打开上传包或添加 PDF"
    if not selected:
        return False, "没有勾选待上传的图纸"
    if not settings_ok:
        return False, "请先填写完整的 CAXA 账号信息"
    return True, ""


def summary_message(result: dict) -> str:
    """One readable summary: files, relations, workflow - including failures."""
    if not result:
        return "没有结果"
    if result.get("duplicate"):
        workflow = result.get("workflow") or {}
        text = result.get("message") or "相同批次已完成，未重复执行"
        if workflow.get("process_instance_id"):
            text += "；原流程 %s" % workflow["process_instance_id"]
        return text

    lines = []
    uploaded = int(result.get("uploaded") or 0)
    failed = int(result.get("failed") or 0)
    uncertain = int(result.get("uncertain") or 0)
    lines.append("图纸：确认成功 %d 项，失败 %d 项，待核实 %d 项" % (uploaded, failed, uncertain))

    rows = result.get("relations") or []
    if rows:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.get("status") or "未知"] = counts.get(row.get("status") or "未知", 0) + 1
        lines.append(
            "关系："
            + "，".join("%s %d 条" % (status, count) for status, count in sorted(counts.items()))
        )
        failed_rows = [row for row in rows if row.get("status") not in ("已借用", "已归属", "已存在", "跳过")]
        for row in failed_rows[:6]:
            lines.append(
                "  · %s ← %s：%s %s"
                % (
                    row.get("parent_code"),
                    row.get("child_code"),
                    row.get("status"),
                    row.get("note") or "",
                )
            )
        if len(failed_rows) > 6:
            lines.append("  · 其余 %d 条见日志" % (len(failed_rows) - 6))

    workflow = result.get("workflow")
    if workflow:
        status = workflow.get("status") or ""
        process_id = workflow.get("process_instance_id")
        if process_id:
            lines.append("流程：%s（流程ID %s）" % (status or "已发起", process_id))
        else:
            lines.append("流程：%s %s" % (status, workflow.get("reason") or ""))
    elif result.get("workflow_blocked"):
        lines.append("流程：" + str(result["workflow_blocked"]))

    failures = [item for item in result.get("items") or [] if item.get("note") and item.get("status") in ("失败", "整机不存在", "失败：图纸已发布，无法追加版次", "部分完成", "待核实")]
    for item in failures[:6]:
        lines.append("  · %s：%s %s" % (item.get("code"), item.get("status"), item.get("note")))
    if len(failures) > 6:
        lines.append("  · 其余 %d 项见日志" % (len(failures) - 6))
    return "\n".join(lines)
