import os
import re

FIELD_ALIASES = {
    "图号": ["图号", "图样代号", "代号", "零件图号"],
    "名称": ["名称", "零件名", "零件名称", "文件名"],
    "成品代码": ["成品代码", "整机代码", "适用机型", "适用型号"],
    "文件编号": ["文件编号", "编号"],
    "上级图号": ["上级图号"],
}

PROMPT = (
    "这是一张工程图纸/技术文档的页面扫描件。请只做转录，找出并输出以下字段（没有的字段不要编造，"
    "找不到就写“无”）：图号、名称、成品代码（整机代码，通常以03.开头）、文件编号、上级图号。"
    "注意：图号、名称、成品代码、文件编号、上级图号都只从标题栏读取；"
    "材料明细表/零件清单里的编号和名称不是图号，不要使用。"
    "请严格按以下格式每行一个字段输出：\n"
    "图号: <值>\n名称: <值>\n成品代码: <值>\n文件编号: <值>\n上级图号: <值>\n"
    "数字和字母必须逐字转录，不要加空格。"
)

PROMPT_MACHINE = (
    "这是工程图纸/文档页面。只做转录：请找出“成品代码/整机代码/适用机型”字段，"
    "它通常是形如 03.OW.06-05035 的编号（03.开头）。只输出一行：\n"
    "成品代码: <值>\n找不到就输出：成品代码: 无"
)

PROMPT_TITLEBLOCK = (
    "请逐字转录这张图纸标题栏区域内的全部文字和编号，每行一项，不要解释、不要改写、不要编造。"
    "特别注意形如 03.XX.XXX 的整机/成品代码、图号和名称。"
)

PROMPT_BOM = (
    "这是工程图纸的材料明细表（BOM）区域扫描件。请只转录材料明细表中的内容，"
    "输出一个 markdown 表格，列：序号 | 图号 | 名称。\n"
    "要求：\n"
    "1. 只输出表格本身，不要任何解释文字；\n"
    "2. 图号必须逐字转录（形如 04.13.MTEST-01、01.70.MTEST-01），不要加空格、不要编造；\n"
    "3. 每行都保留，找不到图号的行图号写“无”；\n"
    "4. 如果页面没有材料明细表，只输出一行：无"
)

PROMPT_BOM_LOCATE = (
    "这是工程图纸的整页扫描件。请找出“材料明细表（BOM）”所在的矩形区域"
    "（通常带“序号/图号/名称/材料”等表头，位置随设计师习惯不同）。"
    "只输出该区域坐标，格式为一行：\n"
    "x0,y0,x1,y1\n"
    "其中 x0,y0 是区域左上角、x1,y1 是右下角占整页宽/高的比例，取值 0~1 的小数"
    "（例如 0.05,0.55,0.95,0.98；左上角为原点、向下为 y 增大）。不要解释。"
    "如果页面没有材料明细表，只输出：无"
)


