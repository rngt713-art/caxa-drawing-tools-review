"""Unified relation (次级条目) model.

Business rules implemented here (confirmed by the user, 2026-11-05):

* The CAXA design tree decides the default relation type: the child part is
  absent -> ``own`` (归属子件, Reference=False); it already exists -> ``borrow``
  (借用, Reference=True).  Offline, "absent" is approximated by "the child is
  part of this upload batch".
* A manual choice always wins (``manual_override=True``) and the automatic
  decision is stable within one task: retrying must not turn an ``own`` entry
  into ``borrow`` merely because the previous attempt created the part.
* ``relations`` is the only executable source.  ``assemblies``,
  ``manual_borrows`` and ``item.parent_code`` are legacy inputs migrated once
  on import; afterwards a deleted relation stays deleted.
* Nothing here forces ``03.*`` machines to borrow, and file-name prefixes never
  decide the final relation type.

This module deliberately has no dependency on the rest of the application so it
can be imported from the CAXA layer without cycles.  Callers that need code
syntax validation pass ``is_valid_code``.
"""

from __future__ import annotations

VALID_LINK_TYPES = ("own", "borrow")

#: Link statuses that mean "this relation is finished, do not execute it again".
DONE_STATUSES = ("已借用", "已归属", "已存在")

#: Statuses that mean "uncertain outcome, do not silently replay".
PENDING_STATUSES = ("待核实", "结果待核实")

MIGRATION_FLAG = "relations_migrated"
MIGRATION_VERSION = 1
MIGRATED_CHILDREN_FLAG = "relations_migrated_children"


def normalize_code(value: str) -> str:
    """Uppercase/canonicalise a drawing code the same way the GUI does."""
    text = str(value or "").strip()
    if not text:
        return ""
    return (
        text.replace("，", ",")
        .replace("：", ":")
        .replace("。", ".")
        .replace("（", "(")
        .replace("）", ")")
    ).upper()


def is_done(relation: dict) -> bool:
    return str(relation.get("link_status") or "") in DONE_STATUSES


def is_pending(relation: dict) -> bool:
    return str(relation.get("link_status") or "") in PENDING_STATUSES


