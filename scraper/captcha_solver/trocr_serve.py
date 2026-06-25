"""Serve the fine-tuned TrOCR captcha solver over HTTP so the Node robot can use
it (--solver trocr). Run from scraper/:

    .venv/bin/python -m captcha_solver.trocr_serve      # 127.0.0.1:8077

POST raw PNG bytes to /solve -> {"text": "...", "confidence": 0.x}
"""
import io
import json
import os
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer

warnings.filterwarnings('ignore')
import torch
from PIL import Image
from transformers import TrOCRProcessor, VisionEncoderDecoderModel

MODEL_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'captcha_model', 'trocr')
DEV = 'mps' if torch.backends.mps.is_available() else 'cpu'

print('loading TrOCR from', MODEL_DIR)
PROC = TrOCRProcessor.from_pretrained(MODEL_DIR)
MODEL = VisionEncoderDecoderModel.from_pretrained(MODEL_DIR).to(DEV).eval()


@torch.no_grad()
def solve(im):
    pv = PROC(im.convert('RGB'), return_tensors='pt').pixel_values.to(DEV)
    out = MODEL.generate(pv, max_new_tokens=8, output_scores=True, return_dict_in_generate=True)
    text = ''.join(PROC.batch_decode(out.sequences, skip_special_tokens=True)[0].lower().split())
    # confidence = mean per-step top-token probability
    conf = 1.0
    if out.scores:
        probs = [s.softmax(-1).max().item() for s in out.scores]
        conf = sum(probs) / len(probs)
    return text, conf


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        data = self.rfile.read(n)
        try:
            text, conf = solve(Image.open(io.BytesIO(data)))
            body = json.dumps({'text': text, 'confidence': round(conf, 3)}).encode()
            code = 200
        except Exception as e:  # noqa: BLE001
            body = json.dumps({'error': str(e)}).encode()
            code = 500
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8077'))
    print(f'captcha TrOCR serving on http://127.0.0.1:{port}/solve  (device={DEV})')
    HTTPServer(('127.0.0.1', port), Handler).serve_forever()
