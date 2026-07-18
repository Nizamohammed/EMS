#!/usr/bin/env python3
"""Fine-tune TrOCR-base-printed to read STAMPED "DELETED" voter cards — the one
case RapidOCR (and general VLMs) fail (the diagonal stamp occludes the text).
Target = the 6 soft fields RapidOCR garbles, serialized as one string:

    name | relation_type | relation_name | house_number | age | gender

(EPIC/serial/reason are NOT targets — RapidOCR reads those fine from the
un-stamped corner in production.) Ground truth = agent-vision labels in
labels.jsonl. Run from scraper/:

    .venv/bin/python -m deleted_card_solver.train_deleted_trocr --epochs 40

Saves the best-by-validation checkpoint to data/deleted_card_model/trocr/.
Val set is drawn only from cards with NO uncertain/review flags, so the
reported accuracy is against reliable labels.
"""
import argparse, json, os, random, sys, time, warnings
warnings.filterwarnings('ignore')
import torch
from PIL import Image
from transformers import TrOCRProcessor, VisionEncoderDecoderModel

HERE = os.path.dirname(__file__)
HARVEST = os.path.join(HERE, '..', 'data', '_bench', 'fr_deleted_harvest')
LABELS = os.path.join(HARVEST, 'labels.jsonl')
OUT = os.path.join(HERE, '..', 'data', 'deleted_card_model', 'trocr')
NAME = 'microsoft/trocr-base-printed'
SEP = ' | '
FIELDS = ['name', 'relation_type', 'relation_name', 'house_number', 'age', 'gender']
MAXLEN = 128


def target_str(r):
    age = '' if r.get('age') is None else str(r['age'])
    return SEP.join([r['name'], r['relation_type'], r['relation_name'],
                     r['house_number'], age, r.get('gender') or ''])


def lev(a, b):
    """Levenshtein distance for CER."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default=NAME,
                    help='HF checkpoint. base=trocr-base-printed (heavy, ~7GB, needs a GPU or a >16GB Mac); '
                         'small=microsoft/trocr-small-printed (fits a 16GB Mac without swap-thrash).')
    ap.add_argument('--epochs', type=int, default=40)
    ap.add_argument('--batch', type=int, default=4)
    ap.add_argument('--val-n', type=int, default=40)
    ap.add_argument('--lr', type=float, default=5e-5)
    ap.add_argument('--eval-every', type=int, default=2)
    args = ap.parse_args()

    recs = [json.loads(l) for l in open(LABELS)]
    pool = [r for r in recs if r.get('flag') != 'no_stamp_exclude']   # 368 trainable
    clean = [r for r in pool if not r['uncertain'] and not r.get('review')]
    random.seed(0)
    random.shuffle(clean)
    val = clean[:args.val_n]
    val_crops = {r['crop'] for r in val}
    train = [r for r in pool if r['crop'] not in val_crops]
    dev = 'mps' if torch.backends.mps.is_available() else 'cpu'
    print(f'model={args.model}  train={len(train)}  val={len(val)}  device={dev}', flush=True)

    proc = TrOCRProcessor.from_pretrained(args.model)
    model = VisionEncoderDecoderModel.from_pretrained(args.model).to(dev)
    sid = getattr(model.generation_config, 'decoder_start_token_id', None)
    model.config.decoder_start_token_id = sid if sid is not None else proc.tokenizer.cls_token_id
    model.config.pad_token_id = proc.tokenizer.pad_token_id
    model.config.vocab_size = model.config.decoder.vocab_size

    def pixels(r):
        im = Image.open(os.path.join(HARVEST, r['crop'])).convert('RGB')
        return proc(im, return_tensors='pt').pixel_values[0]

    def label_ids(text):
        ids = proc.tokenizer(text, padding='max_length', max_length=MAXLEN, truncation=True).input_ids
        return [(-100 if t == proc.tokenizer.pad_token_id else t) for t in ids]

    print('caching pixel values (train+val)...', flush=True)
    train_px = torch.stack([pixels(r) for r in train])                  # on CPU
    train_lb = torch.tensor([label_ids(target_str(r)) for r in train])
    val_px = torch.stack([pixels(r) for r in val])
    print(f'cached. train_px={tuple(train_px.shape)}', flush=True)

    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)

    @torch.no_grad()
    def evaluate():
        model.eval()
        preds = []
        for i in range(0, len(val), 8):                                 # chunked to spare MPS memory
            ids = model.generate(val_px[i:i + 8].to(dev), max_new_tokens=MAXLEN)
            preds += proc.batch_decode(ids, skip_special_tokens=True)
        per_field = {f: 0 for f in FIELDS}
        cer_num = cer_den = 0
        for r, p in zip(val, preds):
            truth = target_str(r)
            cer_num += lev(p.strip(), truth); cer_den += len(truth)
            pv = (p.split(SEP) + [''] * 6)[:6]
            tv = (truth.split(SEP) + [''] * 6)[:6]
            for f, a, b in zip(FIELDS, pv, tv):
                per_field[f] += (a.strip() == b.strip())
        acc = {f: per_field[f] / len(val) for f in FIELDS}
        char_acc = 1 - cer_num / max(cer_den, 1)
        return acc, char_acc, preds

    n = len(train)
    best = -1.0
    t0 = time.time()
    for ep in range(args.epochs):
        model.train()
        perm = torch.randperm(n)
        tot = 0.0
        for i in range(0, n, args.batch):
            idx = perm[i:i + args.batch]
            loss = model(pixel_values=train_px[idx].to(dev), labels=train_lb[idx].to(dev)).loss
            opt.zero_grad(); loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            tot += loss.item()
        line = f'ep {ep + 1:3d}/{args.epochs}  loss {tot / (n / args.batch):.4f}  ({(time.time() - t0) / 60:.1f}m)'
        if (ep + 1) % args.eval_every == 0 or ep == args.epochs - 1:
            acc, char_acc, preds = evaluate()
            avg = sum(acc.values()) / len(acc)
            flag = ''
            if char_acc >= best:                                        # save best by char-accuracy
                best = char_acc
                os.makedirs(OUT, exist_ok=True)
                model.save_pretrained(OUT); proc.save_pretrained(OUT)
                flag = ' *SAVED'
            fieldstr = ' '.join(f'{f[:4]}={acc[f]:.2f}' for f in FIELDS)
            line += f'  char_acc {char_acc:.3f}  avg_field {avg:.3f} | {fieldstr}{flag}'
        print(line, flush=True)

    acc, char_acc, preds = evaluate()
    print(f'\nBEST val char_acc {best:.3f}  ->  {OUT}', flush=True)
    print('\nfinal per-field exact-match:', flush=True)
    for f in FIELDS:
        print(f'  {f:15} {acc[f]:.3f}', flush=True)
    print('\nsample val predictions (pred || truth):', flush=True)
    for r, p in list(zip(val, preds))[:12]:
        print(f'  PRED : {p.strip()}', flush=True)
        print(f'  TRUE : {target_str(r)}', flush=True)
        print(flush=True)


if __name__ == '__main__':
    main()
