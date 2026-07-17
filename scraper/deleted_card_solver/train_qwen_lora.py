#!/usr/bin/env python3
"""LoRA fine-tune Qwen2.5-VL-3B-Instruct to read STAMPED "DELETED" voter cards —
the heavyweight reader, brought in to beat Donut on the OCCLUDED fields (name,
relation_name). A 3B vision-language model has a much stronger language prior than
Donut's small BART decoder, and reading a character UNDER the stamp is fundamentally
a language-prior task (infer the covered glyph from context) — so this is the model
most likely to lift name/relation_name.

Same JSONL loader / target format / per-field eval as train_donut.py so results are
directly comparable (SAME val split via seed 0). Target = <s_field>val</s_field>...
as the assistant turn; prompt tokens are masked in the loss (standard SFT).

Run on the GPU (in the vLLM container, install peft first):
    pip install -q peft accelerate
    python3 -m deleted_card_solver.train_qwen_lora --epochs 8 --grad-accum 8
Saves the merged best-by-val model to data/deleted_card_model/qwen_lora/.
"""
import argparse, json, os, re, time, warnings, random
warnings.filterwarnings('ignore')
import torch
from PIL import Image

HERE = os.path.dirname(__file__)
HARVEST = os.path.join(HERE, '..', 'data', '_bench', 'fr_deleted_harvest')
LABELS = os.path.join(HARVEST, 'labels.jsonl')
OUT = os.path.join(HERE, '..', 'data', 'deleted_card_model', 'qwen_lora')
NAME = 'Qwen/Qwen2.5-VL-3B-Instruct'
FIELDS = ['name', 'relation_type', 'relation_name', 'house_number', 'age', 'gender']
MAXNEW = 160
PROMPT = ("This is one voter card with a diagonal DELETED stamp over it. Read the text "
          "under the stamp and output EXACTLY these tags, nothing else: "
          "<s_name>..</s_name><s_relation_type>father|husband|mother|other</s_relation_type>"
          "<s_relation_name>..</s_relation_name><s_house_number>..</s_house_number>"
          "<s_age>..</s_age><s_gender>Male|Female</s_gender>")


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
    ap.add_argument('--epochs', type=int, default=8)
    ap.add_argument('--val-n', type=int, default=40)
    ap.add_argument('--lr', type=float, default=1e-4)
    ap.add_argument('--grad-accum', type=int, default=8)
    ap.add_argument('--eval-every', type=int, default=1)
    ap.add_argument('--max-pixels', type=int, default=1024 * 28 * 28)   # cap image tokens (cards are small)
    ap.add_argument('--limit-train', type=int, default=0)   # >0 = cap train set (for smoke)
    args = ap.parse_args()

    from transformers import AutoProcessor
    try:
        from transformers import Qwen2_5_VLForConditionalGeneration as QwenVL
    except ImportError:
        from transformers import AutoModelForImageTextToText as QwenVL
    from peft import LoraConfig, get_peft_model

    recs = [json.loads(l) for l in open(LABELS)]
    pool = [r for r in recs if r.get('flag') != 'no_stamp_exclude']
    clean = [r for r in pool if not r['uncertain'] and not r.get('review')]
    random.seed(0); random.shuffle(clean)                               # SAME split as Donut/Pix2Struct
    val = clean[:args.val_n]
    val_crops = {r['crop'] for r in val}
    train = [r for r in pool if r['crop'] not in val_crops]
    if args.limit_train: train = train[:args.limit_train]
    dev = 'cuda'
    print(f'model={args.model}  train={len(train)}  val={len(val)}  grad_accum={args.grad_accum}', flush=True)

    proc = AutoProcessor.from_pretrained(args.model, max_pixels=args.max_pixels)
    model = QwenVL.from_pretrained(args.model, torch_dtype=torch.bfloat16).to(dev)
    model.config.use_cache = False
    lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, task_type='CAUSAL_LM',
                      target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj',
                                      'gate_proj', 'up_proj', 'down_proj'])
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()
    model.gradient_checkpointing_enable()

    def img(r):
        return Image.open(os.path.join(HARVEST, r['crop'])).convert('RGB')

    def build_train(r):
        """input_ids/pixel tensors + labels with the PROMPT masked (-100)."""
        msgs = [{'role': 'user', 'content': [{'type': 'image'}, {'type': 'text', 'text': PROMPT}]}]
        prompt_text = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        full_text = prompt_text + target_seq(r) + '<|im_end|>\n'
        image = img(r)
        prompt_enc = proc(text=[prompt_text], images=[image], return_tensors='pt')
        full_enc = proc(text=[full_text], images=[image], return_tensors='pt')
        labels = full_enc.input_ids.clone()
        plen = prompt_enc.input_ids.shape[1]
        labels[:, :plen] = -100
        labels[labels == proc.tokenizer.pad_token_id] = -100
        full_enc['labels'] = labels
        return full_enc   # natural shapes: input_ids [1,seq], pixel_values [patches,dim], image_grid_thw [1,3]

    @torch.no_grad()
    def predict(r):
        msgs = [{'role': 'user', 'content': [{'type': 'image'}, {'type': 'text', 'text': PROMPT}]}]
        text = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        enc = proc(text=[text], images=[img(r)], return_tensors='pt').to(dev)
        model.config.use_cache = True
        gen = model.generate(**enc, max_new_tokens=MAXNEW, do_sample=False)
        model.config.use_cache = False
        out = proc.batch_decode(gen[:, enc.input_ids.shape[1]:], skip_special_tokens=True)[0]
        return out

    def evaluate():
        model.eval()
        per_field = {f: 0 for f in FIELDS}; cer_num = cer_den = 0; samples = []
        for r in val:
            pv, tv = parse_seq(predict(r)), parse_seq(target_seq(r))
            for f in FIELDS:
                per_field[f] += (pv[f] == tv[f]); cer_num += lev(pv[f], tv[f]); cer_den += max(len(tv[f]), 1)
            samples.append((pv, tv))
        acc = {f: per_field[f] / len(val) for f in FIELDS}
        return acc, 1 - cer_num / max(cer_den, 1), samples

    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=args.lr)
    best = -1.0; t0 = time.time()
    for ep in range(args.epochs):
        model.train(); random.shuffle(train); tot = 0.0
        opt.zero_grad()
        for i, r in enumerate(train):
            b = build_train(r)
            b = {k: (v.to(dev) if torch.is_tensor(v) else v) for k, v in b.items()}
            loss = model(**b).loss / args.grad_accum
            loss.backward(); tot += loss.item() * args.grad_accum
            if (i + 1) % args.grad_accum == 0 or i == len(train) - 1:
                torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
                opt.step(); opt.zero_grad()
        line = f'ep {ep + 1:3d}/{args.epochs}  loss {tot / len(train):.4f}  ({(time.time() - t0) / 60:.1f}m)'
        if (ep + 1) % args.eval_every == 0 or ep == args.epochs - 1:
            acc, char_acc, _ = evaluate()
            avg = sum(acc.values()) / len(acc); flag = ''
            if char_acc >= best:
                best = char_acc
                os.makedirs(OUT, exist_ok=True)
                model.save_pretrained(OUT); proc.save_pretrained(OUT)   # LoRA adapter
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
