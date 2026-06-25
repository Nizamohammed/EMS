'use strict';
// Control-plane browser helpers for the ECI download-eroll page. We MUST drive
// a real browser: the page runs ECI's JS, which handles the AES request/response
// crypto and renders the captcha for free (see CLAUDE.md §6). These helpers are
// distilled from the proven PoC (eci_spike/step_final.js) so the cascade-fill
// and part-reading logic stays identical to what is known to work.

const { chromium } = require('playwright');

const DOWNLOAD_URL = 'https://voters.eci.gov.in/download-eroll';
const UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36';

const sleep = (page, ms) => page.waitForTimeout(ms);

async function launchSession({ downloadDir, headless = true } = {}) {
  const browser = await chromium.launch({ headless });
  const ctx = await browser.newContext({
    userAgent: UA,
    viewport: { width: 1366, height: 900 },
    deviceScaleFactor: 3, // hi-res captcha screenshots, matching the CNN's training data
    acceptDownloads: true,
  });
  const page = await ctx.newPage();
  return { browser, ctx, page };
}

async function gotoDownload(page) {
  await page.goto(DOWNLOAD_URL, { waitUntil: 'networkidle', timeout: 60000 });
  await sleep(page, 1500);
}

// Select a native <select> option by exact visible label, searching every
// select on the page (robust to dropdown ordering). Returns true if matched.
async function pickNative(page, label) {
  for (const s of await page.$$('select')) {
    const opts = await s.$$eval('option', (os) => os.map((o) => o.textContent.trim()));
    if (opts.includes(label)) {
      await s.selectOption({ label });
      return label;
    }
  }
  return null;
}

// Select the first option whose text CONTAINS substr (e.g. "SIR FinalRoll" when
// the full label is "SIR FinalRoll - 2026"). Returns the chosen label.
async function pickNativeContaining(page, substr) {
  for (const s of await page.$$('select')) {
    const opts = await s.$$eval('option', (os) => os.map((o) => o.textContent.trim()));
    const hit = opts.find((o) => o.includes(substr));
    if (hit) {
      await s.selectOption({ label: hit });
      return hit;
    }
  }
  return null;
}

// Snapshot of every native select + its current options — used for discovery
// (what roll types / languages are available for the selected state).
async function dumpSelects(page) {
  return page.$$eval('select', (sels) =>
    sels.map((s) => ({
      options: [...s.options].map((o) => o.textContent.trim()),
      value: s.options[s.selectedIndex] ? s.options[s.selectedIndex].textContent.trim() : null,
    }))
  );
}

// AC is a react-select custom component, not a native <select>. The PoC picks
// the first option via keyboard. (Selecting a specific AC by name is a later
// generalization; for the probe, first AC is enough.)
async function selectAcFirst(page) {
  const acInput = page.locator('input[id^="react-select-"]').first();
  await acInput.focus();
  await sleep(page, 600);
  await page.keyboard.press('ArrowDown');
  await sleep(page, 700);
  await page.keyboard.press('Enter');
  await sleep(page, 600);
}

// The language dropdown is the native select whose placeholder option is
// "Select Language". Returns its options (excluding the placeholder).
async function languageOptions(page) {
  for (const s of await page.$$('select')) {
    const opts = await s.$$eval('option', (os) => os.map((o) => o.textContent.trim()));
    if (opts[0] === 'Select Language') return opts.slice(1);
  }
  return [];
}

async function selectLanguageByIndex(page, idx = 1) {
  for (const s of await page.$$('select')) {
    const opts = await s.$$eval('option', (os) => os.map((o) => o.textContent.trim()));
    if (opts[0] === 'Select Language' && opts.length > idx) {
      await s.selectOption({ index: idx });
      return opts[idx];
    }
  }
  return null;
}