def normalize_code(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    s = s.replace("，", ",").replace("：", ":").replace("。", ".").replace(" ", "")
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("Ｏ", "O").replace("０", "0").replace("１", "1").replace("２", "2")
    s = s.replace("３", "3").replace("４", "4").replace("５", "5").replace("６", "6")
    s = s.replace("７", "7").replace("８", "8").replace("９", "9")
    s = s.upper()
    # 03.0W → 03.OW（整机系列是字母，数字0误读为字母O的情况）
    # 03.0W / M03.0W → 03.OW / M03.OW（整机系列是字母，数字0误读为字母O）
    s = re.sub(r"(^[A-Z]?03\.)0(?=[A-Z])", r"\1O", s)
    return s


def _find_field(text: str, aliases: list) -> str:
    for alias in aliases:
        for m in re.finditer(re.escape(alias) + r"\s*[:：]\s*([^\n]+)", text):
            val = m.group(1).strip().strip("|").strip()
            if val and val != "无":
                return val
    return ""


def parse_vision_output(text: str) -> dict:
    fields = {}
    for key, aliases in FIELD_ALIASES.items():
        fields[key] = _find_field(text, aliases)
    # 兜底：整段文字里找一个像 03.xx 的整机代码
    if not fields["成品代码"]:
        m = re.search(r"(?<![A-Z])03\.[A-Z0-9.\-]+", text.upper())
        if m:
            fields["成品代码"] = m.group(0)
    return fields


def _code_name_from_cells(cells):
    """从 markdown 表格的一行单元格中提取 (图号, 名称)。"""
    code = ""
    name = ""
    for cell in cells:
        c = cell.strip()
        if not c:
            continue
        if not code:
            m = re.match(r"^((?:01|02|04)\.[A-Z0-9.\-]+)$", c.upper())
            if m:
                code = normalize_code(m.group(1))
                continue
            m = re.search(r"(?<![0-9A-Z.])((?:01|02|04)\.[A-Z0-9.\-]+)", c.upper())
            if m:
                code = normalize_code(m.group(1))
                rest = re.sub(r"(?<![0-9A-Z.])((?:01|02|04)\.[A-Z0-9.\-]+)", "", c.upper())
                rest = rest.strip(" |，。、")
                if rest and re.search(r"[\u4e00-\u9fff]", c) and not name:
                    name = rest
                continue
        if not name and re.search(r"[\u4e00-\u9fff]", c):
            name = c.strip(" |，。、")
    return code, name


def parse_bom_markdown(text: str) -> list:
    """qwen 返回的材料明细表 markdown → [{code, name}]。
    只保留 01./02./04. 开头的图号（与可读总装图 bom_codes_from_text 一致），去重保序。
    兼容 markdown 表格（| 序号 | 图号 | 名称 |）与“图号 名称”纯文本行。
    """
    rows = []
    seen = set()
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ln in ("无", "没有", "无材料明细表", "N/A"):
            continue
        if "|" in ln:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            joined = "".join(cells)
            if "图号" in joined or "序号" in joined:
                continue  # 表头
            if not joined.replace("-", "").replace(":", "").strip():
                continue  # 分隔行
            code, name = _code_name_from_cells(cells)
            if not code:
                continue
            code = code.rstrip(".-")
            if not code.startswith(("01.", "02.", "04.")):
                continue
            if code not in seen:
                seen.add(code)
                rows.append({"code": code, "name": name})
            continue
        # 纯文本行：图号 [名称]
        m = re.search(r"(?<![0-9A-Z.])((?:01|02|04)\.[A-Z0-9.\-]+)\s*(.*)$", ln, re.IGNORECASE)
        if not m:
            continue
        code = normalize_code(m.group(1)).rstrip(".-")
        name = m.group(2).strip(" |，。、")
        if code not in seen:
            seen.add(code)
            rows.append({"code": code, "name": name})
    return rows


def parse_bom_region(text: str):
    """从定位结果解析明细表区域 (x0,y0,x1,y1)（0~1 比例）。失败返回 None。
    兼容 “0.05,0.55,0.95,0.98” 与 “5%,55%,95%,98%” 两种写法。"""
    if not text:
        return None
    nums = []
    for m in re.finditer(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*(%)?", text):
        v = float(m.group(1))
        if m.group(2):
            v /= 100.0
        nums.append(v)
    if len(nums) < 4:
        return None
    x0, y0, x1, y1 = nums[:4]
    if any(v > 1.5 for v in (x0, y0, x1, y1)):
        return None  # 不是 0~1 比例（可能给了像素），放弃定位
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    x0 = max(0.0, min(1.0, x0)); x1 = max(0.0, min(1.0, x1))
    y0 = max(0.0, min(1.0, y0)); y1 = max(0.0, min(1.0, y1))
    if (x1 - x0) < 0.02 or (y1 - y0) < 0.02:
        return None
    return (x0, y0, x1, y1)


def classify(code: str):
    """Return (kind, machine_code, ok).
    kind: 'process_doc' | 'machine_drawing' | 'component' | 'unknown'
    """
    c = normalize_code(code)
    if not c:
        return "unknown", "", False
    m = re.match(r"^([A-Z])03\.", c)
    if m:
        return "process_doc", c[1:], True
    if c.startswith("03."):
        return "machine_drawing", c, True
    if re.match(r"^\d{2}\.", c):
        return "component", "", True
    return "unknown", "", False


DOC_NAME_KEYWORDS = ["检测", "试验", "铭牌", "说明", "工艺", "计算", "手册", "维护", "taglist", "pid", "参数", "检验"]
CALC_KEYWORDS = ["流速", "传动计算", "计算书", "皮带传动", "计算"]
PREFIX_BY_KEYWORD = [
    ("铭牌", "M"), ("PID", "P"), ("TAGLIST", "T"), ("标签", "T"), ("总装", "Z"),
    ("检测", "J"), ("检验", "J"), ("试验", "J"),
]


def prefix_from_name(name: str) -> str:
    n = (name or "").upper()
    for kw, pfx in PREFIX_BY_KEYWORD:
        if kw in n:
            return pfx
    return ""


def gas_from_machine(machine: str) -> str:
    """从整机代码推导介质：03.HE* → 氦气，03.NA* → 氮气；HT 等混合系列不推导。"""
    m = re.match(r"^03\.([A-Z]+)\.", normalize_code(machine))
    if not m:
        return ""
    series = m.group(1)
    if series.startswith("HE"):
        return "氦气"
    if series.startswith("NA"):
        return "氮气"
    return ""


def apply_gas_correction(name: str, machine: str) -> str:
    gas = gas_from_machine(machine)
    if not gas:
        return name
    if "氦" in name or "氮" in name:
        if gas == "氦气" and "氦" not in name:
            name = name.replace("氮", "氦")
        elif gas == "氮气" and "氮" not in name:
            name = name.replace("氦", "氮")
    return name


def code_from_filename(filename: str):
    """Extract a code from a source file name if it looks like 图号 [名称].pdf."""
    base = os.path.splitext(os.path.basename(filename or ""))[0]
    m = re.match(r"^\s*([A-Z0-9]+\.[A-Z0-9.\-]+)\s*(.*)$", base, re.IGNORECASE)
    if not m:
        return "", ""
    code = normalize_code(m.group(1))
    name = m.group(2).strip()
    kind, machine, ok = classify(code)
    if not ok:
        return "", ""
    return code, name


def resolve_document(fields: dict, source_filename: str = "", raw: str = "") -> dict:
    """Resolve a scanned page into (code, name, machine, kind).
    Uses filename prefix, 文件编号, 图号, 成品代码 and document-name keywords.
    """
    draw_code = normalize_code(fields.get("图号") or "")
    doc_code = normalize_code(fields.get("文件编号") or "")
    machine = normalize_code(fields.get("成品代码") or "")
    name = (fields.get("名称") or "").strip()
    fn_code, fn_name = code_from_filename(source_filename)
    # 组件图号与整机代码互换校验
    if draw_code.startswith("03.") and machine.startswith(("01.", "02.", "04.")):
        draw_code, machine = machine, draw_code
    comp_name = any(k in (name or "") for k in ("组件", "系统"))

    codes = [fn_code, doc_code, draw_code]
    kind = "unknown"
    code = ""
    # 1) 字母前缀 + 03. → 工艺文档
    for c in codes:
        if re.match(r"^[A-Z]03\.", c):
            kind = "process_doc"
            code = c
            machine = c[1:]
            break
    # 2) 03. 开头 → 整机图纸（若名称像文档则视为工艺文档）
    if kind == "unknown":
        # 组件名称但图号被误读为整机代码 → 判为组件并清空图号，交给标题栏重试
        if comp_name and draw_code.startswith("03."):
            kind = "component"
            code = ""
            machine = machine or draw_code
        for c in codes:
            if kind != "unknown":
                break
            if c.startswith("03."):
                looks_doc = any(k in (name or "") for k in DOC_NAME_KEYWORDS)
                if looks_doc:
                    kind = "process_doc"
                    code = c
                else:
                    kind = "machine_drawing"
                    code = c
                machine = machine or c
                break
    if kind == "machine_drawing" and code:
        machine = code  # 整机图纸的整机代码就是图号本身
    # 3) 01./04. 等 → 组件/普通图纸
    if kind == "unknown" and re.match(r"^\d{2}\.", draw_code):
        kind = "component"
        code = draw_code
    # 4) 只有成品代码且名称像文档 → 工艺文档
    if kind == "unknown" and machine.startswith("03.") and any(k in (name or "") for k in DOC_NAME_KEYWORDS):
        kind = "process_doc"
        code = machine
    # 总装图：图号被读成 03.xx（漏掉 Z 前缀），但页面带材料明细表 → 判为 Z03 工艺文档
    if kind == "machine_drawing" and code.startswith("03.") and raw:
        up = raw.upper()
        if "物料代码" in up or ("序号" in up and "物料" in up):
            kind = "process_doc"
            code = "Z" + code
            machine = code[1:]
    if kind == "process_doc" and re.match(r"^03\.", code):
        pfx = prefix_from_name(name)
        if pfx:
            code = pfx + code
            machine = code[1:]
    # 组件图号与整机代码校验：图号应为 01./02./04. 开头，成品代码应为 03. 开头
    if kind == "component":
        if code.startswith("03.") and machine.startswith(("01.", "02.", "04.")):
            code, machine = machine, code
        elif not code.startswith(("01.", "02.", "04.")):
            code = ""  # 图号被误读为整机代码，交给标题栏重试/人工补录
    name = apply_gas_correction(name, machine)
    if not name:
        gas = gas_from_machine(machine)
        if gas:
            name = gas + "压缩机"
        elif fn_name:
            name = fn_name
    if name:
        m = re.match(r"^(?:[A-Z]?\d{2}\.[A-Z0-9.\-]+)\s+(.*)$", name.strip())
        if m:
            name = m.group(1).strip()
    return {
        "code": code,
        "name": name,
        "machine": machine,
        "kind": kind,
        "ok": kind != "unknown" and bool(code),
        "calc_doc": any(k in (name or "") for k in CALC_KEYWORDS),
    }


def candidate_parent_codes(code: str) -> list:
    """From longest prefix (strip to previous dot) up to the shortest."""
    c = normalize_code(code)
    parts = c.split(".")
    parents = []
    for i in range(len(parts) - 1, 0, -1):
        parents.append(".".join(parts[:i]))
    return parents


def expected_file_name(code: str, name: str) -> str:
    c = normalize_code(code)
    if re.match(r"^[A-Z]03\.", c):
        return "%s.pdf" % c
    safe = re.sub(r'[\\/:*?"<>|]', "_", name or "").strip()
    return "%s %s.pdf" % (c, safe) if safe else c + ".pdf"
