"""Portable hand-off format and unified drawing/relation rules.

The uploader treats a bundle as untrusted input: every item is type checked,
its PDF content is bound to a SHA-256 fingerprint, and the relation list is
migrated once into the single executable form (see :mod:`relations`).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import relations

SCHEMA = "caxa-drawing-upload-bundle"
SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)

#: Item kinds understood by the uploader.  ``unknown`` is rejected on purpose:
#: an unknown kind cannot be mapped to a verified CAXA document class.
ITEM_KINDS = ("process_doc", "machine_drawing", "component")
ALL_ITEM_KINDS = ITEM_KINDS + ("unknown",)

#: Every uploadable item needs an explicit machine for these kinds.
MACHINE_REQUIRED_KINDS = ("process_doc", "machine_drawing")

SINGLE_LETTER_PROCESS_RE = re.compile(
    r"^[A-Z]03\.[A-Z0-9]+(?:[.\-][A-Z0-9()]+)*$",
    re.IGNORECASE,
)

DIGIT_CODE_RE = re.compile(
    r"^\d{2}\.[A-Z0-9]+(?:[.\-][A-Z0-9()]+)*$",
    re.IGNORECASE,
)

STEM_SPLIT_RE = re.compile(r"^(?P<code_token>\S+?)(?:\s+(?P<name>.*))?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_file_stem(value: str) -> str:
    value = _UNSAFE_NAME_RE.sub("_", value).strip(" .")
    return value or "未命名"


def normalize_code(value: str) -> str:
    return relations.normalize_code(value)


def is_single_letter_process_code(value: str) -> bool:
    return bool(SINGLE_LETTER_PROCESS_RE.fullmatch(normalize_code(value)))


def is_digit_code(value: str) -> bool:
    return bool(DIGIT_CODE_RE.fullmatch(normalize_code(value)))


def is_valid_code(value: str) -> bool:
    code = normalize_code(value)
    return is_single_letter_process_code(code) or is_digit_code(code)


def infer_kind(code: str) -> tuple[str, str]:
    """Return (kind, machine_code) from the confirmed naming rules.

    Multi-letter prefixes such as ZJ03.* are naming errors and return
    ('unknown', ''): the ZJ03 rule needs business confirmation before it may be
    relaxed, so the current rejection is kept (see IMPLEMENTATION.md).
    """
    code = normalize_code(code)
    if not code:
        return "unknown", ""
    if is_single_letter_process_code(code):
        return "process_doc", code[1:]
    if is_digit_code(code):
        if code.startswith("03."):
            return "machine_drawing", code
        return "component", ""
    return "unknown", ""


def expected_drawing_filename(code: str, name: str = "") -> str:
    """Canonical PDF name uploaded to CAXA (also used to stage the copy)."""
    code = normalize_code(code)
    clean_name = str(name or "").strip()
    if is_single_letter_process_code(code):
        return safe_file_stem(code) + ".pdf"
    if clean_name:
        return safe_file_stem(f"{code} {clean_name}") + ".pdf"
    return safe_file_stem(code) + ".pdf"


def parse_drawing_filename(path: str) -> tuple[str, str, list[str]]:
    """Parse a PDF file name into (code, name, errors)."""
    raw_stem = os.path.splitext(os.path.basename(str(path or "")))[0]
    stem = raw_stem.strip()
    if not stem:
        return "", "", ["文件名为空"]

    match = STEM_SPLIT_RE.fullmatch(stem)
    if not match:
        return "", "", ["文件名格式无法解析"]

    code = normalize_code(match.group("code_token") or "")
    name = (match.group("name") or "").strip()
    errors: list[str] = []

    if is_single_letter_process_code(code):
        if name or " " in stem:
            errors.append(
                "单字母工艺文档不应带中文名（应为“%s.pdf”，可点“一键规范文件名”裁去）"
                % code
            )
        return code, name, errors

    if is_digit_code(code):
        if not name:
            errors.append("数字开头图纸必须带中文名称（应为“图号 名称.pdf”）")
        elif re.match(r"^\S+\s{2,}", stem):
            errors.append("图号与名称之间应只有一个空格（可点“一键规范文件名”修正）")
        return code, name, errors

    if re.match(r"^[A-Z]{2,}\d{2}\.", code):
        errors.append(
            "多字母前缀“%s”无效（工艺文档应为单字母如 Z03/J03/M03/P03/T03，请双击修正）"
            % code
        )
    else:
        errors.append("图号格式无效（应为“图号 名称.pdf”或单字母工艺文档“Z03.xxx.pdf”）")
    return code, name, errors


def filename_warnings(item: dict) -> list[str]:
    """Non-blocking notice: the file on disk is not named as the upload target.

    Editing ``code``/``name`` by hand is allowed; the upload then always uses
    :func:`expected_drawing_filename` for the staged copy, so the notice only
    explains which name CAXA will receive.
    """
    code = normalize_code(item.get("code"))
    if not is_valid_code(code):
        return []
    name = "" if is_single_letter_process_code(code) else str(item.get("name") or "").strip()
    expected = expected_drawing_filename(code, name)
    actual = os.path.basename(str(item.get("file_path") or item.get("source") or ""))
    if actual and actual != expected:
        return ["将按规范文件名上传：%s（本地文件名 %s）" % (expected, actual)]
    return []


# ---------------------------------------------------------------------------
# item validation / content fingerprints
# ---------------------------------------------------------------------------


def compute_fingerprint(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def register_fingerprint(item: dict, force: bool = False) -> dict:
    """Record SHA-256/size for a hand-added PDF (first registration)."""
    path = str(item.get("file_path") or "")
    if not path or not os.path.isfile(path):
        return item
    if not force and item.get("sha256") and item.get("size") is not None:
        return item
    digest, size = compute_fingerprint(path)
    item["sha256"] = digest
    item["size"] = size
    item["fingerprint_source"] = item.get("fingerprint_source") or "import"
    return item


def verify_fingerprint(item: dict) -> list[str]:
    """Compare the file on disk with the recorded fingerprint (pre-submit)."""
    path = str(item.get("file_path") or "")
    if not path:
        return ["缺少文件路径"]
    if not os.path.isfile(path):
        return ["文件不存在: %s" % path]
    expected_hash = str(item.get("sha256") or "").lower()
    expected_size = item.get("size")
    if not expected_hash or expected_size is None:
        return ["缺少内容指纹（SHA-256/大小），无法确认上传的是原选中文件"]
    if not SHA256_RE.fullmatch(expected_hash):
        return ["内容指纹格式无效"]
    try:
        expected_size = int(expected_size)
    except (TypeError, ValueError):
        return ["文件大小字段无效"]
    actual_hash, actual_size = compute_fingerprint(path)
    if actual_size != expected_size:
        return [
            "文件大小已变化（记录 %d，实际 %d），拒绝上传"
            % (expected_size, actual_size)
        ]
    if actual_hash != expected_hash:
        return ["文件内容已变化（SHA-256 不一致），拒绝上传"]
    return []


def item_problems(item: dict, *, check_content: bool = True) -> list[str]:
    """Blocking problems for one item; empty means it may be executed."""
    if not isinstance(item, dict):
        return ["条目不是对象"]
    problems: list[str] = []
    code = normalize_code(item.get("code"))
    name = str(item.get("name") or "").strip()
    kind = str(item.get("kind") or "")
    machine = normalize_code(item.get("machine"))

    if not code:
        problems.append("缺少图号")
    elif not is_valid_code(code):
        problems.append("图号格式无效：%s" % code)
    if kind not in ITEM_KINDS:
        problems.append("类型无效：%s（应为 %s）" % (kind or "空", "/".join(ITEM_KINDS)))
    elif is_valid_code(code):
        inferred, inferred_machine = infer_kind(code)
        if inferred != kind:
            problems.append("类型 %s 与图号规则（%s）不一致" % (kind, inferred))
        if kind in MACHINE_REQUIRED_KINDS:
            if not machine:
                problems.append("缺少成品代码（%s 必须填写）" % kind)
            elif not is_valid_code(machine):
                problems.append("成品代码格式无效：%s" % machine)
            elif kind == "machine_drawing" and machine != code:
                problems.append("整机图纸的成品代码应与图号一致：%s" % machine)
            elif kind == "process_doc" and machine != (inferred_machine or machine):
                problems.append(
                    "工艺文档 %s 的成品代码应为 %s（当前 %s）"
                    % (code, inferred_machine, machine)
                )
    if is_digit_code(code) and not name:
        problems.append("数字开头图纸必须填写中文名称")
    if is_single_letter_process_code(code) and name:
        problems.append("单字母工艺文档不带中文名")
    if check_content:
        problems.extend(verify_fingerprint(item))
    return problems


def validate_item_filename(item: dict) -> list[str]:
    """Backwards-compatible name: blocking problems for one item."""
    return item_problems(item, check_content=False)


def assembly_code_for_item(item: dict) -> str:
    code = normalize_code(item.get("code"))
    machine = normalize_code(item.get("machine"))
    kind = str(item.get("kind") or "")
    if kind in ("process_doc", "machine_drawing"):
        if machine.startswith("03."):
            return machine
        if is_single_letter_process_code(code):
            return code[1:]
        if code.startswith("03."):
            return code
    return ""


def default_link_type(
    parent_code: str, child_code: str, batch_codes: set[str] | None = None
) -> str:
    """Offline default only; the live tree check decides during pre-check.

    No rule forces ``03.*`` children to borrow and the file-name prefix is not
    consulted (see :mod:`relations`).
    """
    return relations.default_link_type(parent_code, child_code, batch_codes)


def normalize_manifest_relations(manifest: dict, log=None) -> list[dict]:
    """Migrate + normalise the single executable relation list."""
    canonical = relations.normalize_manifest_relations(
        manifest, is_valid_code, log=log
    )
    manifest["relation_issues"] = relations.analyze_relations(canonical)["issues"]
    return canonical


# ---------------------------------------------------------------------------
# bundle import / export
# ---------------------------------------------------------------------------


def _relative_item(item: dict, pdf_path: Path, base_dir: Path) -> dict:
    output = {
        key: value
        for key, value in item.items()
        if key not in ("page_pdf", "png", "file", "file_path")
    }
    digest, size = compute_fingerprint(str(pdf_path))
    output["file"] = pdf_path.relative_to(base_dir).as_posix()
    output["sha256"] = digest
    output["size"] = size
    output["fingerprint_source"] = "export"
    return output


def export_bundle(
    items: list[dict],
    destination: str | Path,
    source_files: list[str] | None = None,
    assemblies: list[dict] | None = None,
    warnings: list[str] | None = None,
) -> Path:
    """Copy reviewed pages into a self-contained hand-off directory."""
    destination = Path(destination).resolve()
    drawings_dir = destination / "drawings"
    drawings_dir.mkdir(parents=True, exist_ok=True)

    manifest_items = []
    used_names = set()
    for index, item in enumerate(items, start=1):
        source_pdf = item.get("page_pdf") or item.get("file_path")
        if not source_pdf:
            raise ValueError("第 %d 项缺少 page_pdf/file_path" % index)
        source_path = Path(source_pdf).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)

        code = normalize_code(item.get("code"))
        name = str(item.get("name") or "").strip()
        if not code:
            raise ValueError("第 %d 项缺少图号，请先补录后再导出" % index)
        if not is_single_letter_process_code(code) and not name:
            raise ValueError(
                "第 %d 项（%s）为数字开头图纸，必须填写中文名称后再导出"
                % (index, code)
            )

        output_name = expected_drawing_filename(code, name)
        stem = os.path.splitext(output_name)[0]
        counter = 2
        while output_name.casefold() in used_names:
            output_name = "%s_%d.pdf" % (stem, counter)
            counter += 1
        used_names.add(output_name.casefold())

        output_path = drawings_dir / output_name
        shutil.copy2(source_path, output_path)
        manifest_items.append(_relative_item(item, output_path, destination))

    manifest = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_files": [Path(path).name for path in (source_files or [])],
        "items": manifest_items,
        "assemblies": assemblies or [],
        "relations": [],
        "warnings": warnings or [],
    }
    normalize_manifest_relations(manifest)
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest_path


def _check_relative_file(relative: str) -> str:
    """Reject absolute / escaping relative paths from a bundle manifest."""
    text = str(relative or "").strip()
    if not text:
        raise ValueError("条目缺少 file 字段")
    if os.path.isabs(text) or re.match(r"^[A-Za-z]:", text):
        raise ValueError("条目 file 不能是绝对路径: %s" % text)
    if text.startswith("/") or text.startswith("\\"):
        raise ValueError("条目 file 不能是绝对路径: %s" % text)
    parts = re.split(r"[\\/]+", text)
    if any(part == ".." for part in parts):
        raise ValueError("条目 file 不能包含上级目录: %s" % text)
    return text


def _within(base_dir: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath(
            [os.path.normcase(str(base_dir)), os.path.normcase(str(candidate))]
        ) == os.path.normcase(str(base_dir))
    except ValueError:
        return False


def _is_executed(item: dict) -> bool:
    """Rows the user deselected/skipped may stay malformed and editable.

    They must not block a valid batch, but they are never sent either.
    """
    if not item.get("upload_sel", True):
        return False
    return str(item.get("status") or "") not in ("已跳过",)


def validate_bundle(manifest: dict, *, base_dir: str | Path | None = None) -> list[str]:
    """Validate structure globally and fields only for rows to be executed."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["上传包不是 JSON 对象"]
    if manifest.get("schema") != SCHEMA:
        errors.append("不是受支持的图纸上传包")
    version = manifest.get("schema_version")
    try:
        version_int = int(version)
    except (TypeError, ValueError):
        version_int = 0
    if version_int not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append("上传包版本不支持: %s" % version)
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        errors.append("上传包缺少图纸条目")
        return errors
    seen_codes: dict[str, int] = {}
    root = Path(base_dir).resolve() if base_dir else None
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errors.append("第 %d 项不是对象" % index)
            continue
        code = normalize_code(item.get("code"))
        executed = _is_executed(item)
        if code and executed:
            if code in seen_codes:
                errors.append(
                    "第 %d 项与第 %d 项的图号重复：%s"
                    % (index, seen_codes[code], code)
                )
            else:
                seen_codes[code] = index
        for key in ("code", "name", "kind", "machine"):
            if key in item and not isinstance(item[key], str):
                errors.append("第 %d 项的 %s 必须是字符串" % (index, key))
        relative = item.get("file")
        if relative is not None:
            try:
                _check_relative_file(relative)
            except ValueError as exc:
                errors.append("第 %d 项：%s" % (index, exc))
            else:
                if root is not None:
                    resolved = (root / str(relative)).resolve()
                    if not _within(root, resolved):
                        errors.append("第 %d 项文件超出上传包目录" % index)
        problems = item_problems(item, check_content=False) if _is_executed(item) else []
        for problem in problems:
            errors.append("第 %d 项（%s）：%s" % (index, code or "?", problem))
    return errors


