# Overlay "3495.00" where "2654.00" was removed, ensuring the text is visibly added with right alignment
from pathlib import Path
import pymupdf

src_path = Path("/Users/vadimgeshiktor/repos/github.com/vgeshiktor/python-projects/bhops/workers/tamara.alexandrov/salary/tamara-alexandrov-320721582-7-2025.pdf")
out_path = src_path.with_name(src_path.stem + "_3495_overlayed.pdf")

doc = pymupdf.open(src_path)

target_text = "2654.00"
replacement_text = "3495.00"

total_hits = 0
page_details = []

# Padding to ensure clean white background before writing new text
pad = 0.8  # points

for page_num, page in enumerate(doc, start=1):
    rects = page.search_for(target_text)
    hits = len(rects)
    total_hits += hits
    
    # First: clear original text area with redaction
    expanded = [pymupdf.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad) for r in rects]
    for r in expanded:
        page.add_redact_annot(r, fill=(1,1,1))
    if expanded:
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
    
    # Second: write replacement text, right-aligned to keep column alignment
    for r in rects:
        # Choose a font size that fits the original text box height
        fontsize = max(9, min(16, (r.y1 - r.y0) * 0.9))
        page.insert_htmlbox(
            r, 
            replacement_text, 
            # fontname="helv", 
            # fontsize=fontsize, 
            # align=2,  # right align numbers
            # color=(0,0,0)
        )
    page_details.append({"page": page_num, "replacements": hits})

doc.save(out_path)
doc.close()
