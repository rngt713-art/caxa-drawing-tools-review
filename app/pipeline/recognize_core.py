# -*- coding: utf-8 -*-
"""无界面识别核心：供中央机服务端（server_main.py）复用。
与 gui.py 的识别流程保持一致，但去掉所有弹窗交互：
- 人工补录 → 返回 ok=False 的条目，由客户端编辑后回传；
- 扫描总装图明细表 → 自动提取并使用（不做确认弹窗），记录到 warnings。
"""
import os
import re
import time

from PIL import Image

from . import extract, matcher, pdf_tools, vision


def _group_two_pagers(items, batch_dir):
    """只合并同一来源中相邻、图号相同且名称不冲突的页。"""
    items.sort(key=lambda x: (x["source"], int(str(x["page"]).split(",")[0])))
    merged = []
    i = 0
    while i < len(items):
        first = items[i]
        j = i
        while j + 1 < len(items):
            nxt = items[j + 1]
            same_source = nxt["source"] == first["source"]
            same_code = nxt["code"] == first["code"]
            same_kind = nxt["kind"] == first["kind"]
            adjacent = int(str(nxt["page"]).split(",")[0]) == int(str(items[j]["page"]).split(",")[0]) + 1
            name_ok = (not first["name"]) or (not nxt["name"]) or (nxt["name"] == first["name"])
            if same_source and same_code and same_kind and adjacent and name_ok:
                j += 1
            else:
                break
        if j > i:
            group = items[i:j + 1]
            first = dict(group[0])
            first["page"] = ",".join(str(x["page"]) for x in group)
            out = os.path.join(batch_dir, "merged_%s.pdf" % (first["code"] or "unknown"))
            pdf_tools.merge_pdfs([x["page_pdf"] for x in group], out)
            first["page_pdf"] = out
            first["status"] = "待上传"
            merged.append(first)
        else:
            merged.append(first)
        i = j + 1
    items[:] = merged


def decide_borrow(it, assemblies):
    """借用判定：只有 Z03 整机总装图明细表驱动；返回 (borrow, reason)。"""
    code = (it.get("code") or "").upper()
    machine = (it.get("machine") or "").upper()
    borrow = None
    for a in assemblies:
        codes = [str(c).upper() for c in (a.get("codes") or [])]
        if code in codes:
            borrow = True
            if not machine and a.get("machine"):
                machine = str(a["machine"]).upper()
                it["machine"] = machine
            break
    if borrow is None:
        borrow = False
    if machine and machine.startswith("03.GPZ."):
        borrow = False
    if borrow:
        reason = "总装图明细表"
    elif machine and machine.startswith("03.GPZ."):
        reason = "03.GPZ钢瓶组"
    elif machine and any(str(a.get("machine", "")).upper() == machine for a in assemblies):
        reason = "有总装图未列入"
    else:
        reason = "无Z03总装图/未命中"
    return bool(borrow), reason