// Read the part list. Each part lives in a <tr> whose text is
// "{part_no} - {polling station name}" (the first row is the Select-All header).
// Returns parsed parts [{idx, partNo, stationName}] + the select-all index.
async function readParts(page) {
  await page.waitForSelector('input[type="checkbox"]', { timeout: 20000 }).catch(() => {});
  return page.evaluate(() => {
    const boxes = [...document.querySelectorAll('input[type="checkbox"]')];
    const items = boxes.map((b, idx) => {
      const tr = b.closest('tr');
      const text = tr ? (tr.innerText || '').replace(/\s+/g, ' ').trim() : '';
      const m = text.match(/^(\d+)\s*-\s*(.+)$/);
      return {
        idx,
        checked: b.checked,
        partNo: m ? Number(m[1]) : null,
        stationName: m ? m[2].trim() : null,
        raw: text.slice(0, 160),
      };
    });
    const selectAllIdx = items.findIndex((it) => it.partNo === null && /select all/i.test(it.raw));
    const parts = items.filter((it) => it.partNo !== null);
    return { count: boxes.length, parts, selectAllIdx, items };
  });
}

// AC is a react-select. Open it and read the option labels (for iterating every
// AC in a state). Returns [] if it can't open.
async function listAcOptions(page) {
  const acInput = page.locator('input[id^="react-select-"]').first();
  await acInput.focus();
  await sleep(page, 400);
  await page.keyboard.press('ArrowDown');
  await sleep(page, 600);
  const opts = await page
    .$$eval('[id^="react-select-"][id*="option"]', (els) => els.map((e) => e.textContent.trim()))
    .catch(() => []);
  await page.keyboard.press('Escape').catch(() => {});
  return opts;
}

// Select an AC option by its 0-based position in the react-select list.
async function selectAcByIndex(page, idx) {
  const acInput = page.locator('input[id^="react-select-"]').first();
  await acInput.focus();
  await sleep(page, 400);
  for (let i = 0; i <= idx; i++) {
    await page.keyboard.press('ArrowDown');
    await sleep(page, 120);
  }
  await page.keyboard.press('Enter');
  await sleep(page, 600);
}

async function checkAll(page) {
  const cbs = page.locator('input[type="checkbox"]');
  const n = await cbs.count();
  let checked = 0;
  for (let i = 0; i < n; i++) {
    const cb = cbs.nth(i);
    if (await cb.isVisible().catch(() => false)) {
      if (!(await cb.isChecked())) {
        await cb.check().catch(() => {});
      }
      checked++;
    }
  }
  return checked;
}

// Screenshot just the captcha image for vision solving.
async function captchaShot(page, outPath) {
  let c = page.locator('img[src^="data:"]:visible').first();
  if ((await c.count()) === 0) c = page.locator('img:visible').last();
  await c.screenshot({ path: outPath });
}

// Fill the captcha answer. The parts "Search" box and the react-select input are
// also text inputs and precede the captcha box in the DOM, so exclude them.
async function fillCaptcha(page, answer) {
  const inputs = page.locator('input');
  const n = await inputs.count();
  for (let i = 0; i < n; i++) {
    const el = inputs.nth(i);
    const id = (await el.getAttribute('id')) || '';
    const ph = (await el.getAttribute('placeholder')) || '';
    const type = (await el.getAttribute('type')) || 'text';
    if (id.startsWith('react-select') || /search/i.test(ph) || type === 'checkbox' || type === 'radio') continue;
    if ((await el.isVisible()) && !(await el.inputValue())) {
      await el.fill(answer);
      return i;
    }
  }
  return -1;
}

async function clickDownloadSelected(page) {
  await page.locator('button:has-text("Download Selected PDFs")').click();
}

module.exports = {
  DOWNLOAD_URL,
  sleep,
  launchSession,
  gotoDownload,
  pickNative,
  pickNativeContaining,
  dumpSelects,
  selectAcFirst,
  listAcOptions,
  selectAcByIndex,
  languageOptions,
  selectLanguageByIndex,
  readParts,
  checkAll,
  captchaShot,
  fillCaptcha,
  clickDownloadSelected,
};
