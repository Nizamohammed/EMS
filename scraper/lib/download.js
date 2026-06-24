'use strict';
// The robot loop, per AC. Lazy enumeration + download in ONE page visit:
//   cascade to AC -> read parts (enumerate) + seed them 'pending'
//   -> select the not-yet-verified parts -> solve ONE captcha (with retry)
//   -> generate -> retrieve each PDF (browser download) -> verify -> mark done.
// Part-level resumable: already-verified parts are skipped, so a re-run only
// fetches what's missing.

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');
const B = require('./browser');

const sleep = (page, ms) => page.waitForTimeout(ms);
const yearOf = (l) => { const m = String(l).match(/(\d{4})/); return m ? Number(m[1]) : null; };
function normalizeRollType(label) {
  const n = String(label).replace(/\s*-?\s*\d{4}\s*$/, '').trim();
  if (/supplement/i.test(n)) return 'Supplement';
  return n.replace(/\s+/g, '_');
}
const langCode = (n) => String(n).trim().slice(0, 3).toUpperCase();

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}
function pdfPageCount(file) {
  try {
    const out = execFileSync('pdfinfo', [file], { encoding: 'utf8' });
    const m = out.match(/Pages:\s*(\d+)/);
    return m ? Number(m[1]) : null;
  } catch { return null; }
}
function isPdf(file) {
  const fd = fs.openSync(file, 'r');
  const buf = Buffer.alloc(5);
  fs.readSync(fd, buf, 0, 5, 0);
  fs.closeSync(fd);
  return buf.toString('latin1') === '%PDF-';
}
// A complete PDF ends with the "%%EOF" trailer. A download truncated by a crash
// keeps a valid %PDF header but loses this — so it's our truncation detector.
function endsWithEof(file) {
  const fd = fs.openSync(file, 'r');
  try {
    const size = fs.fstatSync(fd).size;
    const len = Math.min(1024, size);
    const buf = Buffer.alloc(len);
    fs.readSync(fd, buf, 0, len, size - len);
    return buf.toString('latin1').includes('%%EOF');
  } finally {
    fs.closeSync(fd);
  }
}
// "verified" must mean a genuinely complete, readable PDF — never a crash-
// truncated file (which would otherwise be skipped forever on resume).
function verifyPdf(file) {
  if (!fs.existsSync(file)) return { ok: false, reason: 'missing' };
  if (!isPdf(file)) return { ok: false, reason: 'not a PDF (bad header)' };
  if (!endsWithEof(file)) return { ok: false, reason: 'truncated (no %%EOF trailer)' };
  const pages = pdfPageCount(file);
  if (pages == null || pages < 1) return { ok: false, reason: 'unreadable (no page count)' };
  return { ok: true, pages, bytes: fs.statSync(file).size };
}
const partNoFromFile = (fn) => { const m = fn.match(/-(\d+)-WI\.pdf$/i); return m ? Number(m[1]) : null; };

// Fill state -> roll -> district -> AC -> language and read the part list.
async function cascadeToAc(page, ctx) {
  await B.gotoDownload(page);
  if (!(await B.pickNative(page, ctx.state))) throw new Error(`state not in dropdown: ${ctx.state}`);
  await sleep(page, 2500);
  const rollLabel = await B.pickNativeContaining(page, ctx.roll);
  if (!rollLabel) throw new Error(`no roll matching: ${ctx.roll}`);
  await sleep(page, 2000);
  await B.pickNative(page, ctx.districtLabel);
  await sleep(page, 1800);
  await B.selectAcByIndex(page, ctx.acIdx);
  await sleep(page, 1500);
  if (!(await B.pickNative(page, ctx.language))) throw new Error(`language not available: ${ctx.language}`);
  await sleep(page, 2200);
  const pl = await B.readParts(page);
  return { rollLabel, parts: pl.parts };
}

