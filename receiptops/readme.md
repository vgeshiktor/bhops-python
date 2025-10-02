```bash
python3 bulk_pdf_replace.py \
  --src-dir "/path/to/source/folder" \
  --config "/path/to/replacements.json" \
  --out-dir "/path/to/output/folder" \
  --recursive \
  --suffix "_edited"
```

```bash
python ~/repos/github.com/vgeshiktor/python-projects/bhops/receiptops/bulk_pdf_replace.py \
    --src-dir "~/Downloads/trans.07.2025" \
    --config "~/repos/github.com/vgeshiktor/python-projects/bhops/receiptops/replacements.json" \
    --out-dir "~/Downloads/trans.07.2025/edited" \
    --recursive \
    --suffix "_edited"
```

```bash
find "/path/to/folder" -type f -iname '*.pdf' -print0 \
| xargs -0 -n1 -I{} lp -d "Canon_TS3300_series" \
  -o sides=one-sided \
  -o media=iso_a4_210x297mm \
  -o ColorModel=Gray "{}" \
  -o fit-to-page
```