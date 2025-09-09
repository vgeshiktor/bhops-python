from pathlib import Path
import pymupdf  # recent version with insert_htmlbox

src_path = Path("/Users/vadimgeshiktor/repos/github.com/vgeshiktor/python-projects/bhops/workers/moran.hilo/salary/moran-hilo-302615372-8-2025.pdf")
out_path = src_path.with_name(src_path.stem + "_3495_overlayed.pdf")

doc = pymupdf.open(src_path)

OLD = "הפחתת"
NEW = ""

PAD   = 0.0   # to fully clear background
SLACK = 1.0   # a few extra points so nothing clips

for page in doc:
    rects = page.search_for(OLD)
    if not rects:
        continue

    for r in rects:
        # font size from the original text box height
        fs = max(9, min(16, (r.y1 - r.y0) * 0.95))
        # measure widths using base-14 Helvetica (PDF name "helv")
        w_old = pymupdf.get_text_length(OLD, fontname="helv", fontsize=fs)
        w_new = pymupdf.get_text_length(NEW, fontname="helv", fontsize=fs)
        extra_left = max(0.0, w_new - w_old) + SLACK

        # 1) clean background: expand left by the needed extra width
        rr = pymupdf.Rect(r.x0 - PAD - extra_left, r.y0 - PAD, r.x1 + PAD, r.y1 + PAD)
        page.add_redact_annot(rr, fill=(1, 1, 1))

    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

    for r in rects:
        fs = max(9, min(16, (r.y1 - r.y0) * 0.95))
        w_new = pymupdf.get_text_length(NEW, fontname="helv", fontsize=fs)
        # 2) right-anchored, widened box: x1 stays the same; expand leftwards
        box = pymupdf.Rect(r.x1 - w_new - SLACK, r.y0, r.x1, r.y1)
        html = f"""
        <div style="
            font-family: Helvetica, Arial, sans-serif;
            font-size: {fs:.2f}pt;
            font-weight: 400;
            line-height: 1;
            text-align: right;
            white-space: nowrap;
            margin: 0; padding: 0;
        ">{NEW}</div>
        """
        page.insert_htmlbox(box, html)
        # drawn = page.insert_textbox(box, NEW, fontname="helv", fontsize=fs, align=2, color=(0,0,0))

doc.save(out_path)
doc.close()
print("Saved:", out_path)
