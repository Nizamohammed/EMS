# captcha_solver — local autonomous CAPTCHA solver (TrOCR, trained on the Mac)

Solves the ECI EROLL captcha (6 chars, `[a-z0-9]` case-insensitive, cursive font
+ strikethrough line) so the download robot runs unattended
(`scraper/bin/download.js --solver trocr`). It's a **fine-tuned TrOCR** — a
pretrained text-recognition transformer — which adapts to this font from ~100
labels where a from-scratch CNN couldn't. Trains in minutes on the M3 GPU
(PyTorch MPS); no AWS.

Result so far: fine-tuned on **~100 labels → 81% char / 35% exact-match** on
held-out real captchas. 35% per-attempt is plenty: the download loop retries
against the Success-oracle, so effective per-AC success ≈ `1 − 0.65^N`.

## Pipeline
1. **Harvest** (Node): `node bin/harvest-captchas.js --n 400` → `data/captchas/raw/`.
2. **Label a seed** (one-time, cheap): `.venv/bin/python -m captcha_solver.montage`
   stitches captchas into grids; transcribe each grid → `captcha_solver/labels.csv`
   (`file,label`).
3. **Fine-tune**: `.venv/bin/python -m captcha_solver.train_trocr --epochs 30`
   → `data/captcha_model/trocr/`.
4. **Serve + deploy**: `.venv/bin/python -m captcha_solver.trocr_serve` (HTTP :8077),
   then `node bin/download.js --solver trocr`.
5. **Self-train (free, autonomous)**: every captcha the robot solves correctly
   during real downloads is saved as a verified `(image → answer)` pair in
   `data/captchas/verified/labels.csv`. Periodically re-run step 3 with those folded
   in → accuracy climbs with zero hand-labeling.

## Why TrOCR (not a from-scratch CNN or a big VLM)
A from-scratch CNN must learn to *see* from scratch — ~100 labels gave chance-level
generalization. A general chat-VLM is weak on deliberately-distorted captcha text.
TrOCR is pretrained to *read text*, so it transfers from a handful of examples.

Files: `train_trocr.py` (fine-tune), `trocr_serve.py` (HTTP inference),
`montage.py` (labeling), `labels.csv` (seed labels). Model + data live under
`data/` (gitignored).
