# EMS — Election Management System (India) — Project Context & Handoff

> Purpose of this file: let a brand-new chat/agent resume **exactly** where we left off. Read it fully before acting. It captures the goal, everything we reverse-engineered about the data source, the **confirmed working download recipe**, the document structure, the data-model thinking, the plan, open decisions, and gotchas.

---

## 0. TL;DR — where we are right now
- Building an **Election Management System (EMS)** for India. First subsystem = a **scraper/downloader** for electoral rolls, then OCR/parsing, then a database, then backend software.
- We **proved end-to-end** that we can download a real electoral-roll PDF from the official ECI portal (headless browser, captcha solved by the agent's vision, **no login/OTP needed**). A valid 12-page roll was downloaded and inspected.
- We discovered the roll PDFs have **NO text layer → OCR is mandatory**.
- **Current decision:** we chose **Option 2** = first understand the PDF structure → design the database model → then build OCR. (Not yet building the full download pipeline.)
- **✅ DONE: full structural survey + data model.** The PDF structure is fully mapped (see `roll_pdf_structure_verified` memory) and the **core PostgreSQL schema is written and validated** at `db/schema.sql` — syntax-checked, adversarially reviewed (multi-agent), and **executed on real Postgres 16** (loads clean; the QC reconciliation view + all constraints behave correctly on real extracted data).
- **✅ DONE: OCR/extraction pipeline built** at `ocr/` (see §17). PDF → rasterize → **pluggable VLM extractor** → assemble (dedupe + status) → reconcile vs the summary oracle → **DB loader** into `db/schema.sql`. The full PDF→OCR→DB→reconcile vertical was proven on the Lakshadweep sample (`reconciles=true`, 226 electors). Validated on a 2nd roll (Telangana S29-61, integrated SSR FinalRoll).
- **Model decision (locked):** Tier-A English = **Qwen3-VL-8B (Apache-2.0)** on GPU; bake-off comparators = Chandra-2, DeepSeek-OCR-2. `qwen2.5vl:3b` on a Mac M3/16GB works but is slow + suffers field-overload (drops EPIC) → Mac is for dev/validation, GPU for the corpus.
- **Immediate next step:** the **GPU is provisioned** (AWS, separate session — see §18). Run the **model bake-off** (`--extractor vllm` against the served model) on the two sample rolls, score vs their summary pages, then confirm Qwen3-VL-8B and scale.

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
Structural survey ✅, data model ✅, and the OCR pipeline ✅ are done. **Next = the GPU model bake-off** (§17 + §18): serve Qwen3-VL-8B (and Chandra-2 / DeepSeek-OCR-2) on the AWS GPU, run `ocr/` against them with `--extractor vllm`, score vs the summary oracle on the two sample rolls, confirm the model, then scale to the download→OCR→DB pipeline.

---

## 17. OCR / extraction pipeline (BUILT — `ocr/`)
Stdlib-only Python package; no pip deps (uses `pdftoppm` + stdlib HTTP). Run: `python3 -m ocr.cli <roll.pdf> --extractor <name> [--dpi N --max-voter-pages N --sql-out roll.sql --out result.json -v]`.

**Flow:** `rasterize.py` (PDF → cover / voter half-page crops / summary PNGs; crop bands scale with `--dpi`) → **pluggable `Extractor`** (`ocr/extractors/`) → `assemble.py` (normalize, dedupe by serial, infer status active/deleted/added/modified, structural QA confidence) → `reconcile.py` (counts/gender vs the summary; mirrors `v_roll_reconciliation`) → `load.py` (`--sql-out` → psql-loadable SQL for `db/schema.sql`, incl. Tier-1 person/placement).

**Extractor is the swap point** — `extract(image, json_schema, instruction) -> json`. Registered: `qwen2.5vl` (local Ollama, Mac), `vllm` (remote vLLM OpenAI-compatible API — the GPU path; `--model Qwen/Qwen3-VL-8B-Instruct --host http://<gpu>:8000`), `mock` (no-model wiring test). New engine = one new file, zero pipeline change.

**Key learnings (see `ocr_vertical_proven` memory):**
- Vision extraction nails counts/gender/status (oracle-verified); field text (EPIC digits) needs the structural fidelity gate (`[A-Z]{3}[0-9]{7}`).
- **Field-overload:** a small (3B) model drops hard fields (EPIC, relation_name) when asked for ~10 fields × ~18 cards in one call; it reads them perfectly when the call is scoped. Fix = stronger model (8B does the full schema in one call) OR scoped multi-pass. → GPU + Qwen3-VL-8B chosen.
- Cross-state ENUM/label variance (reservation GENERAL vs GEN; roll_type FinalRoll vs SIR_FinalRoll; area labels) → loader normalizes; schema CHECKs kept tolerant.
- Mac M3/16GB: ~80–186s per half-page call with `qwen2.5vl:3b` → fine for dev/validation, too slow for the corpus.
- **Indic (deferred):** `language_code` (known from the download manifest) routes to a specialist later; no native-language roll obtained yet. Tier-B bake-off (Sarvam/Google DocAI/Bhashini/Indic-tuned open models) when one is available.

---

## 18. AWS GPU / serving — ✅ PROVISIONED (2026-06-24)
**Account 122445004152, region ap-south-1 (Mumbai).** GPU quota ("Running On-Demand G and VT instances", L-DB2E81BA) was 0; AWS denied the first request, **approved on appeal** → now **8 vCPUs**.

**Standing resources (do not recreate):**
- Instance: **`i-06267ed13fcb663b4`** — `g6.xlarge` (NVIDIA **L4 24GB**), AMI `ami-08e5eee927d4b1622` (Deep Learning Base GPU Ubuntu 22.04, drivers+CUDA+Docker+nvidia-runtime preinstalled), 150GB gp3 root. SSH user `ubuntu`. **Currently STOPPED** (model weights + vLLM container preserved on disk).
- Key pair `ems-gpu` → private key at `~/.ssh/ems-gpu.pem` (chmod 600).
- Security group **`sg-0680b4005b958b5c8`** — SSH(22) only; allows `91.148.246.0/24` (user's Mumbai VPN range) + a couple of stale /32s. ⚠️ User IP flaps with VPN; re-add current IP if SSH times out: `aws ec2 authorize-security-group-ingress --group-id sg-0680b4005b958b5c8 --protocol tcp --port 22 --cidr $(curl -s checkip.amazonaws.com)/32 --region ap-south-1`.

**Serving (proven working):** vLLM **v0.23.0** image `vllm/vllm-openai:latest` serves Qwen3-VL-8B fine (resolves `Qwen3VLForConditionalGeneration`). Command used:
`docker run -d --name vllm --gpus all -p 8000:8000 -v $HOME/.cache/huggingface:/root/.cache/huggingface vllm/vllm-openai:latest --model Qwen/Qwen3-VL-8B-Instruct --served-model-name qwen3vl --max-model-len 8192 --gpu-memory-utilization 0.92`. Weights ~16GB download ~140s; loads in a few min. Comparators (Chandra-2, DeepSeek-OCR-2) not yet served.

**To RESUME:** `aws ec2 start-instances --instance-ids i-06267ed13fcb663b4 --region ap-south-1`; the **public IP changes on each start** (no Elastic IP) — re-fetch via `describe-instances`; ensure SG allows current IP; `ssh ... ubuntu@<ip> 'docker start vllm'` (container preserved); wait for `/v1/models` to list `qwen3vl`; then from Mac open a tunnel `ssh -i ~/.ssh/ems-gpu.pem -L 8000:localhost:8000 ubuntu@<ip>` and run `python3 -m ocr.cli <pdf> --extractor vllm --host http://localhost:8000 --model qwen3vl`.

**Cost discipline:** ~$1/hr while running; STOP when idle (`aws ec2 stop-instances ...`) — stopped = only EBS (~$/mo). ⚠️ Data residency: instance is in ap-south-1 (India) — keep real roll PII on this box, never the US hosted APIs (NVIDIA build.nvidia.com ToS bans PII; see gpu research). **Status when stopped: was ~1 min from first GPU extraction — pick up there.**

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
