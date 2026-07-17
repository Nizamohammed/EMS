#!/usr/bin/env python3
"""Fine-tune Donut (naver-clova-ix/donut-base) to read STAMPED "DELETED" voter
cards — reading the WHOLE card (multi-line block), unlike TrOCR (single-line).

Why Donut: OCR-free document-understanding model = a Swin document-scale image
encoder + an AUTOREGRESSIVE decoder (the TrOCR property that lets it infer a
character under the stamp, which RapidOCR's literal CTC cannot). It emits the
fields as a structured sequence, so no line-segmentation step is needed.

Fixes the two things that sank the TrOCR v1 (see ../data/deleted_card_model/RESULT.md):
  - multi-line block  -> Donut is a document model, not single-line.
  - 606x269 squished to 384x384 -> here we keep aspect ratio (do_align_long_axis
    off + a wide input size), so the small text stays legible.

Target = the 6 soft fields RapidOCR garbles, as a Donut field sequence:
  <s_name>..</s_name><s_relation_type>..</s_relation_type>..<s_gender>..</s_gender>
(EPIC/serial/reason are NOT targets — RapidOCR reads those from the un-stamped
corner in production.) Ground truth = agent-vision labels in labels.jsonl.

Run on the AWS GPU (donut-base + Adam will thrash a 16GB Mac):
    python -m deleted_card_solver.train_donut --epochs 30 --batch 4

Saves the best-by-val-char-accuracy checkpoint to data/deleted_card_model/donut/.
Val is drawn only from cards with NO uncertain/review flags (reliable labels).
"""
import argparse, json, os, re, time, warnings
warnings.filterwarnings('ignore')
import torch
from PIL import Image
from transformers import DonutProcessor, VisionEncoderDecoderModel, VisionEncoderDecoderConfig

HERE = os.path.dirname(__file__)
HARVEST = os.path.join(HERE, '..', 'data', '_bench', 'fr_deleted_harvest')
LABELS = os.path.join(HARVEST, 'labels.jsonl')
OUT = os.path.join(HERE, '..', 'data', 'deleted_card_model', 'donut')
NAME = 'naver-clova-ix/donut-base'
FIELDS = ['name', 'relation_type', 'relation_name', 'house_number', 'age', 'gender']
TASK = '<s_deletedcard>'
MAXLEN = 160


def field_val(r, f):
    v = r.get(f)
    return '' if v is None else str(v)


def target_seq(r):
    return ''.join(f'<s_{f}>{field_val(r, f)}</s_{f}>' for f in FIELDS)


def parse_seq(seq):
    """Pull each field value out of a generated Donut sequence (missing -> '')."""
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
    ap.add_argument('--batch', type=int, default=4)
    ap.add_argument('--val-n', type=int, default=40)
    ap.add_argument('--lr', type=float, default=3e-5)
    ap.add_argument('--eval-every', type=int, default=3)
    ap.add_argument('--image-h', type=int, default=448)      # keep card aspect ~2.29 (cards are 606x269 = 2.26)
    ap.add_argument('--image-w', type=int, default=1024)
    args = ap.parse_args()

    recs = [json.loads(l) for l in open(LABELS)]
    pool = [r for r in recs if r.get('flag') != 'no_stamp_exclude']       # 368 trainable
    clean = [r for r in pool if not r['uncertain'] and not r.get('review')]
    import random; random.seed(0); random.shuffle(clean)
    val = clean[:args.val_n]
    val_crops = {r['crop'] for r in val}
    train = [r for r in pool if r['crop'] not in val_crops]
    dev = 'cuda' if torch.cuda.is_available() else ('mps' if torch.backends.mps.is_available() else 'cpu')
    print(f'model={args.model}  train={len(train)}  val={len(val)}  device={dev}  img={args.image_h}x{args.image_w}', flush=True)

    # load with a wide, aspect-preserving input size (rebuilds the Swin patch grid)
    config = VisionEncoderDecoderConfig.from_pretrained(args.model)
    config.encoder.image_size = [args.image_h, args.image_w]
    config.decoder.max_length = MAXLEN
    proc = DonutProcessor.from_pretrained(args.model)
    proc.image_processor.size = {'height': args.image_h, 'width': args.image_w}
    proc.image_processor.do_align_long_axis = False          # cards are landscape; do NOT rotate to portrait
    model = VisionEncoderDecoderModel.from_pretrained(args.model, config=config).to(dev)

    # register our field tags + task token, then grow the decoder embeddings
    new_tokens = [t for f in FIELDS for t in (f'<s_{f}>', f'</s_{f}>')] + [TASK]
    proc.tokenizer.add_special_tokens({'additional_special_tokens': new_tokens})
    model.decoder.resize_token_embeddings(len(proc.tokenizer))
    model.config.pad_token_id = proc.tokenizer.pad_token_id
    model.config.decoder_start_token_id = proc.tokenizer.convert_tokens_to_ids(TASK)
    eos_id = proc.tokenizer.eos_token_id

    def pixels(r):
        im = Image.open(os.path.join(HARVEST, r['crop'])).convert('RGB')
        return proc(im, return_tensors='pt').pixel_values[0]

    def label_ids(r):
        seq = target_seq(r) + proc.tokenizer.eos_token
        ids = proc.tokenizer(seq, add_special_tokens=False, max_length=MAXLEN,
                             padding='max_length', truncation=True).input_ids
        return [(-100 if t == proc.tokenizer.pad_token_id else t) for t in ids]

    print('caching pixel values (train+val)...', flush=True)
    train_px = torch.stack([pixels(r) for r in train])                    # on CPU
    train_lb = torch.tensor([label_ids(r) for r in train])
    val_px = torch.stack([pixels(r) for r in val])
    print(f'cached. train_px={tuple(train_px.shape)}', flush=True)

    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    prompt = proc.tokenizer(TASK, add_special_tokens=False, return_tensors='pt').input_ids

    @torch.no_grad()
    def evaluate():
        model.eval()
        preds = []
        for i in range(0, len(val), 4):
            b = val_px[i:i + 4].to(dev)
            ids = model.generate(b, decoder_input_ids=prompt.repeat(b.size(0), 1).to(dev),
                                 max_length=MAXLEN, eos_token_id=eos_id,
                                 pad_token_id=proc.tokenizer.pad_token_id,
                                 no_repeat_ngram_size=3, num_beams=1)
            preds += [proc.tokenizer.decode(x, skip_special_tokens=False) for x in ids]
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
            loss = model(pixel_values=train_px[idx].to(dev), labels=train_lb[idx].to(dev)).loss
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
