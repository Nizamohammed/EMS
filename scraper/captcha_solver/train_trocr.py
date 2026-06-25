#!/usr/bin/env python3
"""Fine-tune a pretrained text-recognition transformer (TrOCR) on our ~100 real
captcha labels. TrOCR already knows how to READ text, so it adapts to this
font/charset from far fewer examples than a from-scratch CNN. Run from scraper/:

    .venv/bin/python -m captcha_solver.train_trocr --epochs 30

Holds out --test-n real captchas to measure true exact-match. Saves the best to
data/captcha_model/trocr/.
"""
import argparse
import csv
import os
import random
import string
import warnings

warnings.filterwarnings('ignore')
import torch
from PIL import Image
from transformers import TrOCRProcessor, VisionEncoderDecoderModel

HERE = os.path.dirname(__file__)
RAW = os.path.join(HERE, '..', 'data', 'captchas', 'raw')
MODEL_DIR = os.path.join(HERE, '..', 'data', 'captcha_model')
ALPHABET = set(string.digits + string.ascii_lowercase)  # case-insensitive, 36 classes
NAME = 'microsoft/trocr-small-printed'
MAXLEN = 8
OUT = os.path.join(MODEL_DIR, 'trocr')


def load_rows(labels_csv):
    """Read (file, label) rows; keep only valid 6-char [a-z0-9] labels (lowercased)."""
    rows, bad = [], 0
    with open(labels_csv) as f:
        for row in csv.reader(f):
            if not row or row[0].strip().lower() == 'file':
                continue
            fn, label = row[0].strip(), row[1].strip().lower()
            if len(label) == 6 and all(c in ALPHABET for c in label):
                rows.append((fn, label))
            else:
                bad += 1
    if bad:
        print(f'skipped {bad} malformed labels')
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--labels', default=os.path.join(os.path.dirname(__file__), 'labels.csv'))
    ap.add_argument('--model', default=NAME)
    ap.add_argument('--epochs', type=int, default=30)
    ap.add_argument('--batch', type=int, default=8)
    ap.add_argument('--test-n', type=int, default=20)
    ap.add_argument('--lr', type=float, default=5e-5)
    args = ap.parse_args()

    rows = load_rows(args.labels)
    random.seed(0)
    random.shuffle(rows)
    test, train = rows[:args.test_n], rows[args.test_n:]
    dev = 'mps' if torch.backends.mps.is_available() else 'cpu'
    print(f'model={args.model} train={len(train)} test={len(test)} device={dev}')

    proc = TrOCRProcessor.from_pretrained(args.model)
    model = VisionEncoderDecoderModel.from_pretrained(args.model).to(dev)
    sid = getattr(model.generation_config, 'decoder_start_token_id', None)
    model.config.decoder_start_token_id = sid if sid is not None else proc.tokenizer.cls_token_id
    model.config.pad_token_id = proc.tokenizer.pad_token_id
    model.config.vocab_size = model.config.decoder.vocab_size

    def pixels(fn):
        im = Image.open(os.path.join(RAW, fn)).convert('RGB')
        return proc(im, return_tensors='pt').pixel_values[0]

    def label_ids(text):
        ids = proc.tokenizer(text, padding='max_length', max_length=MAXLEN, truncation=True).input_ids
        return [(-100 if t == proc.tokenizer.pad_token_id else t) for t in ids]

    print('caching pixel values...')
    train_px = torch.stack([pixels(fn) for fn, _ in train])
    train_lb = torch.tensor([label_ids(t) for _, t in train])
    test_px = torch.stack([pixels(fn) for fn, _ in test])

    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)

    @torch.no_grad()
    def evaluate():
        model.eval()
        ids = model.generate(test_px.to(dev), max_new_tokens=MAXLEN)
        preds = [''.join(p.lower().split()) for p in proc.batch_decode(ids, skip_special_tokens=True)]
        ex = char_ok = char_tot = 0
        for (fn, true), p in zip(test, preds):
            ex += (p == true)
            char_ok += sum(a == b for a, b in zip(p, true))
            char_tot += len(true)
        return char_ok / char_tot, ex / len(test), preds

    n = len(train)
    best = -1.0
    for ep in range(args.epochs):
        model.train()
        perm = torch.randperm(n)
        tot = 0.0
        for i in range(0, n, args.batch):
            idx = perm[i:i + args.batch]
            loss = model(pixel_values=train_px[idx].to(dev), labels=train_lb[idx].to(dev)).loss
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += loss.item()
        if (ep + 1) % 3 == 0 or ep == args.epochs - 1:
            ca, ea, preds = evaluate()
            flag = ''
            if ea >= best:
                best = ea
                os.makedirs(OUT, exist_ok=True)
                model.save_pretrained(OUT)
                proc.save_pretrained(OUT)
                flag = ' *'
            print(f'ep {ep + 1:3d}  loss {tot:.3f}  test_char {ca:.3f}  test_exact {ea:.3f}{flag}')

    ca, ea, preds = evaluate()
    print(f'\nbest test_exact {best:.3f} -> {OUT}')
    print('sample predictions:')
    for (fn, true), p in list(zip(test, preds))[:12]:
        print(f'  {fn}  true {true}  pred {p}  {"OK" if p == true else ""}')


if __name__ == '__main__':
    main()
