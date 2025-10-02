#!/usr/bin/env python3
from __future__ import annotations
from pathlib import Path
import argparse, json, sys
import pymupdf  # fitz
from typing import List, Tuple, Dict, Any

# ---------- Utilities ----------
def is_hebrew(text: str) -> bool:
    return any("\u0590" <= ch <= "\u05FF" for ch in text)

def guess_font_size_for_rect(page, rect, fs_min: float, fs_max: float, default=11.0) -> float:
    """
    Try to capture the original span's font size intersecting the rect.
    Fallback to ~90% of rect height within [fs_min, fs_max].
    """
    try:
        d = page.get_text("dict")
        best, best_inter = None, -1.0
        for blk in d.get("blocks", []):
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    bbox = sp.get("bbox")
                    if not bbox:
                        continue
                    r2 = pymupdf.Rect(*bbox)
                    inter = rect & r2
                    area = inter.get_area() if inter else 0.0
                    if area > best_inter:
                        best, best_inter = sp, area
        if best is not None:
            return float(best.get("size", default))
    except Exception:
        pass
    return max(fs_min, min(fs_max, (rect.y1 - rect.y0) * 0.90))

def right_anchor_box(rect, text, fs, slack=3.0, fontname="helv"):
    """
    Build a new rectangle right-anchored at rect.x1, widened leftwards to fit text+slack.
    """
    w_new = pymupdf.get_text_length(text, fontname=fontname, fontsize=fs)
    return pymupdf.Rect(rect.x1 - w_new - slack, rect.y0, rect.x1, rect.y1)

def ensure_clean_background(page, rect, pad=1.2, extra_left=0.0):
    """
    Redact (no border) around the original text, expanded slightly on all sides.
    """
    rr = pymupdf.Rect(rect.x0 - pad - extra_left, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
    page.add_redact_annot(rr, fill=(1, 1, 1))

def apply_replacements_to_page(page, replacements: List[Dict[str, Any]],
                               pad: float, slack: float, fs_min: float, fs_max: float,
                               use_htmlbox: bool):
    """
    Apply a list of {old, new} replacements to a single page.
    """
    for rep in replacements:
        old = str(rep["old"])
        new = str(rep.get("new", ""))

        rects = page.search_for(old) or []
        if not rects:
            continue

        # 1) Collect font size for each occurrence; redact background
        matches: List[Tuple[pymupdf.Rect, float]] = []
        for r in rects:
            fs = guess_font_size_for_rect(page, r, fs_min, fs_max)
            w_old = pymupdf.get_text_length(old, fontname="helv", fontsize=fs)
            w_new = pymupdf.get_text_length(new, fontname="helv", fontsize=fs) if new else 0.0
            extra_left = max(0.0, w_new - w_old) + slack
            ensure_clean_background(page, r, pad=pad, extra_left=extra_left)
            matches.append((r, fs))

        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

        # 2) Write the new text (if not empty)
        if new:
            for r, fs in matches:
                rtl = is_hebrew(new)
                box = right_anchor_box(r, new, fs, slack=slack, fontname="helv")
                if use_htmlbox:
                    html = f"""
                    <div style="
                        {'direction: rtl;' if rtl else 'direction: ltr;'}
                        text-align: right;
                        font-size: {fs:.2f}pt;
                        line-height: 1;
                        white-space: nowrap;
                        margin: 0; padding: 0;
                        font-family: {'Arial, Helvetica, sans-serif' if rtl else 'Helvetica, Arial, sans-serif'};
                    ">{new}</div>
                    """
                    page.insert_htmlbox(box, html)
                else:
                    page.insert_textbox(box, new, fontname="helv", fontsize=fs, align=2, color=(0,0,0))

def process_pdf(in_path: Path, out_path: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    pad   = float(cfg.get("pad",   1.2))
    slack = float(cfg.get("slack", 3.0))
    fs_min = float(cfg.get("fs_min", 8.0))
    fs_max = float(cfg.get("fs_max", 18.0))
    replacements = cfg.get("replacements", [])
    if not isinstance(replacements, list):
        raise ValueError("Config must contain 'replacements': [ {old, new}, ... ]")

    doc = pymupdf.open(in_path)
    if doc.is_encrypted:
        try:
            doc.authenticate("")  # try empty password
        except Exception:
            doc.close()
            return {"file": str(in_path), "status": "skipped_encrypted"}

    # Detect htmlbox availability
    test_page = doc[0] if len(doc) else None
    use_htmlbox = bool(test_page and hasattr(test_page, "insert_htmlbox"))

    for page in doc:
        apply_replacements_to_page(page, replacements, pad, slack, fs_min, fs_max, use_htmlbox)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    doc.close()
    return {"file": str(in_path), "status": "ok", "out": str(out_path)}

# ---------- CLI ----------
def main():
    print("Bulk PDF text replacements using PyMuPDF.")
    ap = argparse.ArgumentParser(description="Bulk PDF text replacements using PyMuPDF.")
    ap.add_argument("--src-dir", required=True, type=Path, help="Directory containing PDFs")
    ap.add_argument("--config", required=True, type=Path, help="JSON config with replacements")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Output directory (default: <src-dir>/_edited)")
    ap.add_argument("--recursive", action="store_true", help="Recurse into subfolders")
    ap.add_argument("--suffix", default="", help="Optional suffix to append before .pdf (e.g., _edited)")
    args = ap.parse_args()

    src_dir: Path = args.src_dir
    out_dir: Path = args.out_dir or (src_dir / "_edited")
    recursive: bool = args.recursive
    suffix: str = args.suffix

    if not src_dir.exists() or not src_dir.is_dir():
        print(f"Source dir does not exist or not a directory: {src_dir}", file=sys.stderr)
        sys.exit(2)

    cfg = json.loads(args.config.read_text(encoding="utf-8"))

    pattern = "**/*.pdf" if recursive else "*.pdf"
    files = sorted(src_dir.glob(pattern))

    if not files:
        print("No PDF files found.", file=sys.stderr)
        sys.exit(1)

    results = []
    for f in files:
        rel = f.relative_to(src_dir)
        out_pdf = out_dir / rel
        if suffix:
            out_pdf = out_pdf.with_name(out_pdf.stem + suffix + out_pdf.suffix)
        try:
            res = process_pdf(f, out_pdf, cfg)
        except Exception as e:
            res = {"file": str(f), "status": "error", "error": str(e)}
        results.append(res)
        print(res)

if __name__ == "__main__":
    main()