// Download (or resume) one AC in one language. Returns a summary.
async function downloadAc(page, m, solver, ctx) {
  const dataDir = path.join(ctx.dataDir, ctx.stateCd, String(ctx.acNo));
  fs.mkdirSync(dataDir, { recursive: true });

  // collect download events for the whole AC attempt
  const downloads = [];
  const onDownload = async (d) => {
    try {
      const fn = d.suggestedFilename();
      const dest = path.join(dataDir, fn);
      await d.saveAs(dest);
      downloads.push({ fn, dest });
    } catch (e) { /* a failed save shows up as a missing part later */ }
  };
  page.on('download', onDownload);

  try {
    let { rollLabel, parts } = await cascadeToAc(page, ctx);
    const year = yearOf(rollLabel);
    const rollType = normalizeRollType(rollLabel);
    const lang = langCode(ctx.language);
    const idOf = (partNo) => ({ stateCd: ctx.stateCd, acNo: ctx.acNo, year, rollType, language: lang, partNo });

    // enumerate -> seed all parts 'pending'
    for (const p of parts) {
      m.seedJob({
        stateCd: ctx.stateCd, districtCd: ctx.districtCd, acNo: ctx.acNo, year, rollType,
        language: lang, partNo: p.partNo, stationName: p.stationName,
      });
    }

    // part-level resume: only fetch parts not already verified
    const verified = new Set(m.verifiedPartNos(ctx.stateCd, ctx.acNo, year, rollType, lang));
    const todo = parts.filter((p) => !verified.has(p.partNo));
    if (todo.length === 0) {
      return { acNo: ctx.acNo, lang, parts: parts.length, alreadyDone: parts.length, downloaded: 0, verified: parts.length, attempts: 0, ok: true };
    }

    // select the to-do parts (re-applied after any reload inside the retry loop)
    const selectTodo = async () => {
      const fresh = await B.readParts(page);
      const cbs = page.locator('input[type="checkbox"]');
      let n = 0;
      for (const p of fresh.parts) {
        if (verified.has(p.partNo)) continue;
        const cb = cbs.nth(p.idx);
        if (await cb.isVisible().catch(() => false)) { await cb.check().catch(() => {}); n++; }
      }
      return n;
    };
    await selectTodo();

    // solve ONE captcha; on rejection, reload + re-cascade + re-select, up to N
    let uuids = null;
    let attempts = 0;
    attempt: while (attempts < ctx.maxCaptchaAttempts) {
      attempts++;
      const pngPath = path.join(ctx.workdir, 'captcha.png');
      fs.mkdirSync(ctx.workdir, { recursive: true });
      await B.captchaShot(page, pngPath);
      const answer = await solver.solve(pngPath);
      await B.fillCaptcha(page, answer);
      const genP = page.waitForResponse((r) => r.url().includes('generate-published-pdfs'), { timeout: 40000 }).catch(() => null);
      await B.clickDownloadSelected(page);
      const resp = await genP;
      let json = null;
      if (resp) { try { json = await resp.json(); } catch {} }
      if (json && json.status === 'Success' && Array.isArray(json.payload)) { uuids = json.payload; break attempt; }
      // wrong captcha (or transient) -> fresh page, fresh captcha, re-select
      const r2 = await cascadeToAc(page, ctx);
      parts = r2.parts;
      await selectTodo();
    }

    if (!uuids) {
      for (const p of todo) { m.bumpAttempt(idOf(p.partNo)); m.markJob(idOf(p.partNo), 'failed', { last_error: `captcha failed after ${attempts} attempts` }); }
      return { acNo: ctx.acNo, lang, parts: parts.length, downloaded: 0, verified: 0, attempts, ok: false };
    }

    // committed to fetching these parts now — mark them 'fetching' so a crash
    // mid-download is visible in the DB (and they're retried, never skipped).
    for (const p of todo) m.markJob(idOf(p.partNo), 'fetching');

    // wait for the per-UUID downloads to arrive (no further captcha)
    const want = todo.length;
    const deadline = Date.now() + 180000;
    while (downloads.length < want && Date.now() < deadline) await sleep(page, 3000);

    // verify each downloaded file and mark its job. A part is 'verified' ONLY if
    // the PDF is complete (header + %%EOF trailer + readable page count); a
    // truncated/partial file -> 'failed' so the next run re-fetches it.
    let verifiedCount = 0;
    for (const dl of downloads) {
      const partNo = partNoFromFile(dl.fn);
      if (partNo == null) continue;
      const id = idOf(partNo);
      m.bumpAttempt(id);
      const v = verifyPdf(dl.dest);
      if (!v.ok) { m.markJob(id, 'failed', { last_error: v.reason, file_path: dl.dest }); continue; }
      m.markJob(id, 'verified', { pdf_sha256: sha256(dl.dest), file_path: dl.dest, bytes: v.bytes, page_count: v.pages });
      verifiedCount++;
    }
    // any to-do part with no file -> leave a failed marker so a re-run retries it
    const gotParts = new Set(downloads.map((d) => partNoFromFile(d.fn)));
    for (const p of todo) {
      if (!gotParts.has(p.partNo)) m.markJob(idOf(p.partNo), 'failed', { last_error: 'no file delivered' });
    }

    return { acNo: ctx.acNo, lang, parts: parts.length, requested: want, downloaded: downloads.length, verified: verifiedCount, attempts, ok: verifiedCount === want };
  } finally {
    page.off('download', onDownload);
  }
}

module.exports = { downloadAc, cascadeToAc };
