import os
import re

from difflib import SequenceMatcher

from . import extract, pdf_tools

BAD_NAME_PARTS = ("重量", "比例", "设计", "校对", "工艺", "标准化", "审核", "批准", "版本",
                  "描述", "ECN", "日期", "签名", "上级图号", "汉纬尔", "序号", "物料代码",
                  "数量", "规格型号", "其他", "未注公差", "表面", "材料", "共", "第")


def assembly_machine_from_text(text: str) -> str:
    """从总装图文本中找整机代码（03.xx）。"""
    for m in re.finditer(r"(?<![0-9A-Z.])03\.[A-Z0-9.\-]+", text.upper()):
        cand = m.group(0)
        if not cand.upper().startswith("03.GPZ") and not cand.endswith(("-", ".")):
            return cand
    return ""


def assembly_machine_from_filename(filename: str) -> str:
    """Z03.NA.050080-S01.pdf → 03.NA.050080-S01（仅 Z03 是整机总装图）"""
    base = filename.strip()
    if base.upper().startswith("Z03."):
        return base[1:]
    return ""


def bom_codes_from_text(text: str) -> list:
    """从总装图文本提取明细表（材料明细表）中的物料代码，去重保序。"""
    codes = []
    up = text.upper()
    start = max(up.find("物料代码"), up.find("序号"))
    if start < 0:
        return codes
    end = up.find("未注公差", start)
    if end < 0:
        end = min(len(up), start + 1500)
    seg = text[start:end]
    for c in pdf_tools.find_codes_in_text(seg):
        if c.endswith(("-", ".")):
            continue
        if c.startswith(("01.", "02.", "04.")) and c not in codes:
            codes.append(c)
    return codes


def load_assembly(path: str) -> dict:
    """解析可读总装图：仅 Z03 开头的文件（整机总装）参与借用判定。
    返回 {machine, codes, file}；非 Z03 返回 invalid=True + reason。
    组件图纸（如传动组件）里即使有材料明细表也不参与借用。"""
    base = os.path.basename(path)
    if not base.upper().startswith("Z03"):
        return {"machine": "", "codes": [], "file": path, "invalid": True,
                "reason": "文件名不是 Z03 开头（组件图纸的明细表不参与借用）"}
    texts = pdf_tools.extract_all_text(path)
    full = "\n".join(texts)
    machine = assembly_machine_from_filename(path) or assembly_machine_from_text(full)
    codes = bom_codes_from_text(full)
    return {"machine": machine, "codes": codes, "file": path}


def build_candidates_from_texts(page_texts: list) -> list:
    """Build candidate drawing list from readable PDF text layer.
    Returns list of dicts: {page, code, name, machine}
    """
    cands = []
    for idx, text in enumerate(page_texts):
        codes = pdf_tools.find_codes_in_text(text)
        for code in codes:
            nc = extract.normalize_code(code)
            if nc.count(".") == 1 and nc.split(".")[1].isdigit():
                continue  # 表面粗糙度等纯数值（如 12.5）
            kind, machine, _ = extract.classify(nc)
            if kind == "unknown":
                continue
            name = ""
            # 尝试从文本中该行附近取名称
            for line in text.splitlines():
                if code.upper() in line.upper():
                    rest = line.replace(code, "").strip()
                    rest = re.sub(r"^\d+\s+", "", rest)      # 去掉 BOM 序号
                    rest = re.sub(r"\s+\d+$", "", rest)      # 去掉 BOM 数量
                    rest = rest.strip(" |，。、")
                    if rest and re.search(r"[\u4e00-\u9fff]", rest) and not any(b in rest for b in BAD_NAME_PARTS):
                        name = rest
                    break
            cands.append({"page": idx + 1, "code": nc, "name": name, "machine": machine, "kind": kind})
    return cands


def match_scanned(scanned_fields: dict, candidates: list):
    """Match a scanned page's extracted fields to a candidate by 图号.
    Returns (candidate, score) or (None, 0).
    """
    code = extract.normalize_code(scanned_fields.get("图号") or scanned_fields.get("文件编号") or "")
    if not code:
        return None, 0.0
    best = None
    best_score = 0.0
    for cand in candidates:
        c = cand["code"]
        if c == code:
            return cand, 1.0
        score = SequenceMatcher(None, code, c).ratio()
        if score > best_score:
            best_score = score
            best = cand
    if best_score >= 0.85:
        return best, best_score
    return None, 0.0