def _extract_bom_from_scan(png, v, log):
    """两步提取扫描总装图明细表：定位区域 → 同窗口转录。定位失败回退下部裁剪。"""
    img = Image.open(png).convert("RGB")
    w, h = img.size
    locate_png = png.replace(".png", "_locate.png")
    scale = min(1.0, 1280.0 / max(w, h))
    small = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    small.save(locate_png)
    raw_loc = v.ask(locate_png, extract.PROMPT_BOM_LOCATE, max_tokens=120)
    region = extract.parse_bom_region(raw_loc)
    history = None
    if region:
        x0, y0, x1, y1 = region
        pad = 0.015
        bx0 = max(0, int((x0 - pad) * w)); by0 = max(0, int((y0 - pad) * h))
        bx1 = min(w, int((x1 + pad) * w)); by1 = min(h, int((y1 + pad) * h))
        crop = img.crop((bx0, by0, bx1, by1))
        cw, ch = crop.size
        if max(cw, ch) < 1000:
            k = 1000.0 / max(cw, ch)
            crop = crop.resize((int(cw * k), int(ch * k)), Image.LANCZOS)
        elif max(cw, ch) > 1800:
            k = 1800.0 / max(cw, ch)
            crop = crop.resize((int(cw * k), int(ch * k)), Image.LANCZOS)
        bom_png = png.replace(".png", "_bom.png")
        crop.save(bom_png)
        history = [
            {"role": "user", "content": [vision.image_message(locate_png),
                                         {"type": "text", "text": extract.PROMPT_BOM_LOCATE}]},
            {"role": "assistant", "content": raw_loc},
        ]
        log("已由模型定位明细表区域：(%s)" % ("%.2f,%.2f,%.2f,%.2f" % region))
    else:
        crop = img.crop((0, int(h * 0.45), w, h))
        crop = crop.resize((int(crop.width * 1.6), int(crop.height * 1.6)), Image.LANCZOS)
        bom_png = png.replace(".png", "_bom.png")
        crop.save(bom_png)
        log("明细表区域定位失败，回退页面下部裁剪")
    raw = v.ask(bom_png, extract.PROMPT_BOM, max_tokens=1500, history=history)
    return extract.parse_bom_markdown(raw)


