# EMS — Election Management System (India) — Project Context & Handoff

> Purpose of this file: let a brand-new chat/agent resume **exactly** where we left off. Read it fully before acting. It captures the goal, everything we reverse-engineered about the data source, the **confirmed working download recipe**, the document structure, the data-model thinking, the plan, open decisions, and gotchas.

---

## 0. TL;DR — where we are right now
- Building an **Election Management System (EMS)** for India. First subsystem = a **scraper/downloader** for electoral rolls, then OCR/parsing, then a database, then backend software.
- We **proved end-to-end** that we can download a real electoral-roll PDF from the official ECI portal (headless browser, captcha solved by the agent's vision, **no login/OTP needed**). A valid 12-page roll was downloaded and inspected.
- We discovered the roll PDFs have **NO text layer → OCR is mandatory**.
- **Historical decision:** we chose **Option 2** = structure → data model → OCR first; the full download pipeline came after. **Both are now built** (OCR §17, download robot §19).
- **✅ DONE: full structural survey + data model.** The PDF structure is fully mapped (see `roll_pdf_structure_verified` memory) and the **core PostgreSQL schema is written and validated** at `db/schema.sql` — syntax-checked, adversarially reviewed (multi-agent), and **executed on real Postgres 16** (loads clean; the QC reconciliation view + all constraints behave correctly on real extracted data).
- **✅ DONE: OCR/extraction pipeline built** at `ocr/` (see §17). PDF → rasterize → **pluggable VLM extractor** → assemble (dedupe + status) → reconcile vs the summary oracle → **DB loader** into `db/schema.sql`. The full PDF→OCR→DB→reconcile vertical was proven on the Lakshadweep sample (`reconciles=true`, 226 electors). Validated on a 2nd roll (Telangana S29-61, integrated SSR FinalRoll).
- **✅ DONE: autonomous captcha solver + download robot** (§19) — a fine-tuned **TrOCR** (not the agent) solves captchas; the robot downloads, verifies, resumes, and **self-trains** from each live success. Proven live on Lakshadweep. All committed + **pushed to `origin/dev`** (user reviews → merges to `main`).
- **⚠️ OCR APPROACH FLIPPED (2026-06-30/07-01) — recognizer OCR beats the VLM; the workhorse is now RapidOCR, NOT Qwen. This supersedes the old "Qwen3-VL-8B settled" decision.** The 8B claim was based on **format**-valid EPICs (`[A-Z]{3}[0-9]{7}`), not **correct** digits. Building real ground truth (3 independent reads + manual zoom, all 231 EPICs) showed Qwen3-VL-8B actually reads **EPIC 97.8% native / 99.6% upscaled** — it makes **silent digit-swaps that pass every format & reconciliation check** (unacceptable for an identity key). A bench-off of 8 engines found **RapidOCR (PaddleOCR PP-OCRv5), docTR, and TrOCR-base-printed all read EPIC 100%** — **free, on CPU, ~20–2000× faster** than the VLM (olmOCR-2 86.6%, EasyOCR 71%, Tesseract 23%). **Generalization: 321/321 EPICs correct across 3 rolls** (RapidOCR + docTR, all hand-verified). **New architecture:** workhorse = a modern CRNN recognizer (**RapidOCR** = PaddleOCR PP-OCRv5 on ONNX, CPU) + **docTR as an independent second engine** (different architecture → uncorrelated errors → agree=accept, disagree=flag = free confidence signal). VLMs (Qwen/olmOCR) are demoted to fallback for hard/degraded pages only. **Full bench-off report: `scraper/data/ocr_exploratory_report.md`.** (The two `assemble.py` fixes below + the vLLM `--gpu-memory-utilization 0.96` gotcha in §18 still stand *for the VLM path only*.)
- **Scalability (2026-07-01):** RapidOCR is **CPU-bound, not GPU-bound** — the models are tiny, so the CPU-side prep starves the GPU (L4 at ~57% util, plateaus ~600 rolls/hr on the 4-vCPU g6.xlarge; more workers add GPU RAM, not speed). **Route = CPU-parallel, no GPU** → national corpus (~1M rolls) ≈ **$hundreds–low-thousands** of CPU compute vs ~$160k for the VLM path. (64-vCPU test was quota-capped: ap-south-1 Standard quota=32 vCPU, G/VT quota=8. A clean 32-vCPU number wasn't captured — stale-process thrashing; re-run cleanly.)
- **⚠️ ECI PORTAL BROKEN (2026-07-02) — download currently blocked.** `voters.eci.gov.in/download-eroll` throws `ChunkLoadError` (the app's `asset-manifest.json` requires `5451.4e67c904.chunk.js` but the server returns `index.html` — `text/html` — for it → browser can't run HTML as JS → crash → login redirect loop). **Confirmed ECI-side broken deployment** (collateral of the **SIR 2026** roll rollout + **ECINET** migration), affects everyone, unfixable from our end (login/signup/cache-clear don't help). **Backend API is still alive** (`getCaptcha/EROLL` returns the encrypted blob; `gateway-vpd` delivery host answers) — only the frontend route broke. Likely fixed in hours–days, but expect recurring instability while SIR rolls out state-by-state. Our Playwright scraper (§19) hits the same wall. Fallbacks: (a) API-bypass (backend works but needs the AES crypto + captcha replicated outside the browser), (b) **state CEO sites** (e.g. Telangana CEO — Telugu script, easy 4-digit captcha in a separate window, independent of the broken ECI portal).
- **Two `assemble.py` reconciliation fixes (2026-06-26, applied, validated through real code):** (1) **legend-echo reason code** — the model copies the instruction's legend `"E/S/R/M/Q"` into `deletion_reason_code`; `_infer_status` treated any truthy reason as a deletion (→15 false deletions). Fix: only accept a single char in `{E,S,R,M,Q}`. (2) **bottom-crop section misclassification** — bottom half-page crops have no visible section header, so the model guesses "List of Additions" for main-roll strips (→75 false additions). Fix: supplements are a TRAILING serial block, so reclassify any additions/deletions card whose serial sits **inside the main-roll serial span** back to main_roll (real additions here = serials 226–231 > main-roll max 225). ⚠️ Fix #2 assumes supplements **continue** numbering; a supplement that **resets** serials would collide in the global serial dedup — **re-validate on the 2nd roll (Telangana S29-61, §0/§17) + a multi-/reset-supplement roll** before the batch run.
- **⚠️ CURRENT STATE (2026-07-07) → see §23: the FULL PIPELINE IS CONNECTED.** scraper → `ocr/` (RapidOCR main extractor; DELETED cards → Donut+Pix2Struct→Qwen combine) → assemble/reconcile → `db/schema.sql`. Engines are **LOCKED — stop evaluating**: English = **RapidOCR** (`ocr/extractors/rapidocr.py`), Indic = **Surya** (`ocr/extractors/surya.py`, ported+wired, not roll-verified), stamped **DELETED** cards = the **Donut+Pix2Struct+Qwen2.5-VL-3B combine** (`scraper/deleted_card_solver/combine.py`; the earlier TrOCR specialist FAILED — §22 — Donut/combine replaced it, §22 tail). The old "Indic frontier / ECI blocks us / TrOCR specialist" threads are all RESOLVED. **NEXT = TEST the connected pipeline** on the sample rolls, then fresh downloads (it's built + compiles but NOT yet run-verified — user stopped before testing; §23 has the exact next steps + run command). Deleted-card recovery needs either `pip install peft` in `scraper/.venv` (local) or a served Qwen on the GPU box (http).
- **Deleted / cleaned this session:** the superseded general-VLM extractors `ocr/extractors/{mock,ollama_qwen,vllm_qwen}.py` (registry is now only `rapidocr`+`surya`), `.DS_Store`, `__pycache__`. Still to decide (user): remove the failed `train_deleted_trocr.py` + `_bench/` exploratory scripts? And write `ARCHITECTURE.md` (full system map).

---

## 1. The user (how to work with them)
- **Software engineer.** Comfortable with HTTP, JSON, REST, status codes, queues, retries, DB design, general architecture.
- **Gap is web/networking specifics** (TLS/JA3 fingerprinting, anti-bot/403, captcha/session flows, headers, SPA-vs-API). Explain *those* clearly; do **not** re-explain general SWE concepts and **do not use kiddie analogies** — it reads as condescending. Connect the "why," stay concise.
- Pragmatic: prefers reusing/adapting existing work over building from scratch. Often works in parallel with the agent and gives real-time corrections — incorporate them.

---

## 2. Repo / filesystem layout
- `/Users/nzm/EMS/` — the project repo (this dir). User is initializing git here.
  - `CLAUDE.md` — this file.
  - `archive/electoral_rolls/` — the cloned **in-rolls/electoral_rolls** repo (2018 reference: state-by-state PDF scrapers). It has its own nested `.git` → **add `archive/` to `.gitignore`** to avoid embedded-repo issues. Still useful: some states self-host rolls on their own CEO sites (old direct-PDF approach may apply there), and the sibling parse repos are relevant for OCR later.
- `/Users/nzm/eci_spike/` — **scratch dir OUTSIDE the repo** holding the Playwright proof-of-concept (Node). Keep PoC + sample PDFs here, not in the repo.
  - `step_final.js` — **the working download script** (Final Roll, blue button, captcha file-handoff). Other `step*.js`/`step_dl*.js` are earlier iterations.
  - `downloads/` — downloaded sample roll(s).
  - `node_modules/`, `package.json` — Playwright installed here. Chromium cached at `~/Library/Caches/ms-playwright`.
- Sample roll also copied to `~/Desktop/2026-EROLLGEN-U06-1-SIR-FinalRoll-Revision1-ENG-1-WI.pdf`.
- Persistent agent memory: `/Users/nzm/.claude/projects/-Users-nzm-EMS/memory/` (auto-loaded). `project_ems1.md` mirrors much of this file in more detail.

---

## 3. Data-source landscape
- **ECI centralized** since ~2023. The 2018 world of 34 separate state CEO sites is mostly replaced by ONE portal: **https://voters.eci.gov.in** (React SPA) backed by ONE API: **https://gateway-voters.eci.gov.in/api/v1/**.
- Some state CEO sites still self-host rolls (e.g. ceoassam, ceoodisha, ceouttarpradesh/rollpdf.aspx, ceogoa) — a possible simpler/no-captcha path for those states (hybrid strategy later).
- **Parsed historical data exists** on Harvard Dataverse (doi:10.7910/DVN/MUEGDT) from the in-rolls project, gated behind IRB — option for historical seed data if research terms are acceptable.

---

## 4. ⚠️ GEO RESTRICTION (critical operational constraint)
- The actual roll **download is geo-fenced to Indian IPs.** Confirmed by the user: download fails on a non-Indian IP, works via an **Indian VPN**.
- The agent's `Bash`/Playwright run **on the user's machine**, so they egress through whatever the machine's network/VPN is. **Before any download attempt, verify egress geo** (e.g. `curl -s https://ipinfo.io/json`). The open catalog API answers from anywhere, but the file delivery needs an Indian IP.
- Geo restriction is by design (data intended for domestic use). Routing around it via VPN/proxy is a **legal/ToS posture the USER owns** — flag it, don't decide it. For automation at scale, plan Indian egress (AWS Mumbai `ap-south-1`, Indian VPS, or Indian proxies).

---

## 5. ✅ CONFIRMED DOWNLOAD RECIPE (reproducible)
Target page: `https://voters.eci.gov.in/download-eroll`. The working script is `/Users/nzm/eci_spike/step_final.js`.

**Prereqs:** Indian IP (see §4); Node + Playwright (installed in `/Users/nzm/eci_spike`).

**Flow (what the script does):**
1. Headless Chromium loads the page — Chromium runs ECI's JS, so all AES crypto + captcha rendering are handled for free.
2. Fill the cascade. All are native `<select>` **except AC** which is a **react-select** custom component:
   - State (native), Year of Revision (native), **Roll Type = "SIR FinalRoll - YYYY"** (native) — use the **Final** roll; a Draft may not exist for a state,
   - District (native),
   - **AC**: focus `input[id^="react-select-"]`, press `ArrowDown`, `Enter` to pick the first option,
   - Language (native) — pick the wanted language (English-first; some states native-only).
3. The **part list renders with checkboxes** (one per polling-station part). Check the part(s) you want.
4. **Captcha**: `<img src="data:image/jpg;base64,...">`, ~6-char alphanumeric. Solve it with the agent's **vision** (screenshot the captcha element → the agent reads it). Fresh page load = fresh captcha; on retry **reload the page** (the refresh icon is a stubborn `<span>` that didn't respond to synthetic clicks).
5. Fill the captcha input. **Gotcha:** the parts "Search" box is also a visible text input and precedes the captcha box in the DOM — target the captcha input by **excluding** `placeholder=Search` and `id^=react-select`.
6. Click the **BLUE "Download Selected PDFs"** button. **DO NOT** click the green **"Download SIR Draft Roll for full AC"** — that one **forces a login/OTP redirect**. The blue per-part path needs **only a captcha, no login**.
7. Network: `get-publish-eroll-type` → `get-ac-languages` → `get-publish-part-list` → on submit `POST generate-published-pdfs` → returns `{"status":"Success","payload":["<uuid>"],"file":null}` (`Success` = captcha accepted). The browser then fetches the actual PDF and a normal **download event** fires → save it.

**Captcha file-handoff protocol used by the script** (so the agent can solve mid-run): script fills the form, screenshots the captcha to `captcha.png`, prints `CAPTCHA_READY`, then **polls for `answer.txt`**. The agent: waits for `CAPTCHA_READY`, reads `captcha.png` via vision, writes the solution to `answer.txt`; the script reads it, submits, saves the PDF, writes outcome to `resultF.txt`. Run scripts with `run_in_background` + a `perl -e 'select(undef,undef,undef,0.5)'` poll loop (the `sleep` command is blocked in foreground).

**Result of our run:** `2026-EROLLGEN-U06-1-SIR-FinalRoll-Revision1-ENG-1-WI.pdf`, 2,985,558 bytes, `%PDF-1.7`, **12 pages**, valid. Filename pattern: `{year}-EROLLGEN-{stateCd}-{acNo}-SIR-FinalRoll-Revision{n}-{LANG}-{partNo}-WI.pdf`.

---

## 6. ECI API reference (reverse-engineered)
Base: `https://gateway-voters.eci.gov.in/api/v1/`. Required headers on calls: `User-Agent` (browser-like), `Referer: https://voters.eci.gov.in/`, `applicationName: VSP`, `channelidobo: VSP`, `PLATFORM-TYPE: ECIWEB`, `currentRole`.

**OPEN (HTTP 200, no auth, no captcha) — the completeness backbone:**
- `GET /common/states` → 36 states/UTs, each has `stateCd` (S01..S29, U01..U08).
- `GET /common/districts/{stateCd}` → districts, each has `districtCd` (e.g. S0429).
- `GET /common/constituencies?stateCode={stateCd}` → ACs, each has `asmblyNo`, `acId`, category.

**GATED:**
- `common/parts/{stateCd}`, `common/part/get/bystatecd/districtcd/acNumber` → **401** (these are the registration/search part lookups — different gate).
- `printing-publish/get-publish-eroll-type`, `get-ac-languages`, `get-publish-part-list`, `generate-published-pdfs` → used by the download flow; need the captcha + the app's crypto (handled by the browser). NOT behind OTP for the selected-PDFs path.
- `captcha-service/getCaptcha/{context}` (e.g. `/EROLL`) → 200, returns `{data:"<AES blob>"}` decrypted client-side; `verifyCaptcha`, `generateVoiceCaptcha` (audio captcha exists — accessibility path, often easier to auto-solve).
- `authn-voter/otp-flow-send|otp-flow-verify|refresh` → OTP login (only needed for the "full AC" button and for voter-registration/personal-data flows, NOT for roll download via selected-PDFs).

**App-level crypto:** request/response payloads are AES-encrypted client-side (Web Crypto `subtle` + pkcs7). The AES **key+IV** are sent as **reversed-name headers** `accept_yek` (=key) / `accept_rotcev` (=vector). Don't reimplement — let the browser's JS do it (that's why we use Playwright). A pure-HTTP scraper would have to port this crypto (brittle, rotates).

---

## 7. Document anatomy (what's inside a roll PDF) — from the 12-page English sample
1. **p1 Cover:** AC no/name + category, Parliamentary Constituency, Part no; revision metadata (year, qualifying date, revision type, **date of publication**); area details (block, village/town, post office, police station, tehsil, district, pincode); polling station (no, name, address, type Gen/M/F, # aux stations); headline elector counts.
2. **p2 Polling-station location:** Nazri Naksha sketch map, Google satellite view, building/front photos, CAD view, key map. (Imagery — likely skip in v1 extraction.)
3. **p3 … pN-1 Voter entries:** 30 per page, 3-col × 10-row cards. Each card = **serial no, EPIC no** (e.g. PVN/PUVxxxxxxx), **Name**, **Relation** (Father's/Husband's/Mother's) + name, **House Number**, **Age**, **Gender**, **Photo** box. Entries are grouped into SECTIONS: main roll + **"List of Additions N (date-range)"** + **"List of Deletions"** (deletions carry a reason code).
4. **Last page — Summary of Electors (this is a CHECKSUM):** table by roll type — I Mother Roll, II Additions(Supplement), III Deletions, IV gender-modification difference, **Net = I+II−III+IV**; counts split Male/Female/ThirdGender/Total; plus B) number of modifications. **Deletion reason codes:** `E`=Expired, `S`=Shifted/Change of Residence, `R`=Duplicate, `M`=Missing, `Q`=Disqualified.

---

## 8. OCR reality
- `pdftotext` returns **~0 chars on every page** (even the cover that renders crisp text). Producer = iText 8. Text is drawn as images/non-mapped glyphs → **OCR is mandatory.**
- English rolls → expect high OCR accuracy. Native-language rolls (Hindi/Tamil/Bengali/…) → harder; plan accordingly.
- Use `pdftoppm -png -r 150 file out` to rasterize pages for OCR. OCR options to evaluate: vision LLM (Claude/GPT-V), Google Vision API, Tesseract (the 2018 scrapers found Tesseract unreliable for captchas; quality for roll text TBD).
- The **summary page is a free QC oracle**: reconcile OCR'd record counts (total, M/F/TG, #additions, #deletions) against the printed aggregates.

---

## 9. Data model — ✅ DESIGNED & VALIDATED (see `db/schema.sql`)
**Status:** the core schema is built, reviewed, and verified on real Postgres 16. Engine = **PostgreSQL 15+**. The hard decisions below are now **resolved**:
- **Temporal model = snapshot-of-record + derived events.** Each published PDF is stored faithfully (`roll` + `elector`, history preserved by keeping every roll's rows); identity/analytics are *derived* on top. We ingest snapshots, not events, so this is lossless and the page-12 summary validates it.
- **Identity = `person` (EPIC-keyed, Tier 1 built now) + `placement`** (one per elector appearance = the cross-roll timeline; `match_method`/`match_confidence` are the Tier-2 fuzzy-matching seam, deferred until ≥2 overlapping rolls).
- **`supplement` table** holds the "List of Additions N (from–to)" date-range + per-supplement revision identity (one roll can carry several supplements).
- **Storage split:** DB = structured rows only; **PDFs live in an object store**, hash-addressed (`roll.source_pdf_sha256`) + URI-referenced. Page images are regenerable (not persisted); OCR output is a compressed cache. PDFs are the only irreplaceable artifact → dedup + cold-tier history.
- **Geo/map = deferred hook:** lat/lon columns exist now; **PostGIS polygon `geom`** (temporal, per delimitation era) bolts onto the stable geography keys later without reshaping the core.
- **Enums via `CHECK`** (easy to evolve); only `deletion_reason` (E/S/R/M/Q legend) is a lookup table.
- QC: `v_roll_reconciliation` view reconciles extracted electors against the printed summary/cover (`reconciles` is total, never NULL).

**✅ IDENTITY + LIFECYCLE + TEMPORAL hardened (2026-07-17) — validated on real Postgres 16 with 2 loaded parts.** Addressed the three real-world lifecycle scenarios (person moves AC/part; person dies; AC/part/state structural change):
- **Move (no dup / no loss):** `person_epic(person_id, epic_no unique, first_roll_id)` added; `person.epic_no` now NULLABLE (was NOT NULL — blocked no-EPIC/merged persons). Loader creates one person per NEW epic, registers every epic in `person_epic`, and links `placement` THROUGH `person_epic` (so a re-issued or Tier-2-merged EPIC still resolves to one person). **Proven:** EPIC `PUV1073626` appears in part 1 (deleted/**S**=shifted) AND part 2 (added, serial 1079) → **1 person row, 2 placements** = full timeline, no duplicate, nothing dropped. `person_epic` is also the seam for EPIC-change (a person can hold several EPICs) — Tier-2 fuzzy merges add rows here.
- **Death / exit (derived, never deleted):** new **`v_person_current`** view = each person's status from their MOST RECENT appearance, mapping the deletion reason → `active`/`deceased`(E)/`shifted_out`(S)/`removed_duplicate`(R)/`disqualified`(Q)/`removed_missing`(M). Self-corrects as rolls land (the mover reads `active` because part 2's re-add is their latest). **Proven:** shifted person derives `shifted_out`; flip reason→E and the view derives `deceased` — all without touching stored data.
- **Structural change (hedge now, full model specced):** `delimitation_era` table + `roll.era_id` (loader stamps every roll `'2008 Delimitation'` — the 2026 SIR is a revision, not a re-delimitation). The FULL era-scoped-geography + successor/predecessor **lineage** model (renumber/split/merge/bifurcation) is written up in **`db/TEMPORAL_GEOGRAPHY.md`** (deferred: not exercised until we cross a boundary; the era stamp means the future migration is purely additive).
- **Loader robustness fixes (same session, needed for multi-part batch loads):** geography inserts (district/PC/AC/part/section) are now idempotent **upserts** (`on conflict … do update … returning` / `do nothing`) — a state batch loads thousands of parts under one district/AC; plain inserts collided. And the PC is only created when a valid `pc_no` is read → `ac.pc_id` left NULL instead of fabricating `pc_no=0` (which fails the `>=1` check and crashed the roll).

**✅ COVER PARSER REWRITTEN (2026-07-17) — `_parse_cover` in `rapidocr.py`.** Was a stub (only `district_name`); now a full DPI-robust, anchor-based parser: title→state/year, AC/PC lines→no/name/reservation, part no, revision metadata (qualifying/publication dates, type, roll identification), area details (town/PO/PS/block/tehsil/taluk/district/pincode), polling-station no/name/address/type/aux, sections list, and the **'Number of Electors' table** (start/end serial + net M/F/TG/Total). Helpers: `_cover_find` (exact-label-match first, then contains — so 'block' hits the 'Block' label not the '1-Block1' section), `_value_right` / `_value_below` (skip same-row neighbours; tolerances scale with token height). **Validated: Lakshadweep U06 parts 1&2 + Telangana S29-AC57 (diff state/year 2025) all parse clean.** DB effect (verified on PG16): PC now created+linked, `year_of_revision` real (was 0), and **`cover_count` populated → the cover is now an ACTIVE second checksum** (`v_roll_reconciliation.cover_matches_summary=t`, `reconciles=t`). Minor residual: RapidOCR collapses some spaces (`'Block1'`, `'GHMCHYDERABAD'`) — cosmetic; `type_of_revision` drops a trailing year token. `date_of_updation` is roll-footer, not on the cover (stays NULL).

Original entity thinking (preserved for context — now realized in `db/schema.sql`):
- **Geography:** State → District → Assembly Constituency → Part/Polling-Station (+ cross-link to Parliamentary Constituency). Polling station has rich attributes.
- **Roll/Revision:** versioned artifact keyed by (part × revision × language); attrs: year, roll_type (SIR Final/Draft/Supplement-N), revision_no, publish_date, language, source_pdf.
- **Voter/Elector:** serial_no, epic_no, name, relation_type, relation_name, house_no, age, gender, photo_present.
- **Summary/Aggregate:** per-roll counts (used as checksum).

**Hard decisions (these are where the "complex relations" the user wants live):**
1. **Temporal/revision model** — the roll encodes additions/deletions(with reasons)/modifications, so a voter has a **status within a revision**, not a flat presence. Choose: snapshot-per-revision vs event-sourced (adds/deletes as events) vs SCD. This decision enables churn/migration/turnout-history analytics.
2. **EPIC is identity but NOT a stable PK** — a "Shifted" voter is a deletion in one part + an addition in another → person identity spans parts/revisions. Model EPIC → placements over time.
3. **Variations to model now:** language (store raw native Unicode + optional transliteration; English-first), latest-roll-per-state (year/type/publish_date columns; latest = max publish_date), deletion-reason enum (E/S/R/M/Q), photo-present flag.

---

## 10. Plan & sequencing
**Chosen path = Option 2, in this order:** **A (understand structure)** → **design data model / ER + field dictionary + checksum spec** → **B (OCR to populate it)**. OCR is the means, not the start — you can't extract blind. Prove the full vertical (download → OCR → fields → DB) on ONE file before scaling.

**Then Option 1 (the full download pipeline)** comes last and better-informed. Its shape (already designed conceptually):
- **Data plane:** crawl open catalog → build a **manifest** (state→district→AC→part) = the completeness denominator.
- **Control plane:** headless browser session per download (handles crypto+captcha); **agent solves captcha** (vision; try audio captcha too); no OTP for selected-PDFs path.
- **System backbone:** manifest + job queue + per-leaf state machine (pending→fetched→verified|failed) + **reconciliation** against the part-list count and the summary-page checksum.
- **Self-heal agent:** when a request starts returning 400/401/404 (ECI rotated headers/paths/crypto), re-derive the contract from a fresh session/JS bundle.
- Handles disparities: pick latest year per state; prefer English language, fall back to native.

---

## 11. Open decisions / risks (raise with user before scaling)
- **Legal/ToS posture** for automating an India-geo-fenced government portal at scale (VPN/proxy, volume). User's call; shapes architecture (single vs proxy pool, rate limits, retention).
- **OCR cost/quality at scale** — per-page OCR × millions of pages is the biggest cost/risk; validate on the sample first.
- **PII/storage/retention** — rolls are sensitive personal data; storage, access control, retention must be planned.

---

## 12. Disparities to keep in mind (user-flagged)
- **Latest roll year varies per state** (2026/2025/2024). Must probe roll-types per state and pick the newest; annotate which revision was captured.
- **Language availability varies** — some states English+native, some native-only. Enumerate languages per AC; prefer English, else native.

---

## 13. Corrections / things we learned were wrong (don't repeat these)
- ❌ Early belief: roll download needs OTP login + bound tokens + heavy auth. ✅ Reality: the **selected-PDFs path needs only a captcha, no login.** (OTP/atkn_bnd/Form6/Aadhaar are for voter *registration/search*, not roll download.)
- ❌ Belief: a static ZIP URL like `/eroll/2026/.../*.zip` works. ✅ It's **stale (404)**; the real download is the `generate-published-pdfs` flow → browser download.
- ❌ The captcha endpoint is `getCaptcha` (not `generateCaptcha`). The latter 400s.
- The **green "full AC" button forces login**; use the **blue "Download Selected PDFs"**.

---

## 14. Environment / tooling (this machine)
- macOS (darwin), shell zsh. Python **3.14** (too new for some wheels — Playwright Python avoided). Node **25** + npx. 
- Poppler installed: `pdftotext`, `pdfinfo`, `pdftoppm` at `/opt/homebrew/bin`. **No** `pypdf`/`pdfminer`/`mutool`/`qpdf` yet.
- macOS built-ins useful: `mdls -name kMDItemNumberOfPages file.pdf` (page count), `qlmanage -t` (page-1 thumbnail).
- Playwright (Node) installed in `/Users/nzm/eci_spike`; Chromium headless shell cached in `~/Library/Caches/ms-playwright`.

---

## 15. PII & safety rules
- A real roll = sensitive personal data (names, EPIC, addresses). **Never commit roll PDFs to git.** Keep samples in a **gitignored** `data/`/`samples/` dir or outside the repo (we use `~/eci_spike`). The repo-root copy was deliberately removed.
- Don't push/share roll data externally without the user's explicit say-so.

---

## 16. Immediate next step
> ⚠️ **OCR side SUPERSEDED (2026-07-01) — read §0 + §21.** The "OCR model = Qwen3-VL-8B" thread below is historical: a bench-off showed the VLM makes silent EPIC digit-swaps and **RapidOCR (free CPU) reads EPIC 100%** and is now the workhorse. Current OCR next-steps are in §0/§21 (Indic test — blocked by the ECI portal outage; + full per-field extraction). The scraper thread below is still valid but the ECI download is currently broken (§0).

**Two parallel threads:**
1. **OCR model — ✅ RESOLVED 2026-06-26 (was the gate on the OCR side, §17).** Qwen3-VL-8B on the L4 reads **EPIC 231/231 + all soft fields**; full vertical **reconciles=true** after two `assemble.py` fixes (see §0). The VLM path is confirmed; no specialized EPIC reader needed. **Remaining OCR work, in order:** (a) **re-validate the two assemble fixes on the 2nd roll (Telangana S29-61) + a reset-numbered-supplement roll** — fix #2 (trailing-serial section guard) is validated on Lakshadweep only; don't ship to the batch run until a multi-/reset-supplement roll passes. (b) **Throughput experiment** — serve the **FP8** 8B (frees ~8 GB → more KV/concurrency), re-measure seconds/roll (baseline: ~12 min/roll, ~4–6 concurrent, KV-bound on the L4). To re-run a full roll concurrently: the throwaway harness `scratchpad/concurrent_roll.py` (this session) or just `ocr.cli` (sequential). Smoke command (after the tunnel is up; the extractor defaults to `localhost:8000`, so `--host` is optional):
   `python3 -m ocr.cli scraper/data/rolls/U06/1/2026-EROLLGEN-U06-1-SIR-FinalRoll-Revision1-ENG-1-WI.pdf --extractor vllm --model qwen3vl --dpi 200 -v --out /tmp/r.json` (use `--dpi 200`, NOT 300 — a 300-dpi half-page is ~6100 vision tokens and overflows the 8192 ctx; 200 ≈ 2700 and reads EPIC fine).
2. **Scraper scale — gates the download side (§19).** Harden multi-AC selection (`selectAcByIndex` index-walk → type-to-filter) so the robot runs on 200–400-AC states, then run real states with `--solver trocr`.

Both feed the **batch-run architecture (§20)** — state-at-a-time, clear-per-AC, co-located on the AWS box.

---

## 17. OCR / extraction pipeline (BUILT — `ocr/`)
> ⚠️ **Extractor choice SUPERSEDED (2026-07-01) — see §21.** This `ocr/` package + its VLM extractors still exist and work, but the chosen workhorse is now **RapidOCR (CPU)**, not the Qwen VLM. A RapidOCR extractor should be added to `ocr/extractors/` as the default. Everything below about rasterize/assemble/reconcile/load still applies.

Stdlib-only Python package; no pip deps (uses `pdftoppm` + stdlib HTTP). Run: `python3 -m ocr.cli <roll.pdf> --extractor <name> [--dpi N --max-voter-pages N --sql-out roll.sql --out result.json -v]`.

**Flow:** `rasterize.py` (PDF → cover / voter half-page crops / summary PNGs; crop bands scale with `--dpi`) → **pluggable `Extractor`** (`ocr/extractors/`) → `assemble.py` (normalize, dedupe by serial, infer status active/deleted/added/modified, structural QA confidence) → `reconcile.py` (counts/gender vs the summary; mirrors `v_roll_reconciliation`) → `load.py` (`--sql-out` → psql-loadable SQL for `db/schema.sql`, incl. Tier-1 person/placement).

**Extractor is the swap point** — `extract(image, json_schema, instruction) -> json`. Registered: `qwen2.5vl` (local Ollama, Mac), `vllm` (remote vLLM OpenAI-compatible API — the GPU path; `--model Qwen/Qwen3-VL-8B-Instruct --host http://<gpu>:8000`), `mock` (no-model wiring test). New engine = one new file, zero pipeline change.

**Key learnings (see `ocr_vertical_proven` memory):**
- Vision extraction nails counts/gender/status (oracle-verified); field text (EPIC digits) needs the structural fidelity gate (`[A-Z]{3}[0-9]{7}`).
- **Field-overload:** a small (3B) model drops hard fields (EPIC, relation_name) when asked for ~10 fields × ~18 cards in one call; it reads them perfectly when the call is scoped. Fix = stronger model (8B does the full schema in one call) OR scoped multi-pass. → GPU + Qwen3-VL-8B chosen.
- Cross-state ENUM/label variance (reservation GENERAL vs GEN; roll_type FinalRoll vs SIR_FinalRoll; area labels) → loader normalizes; schema CHECKs kept tolerant.
- Mac M3/16GB: ~80–186s per half-page call with `qwen2.5vl:3b` → fine for dev/validation, too slow for the corpus.
- **Indic (deferred):** `language_code` (known from the download manifest) routes to a specialist later; no native-language roll obtained yet. Tier-B bake-off (Sarvam/Google DocAI/Bhashini/Indic-tuned open models) when one is available.

**✅ OCR model RESOLVED (2026-06-26) — Qwen3-VL-8B on the GPU.** The 3b reality check (Mac, dpi 200): soft fields read, **EPIC 0/23** (field-overload) → 3b deleted. The GPU test settled it: `qwen3-vl:8b` via vLLM (L4, dpi 200) reads **EPIC 231/231 + every soft field 231/231** in one call, and the full roll **reconciles=true** (226=oracle, M/F 132/94, +6/−5) after two `assemble.py` fixes. So the VLM path stands; **the specialized/hybrid EPIC-reader fallback is NOT needed.** What surfaced instead: (1) two assemble bugs — legend-echo `deletion_reason_code` and bottom-crop section misclassification — both fixed (see §0; the §2 fix is Lakshadweep-validated only, re-check on a reset-numbered-supplement roll); (2) throughput is the real §20 cost driver (~12 min/roll on one L4, KV-bound to ~4–6 concurrent) → FP8 experiment next. Connection to the scraper = the **filename**: `ocr/cli.py` parses state/ac/lang/part/roll-type from `…EROLLGEN-{state}-{ac}-…-{LANG}-{part}-WI.pdf`. Gotcha: run extraction at **`--dpi 200`** (300 overflows the 8192 ctx).

---

## 18. AWS GPU / serving — ✅ PROVISIONED (2026-06-24)
**Account 122445004152, region ap-south-1 (Mumbai).** GPU quota ("Running On-Demand G and VT instances", L-DB2E81BA) was 0; AWS denied the first request, **approved on appeal** → now **8 vCPUs**.

**Standing resources (do not recreate):**
- Instance: **`i-06267ed13fcb663b4`** — `g6.xlarge` (NVIDIA **L4 24GB**), AMI `ami-08e5eee927d4b1622` (Deep Learning Base GPU Ubuntu 22.04, drivers+CUDA+Docker+nvidia-runtime preinstalled), 150GB gp3 root. SSH user `ubuntu`. **Currently STOPPED** (model weights + vLLM container preserved on disk).
- Key pair `ems-gpu` → private key at `~/.ssh/ems-gpu.pem` (chmod 600).
- Security group **`sg-0680b4005b958b5c8`** — SSH(22) only; allows `91.148.246.0/24` (user's Mumbai VPN range) + a couple of stale /32s. ⚠️ User IP flaps with VPN; re-add current IP if SSH times out: `aws ec2 authorize-security-group-ingress --group-id sg-0680b4005b958b5c8 --protocol tcp --port 22 --cidr $(curl -s checkip.amazonaws.com)/32 --region ap-south-1`.

**Serving (✅ actually working as of 2026-06-26):** vLLM **v0.23.0** image `vllm/vllm-openai:latest` serves Qwen3-VL-8B (resolves `Qwen3VLForConditionalGeneration`). **Working command — note `0.96`, NOT `0.92`:**
`docker run -d --name vllm --gpus all -p 8000:8000 -v $HOME/.cache/huggingface:/root/.cache/huggingface vllm/vllm-openai:latest --model Qwen/Qwen3-VL-8B-Instruct --served-model-name qwen3vl --max-model-len 8192 --gpu-memory-utilization 0.96`. ⚠️ **`0.92` does NOT serve** — it boots, then dies with a KV-cache OOM (`ValueError: ... 1.12 GiB KV cache needed > 0.77 GiB available ... estimated max len 5568`); `0.96` leaves ~1.7 GiB KV which fits the 8192 ctx. Weights ~16GB load; **ready in ~250 s** (watch `docker logs vllm | grep "Application startup complete"`, then `/v1/models` lists `qwen3vl`). GPU then sits at ~20.3/23 GiB. Comparators (Chandra-2, DeepSeek-OCR-2) not yet served. **FP8 variant untested — the next throughput experiment (frees ~8 GB → more concurrency).**

**To RESUME:** `aws ec2 start-instances --instance-ids i-06267ed13fcb663b4 --region ap-south-1`; the **public IP changes on each start** (no Elastic IP) — re-fetch via `describe-instances`; ensure SG allows current IP; `ssh ... ubuntu@<ip> 'docker start vllm'` (container preserved); wait for `/v1/models` to list `qwen3vl`; then from Mac open a tunnel `ssh -i ~/.ssh/ems-gpu.pem -L 8000:localhost:8000 ubuntu@<ip>` and run `python3 -m ocr.cli <pdf> --extractor vllm --host http://localhost:8000 --model qwen3vl`.

**Cost discipline:** ~$1/hr while running; STOP when idle (`aws ec2 stop-instances ...`) — stopped = only EBS (~$/mo). ⚠️ Data residency: instance is in ap-south-1 (India) — keep real roll PII on this box, never the US hosted APIs (NVIDIA build.nvidia.com ToS bans PII; see gpu research). **Status 2026-06-26: full OCR vertical proven on the 8B (EPIC 231/231, reconciles=true); ~12 min/roll unoptimized. Next GPU session = FP8 throughput experiment + 2nd-roll fix validation (§16).** OCR runs from the Mac via SSH tunnel send roll images Mac(US)→GPU(India) over encrypted SSH — fine for dev; the §20 batch run co-locates scraper+OCR on this box so PII never leaves India.

---

## 19. Download pipeline (BUILT — `scraper/`) — Option 1, now in progress
Decision (2026-06-24): build the **real downloader** before the GPU OCR bake-off. The PoC (`eci_spike/step_final.js`) was a hardcoded one-shot with the agent solving the captcha — not a pipeline. New subsystem lives in-repo at **`scraper/`** (Node, **zero runtime deps**: `node:sqlite` + global `fetch`, Node ≥22; Playwright for the browser leg). Code is committed; `scraper/{node_modules,data}/` + `*.db` are gitignored (rolls = PII).

**Two planes:**
- **Data plane (manifest / completeness denominator)** — `scraper/bin/crawl-manifest.js` crawls the OPEN catalog (`/common/states`, `/common/districts/{cd}`, `/common/constituencies?stateCode={cd}` — 200 from any IP, no captcha). ✅ Ran clean: **36 states, 787 districts, 4,129 ACs, 0 errors** into `scraper/manifest.db`. Counts validate vs reality (UP 403, MH 288…); 0 null extractions. Keys mirror `db/schema.sql` geography (`state_cd`/`district_cd`/`ac_no`). Raw API JSON stored per row (resilient to shape drift); bonus fields captured for free: native-lang AC name (`asmblyNameL1`), `pcNo`. `download_job` state-machine table built, awaiting seed.
- **Control plane (download)** — needs the **browser** (runs ECI's AES crypto + renders captcha, §6) and an **Indian IP** (geo-fenced; user's VPN gives ap-south egress even though ipinfo shows NL). `scraper/lib/browser.js` distills the proven PoC cascade-fill.

**★ Captcha-scope finding (architecture-deciding, proven on Lakshadweep U06 twice):** **ONE captcha covers a whole multi-part AC selection.** Check N parts → solve 1 captcha → `POST printing-publish/generate-published-pdfs` returns `{status:"Success", payload:[<N UUIDs>]}` (Success **iff** captcha correct → a free correctness oracle). 10 parts → 1 captcha → 10 UUIDs → **10/10 PDFs delivered**. ⇒ captcha unit = **(AC × year × roll_type × language) ≈ low thousands nationally**, NOT per-part (~1M). Three orders of magnitude.

**Delivery mechanism (fully mapped):** per UUID, `GET https://gateway-vpd.eci.gov.in/api/v1/ext-printing-publish/get-published-file?fileId=<uuid>` → the PDF (browser download event). **No further captcha**, different host (`gateway-vpd`), ~8–9s each, parallelizable/retryable. ⚠️ The page's on-screen "Success/Error x/10" counter is **bogus** (ticked Error 9/10 while all 10 downloaded fine) — trust the `get-published-file` 200 + saved bytes, not the UI. Filename: `{year}-EROLLGEN-{stateCd}-{acNo}-{rollType}-Revision{n}-{LANG}-{partNo}-WI.pdf`.

**Cascade selects (Lakshadweep, discovered):** State → **Year** (2026/2025/2024) → **Roll** (`Supplement-2 2026` / `SIR FinalRoll - 2026` / `SIR DraftRoll - 2026`) → District → **AC** (react-select) → **Language** (ENGLISH/MALAYALAM). `stateCd`/`year` are AES-encrypted tokens in the publish endpoints (browser handles it).

**Captcha solver (BUILT, DEPLOYED, self-training) — `scraper/captcha_solver/`:** pluggable `getSolver('manual'|'trocr')`. The autonomous solver is a **fine-tuned TrOCR** (pretrained text-recognition transformer), trained on the Mac M3 GPU (PyTorch MPS, `scraper/.venv` Python 3.12). A from-scratch CNN was tried and **failed** (~100 labels → chance-level generalization; synthetic pretraining didn't transfer — the whole CNN/synthetic experiment was removed in cleanup). TrOCR fine-tuned on ~100 hand-labels (via the `montage.py` grid-labeler) → **81% char / 35% exact** on held-out real captchas; **proven live** (solved a real ECI captcha → downloaded + verified, zero agent). Deployable at 35% because retry + the Success oracle → effective per-AC success ≈ `1−0.65^N`. **Self-training loop:** every correct live solve is saved as a verified `(image→answer)` pair in `data/captchas/verified/labels.csv`; re-run `train_trocr` with those folded in → accuracy climbs, no hand-labeling. Serve: `python -m captcha_solver.trocr_serve` (:8077) ← `--solver trocr`. Files: `captcha_solver/{train_trocr,trocr_serve,montage}.py` + `labels.csv` (seed), `README.md`.

**Files:** `scraper/lib/{api,manifest,browser}.js`, `scraper/bin/{crawl-manifest,probe-captcha-scope,enumerate-parts}.js`.

**Robot loop / orchestrator (BUILT & PROVEN end-to-end on Lakshadweep):** decision (user) = **lazy per-AC enumeration**, not a separate up-front pass — enumerating an AC's parts and downloading them use the SAME page visit, so the robot reads the part list, seeds the parts `pending`, then downloads in one go (no double-visit, no staleness). Files: `scraper/lib/download.js` (`downloadAc`: cascade → `readParts`+seed pending → select not-yet-verified parts → solve 1 captcha (retry-on-reject by reloading) → wait per-UUID downloads → verify `%PDF`+`pdfinfo` page count + sha256 → `markJob` verified/failed), `scraper/lib/captcha.js` (pluggable `getSolver('manual'|'trocr')` — manual=file-handoff bootstrap, trocr=fine-tuned TrOCR over HTTP :8077; on each Success it saves a verified captcha label for self-training), `scraper/bin/download.js` (driver: discover AC coords → run robot per AC; English-first, one language per part). **Proven:** Lakshadweep U06 ENG → 10 parts, 1 captcha, **10/10 downloaded + verified** (distinct sha256, page counts 12–41), to `scraper/data/rolls/U06/1/`; **part-level resume works** (re-run → 0 downloads, 0 captchas, skips verified). Browser context is `deviceScaleFactor:3` so live captcha screenshots match the solver's hi-res training data. The `enumerate-parts.js` standalone seeder still exists (enumeration-only / `ac_availability` menu).

**Immediate next:** (a) **scale the download** — harden multi-AC/multi-district enumeration (`selectAcByIndex` index-walk is robust for single-AC states but needs type-to-filter for 200–400-AC states), then run real states with `--solver trocr` (English-first, one language per part). The solver self-improves during the run; periodically re-fine-tune on the accumulated `data/captchas/verified/labels.csv` to push past 35%. (b) test whether one captcha covers a *large* AC selection (300+ parts) or needs batching; (c) feed `scraper/data/rolls/` PDFs into the `ocr/` pipeline. (Downloaded so far: only Lakshadweep U06 AC1 SIR FinalRoll 2026, 10 parts, ENG.)

---

## 20. Batch-run architecture (decided 2026-06-25)
How the full pipeline runs at scale, once the OCR model (§16/§17) is settled.

**Scale reality:** ~1.04M polling stations ≈ **~1M roll PDFs ≈ ~5–10 TB**, ~weeks even parallelized — NOT a press-go job. Validate + measure on ONE state first.

**Batch state-by-state, but CLEAR LOCAL per-AC** (a big state like UP ≈1.2 TB ≫ the Mac's ~390 GB free). Per AC: download parts → OCR-extract → load DB → push verified PDFs to **OneDrive** (cold archive) → **delete local** → next AC. Local footprint stays ~GBs.

**Co-locate scraper + OCR on the AWS Mumbai box (§18):** scraper needs Indian egress, OCR needs the GPU, and it keeps PII in-region. Mac = dev/monitoring + the OneDrive hand-off. Ideally **pipeline** (download AC N+1 while OCR-ing AC N) so the GPU never idles → production may want **g6.2xlarge (8 vCPU)** for the extra CPU (Playwright + `pdftoppm` rasterization); size it after the smoke test gives seconds/roll.

**Storage:** user's **OneDrive 5 TB (~4.9 TB free)** = cold archive. May NOT hold the full corpus — batch 1 measures real per-state GB to project + decide (S3 fallback, paid). Compression is marginal (roll PDFs are already compressed internally).

**Dominant cost = OCR GPU compute** (~12M pages) — batch 1 measures GPU-hours/state to project the national bill. This is *the* reason to do one state first.

**Gaps to build before a batch run:** (a) the **orchestrator** chaining download→OCR→DB→archive→clear; (b) an **extraction-status** field on `download_job` (it tracks download only today); (c) the OneDrive push (e.g. rclone). The cross-pipeline contract already exists: the **filename** (`ocr/cli.py` parses identity from it) + the shared `db/schema.sql` geography keys. ⚠️ **Cost model changed (see §21): the extractor is now CPU RapidOCR, not the GPU VLM — "dominant cost = OCR GPU compute" above is obsolete; it's cheap CPU-parallel now.**

---

## 21. OCR bench-off + engine decision (2026-06-30 / 07-01) — SUPERSEDES the VLM "decision" in §16/§17
Full write-up + all tables: **`scraper/data/ocr_exploratory_report.md`**. Summary of the flip:

**The catch that overturned it:** the earlier "EPIC 231/231" only meant *format*-valid (`[A-Z]{3}[0-9]{7}`), not *digit*-correct. Real ground truth (3 independent reads/card via a reader workflow + manual zoom on conflicts) for all 231 EPICs revealed the VLM makes **silent digit-swaps** (e.g. `PUV0333583`→`PUV0335383`) that pass BOTH the format gate AND page-12 reconciliation — invisible, unacceptable for an identity key.

**Bench-off (EPIC vs verified truth, Lakshadweep part 1, 231 cards):**
| Engine | Type | EPIC | HW |
|---|---|---|---|
| **RapidOCR (PaddleOCR PP-OCRv5, ONNX)** | detect+CRNN | **100%** | CPU free |
| **docTR** | DBNet+CRNN | **100%** | CPU free |
| **TrOCR-base-printed** (isolated crops) | transformer recog | **100%** | CPU free |
| Qwen3-VL-8B upscaled / native | general VLM | 99.6% / 97.8% | GPU |
| olmOCR-2-FP8 | OCR-VLM | 86.6% (truncation; emitted ones 100%) | GPU |
| EasyOCR / Tesseract 5.5 | CRNN / LSTM | 71% / 23% | CPU |

**Generalization:** hand-read 90 more EPICs from 2 NEW rolls (Lakshadweep parts 2 & 5) → RapidOCR + docTR **90/90 = 100%** each → **321/321 total**. Ground-truth copies: `scraper/data/roll_U06_1_full_copy.{csv,json}` (part1, all fields, verified EPIC) + `scraper/data/_bench/gen_ground_truth.{csv,json}` (90 EPICs).

**Decision:** workhorse = **RapidOCR** (`pip install rapidocr-onnxruntime`, CPU) + **docTR** as an independent cross-check engine (different arch → uncorrelated errors → agree=accept, disagree=flag = free confidence). VLMs demoted to fallback for hard/degraded pages. TrOCR (fine-tunable on our one font) = optional belt-and-suspenders for EPIC. **Why recognizers win:** a CTC/transformer recognizer trained on printed text doesn't invent a "plausible" digit like an autoregressive VLM does. Engine *generation* > *class* (old Tesseract fails; modern PP-OCRv5/docTR ace it).

**Scalability:** RapidOCR is **CPU-bound** (tiny models starve the GPU — L4 ~57% util, ~600 rolls/hr on 4 vCPU). Route = **CPU-parallel many-core, no GPU** → national corpus ≈ **$hundreds–low-thousands** vs ~$160k for the VLM path. AWS quotas ap-south-1: Standard=32 vCPU, G/VT=8 vCPU (so a clean 64-vCPU run needs a quota bump; the 32-vCPU attempt was botched by stale-process thrashing — re-run cleanly).

**Still open — PER-FIELD:** only EPIC is precisely scored. name/relation/house/age/gender need (a) RapidOCR line-output → structured-per-card mapping via the fixed 3×10 template, and (b) verified per-field ground truth. Names looked ~93%+ but measured vs an imperfect baseline copy → NOT trustworthy yet.

**Indic — THE open frontier (untested):** RapidOCR/docTR proven on English only; CRNN OCR is weaker on complex scripts. Need one Indic roll to measure. Specialized-Indic model landscape: **PaddleOCR-Telugu** (local, PP-OCRv5 Telugu recognizer — the practical self-host option) and **Surya** (local, multilingual). The strong printed-Indic model from research (~98–99.5% Telugu char, arXiv 2205.06740) is **IIIT Hyderabad's IndicOCR**, mainly a **hosted app** (`ilocr.iiit.ac.in/indicocr` = third-party egress). **NOT** `iitb-research-code/indic-trocr` (handwritten / no-Telugu / recognition-only — wrong tool; the earlier "IIT Bombay CRNN" label in old notes was a mistake).

**Getting an Indic roll is BLOCKED (§0):** ECI portal `ChunkLoadError` (missing chunk `5451`) — real ECI-side broken deploy during SIR 2026 / ECINET migration; backend API alive, frontend dead; scraper (§19) hits the same wall. Fallback = **state CEO site** (Telangana CEO = Telugu, easy 4-digit captcha, separate window — solve with agent vision, not the ECI-tuned TrOCR).

**Session working files (Mac, all under gitignored `scraper/data/`):** `ocr_exploratory_report.md`, `roll_U06_1_full_copy.*`, `_bench/` (crops + gen ground truth + engine outputs), `ocr_benchmark_batch1.md`. Mac venv `scraper/.venv` (py3.12) has rapidocr-onnxruntime / python-doctr / easyocr / transformers installed.

**GPU box (`i-06267ed13fcb663b4`):** currently **stopped**, restored to **g6.xlarge** (was temporarily modify-instance-type'd to c7i.8xlarge for the CPU test — MUST stay g6 for GPU work). vLLM + Qwen3-VL-8B-FP8 + olmOCR-2-FP8 weights/containers preserved on EBS. Now peripheral (RapidOCR is CPU) — use only for VLM-fallback experiments. To resume GPU: start instance → new public IP → add current IP to `sg-0680b4005b958b5c8` → `docker start` the container.

**Indic UPDATE (RESOLVED — supersedes "Indic is THE open frontier" above):** Telugu WAS tested (Telangana S29-AC057). RapidOCR = **0% on Telugu-script fields** (reads the Latin/Arabic EPIC/serial/house/age fine); **Surya** (multilingual OCR-VLM, `surya-ocr` 0.20 + `llama.cpp`/`llama-server` backend) reads the whole Telugu card (names ~90% char, SOFT truth — no native verifier). **Decision LOCKED: Indic = Surya as a rare fallback; stop benchmarking Indic engines.** Full: `ocr_allfields_and_indic` memory + `scraper/data/_bench/allfields_eval/`.

---

## 22. Deleted-card TrOCR specialist (stamped DELETED cards) — IN PROGRESS (2026-07-05)
The ONE case RapidOCR (and every VLM) fails: cards struck with a diagonal **DELETED** stamp (SIR-style in-place deletions; also present in Telangana Final-Roll **bodies**). The stamp is **physical occlusion** — more pixels / zoom / isolating the single card does NOT help (tested at 600 dpi); RapidOCR garbles the crossed fields. Plan below is **all user-confirmed**; durable record = `deleted_card_specialist_plan` memory.

**Why a fine-tuned specialist, not a VLM:** production is ~1M PDFs (~30M cards) — can't run a heavy VLM on every card, and off-the-shelf VLMs *guess* occluded text (Qwen2.5-VL-3B / Qwen3-VL-8B / MiniCPM-V / Granite all read these **worse than RapidOCR**). The task is narrow + fixed (one stamp, one font, one 3×10 layout) → exactly what a fine-tuned recognizer nails.

**Model = fresh `microsoft/trocr-base-printed`** (a size UP from the captcha solver's `trocr-small-printed`), fine-tuned on the labeled deleted cards → saved to its OWN dir (e.g. `data/deleted_card_model/trocr/`). This is **independent** of the captcha TrOCR (`scraper/captcha_solver/` → `data/captcha_model/trocr/`): both fine-tune from the same public HF base, so a second copy is automatic — nothing is reused or overwritten. TrOCR's autoregressive decoder can INFER a partly-occluded char from context; a CTC/CRNN (RapidOCR) reads literally → TrOCR is the right architecture, not a compromise.

**Labels = the AGENT reading each card by vision.** The true text is UNDER the stamp, so no automated label source exists (synthetic overlay REJECTED by user; draft-roll EPIC-match DISCARDED by user — "only look at final rolls"; VLMs fail). The agent reads the card and that read IS the label → this *distills the agent's reading into a cheap, agent-free deployable model*. **Validated live 2026-07-05** (user confirmed 3/3 cards, incl. occluded name-tail / relation / house). **Honest ceiling:** model ≈ agent's label quality; where the stamp fully wipes ink (some house digits) NO label — mine or anyone's — recovers it → those fields stay unreliable. Acceptable: these are DELETED voters, and EPIC + reason-code + serial always read fine from the un-stamped corner.

**Production shape:** RapidOCR reads every card → detects DELETED cheaply via the **reason-code letter** (single char in {E,S,R,M,Q} in the serial box, same line as EPIC, x<~340px — col-3's box sits at x~16, so no x lower-bound) → routes ONLY the flagged card to the deleted-card TrOCR specialist. No agent / no VLM at inference.

**Data source — Telangana CEO site (ECI portal still broken):** `https://ceotserms2.telangana.gov.in/ts_erolls/rolls.aspx` — self-hosted, NO login/OTP/AES-crypto; RapidOCR solves its 4-digit numeric captcha at 100% (4× upscale + threshold) → **fully automated**. "Final Roll" = `EnglishMotherRoll`. Stamped deleted cards live in the **Final-Roll body** (the "supplements list deletions cleanly" idea was WRONG — ignore it). Harvest tools in `scraper/data/_bench/harvest_tools/` (`ts_bulk_dl.js`, `solve_captcha.py`, `captcha_serve.py`).

**State (2026-07-05):**
- ✅ **274 Musheerabad (S29-AC057) English Final Rolls downloaded** → `scraper/data/rolls/S29-AC057-Musheerabad-ENG/` (~2.9 GB, gitignored PII).
- 🔄 **Harvest** via `scraper/data/_bench/harvest_tools/hybrid_harvest.py` (coarse 120-dpi OCR to detect the reason-letter → fine 220-dpi crop of hits; thread-capped, resumable via `processed_*.txt`; run `python hybrid_harvest.py <widx> <nworkers>`). ⚠️ **Mac 16GB thrashes with >2 workers** — keep it to 1–2 gentle. ~241 cards so far → `scraper/data/_bench/fr_deleted_harvest/cards/` (target ~330 @ ~1.3 deletions/part). Live dashboard: `scraper/data/_bench/harvest_monitor.py`.
- ✅ **Labeling DONE: 372/372** → `scraper/data/_bench/fr_deleted_harvest/labels.jsonl` (JSONL, one record/card: `crop, part, page, serial, epic, reason, name, relation_type[father|husband|mother|other], relation_name, house_number, age, gender, uncertain[], labeler:"agent-vision"`; optional `flag` = `no_stamp_exclude` (4 harvester false-positives → **368 trainable**) / `partial_crop`; optional `review:true` (5 cards I was unsure of — user to eyeball). Reason mix S:359/R:5/Q:5/E:3. QC clean: every epic ⊂ its crop filename, no dup crops.
- ⚠️ **v1 fine-tune FAILED — see `scraper/data/deleted_card_model/RESULT.md`.** Trained `trocr-small-printed` on whole-card crops → serialized 6-field string. Loss 6.58→0.33 (memorized train) but **val = 0% exact on every field**; degenerate repetition loops. **Root cause is DESIGN, not hyperparams:** TrOCR reads a single LINE, but a card is a multi-LINE block, AND the 606×269 (2.26:1) crop is squished to 384×384 → unreadable (even gender = 0%). `trocr-base-printed` thrashed the 16GB Mac (swap 6.75GB) → used small; base wouldn't fix the framing anyway. Trainer (`scraper/deleted_card_solver/train_deleted_trocr.py`, takes `--model`) + dataset are preserved — nothing lost.

**✅ v2 = Donut TRAINED & WORKS (2026-07-06).** TrOCR is single-line; a card is a multi-line block → wrong tool. **Donut** (`naver-clova-ix/donut-base`) is an **OCR-free document-understanding** model = Swin document-scale encoder + AUTOREGRESSIVE decoder (keeps the infer-under-the-stamp property that beats RapidOCR's literal CTC) reading the WHOLE card → structured fields, no line-segmentation. Trainer `scraper/deleted_card_solver/train_donut.py` (same JSONL loader + per-field eval; `<s_field>val</s_field>` targets; input **448×1024 aspect-preserving** = fixes v1 squish; `no_repeat_ngram_size=3` = fixes v1 loops). **Fine-tuned on the L4 GPU (30 epochs, ~35 min) → val char-acc 0.986**; per-field exact: age/gender/relation_type **1.00**, house 0.90, name 0.80, relation_name 0.62 (weakest, long+stamp-crossed, but ~0.98 char-acc). Best checkpoint → `data/deleted_card_model/donut/`. **✅ Then trained 2 MORE readers for comparison + a combine (2026-07-06, same val split):** **Pix2Struct** (`train_pix2struct.py`, avg-field 0.833 — worst, but uncorrelated errors → confidence partner; ⚠️ generate ONE image at a time, batched gen is broken) and **Qwen2.5-VL-3B LoRA** (`train_qwen_lora.py` + peft, **avg-field 0.950 — BEST**, name 0.93 / relation_name 0.88 — its strong LM prior solves the occluded fields Donut lagged: name 0.82→0.93, rel_name 0.62→0.88). **The COMBINE wins:** run cheap Donut+Pix2Struct on all cards → escalate only their disagreements to Qwen → **avg 0.958 (> Qwen-alone), Qwen invoked on just 19% of fields**; Donut↔Qwen agree on 89% of fields, 99% correct when they agree (free confidence signal). Models → `data/deleted_card_model/{donut,pix2struct,qwen_lora}/` (Qwen=LoRA adapter, needs base `Qwen/Qwen2.5-VL-3B-Instruct`); per-card preds `combine_preds.json`; full table `RESULT.md`. **GPU-run recipe:** vLLM container has torch/transformers 5.12; `pip install peft` for Qwen; Pix2Struct needs `max_patches=1024` (2048 OOMs the L4) + `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`. ⚠️ **Train on the GPU, NOT the Mac** — donut-base = 51s/step + 11GB swap on the 16GB Mac; on L4 sub-second/step. **How it ran on the box:** vLLM docker image already has torch+transformers 5.12+CUDA → `docker run --gpus all -v ~/donut_train:/work -v ~/.cache/huggingface:/root/.cache/huggingface -w /work --entrypoint python3 vllm/vllm-openai:latest -m deleted_card_solver.train_donut --epochs 30 --batch 4` (data tarred Mac→box to `~/donut_train/`, model tarred back). **NEXT:** (1) score on the 6 Lakshadweep known-truth cards; (2) wire into `ocr/` as the reason-code-triggered watermark fallback (§17) — RapidOCR reads card + detects DELETED via reason letter → routes the crop to Donut → take Donut's soft fields, keep RapidOCR's EPIC/serial/reason. (3) If relation_name/name want improvement: more labels, or Pix2Struct/Qwen2.5-VL-3B-LoRA. ⚠️ 5 cards flagged `review:true` in labels.jsonl need the user's eyeball.

---

## 23. ✅ FULL PIPELINE CONNECTED (2026-07-07) — scraper → RapidOCR OCR (+deleted combine) → DB
The three parts (scraper, `ocr/`, the trained deleted-card models) are now wired into ONE runnable path. Approved plan: `/Users/nzm/.claude/plans/piped-noodling-melody.md`.

**Flow:** `scraper/bin/download.js` → roll PDF (filename carries state/ac/lang/part/roll-type) → `ocr/cli.py` (default `--extractor rapidocr`) → `rasterize` (cover / voter half-pages / summary) → **RapidOCR extractor** reads every card; a **DELETED** card (reason letter {E,S,R,M,Q} in the serial box) → **DeletedCardReader** combine → `assemble` → `reconcile` (vs summary oracle) → `load.to_sql` → `db/schema.sql`.

**Built / changed this session (all verified to compile + imports resolve):**
- **`ocr/extractors/rapidocr.py`** — the main (English) extractor. Ported + generalized from `scraper/data/_bench/allfields_eval/parse_rapidocr.py`: splits the half-page into **3 columns** (relative geometry, DPI-independent), anchors cards on the EPIC token, label state-machine → name/relation_type/relation_name/house_number/age/gender; reads serial + reason letter from the serial box; **row-major serial inference** fills serials RapidOCR fails to emit (same visual row → consecutive serials across cols); deterministic **SUMMARY oracle parser** (column headers Male/Female/Third/Total × row labels → net M/F/TG/Total + additions/deletions totals); best-effort cover. Dispatches on the schema's shape (cards vs summary vs cover). Lazy-imports rapidocr so `ocr/` core stays stdlib-only. On a DELETED card it crops the card band and calls the combine, overriding name/relation_name/house_number (keeps RapidOCR's EPIC/serial/reason from the clean corner).
- **`scraper/deleted_card_solver/combine.py`** — `DeletedCardReader`: Donut + Pix2Struct read the card; per field **agree=accept, disagree=escalate to Qwen2.5-VL-3B**. Backends: `local` (peft adapter on this machine), `http` (POST to a served Qwen, OpenAI-compatible), `none` (keep Donut, mark field low-confidence). Returns CardRecord-shaped fields. Reuses the exact proven generate/parse (target tags, `no_repeat_ngram_size=3`, Pix2Struct one-image-at-a-time). Qwen loaded lazily on the first disagreement.
- **`ocr/extractors/surya.py`** — Indic extractor (RapidOCR for Latin EPIC/serial/age, Surya for Indic name/relation/gender). ⚠️ **PORTED + WIRED but NOT roll-verified** (needs an Indic roll + the `llama.cpp`/`llama-server` backend).
- **`ocr/cli.py`** — default `--extractor rapidocr`; added `--deleted-backend {none,local,http}` + `--qwen-host`.
- **`ocr/extractors/__init__.py`** — registry is now exactly **`rapidocr` + `surya`** (final models only).
- **DELETED (cleanup):** `ocr/extractors/{mock,ollama_qwen,vllm_qwen}.py` (superseded general-VLM path), 3× `.DS_Store`, `__pycache__`.

**Run it (use `scraper/.venv` — it has rapidocr + torch + transformers):**
`scraper/.venv/bin/python -m ocr.cli <roll.pdf> --extractor rapidocr --deleted-backend http --qwen-host http://<gpu>:8000 --dpi 200 -v --sql-out roll.sql`
(`--deleted-backend none` = RapidOCR only; `local` needs `pip install peft` in `.venv` + Qwen runs slow on the Mac; `http` = escalate to a served Qwen on the GPU box.)

**✅ STATUS = TESTED ON ROLLS & VERIFIED (2026-07-17).** Ran the full RapidOCR pipeline (`--deleted-backend none`) on ALL 10 parts of Lakshadweep U06-1. Result: **8/10 parts fully RECONCILE; net_total reconciles on all 10** (no elector lost anywhere). The 2 non-reconciling parts (3 & 5) fail ONLY on one gender check each, and the cause is a **source-PDF defect, not extraction**: when a card's name + relation + house each wrap to two lines, the printed card overflows and the "Age : X Gender : Y" line is physically absent (verified by eye on part 3 s531 + part 5 s55 — RapidOCR reads everything else at 99–100% conf; there is nothing to read). Those people still exist and are counted in net_total, so only the M/F split is off by 1.

**Three bugs found & fixed this session (all verified through real runs):**
1. **Serial digit misread → lost elector.** RapidOCR read a serial "9" as "6" (part 1 s9), creating a duplicate that dedup collapsed → 1 elector lost. Fix (`rapidocr.py`, serial-assignment block): the rigid 3×10 grid means each card in a visual row implies a row base = `serial − col`; take the MAJORITY base per row → a single misread serial digit is outvoted and corrected. → part 1 226/226 ✓.
2. **Multi-page additions undercounted.** "List of Additions" prints ONCE atop a section that spans several pages; continuation pages/bottom-crops have no header, so `_region` defaulted them to main_roll → additions counted as `active`. Fix (`pipeline.py`): `_region` returns None for headerless strips and the loop CARRIES the last-seen section forward. → part 2 additions 32/32 ✓. (Confirms Lakshadweep additions CONTINUE numbering, so the §0 assemble trailing-serial guard is safe here; a reset-supplement roll still needs separate validation.)
3. **Serial "9"→"S" misread → fake deletion.** RapidOCR read serial "905" as "S06" (part 8); the leading 'S' (a valid reason letter {E,S,R,M,Q}) faked a deletion on a live female card (→ net_total −1, net_female −1, deletions +1). Fix (`rapidocr.py`, serial-box branch): a GENUINE deletion renders the reason letter SEPARATED from the serial ("S  905" = letter+space+digits, or a standalone letter token); a letter FUSED to the digits ("S06") is a misread digit, not a marker → strip it, keep the digits (grid consensus fixes the value). → part 8 982/982, deletions 8 ✓.

**✅ DELETED-card combine VERIFIED in-pipeline (2026-07-17, Donut+Pix2Struct, `--deleted-backend dp`, on the Mac, free).** Ran part 1 (5 deleted cards): combine correctly detects DELETED (reason letter) → crops → recovers occluded name/relation/house, merges back; roll still **RECONCILES 226/226** (deleted cards don't count toward net, as expected). Clear win on stamp-garbled fields, esp. house NUMBERS (RapidOCR `$ 1/Rented House Male`→ `1/4 Rented House`; `1/2PALLIPURAM ndsrMale`→ `1/62 PALLIPURAM`; `1/gZ…AderFemale`→ `1/87 Cheriya…`). Residual name/relation errors (e.g. `Bairul`/`Hairul`) are exactly the ~19% disagreement fields the Qwen escalation fixes (Donut+PS name ~0.80 → +Qwen ~0.93). Changes this session: (a) **new `--deleted-backend dp`** = Donut+Pix2Struct, no Qwen (free/local tier + graceful Qwen-down fallback; reader's `qwen_backend` other-than-http/local ⇒ no escalation). (b) **version-robust processor loaders in `combine.py`** (`_load_donut_processor`/`_load_pix2struct_processor`): the GPU-box-saved tokenizer_configs name SLOW tokenizers needing sentencepiece files that weren't saved → from_pretrained fails on transformers 4.57.6 (Mac) → fall back to building the FAST tokenizer straight from `tokenizer.json` (Donut=XLMRobertaTokenizerFast, Pix2Struct=T5TokenizerFast). Works on Mac AND GPU box. (c) **`_reader_for` hardened** (`rapidocr.py`): a reader-BUILD failure now warns + disables the combine (`_reader_failed`) and continues RapidOCR-only, instead of throwing on every crop and nuking the whole roll (the original dp run failed this way — 0/226).

**NEXT (in order):** (1) **run with `--deleted-backend http`** (or `local`) to add the Qwen escalation: start GPU box + `vllm serve` base `Qwen/Qwen2.5-VL-3B-Instruct` + LoRA adapter (or `pip install peft` in `scraper/.venv` + ~7GB base download for `local`, slow on Mac). Improves deleted-card name/relation only; reconcile already passes without it. (2) **fresh downloads** end-to-end (scraper → ocr → DB) once the ECI portal / a CEO site is reachable. (3) `--sql-out` → load into `db/schema.sql` and eyeball the rows. (4) Surya on-roll verify (Indic). (5) Decide leftover cleanup: `train_deleted_trocr.py` (failed v1) + `_bench/` exploratory scripts. (6) Write `ARCHITECTURE.md`. (7) Minor open items: summary `num_modifications` parse grabs the wrong number (not a reconcile check); the source-defect gender cards could be flagged (not fixed — data isn't there). ⚠️ Reconcile is a HARD pass/fail on M/F; consider treating "net_total matches + M/F off by exactly the count of unreadable-gender active cards" as a soft-pass-with-flag so source defects don't fail the QC gate at batch scale.
