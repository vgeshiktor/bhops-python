# Annotate the provided PDF by drawing red rectangles around the "שכר נטו" (net salary) 
# and the "לתשלום" (amount to be paid) lines so the payment amount is clearly highlighted.

import pymupdf  # PyMuPDF

input_path  = "/Users/vadimgeshiktor/repos/github.com/vgeshiktor/python-projects/bhops/workers/tamara.alexandrov/salary/tamara-alexandrov-320721582-7-2025.pdf"
output_path = "/Users/vadimgeshiktor/repos/github.com/vgeshiktor/python-projects/bhops/workers/tamara.alexandrov/salary/tamara-alexandrov-320721582-7-2025-marked.pdf"

doc = pymupdf.open(input_path)

for page in doc:
    # Highlight the "שכר נטו" line (net salary)
    net_labels = page.search_for("שכר נטו")
    for rect in net_labels:
        # Expand rectangle to the left to include the number, and a bit around for visibility
        expanded = pymupdf.Rect(rect.x0 - 140, rect.y0 - 6, rect.x1 + 6, rect.y1 + 6)
        page.draw_rect(expanded, color=(1, 0, 0), width=2)
    
    # Highlight the bottom "לתשלום" line (to be paid)
    to_pay_labels = page.search_for("לתשלום")
    for rect in to_pay_labels:
        expanded = pymupdf.Rect(rect.x0 - 140, rect.y0 - 6, rect.x1 + 6, rect.y1 + 6)
        page.draw_rect(expanded, color=(1, 0, 0), width=2)

doc.save(output_path)

