#!/usr/bin/env python3
"""Fine-tune Pix2Struct (google/pix2struct-base) to read STAMPED "DELETED" voter
cards — a second whole-card autoregressive reader to compare with / ensemble
against Donut (see ../data/deleted_card_model/RESULT.md).

Why Pix2Struct as the 2nd engine: same image->text autoregressive class as Donut,
but a DIFFERENT vision front-end — it renders the image into variable-resolution
patches (native aspect-ratio handling, no fixed resize) rather than Donut's Swin.
Different architecture -> errors uncorrelated with Donut -> agree=accept /
disagree=flag gives a free per-field confidence signal, and per-field routing can
pick whichever engine wins each field.

Same JSONL loader / target format / per-field eval as train_donut.py, so results
are directly comparable. Target = <s_field>val</s_field>... (plain text here).

Run on the GPU (in the vLLM container):
    python3 -m deleted_card_solver.train_pix2struct --epochs 30 --batch 2
Saves best-by-val-char-acc to data/deleted_card_model/pix2struct/.
"""
import argparse, json, os, re, time, warnings, random
warnings.filterwarnings('ignore')
import torch
from PIL import Image
from transformers import Pix2StructProcessor, Pix2StructForConditionalGeneration

HERE = os.path.dirname(__file__)
HARVEST = os.path.join(HERE, '..', 'data', '_bench', 'fr_deleted_harvest')
LABELS = os.path.join(HARVEST, 'labels.jsonl')
OUT = os.path.join(HERE, '..', 'data', 'deleted_card_model', 'pix2struct')
NAME = 'google/pix2struct-base'
FIELDS = ['name', 'relation_type', 'relation_name', 'house_number', 'age', 'gender']
MAXLEN = 160


def field_val(r, f):
    v = r.get(f)
    return '' if v is None else str(v)


def target_seq(r):
    return ''.join(f'<s_{f}>{field_val(r, f)}</s_{f}>' for f in FIELDS)


def parse_seq(seq):
    out = {}
    for f in FIELDS:
        m = re.search(rf'<s_{f}>(.*?)</s_{f}>', seq, re.DOTALL)
        out[f] = (m.group(1).strip() if m else '')
    return out


def lev(a, b):
    if a == b: return 0
    if not a: return len(b)
    if not b: return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default=NAME)
    ap.add_argument('--epochs', type=int, default=30)
    ap.add_argument('--batch', type=int, default=2)
    ap.add_argument('--val-n', type=int, default=40)
    ap.add_argument('--lr', type=float, default=1e-4)
    ap.add_argument('--eval-every', type=int, default=3)
    ap.add_argument('--max-patches', type=int, default=1024)
    args = ap.parse_args()

    recs = [json.loads(l) for l in open(LABELS)]
    pool = [r for r in recs if r.get('flag') != 'no_stamp_exclude']       # 368 trainable
    clean = [r for r in pool if not r['uncertain'] and not r.get('review')]
    random.seed(0); random.shuffle(clean)                                  # SAME seed as Donut -> same val split
    val = clean[:args.val_n]
    val_crops = {r['crop'] for r in val}
    train = [r for r in pool if r['crop'] not in val_crops]
    dev = 'cuda' if torch.cuda.is_available() else ('mps' if torch.backends.mps.is_available() else 'cpu')
    print(f'model={args.model}  train={len(train)}  val={len(val)}  device={dev}  max_patches={args.max_patches}', flush=True)

    proc = Pix2StructProcessor.from_pretrained(args.model)
    model = Pix2StructForConditionalGeneration.from_pretrained(args.model).to(dev)

    def image_inputs(r):
        im = Image.open(os.path.join(HARVEST, r['crop'])).convert('RGB')
        enc = proc(images=im, return_tensors='pt', max_patches=args.max_patches)
        return enc.flattened_patches[0], enc.attention_mask[0]

    def label_ids(r):
        ids = proc.tokenizer(target_seq(r), add_special_tokens=True, max_length=MAXLEN,
                             padding='max_length', truncation=True).input_ids
        return [(-100 if t == proc.tokenizer.pad_token_id else t) for t in ids]

    print('caching patches (train+val)...', flush=True)
    tp, ta = zip(*[image_inputs(r) for r in train])
    train_fp, train_am = torch.stack(tp), torch.stack(ta)
    train_lb = torch.tensor([label_ids(r) for r in train])
    vp, va = zip(*[image_inputs(r) for r in val])
    val_fp, val_am = torch.stack(vp), torch.stack(va)
    print(f'cached. train_fp={tuple(train_fp.shape)}', flush=True)

    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)

    @torch.no_grad()
    def evaluate():
        model.eval()
        preds = []
        for i in range(0, len(val), 4):
            ids = model.generate(flattened_patches=val_fp[i:i + 4].to(dev),
                                 attention_mask=val_am[i:i + 4].to(dev),
                                 max_new_tokens=MAXLEN, no_repeat_ngram_size=3, num_beams=1)
            preds += proc.tokenizer.batch_decode(ids, skip_special_tokens=True)
        per_field = {f: 0 for f in FIELDS}
        cer_num = cer_den = 0
        samples = []
        for r, raw in zip(val, preds):
            pv, tv = parse_seq(raw), parse_seq(target_seq(r))
            for f in FIELDS:
                per_field[f] += (pv[f] == tv[f])
                cer_num += lev(pv[f], tv[f]); cer_den += max(len(tv[f]), 1)
            samples.append((pv, tv))
        acc = {f: per_field[f] / len(val) for f in FIELDS}
        return acc, 1 - cer_num / max(cer_den, 1), samples

    n = len(train)
    best = -1.0
    t0 = time.time()
    for ep in range(args.epochs):
        model.train()
        perm = torch.randperm(n)
        tot = 0.0
        for i in range(0, n, args.batch):
            idx = perm[i:i + args.batch]
            loss = model(flattened_patches=train_fp[idx].to(dev),
                         attention_mask=train_am[idx].to(dev),
                         labels=train_lb[idx].to(dev)).loss
            opt.zero_grad(); loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step(); tot += loss.item()
        line = f'ep {ep + 1:3d}/{args.epochs}  loss {tot / (n / args.batch):.4f}  ({(time.time() - t0) / 60:.1f}m)'
        if (ep + 1) % args.eval_every == 0 or ep == args.epochs - 1:
            acc, char_acc, _ = evaluate()
            avg = sum(acc.values()) / len(acc)
            flag = ''
            if char_acc >= best:
                best = char_acc
                os.makedirs(OUT, exist_ok=True)
                model.save_pretrained(OUT); proc.save_pretrained(OUT)
                flag = ' *SAVED'
            line += ('  char_acc %.3f  avg_field %.3f | ' % (char_acc, avg)) + \
                    ' '.join('%s=%.2f' % (f[:4], acc[f]) for f in FIELDS) + flag
        print(line, flush=True)

    acc, char_acc, samples = evaluate()
    print(f'\nBEST val char_acc {best:.3f}  ->  {OUT}', flush=True)
    print('final per-field exact-match:', flush=True)
    for f in FIELDS:
        print(f'  {f:15} {acc[f]:.3f}', flush=True)
    print('\nsample val predictions:', flush=True)
    for pv, tv in samples[:10]:
        print('  PRED', {f: pv[f] for f in FIELDS}, flush=True)
        print('  TRUE', {f: tv[f] for f in FIELDS}, flush=True)
        print(flush=True)


if __name__ == '__main__':
    main()
