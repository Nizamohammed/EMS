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

// Local small VLM via Ollama — the "ML model in the loop". Not perfect per-solve
// (~5/6 chars observed); relies on the orchestrator's retry + Success oracle.
class ModelSolver {
  constructor({ host = 'http://localhost:11434', model = 'qwen2.5vl:3b' } = {}) {
    this.host = host;
    this.model = model;
  }
  async solve(pngPath) {
    const img = fs.readFileSync(pngPath).toString('base64');
    const body = JSON.stringify({
      model: this.model,
      prompt:
        'This is a CAPTCHA image with about 6 alphanumeric characters (letters and digits, ' +
        'possibly mixed case) with a distracting line drawn through them. Read the characters ' +
        'exactly. Respond with ONLY the characters as one string: no spaces, no punctuation.',
      images: [img],
      stream: false,
      options: { temperature: 0 },
    });
    const res = await fetch(`${this.host}/api/generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    });
    const j = await res.json();
    return (j.response || '').replace(/[^A-Za-z0-9]/g, '').trim();
  }
}

function getSolver(name, opts = {}) {
  if (name === 'manual') return new ManualSolver(opts);
  if (name === 'model') return new ModelSolver(opts);
  throw new Error(`unknown captcha solver: ${name}`);
}

module.exports = { getSolver, ManualSolver, ModelSolver };
