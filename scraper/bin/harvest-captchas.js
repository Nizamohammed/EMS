#!/usr/bin/env node
'use strict';
// Harvest captcha images for the solver effort (needed by BOTH candidate paths:
// a bigger-VLM eval set and/or a CNN training set). Saves high-resolution PNGs
// the model and labeling pipeline can consume.
//
// Fresh captcha per page load. Auto-detects whether the captcha renders right
// after load (fast path) or only after the cascade (fallback), so harvesting is
// as quick as the site allows. Labels are added later (separate step) — this
// only collects raw images + an index.
//
// Usage:
//   node bin/harvest-captchas.js --n 50
//   node bin/harvest-captchas.js --n 300 --out data/captchas/raw

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const B = require('../lib/browser');

function parseArgs(argv) {
  const a = { n: 50, out: path.join(__dirname, '..', 'data', 'captchas', 'raw') };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--n') a.n = Number(argv[++i]);
    else if (argv[i] === '--out') a.out = path.resolve(argv[++i]);
  }
  return a;
}

async function visibleDataImg(page) {
  return page.locator('img[src^="data:"]:visible').count().catch(() => 0);
}

async function ensureCaptcha(page) {
  // fast path: captcha already on the freshly-loaded page?
  if ((await visibleDataImg(page)) > 0) return 'load';
  // fallback: the minimal cascade that makes the captcha render (Lakshadweep).
  await B.pickNative(page, 'Lakshadweep'); await B.sleep(page, 2200);
  await B.pickNativeContaining(page, 'SIR FinalRoll'); await B.sleep(page, 1800);
  await B.pickNative(page, 'Lakshadweep'); await B.sleep(page, 1600);
  await B.selectAcFirst(page); await B.sleep(page, 1300);
  await B.pickNative(page, 'ENGLISH'); await B.sleep(page, 2000);
  return 'cascade';
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  fs.mkdirSync(args.out, { recursive: true });
  const indexPath = path.join(args.out, 'index.jsonl');

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    deviceScaleFactor: 3, // hi-res: materially helps any downstream reader
    viewport: { width: 1366, height: 900 },
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36',
  });
  const page = await ctx.newPage();

  let saved = 0;
  let mode = null;
  for (let i = 0; i < args.n; i++) {
    try {
      await B.gotoDownload(page);
      mode = await ensureCaptcha(page);
      const name = `cap_${String(i).padStart(4, '0')}.png`;
      const dest = path.join(args.out, name);
      await B.captchaShot(page, dest);
      const bytes = fs.existsSync(dest) ? fs.statSync(dest).size : 0;
      if (bytes < 500) { console.log(`  ${name} too small (${bytes}b) — skipping`); continue; }
      fs.appendFileSync(indexPath, JSON.stringify({ file: name, bytes, mode }) + '\n');
      saved++;
      if (saved % 10 === 0 || i === args.n - 1) console.log(`harvested ${saved}/${args.n} (mode=${mode})`);
    } catch (e) {
      console.log(`  [${i}] error: ${e.message.slice(0, 80)}`);
    }
  }
  console.log(`\ndone: ${saved} captchas -> ${args.out}`);
  await browser.close();
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
