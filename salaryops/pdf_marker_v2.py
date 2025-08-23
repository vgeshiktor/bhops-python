# Re-running the annotation code after kernel reset to produce the marked PDF and return its path.

import re
import pymupdf  # PyMuPDF

INPUT  = "/Users/vadimgeshiktor/repos/github.com/vgeshiktor/python-projects/bhops/workers/tamara.alexandrov/salary/tamara-alexandrov-320721582-7-2025.pdf"
OUTPUT = "/Users/vadimgeshiktor/repos/github.com/vgeshiktor/python-projects/bhops/workers/tamara.alexandrov/salary/tamara-alexandrov-320721582-7-2025-marked.pdf"

AMOUNT_RE = re.compile(
    r"""
    ^\s*
    (?:₪|\$)?\s*
    (?:\d{1,3}(?:[,\u200f\u200e\ ]\d{3})+|\d+)
    (?:[.,]\d{2})?
    \s*$
    """,
    re.VERBOSE
)

def words_on_page(page):
    return page.get_text("words")

def y_overlap(rect, w_tuple):
    return max(0, min(rect.y1, w_tuple[3]) - max(rect.y0, w_tuple[1]))

def find_amount_rect_near_label(page, label_rect):
    words = words_on_page(page)
    candidates = []
    label_h = label_rect.y1 - label_rect.y0
    label_center_x = (label_rect.x0 + label_rect.x1) / 2

    for w in words:
        x0, y0, x1, y1, text, blk, ln, wno = w
        if y_overlap(label_rect, w) < 0.45 * min(label_h, (y1 - y0)):
            continue
        if AMOUNT_RE.search(text):
            if x1 <= label_rect.x0:
                dx = label_rect.x0 - x1
            elif x0 >= label_rect.x1:
                dx = x0 - label_rect.x1
            else:
                dx = 0
            candidates.append((dx, (x0, y0, x1, y1), blk, ln, text))

    if not candidates:
        return None

    candidates.sort(key=lambda c: (c[0], abs(((c[1][0]+c[1][2])/2) - label_center_x)))
    x0, y0, x1, y1 = candidates[0][1]
    blk, ln = candidates[0][2], candidates[0][3]

    GAP = 12
    for w in words:
        wx0, wy0, wx1, wy1, wt, wblk, wln, wno = w
        if wblk == blk and wln == ln:
            same_line = y_overlap(pymupdf.Rect(x0, y0, x1, y1), w) > 0.6 * min((wy1-wy0), (y1-y0))
            if not same_line:
                continue
            neighbor = (AMOUNT_RE.search(wt) is not None) or bool(re.fullmatch(r"[₪:$,.-]+", wt))
            if neighbor:
                if 0 <= x0 - wx1 < GAP or 0 <= wx0 - x1 < GAP:
                    x0, y0, x1, y1 = min(x0, wx0), min(y0, wy0), max(x1, wx1), max(y1, wy1)

    pad = 2
    return pymupdf.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)

doc = pymupdf.open(INPUT)
labels = ["שכר נטו", "לתשלום"]

marked = 0
for page in doc:
    for label in labels:
        for rect in page.search_for(label):
            amt_rect = find_amount_rect_near_label(page, rect)
            target_rect = amt_rect if amt_rect is not None else rect
            page.draw_rect(target_rect, color=(1, 0, 0), width=2)
            marked += 1

doc.save(OUTPUT)
