# EMS — Election Management System (India) — Project Context & Handoff

> Purpose of this file: let a brand-new chat/agent resume **exactly** where we left off. Read it fully before acting. It captures the goal, everything we reverse-engineered about the data source, the **confirmed working download recipe**, the document structure, the data-model thinking, the plan, open decisions, and gotchas.

---

## 0. TL;DR — where we are right now
- Building an **Election Management System (EMS)** for India. First subsystem = a **scraper/downloader** for electoral rolls, then OCR/parsing, then a database, then backend software.
- We **proved end-to-end** that we can download a real electoral-roll PDF from the official ECI portal (headless browser, captcha solved by the agent's vision, **no login/OTP needed**). A valid 12-page roll was downloaded and inspected.
- We discovered the roll PDFs have **NO text layer → OCR is mandatory**.
- **Current decision:** we chose **Option 2** = first understand the PDF structure → design the database model → then build OCR. (Not yet building the full download pipeline.)
- **✅ DONE: full structural survey + data model.** The PDF structure is fully mapped (see `roll_pdf_structure_verified` memory) and the **core PostgreSQL schema is written and validated** at `db/schema.sql` — syntax-checked (pglast), adversarially reviewed (multi-agent), and **executed on real Postgres 16** (loads clean; the QC reconciliation view + all constraints behave correctly on a sample-data slice).
- **Immediate next step:** build the **OCR/extraction** pass (Option-2 step B) that reads the sample PDF's voter pages and populates `db/schema.sql`, reconciled against the page-12 summary (all 226 real electors). Optionally still grab roll *variants* (native-language + Draft) to stress the schema.

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
Structural survey ✅ and data model ✅ are done (`db/schema.sql`, validated on Postgres 16). **Next = OCR/extraction (Option-2 step B):**
- Build the pass that rasterizes the sample PDF's voter pages (`pdftoppm`, already done at `~/eci_spike/scan/`) and extracts each card's fields → loads `elector`/`roll`/`summary`/etc.
- **Validate against the page-12 summary** on all 226 real electors via `v_roll_reconciliation` (must return `reconciles=true`) — the free QC oracle.
- Evaluate OCR options on the voter grid (vision LLM vs Google Vision vs Tesseract); English roll first.
- Then: build the download→OCR→DB vertical for one part → only then scale the pipeline.
- Optional hardening: pull roll *variants* (native-language + Draft) to stress the schema before scaling.
