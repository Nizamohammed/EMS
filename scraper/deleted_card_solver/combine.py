"""DeletedCardReader — the deleted-card combine (OCR fallback for STAMPED cards).

RapidOCR flags a DELETED card (diagonal stamp occludes the text) -> this reader
recovers the occluded fields: Donut + Pix2Struct each read the whole card; per
field, if they AGREE we accept it (cheap), if they DISAGREE we escalate just that
field to Qwen2.5-VL-3B (the strongest reader). Measured: combine ~0.958 avg-field,
Qwen invoked on ~19% of fields (see data/deleted_card_model/RESULT.md).

Models live in data/deleted_card_model/{donut,pix2struct,qwen_lora}/ (Qwen = a LoRA
adapter on base Qwen/Qwen2.5-VL-3B-Instruct). Everything is imported lazily; Qwen
is only loaded/contacted on the first disagreement.

Qwen backend:
  local -> load base Qwen2.5-VL-3B + the LoRA adapter on this machine (needs `peft`)
  http  -> POST the card to a served Qwen (OpenAI-compatible chat) at qwen_host
  none  -> no Qwen; on disagreement keep Donut's read (field marked in `.flagged`)
"""
from __future__ import annotations
import os
import re

HERE = os.path.dirname(__file__)
DEFAULT_MODEL_DIR = os.path.join(HERE, "..", "data", "deleted_card_model")
FIELDS = ["name", "relation_type", "relation_name", "house_number", "age", "gender"]
QWEN_BASE = "Qwen/Qwen2.5-VL-3B-Instruct"
QWEN_PROMPT = (
    "This is one voter card with a diagonal DELETED stamp over it. Read the text under "
    "the stamp and output EXACTLY these tags, nothing else: "
    "<s_name>..</s_name><s_relation_type>father|husband|mother|other</s_relation_type>"
    "<s_relation_name>..</s_relation_name><s_house_number>..</s_house_number>"
    "<s_age>..</s_age><s_gender>Male|Female</s_gender>"
)


def parse_seq(seq: str) -> dict:
    """Pull each <s_field>value</s_field> out of a generated sequence."""
    out = {}
    for f in FIELDS:
        m = re.search(rf"<s_{f}>(.*?)</s_{f}>", seq or "", re.DOTALL)
        out[f] = (m.group(1).strip() if m else "")
    return out


def _load_donut_processor(path):
    """DonutProcessor, robust to transformers version skew. The saved tokenizer_config
    names the SLOW XLMRobertaTokenizer (needs a sentencepiece file that was never saved),
    so from_pretrained fails on some versions -> rebuild the fast tokenizer from
    tokenizer.json (the model was trained/saved with these exact vocab ids)."""
    from transformers import DonutProcessor
    try:
        proc = DonutProcessor.from_pretrained(path)
    except Exception:
        from transformers import DonutImageProcessor, XLMRobertaTokenizerFast
        ip = DonutImageProcessor.from_pretrained(path)
        tok = XLMRobertaTokenizerFast(tokenizer_file=os.path.join(path, "tokenizer.json"),
                                      bos_token="<s>", eos_token="</s>", unk_token="<unk>",
                                      pad_token="<pad>", cls_token="<s>", sep_token="</s>",
                                      mask_token="<mask>")
        proc = DonutProcessor(image_processor=ip, tokenizer=tok)
    proc.image_processor.size = {"height": 448, "width": 1024}
    proc.image_processor.do_align_long_axis = False
    return proc


def _load_pix2struct_processor(path):
    """Pix2StructProcessor, robust to transformers version skew (the saved
    tokenizer_config's extra_special_tokens list trips some versions) -> rebuild
    the fast T5 tokenizer from tokenizer.json."""
    from transformers import Pix2StructProcessor
    try:
        return Pix2StructProcessor.from_pretrained(path)
    except Exception:
        from transformers import Pix2StructImageProcessor, T5TokenizerFast
        ip = Pix2StructImageProcessor.from_pretrained(path)
        tok = T5TokenizerFast(tokenizer_file=os.path.join(path, "tokenizer.json"),
                              eos_token="</s>", unk_token="<unk>", pad_token="<pad>")
        return Pix2StructProcessor(image_processor=ip, tokenizer=tok)


class _Donut:
    """Whole-card autoregressive reader (naver-clova-ix/donut-base fine-tune)."""

    def __init__(self, path):
        import torch
        from transformers import VisionEncoderDecoderModel
        self.torch = torch
        self.dev = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
        self.proc = _load_donut_processor(path)
        self.model = VisionEncoderDecoderModel.from_pretrained(path).to(self.dev).eval()
        self.prompt = self.proc.tokenizer("<s_deletedcard>", add_special_tokens=False,
                                           return_tensors="pt").input_ids.to(self.dev)

    def read(self, pil):
        px = self.proc(pil.convert("RGB"), return_tensors="pt").pixel_values.to(self.dev)
        with self.torch.no_grad():
            ids = self.model.generate(px, decoder_input_ids=self.prompt, max_length=160,
                                      eos_token_id=self.proc.tokenizer.eos_token_id,
                                      pad_token_id=self.proc.tokenizer.pad_token_id,
                                      no_repeat_ngram_size=3)
        return parse_seq(self.proc.tokenizer.decode(ids[0], skip_special_tokens=False))