def run_recognition(scans, readable_path, assembly_paths, v, work_dir=None,
                    log=print, progress=None):
    """扫描识别（无界面）。scans/readable_path/assembly_paths 为本地文件路径。
    返回 (items, assemblies, warnings)。"""
    base_work = work_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", "work")
    batch_dir = os.path.join(base_work, "server_batch_%s" % int(time.time()))
    os.makedirs(batch_dir, exist_ok=True)
    warnings = []
    assemblies = []
    for ap in assembly_paths:
        if not os.path.exists(ap):
            continue
        try:
            a = matcher.load_assembly(ap)
            if a.get("invalid"):
                log("已忽略 %s：%s" % (os.path.basename(ap), a.get("reason", "非 Z03 总装图")))
                continue
            assemblies.append(a)
            log("总装图 %s → 整机 %s，明细表 %d 项" % (
                os.path.basename(ap), a["machine"] or "?", len(a["codes"])))
        except Exception as ex:
            log("总装图解析失败 %s：%s" % (os.path.basename(ap), ex))
    candidates = []
    if readable_path and os.path.exists(readable_path):
        try:
            candidates = matcher.build_candidates_from_texts(
                pdf_tools.extract_all_text(readable_path))
            log("可读原图候选 %d 条" % len(candidates))
        except Exception as ex:
            log("可读原图解析失败：%s" % ex)
    items = []
    recognized_z_machines = set()
    total_pages = 0
    for src in scans:
        try:
            pages = pdf_tools.split_pdf(src, os.path.join(batch_dir, "pages"))
        except Exception as ex:
            warnings.append("拆分失败 %s：%s" % (os.path.basename(src), ex))
            log("拆分失败 %s：%s" % (os.path.basename(src), ex))
            continue
        total_pages += len(pages)
    done_pages = 0
    for src in scans:
        log("处理：%s" % os.path.basename(src))
        try:
            pages = pdf_tools.split_pdf(src, os.path.join(batch_dir, "pages"))
        except Exception:
            continue
        for idx, page_pdf in enumerate(pages):
            if progress:
                progress(done_pages, total_pages)
            done_pages += 1
            png = os.path.join(batch_dir, "render_%d.png" % done_pages)
            pdf_tools.render_pdf_page(page_pdf, 0, png)
            raw = v.ask(png, extract.PROMPT)
            fields = extract.parse_vision_output(raw)
            resolved = extract.resolve_document(fields, source_filename=src, raw=raw)
            cand = None
            if candidates:
                cand, score = matcher.match_scanned(fields, candidates)
                if cand and score >= 0.85:
                    if not resolved["code"]:
                        resolved["code"] = cand["code"]
                    if not resolved["name"]:
                        resolved["name"] = cand["name"]
                    if not resolved["machine"]:
                        resolved["machine"] = cand["machine"]
                    if resolved["kind"] == "unknown" and cand["kind"] != "unknown":
                        resolved["kind"] = cand["kind"]
                    log("可读原图匹配候选：%s（%s）" % (cand["code"], cand["name"]))
            items.append({
                "source": os.path.basename(src),
                "page": idx + 1,
                "page_pdf": page_pdf,
                "code": resolved["code"],
                "name": resolved["name"],
                "machine": resolved["machine"],
                "kind": resolved["kind"],
                "ok": resolved["ok"],
                "calc_doc": resolved.get("calc_doc", False),
                "upload_sel": True,
                "status": "待确认" if not resolved["ok"] else "待上传",
                "target": "",
                "note": "",
            })
            need_code = resolved["kind"] == "component" and not resolved["code"].startswith(("01.", "02.", "04."))
            need_machine = resolved["kind"] in ("component", "machine_drawing") and not resolved["machine"]
            need_z = resolved["kind"] == "machine_drawing" and resolved["code"].startswith("03.")
            # 组件图号以标题栏为准：全页模型可能把材料明细表里的图号读成“图号”字段，
            # 紧裁剪（标题栏本身）取出的最后一个 01/02/04 代码才是组件自己的图号
            verify_component = resolved["kind"] == "component"
            if need_code or need_machine or need_z or verify_component:
                try:
                    cand_code = ""
                    cand_machine = ""
                    cand_z = ""
                    try:
                        img = Image.open(png).convert("RGB")
                        w, h = img.size
                        # 紧裁剪：只圈标题栏本身（明细表通常在标题栏上方，避免误取明细表图号）
                        tb = img.crop((int(w * 0.45), int(h * 0.70), w, h))
                        tb = tb.resize((int(tb.width * 1.8), int(tb.height * 1.8)), Image.LANCZOS)
                        tb_png = png.replace(".png", "_tb.png")
                        tb.save(tb_png)
                        raw2 = v.ask(tb_png, extract.PROMPT_TITLEBLOCK, max_tokens=800)
                        found_codes = []
                        for m in re.finditer(r"(?<![0-9A-Z.])(\d{2}\.[A-Z0-9.\-]+)", raw2.upper()):
                            c = extract.normalize_code(m.group(1))
                            if c.startswith(("01.", "02.", "04.")):
                                found_codes.append(c)
                        if found_codes:
                            cand_code = found_codes[-1]
                        for m in re.finditer(r"(?<![0-9A-Z.])03\.[A-Z0-9.\-]+", raw2.upper()):
                            c = extract.normalize_code(m.group(0))
                            if c.startswith("03.") and not c.startswith("03.GPZ."):
                                cand_machine = c
                                break
                        if need_z:
                            # Z03/明细表判定用宽裁剪（含材料明细表区域）
                            tbw = img.crop((int(w * 0.40), int(h * 0.45), w, h))
                            tbw = tbw.resize((int(tbw.width * 1.5), int(tbw.height * 1.5)), Image.LANCZOS)
                            tbw_png = png.replace(".png", "_tbw.png")
                            tbw.save(tbw_png)
                            raw2z = v.ask(tbw_png, extract.PROMPT_TITLEBLOCK, max_tokens=800)
                            mz = re.search(r"Z03\.[A-Z0-9.\-]+", raw2z.upper())
                            if mz:
                                cand_z = extract.normalize_code(mz.group(0))
                            else:
                                up2 = raw2z.upper()
                                if "物料代码" in up2 or ("序号" in up2 and "物料" in up2):
                                    cand_z = "Z" + items[-1]["code"]
                    except Exception as ex:
                        log("标题栏放大识别失败：%s" % ex)
                    if (need_code and not cand_code) or (need_machine and not cand_machine):
                        raw3 = v.ask(png, extract.PROMPT_MACHINE, max_tokens=120)
                        fields3 = extract.parse_vision_output(raw3)
                        if not cand_code:
                            cand_code = extract.normalize_code(fields3.get("图号") or "")
                        if not cand_machine:
                            cand_machine = extract.normalize_code(fields3.get("成品代码") or "")
                    if cand_code.startswith(("01.", "02.", "04.")) and (need_code or verify_component):
                        if cand_code != items[-1]["code"]:
                            log("标题栏校正图号：%s" % cand_code)
                        items[-1]["code"] = cand_code
                        items[-1]["ok"] = True
                        items[-1]["status"] = "待上传"
                    if need_machine and cand_machine.startswith("03."):
                        items[-1]["machine"] = cand_machine
                        log("补充识别成品代码：%s" % items[-1]["machine"])
                    if need_z and cand_z:
                        items[-1]["code"] = cand_z
                        items[-1]["kind"] = "process_doc"
                        items[-1]["machine"] = cand_z[1:]
                        items[-1]["ok"] = True
                        items[-1]["status"] = "待上传"
                        recognized_z_machines.add(cand_z[1:])
                        log("识别为总装图：%s" % cand_z)
                    if need_code and not items[-1]["code"].startswith(("01.", "02.", "04.")):
                        log("组件图号仍缺失，请在客户端补录中手动填写")
                    elif need_machine and not items[-1]["machine"]:
                        log("未找到 03. 开头的整机代码（通用件，无需借用）")
                except Exception as ex:
                    log("成品代码重试失败：%s" % ex)
            # 扫描件识别出的 Z03 总装图（含 03→Z 转换与直接读出 Z03.xx 两种路径）：
            # 统一自动提取材料明细表用于借用，避免只走转换分支时漏提取
            if items[-1]["kind"] == "process_doc" and items[-1]["code"].startswith("Z03."):
                z_machine = items[-1]["code"][1:]
                recognized_z_machines.add(z_machine)
                if not any(a.get("machine") == z_machine and a.get("from_scan") for a in assemblies):
                    try:
                        bom_rows = _extract_bom_from_scan(png, v, log)
                    except Exception as ex:
                        bom_rows = []
                        log("总装图明细表提取失败：%s" % ex)
                    if bom_rows:
                        assemblies.append({
                            "machine": z_machine,
                            "codes": [r["code"] for r in bom_rows],
                            "names": {r["code"]: r["name"] for r in bom_rows},
                            "file": page_pdf,
                            "from_scan": True,
                        })
                        warnings.append("扫描总装图 %s 明细表已自动提取 %d 项（未人工核对），"
                                        "如需精确借用请添加可读总装图" % (items[-1]["code"], len(bom_rows)))
                        log("扫描总装图明细表自动提取 %d 项" % len(bom_rows))
                    else:
                        log("扫描总装图未提取到明细表（如需借用请添加可读总装图）")
    for it in items:
        if it["kind"] == "component":
            borrow, reason = decide_borrow(it, assemblies)
            it["borrow"] = borrow
            log("组件 %s 借用=%s（%s）" % (it["code"], "是" if borrow else "否", reason))
    if recognized_z_machines:
        added_z = {a.get("machine") for a in assemblies if a.get("machine")}
        missing_z = sorted(recognized_z_machines - added_z)
        if missing_z:
            warnings.append("识别到总装图 %s，但未添加可读总装图且扫描明细表未提取，"
                            "这些整机的组件将不借用" % "、".join(missing_z))
    _group_two_pagers(items, batch_dir)
    calc_items = [it for it in items if it.get("calc_doc")]
    if calc_items:
        warnings.append("检测到计算书类文档（正常不应出现）：%s" %
                        "、".join("第%s页 %s" % (it["page"], it["name"] or it["code"]) for it in calc_items))
    # 结果不落盘 page_pdf（客户端不需要本地文件）
    for it in items:
        it.pop("page_pdf", None)
        it.pop("png", None)
    return items, assemblies, warnings
