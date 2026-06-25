'use strict';
// Pluggable captcha solver (mirrors the OCR pipeline's swappable extractor).
// Interface: solve(pngPath) -> string. The orchestrator screenshots the captcha
// to pngPath, asks the solver, and submits; if the server rejects it (the
// generate call is not "Success"), the orchestrator reloads a fresh captcha and
// asks again. So a solver need not be perfect — retries + the Success oracle
// make a modest solver workable, and we only solve ~once per AC.

const fs = require('fs');
const path = require('path');

// Bootstrap/escalation solver: hand the image to a human/agent via files.
// Writes ready.flag, prints CAPTCHA_READY, polls answer.txt.
class ManualSolver {
  constructor({ workdir }) {
    this.workdir = workdir;
  }
  async solve(pngPath) {
    const answerFile = path.join(this.workdir, 'answer.txt');
    const readyFile = path.join(this.workdir, 'ready.flag');
    try { fs.unlinkSync(answerFile); } catch {}
    fs.writeFileSync(readyFile, 'ready\n');
    console.log('CAPTCHA_READY ' + pngPath);
    const start = Date.now();
    while (Date.now() - start < 180000) {
      if (fs.existsSync(answerFile)) {
        const a = fs.readFileSync(answerFile, 'utf8').trim();
        try { fs.unlinkSync(readyFile); } catch {}
        return a;
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error('manual captcha timeout (no answer.txt within 180s)');
  }
}

// Our fine-tuned TrOCR model, served by captcha_solver/trocr_serve.py over HTTP
// (the autonomous scale path). Solving is a POST of the captcha PNG bytes.
class TrocrSolver {
  constructor({ host = 'http://127.0.0.1:8077' } = {}) {
    this.host = host;
  }
  async solve(pngPath) {
    const buf = fs.readFileSync(pngPath);
    const res = await fetch(`${this.host}/solve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: buf,
    });
    const j = await res.json();
    if (j.error) throw new Error(`trocr solver: ${j.error}`);
    return (j.text || '').trim();
  }
}

function getSolver(name, opts = {}) {
  if (name === 'manual') return new ManualSolver(opts);
  if (name === 'trocr') return new TrocrSolver(opts);
  throw new Error(`unknown captcha solver: ${name}`);
}

module.exports = { getSolver, ManualSolver, TrocrSolver };
