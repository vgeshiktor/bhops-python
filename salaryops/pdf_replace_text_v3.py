# Replace "2654.00" with "3495.00" matching the original span's font SIZE and avoiding any borders
from pathlib import Path
import pymupdf  # PyMuPDF

src_path = Path("/Users/vadimgeshiktor/repos/github.com/vgeshiktor/python-projects/bhops/workers/moran.hilo/salary/moran-hilo-302615372-8-2025.pdf")
out_path = src_path.with_name(src_path.stem + "_3495_matchsize.pdf")

doc = pymupdf.open(src_path)

OLD = "4704.32"
NEW = "2723.00"

PAD   = 1.2   # background cleanup
SLACK = 3.0   # extra width to the left to avoid clipping

ops = []

for page_index, page in enumerate(doc, start=1):
    # find all rectangles for OLD
    rects = page.search_for(OLD)
    if not rects:
        continue

    # parse text to get spans with sizes
    text_dict = page.get_text("dict")
    candidate_spans = []
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = span.get("text", "")
                if OLD in txt:
                    # bbox is [x0, y0, x1, y1]
                    bbox = span.get("bbox", None)
                    if bbox:
                        candidate_spans.append({
                            "text": txt,
                            "bbox": pymupdf.Rect(*bbox),
                            "size": span.get("size", 11.0),
                            "font": span.get("font", ""),
                        })

    # For each found rect, match to the nearest candidate span to get its size
    matched = []
    for r in rects:
        # choose the candidate span with max intersection area
        best = None
        best_i = -1.0
        for cs in candidate_spans:
            inter = r & cs["bbox"]
            inter_area = inter.get_area() if inter else 0.0
            if inter_area > best_i:
                best = cs
                best_i = inter_area
        if best is None:
            # fallback: approximate fs from rect height
            fs = max(9, min(16, (r.y1 - r.y0) * 0.90))
        else:
            fs = best["size"]
        # 1) redact a slightly bigger area (no border)
        rr = pymupdf.Rect(r.x0 - PAD, r.y0 - PAD, r.x1 + PAD, r.y1 + PAD)
        page.add_redact_annot(rr, fill=(1,1,1))
        matched.append((r, fs))

    # apply all redactions for the page
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

    # 2) overlay with right-anchored widened box using Helvetica at the matched size
    for (r, fs) in matched:
        new_w = pymupdf.get_text_length(NEW, fontname="helv", fontsize=fs)
        old_w = pymupdf.get_text_length(OLD, fontname="helv", fontsize=fs)
        extra_left = max(0.0, new_w - old_w) + SLACK
        box = pymupdf.Rect(r.x1 - new_w - SLACK, r.y0, r.x1, r.y1)
        drawn = page.insert_textbox(box, NEW, fontname="helv", fontsize=fs, color=(0,0,0), align=2)
        ops.append({"page": page_index, "fs": fs, "box": (box.x0, box.y0, box.x1, box.y1), "drawn": drawn})

doc.save(out_path)
doc.close()