class _Pix2Struct:
    """Whole-card autoregressive reader (google/pix2struct-base fine-tune)."""

    def __init__(self, path):
        import torch
        from transformers import Pix2StructForConditionalGeneration
        self.torch = torch
        self.dev = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
        self.proc = _load_pix2struct_processor(path)
        self.model = Pix2StructForConditionalGeneration.from_pretrained(path).to(self.dev).eval()

    def read(self, pil):
        enc = self.proc(images=pil.convert("RGB"), return_tensors="pt", max_patches=1024)
        with self.torch.no_grad():                       # one image at a time (batched gen is broken)
            ids = self.model.generate(flattened_patches=enc.flattened_patches.to(self.dev),
                                      attention_mask=enc.attention_mask.to(self.dev),
                                      max_new_tokens=160, no_repeat_ngram_size=3, num_beams=1)
        return parse_seq(self.proc.tokenizer.batch_decode(ids, skip_special_tokens=True)[0])


class _QwenLocal:
    """Qwen2.5-VL-3B + LoRA adapter, loaded locally (needs `peft`)."""

    def __init__(self, adapter_path, base=QWEN_BASE, max_pixels=1024 * 28 * 28):
        import torch
        from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration
        from peft import PeftModel
        self.torch = torch
        self.dev = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
        self.proc = AutoProcessor.from_pretrained(base, max_pixels=max_pixels)
        dtype = torch.bfloat16 if self.dev != "cpu" else torch.float32
        m = Qwen2_5_VLForConditionalGeneration.from_pretrained(base, torch_dtype=dtype).to(self.dev)
        self.model = PeftModel.from_pretrained(m, adapter_path).eval()

    def read(self, pil):
        msgs = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": QWEN_PROMPT}]}]
        text = self.proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        enc = self.proc(text=[text], images=[pil.convert("RGB")], return_tensors="pt").to(self.dev)
        with self.torch.no_grad():
            gen = self.model.generate(**enc, max_new_tokens=160, do_sample=False)
        out = self.proc.batch_decode(gen[:, enc.input_ids.shape[1]:], skip_special_tokens=True)[0]
        return parse_seq(out)


class _QwenHttp:
    """Qwen2.5-VL served over an OpenAI-compatible chat endpoint (e.g. vLLM on the GPU box)."""

    def __init__(self, host, model="qwen2.5vl", api_key="EMPTY", timeout=120):
        self.host = host.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def read(self, pil):
        import base64, io, json, urllib.request
        buf = io.BytesIO()
        pil.convert("RGB").save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        payload = {"model": self.model, "temperature": 0.0, "messages": [{"role": "user", "content": [
            {"type": "text", "text": QWEN_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]}
        req = urllib.request.Request(self.host + "/v1/chat/completions",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = json.loads(r.read())
        return parse_seq(body["choices"][0]["message"]["content"])


class DeletedCardReader:
    """Donut + Pix2Struct agree=accept / disagree=escalate to Qwen. Returns CardRecord-shaped fields."""

    def __init__(self, qwen_backend: str = "http", qwen_host: str | None = None,
                 model_dir: str | None = None):
        md = model_dir or DEFAULT_MODEL_DIR
        self.donut = _Donut(os.path.join(md, "donut"))
        self.pix2struct = _Pix2Struct(os.path.join(md, "pix2struct"))
        self.qwen_backend = qwen_backend
        self.qwen_host = qwen_host
        self._md = md
        self._qwen = None                                # lazy: built on the first disagreement

    def _qwen_reader(self):
        if self._qwen is None:
            if self.qwen_backend == "http":
                if not self.qwen_host:
                    raise ValueError("deleted_backend=http needs --qwen-host")
                self._qwen = _QwenHttp(self.qwen_host)
            elif self.qwen_backend == "local":
                self._qwen = _QwenLocal(os.path.join(self._md, "qwen_lora"))
            else:
                self._qwen = False                       # 'none' -> no escalation
        return self._qwen

    def read(self, pil) -> dict:
        d = self.donut.read(pil)
        p = self.pix2struct.read(pil)
        q = None
        flagged = []
        merged = {}
        for f in FIELDS:
            if d[f] == p[f]:
                merged[f] = d[f]                          # agree -> accept (cheap)
            else:
                qr = self._qwen_reader()
                if qr:
                    if q is None:
                        q = qr.read(pil)                 # one Qwen call per card, cached
                    merged[f] = q[f]
                else:
                    merged[f] = d[f]                     # no Qwen -> keep Donut, flag as low-confidence
                    flagged.append(f)
        # translate model field names to the pipeline's CardRecord field names
        out = {"full_name": merged["name"], "relation_type": merged["relation_type"],
               "relation_name": merged["relation_name"], "house_number": merged["house_number"],
               "age": merged["age"], "gender": merged["gender"]}
        out["flagged"] = flagged                          # fields kept low-confidence (no Qwen available)
        return out
