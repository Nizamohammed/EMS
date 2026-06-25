#!/usr/bin/env node
'use strict';
// Phase B: enumerate parts per AC and seed the download_job leaves.
//
// Drives the cascade (state -> roll -> district -> AC -> language) in the
// browser — NO captcha is needed to *see* the part list, only to download — and
// for each AC reads the real part list (part_no + polling-station name) and
// seeds one download_job per part, per language in scope. Also records the AC's
// available years / roll types / languages in ac_availability (the §12 menu).
//
// Idempotent: re-running never clobbers an existing job's status.
//
// Usage:
//   node bin/enumerate-parts.js --state Lakshadweep              # English-first, FinalRoll
//   node bin/enumerate-parts.js --state Lakshadweep --languages all   # every language
//   node bin/enumerate-parts.js --state Goa --roll FinalRoll --max-acs 2
//
// NOTE: AC selection by react-select index is robust for single-AC states; for
// multi-AC states the index walk needs hardening (type-to-filter) — tracked.

const path = require('path');
const { Manifest } = require('../lib/manifest');
const B = require('../lib/browser');

const DB_PATH = path.join(__dirname, '..', 'manifest.db');

function parseArgs(argv) {
  const a = { state: 'Lakshadweep', roll: 'FinalRoll', languages: 'english', maxAcs: 0, headful: false };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === '--state') a.state = argv[++i];
    else if (k === '--roll') a.roll = argv[++i];
    else if (k === '--languages') a.languages = argv[++i]; // all | english | first
    else if (k === '--max-acs') a.maxAcs = Number(argv[++i]);
    else if (k === '--headful') a.headful = true;
  }
  return a;
}

// "SIR FinalRoll - 2026" -> {year:2026, type:'SIR_FinalRoll'}; "Supplement-2 2026" -> 'Supplement'
const yearOf = (label) => {
  const m = String(label).match(/(\d{4})/);
  return m ? Number(m[1]) : null;
};
function normalizeRollType(label) {
  const noYear = String(label).replace(/\s*-?\s*\d{4}\s*$/, '').trim();
  if (/supplement/i.test(noYear)) return 'Supplement';
  return noYear.replace(/\s+/g, '_'); // SIR_FinalRoll, SIR_DraftRoll
}
const langCode = (name) => String(name).trim().slice(0, 3).toUpperCase(); // ENGLISH->ENG, MALAYALAM->MAL

function scopeLanguages(langs, mode) {
  if (mode === 'first') return langs.slice(0, 1);
  if (mode === 'english') {
    const eng = langs.filter((l) => /english/i.test(l));
    return eng.length ? eng : langs.slice(0, 1); // fall back to native (§12)
  }
  return langs; // all
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const m = new Manifest(DB_PATH);
  const stateCd = m.stateCdByName(args.state);
  if (!stateCd) {
    console.error(`unknown state "${args.state}" (not in manifest — run crawl-manifest first)`);
    m.close();
    process.exitCode = 1;
    return;
  }

  const { browser, page } = await B.launchSession({ headless: !args.headful });
  try {
    await B.gotoDownload(page);
    if (!(await B.pickNative(page, args.state))) throw new Error(`state "${args.state}" not in dropdown`);
    await B.sleep(page, 2500);
    const rollLabel = await B.pickNativeContaining(page, args.roll);
    if (!rollLabel) throw new Error(`no roll option matching "${args.roll}"`);
    await B.sleep(page, 2000);
    const year = yearOf(rollLabel);
    const rollType = normalizeRollType(rollLabel);

    const sels = await B.dumpSelects(page);
    const years = (sels.find((s) => /^\d{4}$/.test(s.options[0])) || { options: [] }).options;
    const rollOpts = (sels.find((s) => s.options[0] === 'Select Roll') || { options: [] }).options.slice(1);
    const districts = (sels.find((s) => s.options[0] === 'Select District') || { options: [] }).options.slice(1);
    console.log(`state=${args.state} (${stateCd}) roll="${rollLabel}" year=${year} type=${rollType}`);
    console.log(`years=${JSON.stringify(years)} rollOptions=${JSON.stringify(rollOpts)} districts=${districts.length}`);

    let acSeen = 0;
    let jobsNew = 0;
    let jobsTotal = 0;
    for (const dLabel of districts) {
      if (args.maxAcs && acSeen >= args.maxAcs) break;
      await B.pickNative(page, dLabel);
      await B.sleep(page, 1800);
      const acOpts = await B.listAcOptions(page);
      console.log(`district="${dLabel}" acs=${acOpts.length}`);

      for (let ai = 0; ai < acOpts.length; ai++) {
        if (args.maxAcs && acSeen >= args.maxAcs) break;
        await B.selectAcByIndex(page, ai);
        await B.sleep(page, 1500);
        const acLabel = acOpts[ai];
        const acRow = m.findAc(stateCd, acLabel);
        if (!acRow) {
          console.log(`  ! AC "${acLabel}" not found in manifest — skipping`);
          continue;
        }

        const langs = await B.languageOptions(page);
        const inScope = scopeLanguages(langs, args.languages);
        let partsCount = 0;
        for (const langLabel of inScope) {
          await B.pickNative(page, langLabel);
          await B.sleep(page, 2200);
          const pl = await B.readParts(page);
          partsCount = pl.parts.length;
          for (const p of pl.parts) {
            jobsTotal++;
            jobsNew += m.seedJob({
              stateCd,
              districtCd: acRow.district_cd,
              acNo: acRow.ac_no,
              year,
              rollType,
              language: langCode(langLabel),
              partNo: p.partNo,
              stationName: p.stationName,
            });
          }
          console.log(`  AC ${acRow.ac_no} "${acRow.ac_name}" lang=${langLabel} parts=${pl.parts.length}`);
        }
        m.setAvailability(stateCd, acRow.ac_no, { years, rollTypes: rollOpts, languages: langs, partsCount });
        acSeen++;
      }
    }

    console.log(`\n=== seeded ===`);
    console.log(`ACs enumerated: ${acSeen}  jobs touched: ${jobsTotal}  newly seeded: ${jobsNew}`);
    const js = m.jobStats();
    console.log(`download_job total: ${js.total}  byStatus: ${JSON.stringify(js.byStatus)}`);
    for (const g of js.byGroup) console.log(`  ${g.state_cd} ${g.year} ${g.roll_type} ${g.language}: ${g.parts} parts`);
  } finally {
    await browser.close();
    m.close();
  }
}

main().catch((e) => {
  console.error('FATAL:', e && e.stack ? e.stack : e);
  process.exitCode = 1;
});
