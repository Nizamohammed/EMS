#!/usr/bin/env node
'use strict';
// CAPTCHA-SCOPE PROBE — the architecture-deciding experiment.
//
// Question: does a SINGLE captcha cover a multi-part selection? If checking N
// parts + solving one captcha yields N PDFs, the national job count collapses
// from ~1M (one captcha per part) to ~4,129-ish (one per AC selection). If it's
// strictly one-captcha-per-part, the captcha solver is the dominant cost.
//
// What it does on the Lakshadweep AC (small, proven in the PoC):
//   1. cascade-fill, then report available roll types + languages + the full
//      part list (this part needs NO captcha — pure enumeration).
//   2. unless --enumerate-only: check the first N parts, screenshot the captcha
//      for vision solving (file handoff), submit, and record the
//      generate-published-pdfs payload (UUID count) + how many download events
//      fire. payload.length and download count answer the question.
//
// Captcha handoff: writes captcha.png + a ready.flag, then polls answer.txt.
// The agent reads captcha.png, writes the solution to answer.txt.
//
// Usage:
//   node bin/probe-captcha-scope.js --enumerate-only
//   node bin/probe-captcha-scope.js --parts 3

const fs = require('fs');
const path = require('path');
const B = require('../lib/browser');

const WORK = path.join(__dirname, '..', 'data', 'probe');
const DOWNLOADS = path.join(WORK, 'downloads');
const CAPTCHA_PNG = path.join(WORK, 'captcha.png');
const ANSWER = path.join(WORK, 'answer.txt');
const READY = path.join(WORK, 'ready.flag');
const LOG = path.join(WORK, 'probe.log');

function log(m) {
  const line = typeof m === 'string' ? m : JSON.stringify(m);
  console.log(line);
  fs.appendFileSync(LOG, line + '\n');
}

function parseArgs(argv) {
  const a = { enumerateOnly: false, parts: 3, state: 'Lakshadweep', roll: 'SIR FinalRoll' };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--enumerate-only') a.enumerateOnly = true;
    else if (argv[i] === '--parts') a.parts = argv[++i] === 'all' ? Infinity : Number(argv[++i]);
    else if (argv[i] === '--state') a.state = argv[++i];
    else if (argv[i] === '--roll') a.roll = argv[++i];
  }
  return a;
}

