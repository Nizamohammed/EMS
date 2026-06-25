#!/usr/bin/env node
'use strict';
// Phase A of the manifest build: crawl the OPEN ECI catalog (no auth, no
// captcha, works from any IP) and populate the completeness denominator —
// state -> district -> assembly constituency — into the SQLite manifest.
//
// This is the skeleton: once it is populated we know exactly how many states /
// districts / ACs exist, the crawl_log shows any state that failed, and the
// control plane can enumerate parts per AC and seed download_job rows on top.
//
// Usage:
//   node bin/crawl-manifest.js                 # full crawl, then print stats
//   node bin/crawl-manifest.js --state U06     # crawl just one state (resume/test)
//   node bin/crawl-manifest.js --stats-only    # print stats, no network

const path = require('path');
const { Manifest } = require('../lib/manifest');
const api = require('../lib/api');

const DB_PATH = path.join(__dirname, '..', 'manifest.db');

function parseArgs(argv) {
  const a = { statsOnly: false, state: null };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--stats-only') a.statsOnly = true;
    else if (argv[i] === '--state') a.state = argv[++i];
  }
  return a;
}

function printStats(m) {
  const s = m.stats();
  console.log('\n=== manifest stats ===');
  console.log(`states:    ${s.states}`);
  console.log(`districts: ${s.districts}`);
  console.log(`ACs:       ${s.acs}`);
  console.log(`download jobs seeded: ${s.jobs}`);
  if (s.crawlErrors.length) {
    console.log(`\n!! ${s.crawlErrors.length} crawl error(s):`);
    for (const e of s.crawlErrors) console.log(`   ${e.scope}: ${e.error}`);
  }
  console.log('\nstate_cd  districts  ACs  name');
  for (const r of s.perState) {
    console.log(
      `${r.state_cd.padEnd(8)}  ${String(r.districts).padStart(9)}  ${String(r.acs).padStart(4)}  ${r.state_name}`
    );
  }
}

async function crawlState(m, st) {
  const stateCd = st.stateCd;
  // districts
  try {
    const districts = await api.getDistricts(stateCd);
    for (const d of districts) m.upsertDistrict(stateCd, d);
    m.logCrawl(`districts:${stateCd}`, 'ok', districts.length, null);
    process.stdout.write(`  ${stateCd} districts=${districts.length}`);
  } catch (e) {
    m.logCrawl(`districts:${stateCd}`, 'error', null, e.message);
    process.stdout.write(`  ${stateCd} districts=ERR(${e.message.slice(0, 60)})`);
  }
  await api.sleep(400); // be polite
  // constituencies (ACs)
  try {
    const acs = await api.getConstituencies(stateCd);
    for (const a of acs) m.upsertAc(stateCd, a);
    m.logCrawl(`constituencies:${stateCd}`, 'ok', acs.length, null);
    process.stdout.write(` acs=${acs.length}\n`);
  } catch (e) {
    m.logCrawl(`constituencies:${stateCd}`, 'error', null, e.message);
    process.stdout.write(` acs=ERR(${e.message.slice(0, 60)})\n`);
  }
  await api.sleep(400);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const m = new Manifest(DB_PATH);
  console.log(`manifest: ${DB_PATH}`);

  if (args.statsOnly) {
    printStats(m);
    m.close();
    return;
  }

  let states;
  try {
    states = await api.getStates();
  } catch (e) {
    console.error(`\nFATAL: could not fetch states list: ${e.message}`);
    console.error('If this is a timeout/connection error, the egress/VPN is likely down.');
    m.close();
    process.exitCode = 1;
    return;
  }
  for (const st of states) m.upsertState(st);
  console.log(`states: ${states.length}`);

  const todo = args.state ? states.filter((s) => s.stateCd === args.state) : states;
  if (args.state && !todo.length) {
    console.error(`no such state in catalog: ${args.state}`);
    m.close();
    process.exitCode = 1;
    return;
  }

  console.log(`crawling districts + ACs for ${todo.length} state(s)...`);
  for (const st of todo) await crawlState(m, st);

  printStats(m);
  m.close();
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
