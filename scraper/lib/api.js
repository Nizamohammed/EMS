'use strict';
// Open ECI catalog client — the "data plane" that builds the completeness
// denominator (state -> district -> assembly constituency). These three
// endpoints are HTTP 200 with NO auth and NO captcha and answer from any IP
// (only the actual PDF *download* is India-geo-fenced). See CLAUDE.md §6.
//
// Everything past the AC level (roll types, languages, parts) is exposed only
// through the gated publish endpoints inside the browser download flow, so it
// is discovered later by the control plane, not here.

const BASE = 'https://gateway-voters.eci.gov.in/api/v1';

// Headers the gateway requires on every call (reverse-engineered, §6).
const HEADERS = {
  'User-Agent':
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
    '(KHTML, like Gecko) Chrome/124.0 Safari/537.36',
  Referer: 'https://voters.eci.gov.in/',
  applicationName: 'VSP',
  channelidobo: 'VSP',
  'PLATFORM-TYPE': 'ECIWEB',
  currentRole: 'CITIZEN',
  Accept: 'application/json, text/plain, */*',
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// fetch with an explicit timeout (AbortController) + bounded exponential
// backoff. Treats network failures (the HTTP 000 / timeout class we hit when
// the VPN flaps) and 5xx/429 as retryable; 4xx is returned to the caller.
async function getJson(path, { timeoutMs = 20000, retries = 4, baseDelayMs = 800 } = {}) {
  const url = path.startsWith('http') ? path : `${BASE}${path}`;
  let lastErr;
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (attempt > 0) await sleep(baseDelayMs * 2 ** (attempt - 1));
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), timeoutMs);
    try {
      const res = await fetch(url, { headers: HEADERS, signal: ac.signal });
      clearTimeout(timer);
      if (res.status === 429 || res.status >= 500) {
        lastErr = new Error(`HTTP ${res.status} for ${path}`);
        continue; // retryable
      }
      if (!res.ok) {
        // 4xx — a contract problem, not a blip. Surface immediately.
        const body = await res.text().catch(() => '');
        const err = new Error(`HTTP ${res.status} for ${path}: ${body.slice(0, 200)}`);
        err.status = res.status;
        throw err;
      }
      return await res.json();
    } catch (e) {
      clearTimeout(timer);
      if (e.status) throw e; // non-retryable 4xx
      lastErr = e; // network/abort — retry
    }
  }
  throw new Error(`giving up on ${path} after ${retries + 1} attempts: ${lastErr && lastErr.message}`);
}

// --- catalog endpoints -----------------------------------------------------

// 36 states/UTs. Each row carries stateCd (S01..S29, U01..U08) + names.
const getStates = () => getJson('/common/states');

// Districts for one state. Each row carries districtCd (e.g. S0429) + name.
const getDistricts = (stateCd) => getJson(`/common/districts/${stateCd}`);

// Assembly constituencies for one state. Each row carries asmblyNo + acId +
// category (reservation). The query-param spelling matters: stateCode, not
// stateCd, on this endpoint.
const getConstituencies = (stateCd) =>
  getJson(`/common/constituencies?stateCode=${encodeURIComponent(stateCd)}`);

module.exports = { BASE, HEADERS, getJson, getStates, getDistricts, getConstituencies, sleep };
