#!/usr/bin/env node
'use strict';
// The robot driver. Walks the ACs of a state and runs the per-AC loop
// (enumerate -> mark pending -> 1 captcha -> retrieve -> verify -> mark done),
// resumable at part granularity. Captcha solving is pluggable: --solver manual
// (file handoff, bootstrap) or --solver trocr (the autonomous fine-tuned model,
// served by captcha_solver/trocr_serve.py on :8077).
//
// Usage:
//   node bin/download.js --state Lakshadweep --solver manual --languages english
//   node bin/download.js --state Lakshadweep --solver trocr --max-acs 1

const path = require('path');
const { Manifest } = require('../lib/manifest');
const B = require('../lib/browser');
const { getSolver } = require('../lib/captcha');
const { downloadAc } = require('../lib/download');

const DB_PATH = path.join(__dirname, '..', 'manifest.db');
const DATA_DIR = path.join(__dirname, '..', 'data', 'rolls');
const WORK_DIR = path.join(__dirname, '..', 'data', '_work');

function parseArgs(argv) {
  const a = {
    state: 'Lakshadweep', roll: 'FinalRoll', languages: 'english', lang: null, maxAcs: 0,
    solver: 'manual', host: undefined, maxCaptchaAttempts: 4, headful: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === '--state') a.state = argv[++i];
    else if (k === '--roll') a.roll = argv[++i];
    else if (k === '--languages') a.languages = argv[++i];
    else if (k === '--lang') a.lang = argv[++i];
    else if (k === '--max-acs') a.maxAcs = Number(argv[++i]);
    else if (k === '--solver') a.solver = argv[++i];
    else if (k === '--host') a.host = argv[++i];
    else if (k === '--max-captcha-attempts') a.maxCaptchaAttempts = Number(argv[++i]);
    else if (k === '--headful') a.headful = true;
  }
  return a;
}

const langLabelsFor = (args) => (args.lang ? [args.lang] : ['ENGLISH']); // --lang overrides; default English-first

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const m = new Manifest(DB_PATH);
  const stateCd = m.stateCdByName(args.state);
  if (!stateCd) { console.error(`unknown state "${args.state}"`); m.close(); process.exitCode = 1; return; }

  const solverOpts = { workdir: WORK_DIR };
  if (args.host) solverOpts.host = args.host;
  const solver = getSolver(args.solver, solverOpts);

  const { browser, page } = await B.launchSession({ headless: !args.headful });
  try {
    // Phase 1: discover the (district, acIdx, acLabel) coordinates to visit.
    await B.gotoDownload(page);
    await B.pickNative(page, args.state);
    await B.sleep(page, 2500);
    const rollLabel = await B.pickNativeContaining(page, args.roll);
    if (!rollLabel) throw new Error(`no roll matching "${args.roll}"`);
    await B.sleep(page, 2000);
    const sels = await B.dumpSelects(page);
    const districts = (sels.find((s) => s.options[0] === 'Select District') || { options: [] }).options.slice(1);

    const coords = [];
    for (const dLabel of districts) {
      if (args.maxAcs && coords.length >= args.maxAcs) break;
      await B.pickNative(page, dLabel);
      await B.sleep(page, 1800);
      const acOpts = await B.listAcOptions(page);
      for (let ai = 0; ai < acOpts.length; ai++) {
        if (args.maxAcs && coords.length >= args.maxAcs) break;
        coords.push({ districtLabel: dLabel, acIdx: ai, acLabel: acOpts[ai] });
      }
    }
    console.log(`state=${args.state} (${stateCd}) roll="${rollLabel}" districts=${districts.length} ACs to visit=${coords.length}\n`);

    // Phase 2: run the robot per AC.
    for (const c of coords) {
      const acRow = m.findAc(stateCd, c.acLabel);
      if (!acRow) { console.log(`! AC "${c.acLabel}" not in manifest — skip`); continue; }
      for (const language of langLabelsFor(args)) {
        try {
          const s = await downloadAc(page, m, solver, {
            stateCd, state: args.state, roll: args.roll, districtLabel: c.districtLabel,
            acIdx: c.acIdx, acNo: acRow.ac_no, districtCd: acRow.district_cd, language,
            dataDir: DATA_DIR, workdir: WORK_DIR, maxCaptchaAttempts: args.maxCaptchaAttempts,
          });
          console.log(
            `AC ${acRow.ac_no} "${acRow.ac_name}" [${s.lang}] parts=${s.parts} ` +
              `${s.alreadyDone ? `(resume: ${s.alreadyDone} done) ` : ''}` +
              `downloaded=${s.downloaded} verified=${s.verified} attempts=${s.attempts} ${s.ok ? 'OK' : 'INCOMPLETE'}`
          );
        } catch (e) {
          console.log(`AC ${acRow.ac_no} "${acRow.ac_name}" [${language}] ERROR: ${e.message}`);
        }
      }
    }

    const js = m.jobStats();
    console.log(`\n=== download_job ===  total=${js.total}  ${JSON.stringify(js.byStatus)}`);
  } finally {
    await browser.close();
    m.close();
  }
}

main().catch((e) => { console.error('FATAL:', e && e.stack ? e.stack : e); process.exitCode = 1; });