async function waitForAnswer(timeoutMs = 180000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (fs.existsSync(ANSWER)) return fs.readFileSync(ANSWER, 'utf8').trim();
    await new Promise((r) => setTimeout(r, 1000));
  }
  return null;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  fs.mkdirSync(DOWNLOADS, { recursive: true });
  for (const f of [CAPTCHA_PNG, ANSWER, READY, LOG]) {
    try { fs.unlinkSync(f); } catch {}
  }

  const { browser, page } = await B.launchSession({ downloadDir: DOWNLOADS });

  // Capture the decisive signals.
  const genpdf = []; // generate-published-pdfs responses
  const downloads = []; // download events
  page.on('response', async (r) => {
    const u = r.url();
    if (u.includes('generate-published-pdfs')) {
      let json = null;
      try { json = await r.json(); } catch {}
      genpdf.push({ status: r.status(), json });
      log(`[net] generate-published-pdfs HTTP ${r.status()} payload=${json ? JSON.stringify(json).slice(0, 240) : '<unparsed>'}`);
    } else if (/\/eroll|amazonaws|cloudfront|\.pdf|getFile|download|publish/i.test(u)) {
      // delivery traffic: how each generated PDF is actually retrieved after the
      // single captcha'd generate call.
      const ct = await r.headerValue('content-type').catch(() => null);
      const cl = await r.headerValue('content-length').catch(() => null);
      log(`[delivery] ${r.request().method()} ${r.status()} ct=${ct} len=${cl} ${u.replace('https://gateway-voters.eci.gov.in', '').slice(0, 120)}`);
    }
  });
  page.on('download', async (d) => {
    try {
      const dest = path.join(DOWNLOADS, d.suggestedFilename());
      await d.saveAs(dest);
      const bytes = fs.statSync(dest).size;
      downloads.push({ file: d.suggestedFilename(), bytes });
      log(`[download] ${d.suggestedFilename()} bytes=${bytes}`);
    } catch (e) {
      log(`[download] save fail: ${e.message}`);
    }
  });

  log(`=== cascade-fill: ${args.state} ===`);
  await B.gotoDownload(page);
  log('state: ' + (await B.pickNative(page, args.state)));
  await B.sleep(page, 2500);
  const rollLabel = await B.pickNativeContaining(page, args.roll);
  log('roll type: ' + rollLabel);
  await B.sleep(page, 2500);
  await B.pickNative(page, args.state); // district (same name for single-district UTs)
  await B.sleep(page, 2000);
  await B.selectAcFirst(page);
  await B.sleep(page, 1500);

  log('available languages: ' + JSON.stringify(await B.languageOptions(page)));
  const lang = await B.selectLanguageByIndex(page, 1);
  log('language chosen: ' + lang);
  await B.sleep(page, 2500);

  log('all selects: ' + JSON.stringify(await B.dumpSelects(page)));

  const parts = await B.readParts(page);
  log(`=== part list ===`);
  log(`total checkboxes: ${parts.count}  select-all idx: ${parts.selectAllIdx}`);
  for (const it of parts.items.slice(0, 8)) log(`  [${it.idx}] checked=${it.checked} | ${it.label}`);
  if (parts.items.length > 8) log(`  ... (${parts.items.length - 8} more)`);

  if (args.enumerateOnly) {
    log('\n--enumerate-only: stopping before captcha/download.');
    await browser.close();
    return;
  }

  // --- multi-part download test ---
  const cbs = page.locator('input[type="checkbox"]');
  const n = await cbs.count();
  // skip a leading select-all box if present; check the next N part boxes
  const startIdx = parts.selectAllIdx === 0 ? 1 : 0;
  let toCheck = Number.isFinite(args.parts) ? args.parts : n;
  let checked = 0;
  for (let i = startIdx; i < n && checked < toCheck; i++) {
    const cb = cbs.nth(i);
    if (await cb.isVisible().catch(() => false)) {
      if (!(await cb.isChecked())) await cb.check().catch(() => {});
      checked++;
    }
  }
  log(`\n=== checked ${checked} part(s); requesting ONE captcha ===`);

  await B.captchaShot(page, CAPTCHA_PNG);
  await page.screenshot({ path: path.join(WORK, 'form.png') });
  fs.writeFileSync(READY, 'ready\n');
  log('CAPTCHA_READY'); // <- agent watches for this

  const answer = await waitForAnswer();
  if (!answer) { log('ERROR: no captcha answer within timeout'); await browser.close(); process.exitCode = 1; return; }
  log('captcha answer received: ' + answer);
  const idx = await B.fillCaptcha(page, answer);
  log('filled captcha into input idx=' + idx);
  await B.sleep(page, 600);

  await B.clickDownloadSelected(page);
  log(`clicked Download Selected PDFs; watching delivery up to 120s for ${checked} part(s)...`);
  const deadline = Date.now() + 120000;
  let lastStatus = '';
  while (Date.now() < deadline) {
    await B.sleep(page, 5000);
    const status = await page
      .evaluate(() => {
        const t = document.body.innerText || '';
        const m = t.match(/Success:\s*(\d+)\s*\/\s*(\d+)[\s\S]*?Error:\s*(\d+)\s*\/\s*(\d+)/);
        return m ? `Success ${m[1]}/${m[2]} Error ${m[3]}/${m[4]}` : null;
      })
      .catch(() => null);
    if (status && status !== lastStatus) {
      log(`[status] ${status}  downloaded=${downloads.length}`);
      lastStatus = status;
    }
    if (downloads.length >= checked) {
      log('all selected parts downloaded');
      break;
    }
  }

  log('\n=== RESULT ===');
  log(`parts selected   : ${checked}`);
  const totalUuids = genpdf.reduce((s, g) => s + (g.json && Array.isArray(g.json.payload) ? g.json.payload.length : 0), 0);
  log(`genpdf calls     : ${genpdf.length}  status(es): ${genpdf.map((g) => g.json && g.json.status).join(',')}`);
  log(`payload UUIDs     : ${totalUuids}`);
  log(`download events  : ${downloads.length}  bytes: ${downloads.map((d) => d.bytes).join(',')}`);
  const pageText = (await page.evaluate(() => document.body.innerText)).replace(/\n+/g, ' | ').slice(0, 240);
  log('page after       : ' + pageText);
  log(
    `\nVERDICT: ${checked} parts + 1 captcha -> ${genpdf.map((g) => g.json && g.json.status).join(',') || 'no-response'}; ` +
      `${totalUuids} pdf(s) generated, ${downloads.length} downloaded.`
  );
  await page.screenshot({ path: path.join(WORK, 'after.png') });
  await browser.close();
}

main().catch((e) => {
  log('FATAL: ' + (e && e.stack ? e.stack : e));
  process.exitCode = 1;
});