def normalize_link_type(value: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in VALID_LINK_TYPES else ""


def default_link_type(
    parent_code: str,
    child_code: str,
    batch_codes: set[str] | None = None,
    child_exists_in_tree: bool | None = None,
) -> str:
    """Return the default relation type.

    ``child_exists_in_tree`` is authoritative when it is known (pre-check or
    upload time).  Without it we approximate with "child is in this batch".
    Note there is no rule that makes everything under ``03.*`` a borrow.
    """
    del parent_code  # kept in the signature for readability of call sites
    if child_exists_in_tree is not None:
        return "borrow" if child_exists_in_tree else "own"
    child = normalize_code(child_code)
    if batch_codes is not None and child in {normalize_code(c) for c in batch_codes}:
        return "own"
    return "borrow"


def _dedupe_key(relation: dict) -> tuple[str, str]:
    return (
        normalize_code(relation.get("parent_code")),
        normalize_code(relation.get("child_code")),
    )


def _clean_relation(relation: dict, is_valid) -> dict | None:
    parent = normalize_code(relation.get("parent_code"))
    child = normalize_code(relation.get("child_code"))
    if not parent or not child or parent == child:
        return None
    if not is_valid(parent) or not is_valid(child):
        return None
    link_type = normalize_link_type(relation.get("link_type"))
    if not link_type:
        link_type = default_link_type(parent, child)
    return {
        "parent_code": parent,
        "child_code": child,
        "link_type": link_type,
        "source": str(relation.get("source") or "manual"),
        "manual_override": bool(relation.get("manual_override")),
        "link_status": str(relation.get("link_status") or ""),
        "note": str(relation.get("note") or ""),
        # Classification provenance must survive normalisation: a retry must
        # not silently reinterpret an own child as borrow.
        "auto_decided": bool(relation.get("auto_decided")),
        "tree_exists": relation.get("tree_exists"),
    }


def _merge_into(index: dict, order: list, entry: dict, *, force: bool = False) -> None:
    pair = (entry["parent_code"], entry["child_code"])
    existing = index.get(pair)
    if existing is None:
        index[pair] = entry
        order.append(entry)
        return
    if entry["manual_override"] and not existing.get("manual_override"):
        existing["link_type"] = entry["link_type"]
        existing["manual_override"] = True
    elif not existing.get("manual_override") and force:
        existing["link_type"] = entry["link_type"]
    if entry.get("link_status") and not existing.get("link_status"):
        existing["link_status"] = entry["link_status"]
    if entry.get("note") and not existing.get("note"):
        existing["note"] = entry["note"]
    # Keep every origin so the UI can explain where a relation came from.
    sources = existing.setdefault("sources", [])
    for source in [existing.get("source"), entry.get("source")]:
        if source and source not in sources:
            sources.append(source)


def migrate_manifest_relations(manifest: dict, is_valid_code=None, log=None) -> list[dict]:
    """One-time migration of legacy inputs into ``manifest['relations']``.

    ``assemblies`` / ``manual_borrows`` / ``item.parent_code`` stay in the file
    as provenance only.  After this call they are never merged again, so a
    relation deleted in the UI cannot come back on the next upload.
    """
    is_valid = is_valid_code or (lambda code: bool(code))
    items = manifest.get("items") or []
    batch_codes = {
        normalize_code(item.get("code")) for item in items if item.get("code")
    }

    index: dict[tuple[str, str], dict] = {}
    order: list[dict] = []
    migrated_children: set[str] = set()

    def upsert(parent, child, *, link_type="", source="manual",
               manual_override=False, link_status=""):
        entry = _clean_relation(
            {
                "parent_code": parent,
                "child_code": child,
                "link_type": link_type or default_link_type(parent, child, batch_codes),
                "source": source,
                "manual_override": manual_override,
                "link_status": link_status,
            },
            is_valid,
        )
        if entry is None:
            return
        _merge_into(index, order, entry, force=bool(link_type))

    # Existing canonical entries win (an uploaded package may already have them).
    for relation in manifest.get("relations") or []:
        upsert(
            relation.get("parent_code"),
            relation.get("child_code"),
            link_type=normalize_link_type(relation.get("link_type")),
            source=str(relation.get("source") or "manual"),
            manual_override=bool(relation.get("manual_override")),
            link_status=str(relation.get("link_status") or ""),
        )

    for item in items:
        parent = normalize_code(item.get("parent_code") or item.get("borrow_to"))
        child = normalize_code(item.get("code"))
        if parent and child and child in batch_codes:
            upsert(
                parent,
                child,
                link_type=normalize_link_type(item.get("link_type")),
                source="drag",
            )

    for entry in manifest.get("manual_borrows") or []:
        parent = normalize_code(entry.get("parent_code") or entry.get("machine"))
        for child in entry.get("codes") or []:
            upsert(parent, child, link_type="borrow", source="manual")

    for assembly in manifest.get("assemblies") or []:
        parent = normalize_code(assembly.get("machine"))
        if parent.startswith("03.GPZ."):
            continue
        for child in assembly.get("codes") or []:
            upsert(parent, child, link_type="borrow", source="z03_bom")

    for entry in order:
        migrated_children.add(entry["child_code"])

    manifest["relations"] = order
    manifest[MIGRATION_FLAG] = MIGRATION_VERSION
    manifest[MIGRATED_CHILDREN_FLAG] = sorted(migrated_children)
    manifest["legacy_relation_counts"] = {
        "relations": len(order),
        "assemblies": len(manifest.get("assemblies") or []),
        "manual_borrows": len(manifest.get("manual_borrows") or []),
    }
    if log and order:
        log("已导入 %d 条次级关系（旧包 assemblies/manual_borrows 仅保留为来源记录）" % len(order))
    return order


def _migrate_new_items(manifest: dict, is_valid, index, order) -> None:
    """Migrate items added after the first import (e.g. dropped into the GUI)."""
    migrated = set(manifest.get(MIGRATED_CHILDREN_FLAG) or [])
    batch_codes = {
        normalize_code(item.get("code")) for item in manifest.get("items") or []
    }
    changed = False
    for item in manifest.get("items") or []:
        parent = normalize_code(item.get("parent_code") or item.get("borrow_to"))
        child = normalize_code(item.get("code"))
        if not parent or not child or child not in batch_codes:
            continue
        if child in migrated:
            continue
        entry = _clean_relation(
            {
                "parent_code": parent,
                "child_code": child,
                "link_type": normalize_link_type(item.get("link_type"))
                or default_link_type(parent, child, batch_codes),
                "source": "drag",
            },
            is_valid,
        )
        if entry is not None:
            _merge_into(index, order, entry)
            migrated.add(child)
            changed = True
    if changed:
        manifest[MIGRATED_CHILDREN_FLAG] = sorted(migrated)


def normalize_manifest_relations(manifest: dict, is_valid_code=None, log=None) -> list[dict]:
    """Normalise ``manifest['relations']`` without reviving deleted entries."""
    if manifest.get(MIGRATION_FLAG) != MIGRATION_VERSION:
        return migrate_manifest_relations(manifest, is_valid_code, log=log)

    is_valid = is_valid_code or (lambda code: bool(code))
    index: dict[tuple[str, str], dict] = {}
    order: list[dict] = []
    for relation in manifest.get("relations") or []:
        entry = _clean_relation(relation, is_valid)
        if entry is None:
            continue
        entry["sources"] = list(relation.get("sources") or [])
        _merge_into(index, order, entry, force=False)

    _migrate_new_items(manifest, is_valid, index, order)

    manifest["relations"] = order
    return order


def analyze_relations(relations: list[dict]) -> dict:
    """Detect explicit conflicts: cycles and an ``own`` child with two parents."""
    edges: dict[str, set[str]] = {}
    own_parents: dict[str, list[str]] = {}
    for relation in relations or []:
        parent = normalize_code(relation.get("parent_code"))
        child = normalize_code(relation.get("child_code"))
        if not parent or not child or parent == child:
            continue
        edges.setdefault(parent, set()).add(child)
        if normalize_link_type(relation.get("link_type")) == "own":
            own_parents.setdefault(child, [])
            if parent not in own_parents[child]:
                own_parents[child].append(parent)

    cycles: list[list[str]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for neighbour in sorted(edges.get(node, ())):
            if state.get(neighbour, 0) == 0:
                visit(neighbour)
            elif state.get(neighbour) == 1:
                start = stack.index(neighbour)
                cycles.append(stack[start:] + [neighbour])
        stack.pop()
        state[node] = 2

    for node in sorted(edges):
        if state.get(node, 0) == 0:
            visit(node)

    multi_parent = {
        child: parents for child, parents in own_parents.items() if len(parents) > 1
    }
    issues: list[dict] = []
    for cycle in cycles:
        issues.append(
            {
                "type": "cycle",
                "message": "关系存在循环：%s" % " → ".join(cycle),
                "codes": cycle,
            }
        )
    for child, parents in sorted(multi_parent.items()):
        issues.append(
            {
                "type": "own_multi_parent",
                "message": "归属子件 %s 同时挂在多个父级下：%s" % (
                    child,
                    "、".join(parents),
                ),
                "codes": [child] + parents,
            }
        )
    return {
        "issues": issues,
        "cycles": cycles,
        "own_multi_parent": multi_parent,
        "ok": not issues,
    }


def relation_label(relation: dict) -> str:
    parent = relation.get("parent_code") or ""
    child = relation.get("child_code") or ""
    kind = "归属" if normalize_link_type(relation.get("link_type")) == "own" else "借用"
    return "%s ← %s（%s）" % (parent, child, kind)
