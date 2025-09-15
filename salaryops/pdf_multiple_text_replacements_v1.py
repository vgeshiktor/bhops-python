from pathlib import Path
import pymupdf  # fitz
from typing import List, Tuple, Dict, Any

# ---------- הגדרות קלט ----------
src_path = Path("/Users/vadimgeshiktor/repos/github.com/vgeshiktor/python-projects/bhops/workers/moran.hilo/salary/moran-hilo-302615372-8-2025.pdf")
out_path = src_path.with_name(src_path.stem + "_multi_replacements.pdf")

# רשימת החלפות: כל איבר הוא tuple או dict עם old/new (אפשר להשאיר new="" כדי למחוק)
REPLACEMENTS: List[Dict[str, Any]] = [
    {"old": "005", "new": ""},          # דוגמה: מחיקה
    {"old": "073", "new": ""},
    {"old": "הפחתת הבראה פ", "new": ""},
    {"old": "הבראה פ", "new": ""},
    {"old": "5.42", "new": ""},
    {"old": "-0.68", "new": ""},
    {"old": "418.00", "new": ""},
    {"old": "2265.56",  "new": ""},
    {"old": "-284.24",  "new": ""},
    {"old": "4704.32",  "new": "2723.00"},
    {"old": "4349.00",  "new": "2367.68"},    
]

# רגישויות עדינות
PAD   = 0.0  # "לנקות" רקע מסביב לטקסט הישן
SLACK = 0.0  # מרווח בטחון שמאלה כדי למנוע חיתוך
FS_MIN, FS_MAX = 8.0, 18.0  # גבולות סבירים לפונט


# ---------- פונקציות עזר ----------
def is_hebrew(text: str) -> bool:
    return any("\u0590" <= ch <= "\u05FF" for ch in text)

def guess_font_size_for_rect(page, rect, default=11.0):
    """
    מאתר span שמצטלב עם ה-rect ולוקח ממנו את גודל הפונט המקורי.
    אם לא נמצא — חוזר לברירת מחדל לפי גובה המלבן.
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
    # fallback: מגובה המלבן
    return max(FS_MIN, min(FS_MAX, (rect.y1 - rect.y0) * 0.90))

def right_anchor_box(rect, text, fs, slack=SLACK, fontname="helv"):
    """
    מחשב תיבה חדשה המעוגנת לימין (x1 קבוע), מורחבת שמאלה לפי רוחב הטקסט + slack.
    """
    w_new = pymupdf.get_text_length(text, fontname=fontname, fontsize=fs)
    return pymupdf.Rect(rect.x1 - w_new - slack, rect.y0, rect.x1, rect.y1)

def ensure_clean_background(page, rect, extra_left=0.0, pad=PAD):
    """
    רדקציה (ללא מסגרת) על אזור הטקסט הישן, אפשר להרחיב שמאלה למניעת שאריות/חיתוך.
    """
    rr = pymupdf.Rect(rect.x0 - pad - extra_left, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
    page.add_redact_annot(rr, fill=(1, 1, 1))


# ---------- עיבוד ----------
doc = pymupdf.open(src_path)

for page in doc:
    # נריץ את כל ההחלפות על העמוד הנוכחי
    for rep in REPLACEMENTS:
        OLD = str(rep["old"])
        NEW = str(rep.get("new", ""))

        rects = page.search_for(OLD)
        if not rects:
            continue

        # 1) איסוף גודל פונט לכל מופע + רדקציה נקייה (ללא מסגרת)
        matches: List[Tuple[pymupdf.Rect, float]] = []
        for r in rects:
            fs = guess_font_size_for_rect(page, r, default=max(FS_MIN, min(FS_MAX, (r.y1 - r.y0) * 0.95)))

            # כמה להרחיב שמאלה? אם NEW ארוך מ-OLD — נרחיב לפי ההפרש, אחרת רק SLACK
            w_old = pymupdf.get_text_length(OLD, fontname="helv", fontsize=fs)
            w_new = pymupdf.get_text_length(NEW, fontname="helv", fontsize=fs) if NEW else 0.0
            extra_left = max(0.0, w_new - w_old) + SLACK

            ensure_clean_background(page, r, extra_left=extra_left, pad=PAD)
            matches.append((r, fs))

        # החלת כל הרדקציות של ההחלפה הזו בבת אחת
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

        # 2) כתיבה מחדש (אם NEW לא ריק)
        if NEW:
            for r, fs in matches:
                # עבור טקסט עברי נגדיר RTL ויישור לימין; עבור מספרים/אנגלית — גם לימין.
                rtl = is_hebrew(NEW)
                # תיבת מלל מעוגנת לימין ומורחבת שמאלה
                box = right_anchor_box(r, NEW, fs, slack=SLACK, fontname="helv")

                # HTML עם CSS — ללא מסגרת, מתאים עצמו לתיבה
                html = f"""
                <div style="
                    {'direction: rtl;' if rtl else 'direction: ltr;'}
                    text-align: right;
                    font-size: {fs:.2f}pt;
                    line-height: 1;
                    white-space: nowrap;
                    margin: 0; padding: 0;
                    /* חשוב: משפחת פונט כללית, אם יש פונט עברי ספציפי – החלף כאן */
                    font-family: {'Arial, Helvetica, sans-serif' if rtl else 'Helvetica, Arial, sans-serif'};
                ">{NEW}</div>
                """
                page.insert_htmlbox(box, html)

# שמירה
doc.save(out_path)
doc.close()
print("Saved:", out_path)