def load_bundle(path: str | Path) -> tuple[Path, dict]:
    """Load a manifest or bundle directory, verify content, migrate relations."""
    candidate = Path(path).resolve()
    manifest_path = candidate / "manifest.json" if candidate.is_dir() else candidate
    if not manifest_path.is_file():
        raise FileNotFoundError("找不到 manifest.json: %s" % manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError("不是受支持的图纸上传包")
    if manifest.get("schema") != SCHEMA:
        raise ValueError("不是受支持的图纸上传包")
    try:
        version = int(manifest.get("schema_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError("上传包版本不支持: %s" % manifest.get("schema_version"))

    base_dir = manifest_path.parent
    for index, item in enumerate(manifest.get("items") or [], start=1):
        if not isinstance(item, dict):
            raise ValueError("第 %d 项不是对象" % index)
        relative = _check_relative_file(item.get("file") or "")
        pdf_path = (base_dir / relative).resolve()
        if not _within(base_dir.resolve(), pdf_path):
            raise ValueError("第 %d 项文件超出上传包目录: %s" % (index, relative))
        if not pdf_path.is_file():
            raise FileNotFoundError("第 %d 项 PDF 不存在: %s" % (index, pdf_path))
        item["file_path"] = str(pdf_path)
        # Import-time content check: a replaced PDF must never be accepted.
        if item.get("sha256") or item.get("size") is not None:
            problems = verify_fingerprint(item)
            if problems:
                raise ValueError(
                    "第 %d 项（%s）内容校验失败：%s"
                    % (index, item.get("code") or "?", "；".join(problems))
                )
        else:
            register_fingerprint(item)

    errors = validate_bundle(manifest, base_dir=base_dir)
    if errors:
        raise ValueError("上传包校验失败：\n" + "\n".join(errors[:20]))
    normalize_manifest_relations(manifest)
    return manifest_path, manifest
