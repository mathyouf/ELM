import itertools
import os
import random
import re
import shutil
from typing import Dict, Iterator, List

import numpy as np
import torch
from codex_execute import (TimeoutException, create_tempdir, reliability_guard,
                           swallow_io, time_limit)
from constants import PROJECT_PATH
from tqdm import tqdm
from transformers import GPT2TokenizerFast

from codegen.modelling_codegen import CodeGenForCausalLM


def set_seed(seed, deterministic=True):
    random.seed(seed)
    # os.environ['PYTHONHASHSEED'] = str(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.backends.cudnn.deterministic = deterministic
        torch.backends.cudnn.benchmark = not deterministic
        # torch.use_deterministic_algorithms(deterministic)


def create_model(ckpt_path, fp16=True):
    if fp16:
        return CodeGenForCausalLM.from_pretrained(ckpt_path,
                                                  torch_dtype=torch.float16,
                                                  low_cpu_mem_usage=True)
    else:
        return CodeGenForCausalLM.from_pretrained(ckpt_path)


def create_tokenizer():
    t = GPT2TokenizerFast.from_pretrained('gpt2')
    t.max_model_input_sizes['gpt2'] = 1e20
    return t


def include_whitespace(t, n_min=2, n_max=20, as_special_tokens=False):
    t.add_tokens([' ' * n for n in reversed(range(n_min, n_max))],
                 special_tokens=as_special_tokens)
    return t


def include_tabs(t, n_min=2, n_max=20, as_special_tokens=False):
    t.add_tokens(['\t' * n for n in reversed(range(n_min, n_max))],
                 special_tokens=as_special_tokens)
    return t


def create_custom_gpt2_tokenizer():
    t = create_tokenizer()
    t = include_whitespace(t=t, n_min=2, n_max=32, as_special_tokens=False)
    t = include_tabs(t=t, n_min=2, n_max=10, as_special_tokens=False)
    return t


def truncate(completion):

    def find_re(string, pattern, start_pos):
        m = pattern.search(string, start_pos)
        return m.start() if m else -1

    terminals = [
        re.compile(r, re.MULTILINE)
        for r in
        [
            '^#',
            re.escape('<|endoftext|>'),
            "^'''",
            '^"""',
            '\n\n\n'
        ]
    ]
    prints = list(re.finditer('^print', completion, re.MULTILINE))
    if len(prints) > 1:
        completion = completion[:prints[1].start()]

    defs = list(re.finditer('^def', completion, re.MULTILINE))
    if len(defs) > 1:
        completion = completion[:defs[1].start()]

    start_pos = 0

    terminals_pos = [pos for pos in [find_re(completion, terminal, start_pos)
                                     for terminal in terminals] if pos != -1]
    if len(terminals_pos) > 0:
        return completion[:min(terminals_pos)]
    else:
        return completion


def model_setup(cfg):
    set_seed(cfg.seed, deterministic=True)
    device = torch.device(cfg.device)
    use_fp16 = True
    if (not cfg.fp16 or device.type == "cpu"):
        use_fp16 = False

    if cfg.model.startswith("codegen-16B"):
        use_fp16 = True

    tokenizer = create_custom_gpt2_tokenizer()
    tokenizer.padding_side = 'left'
    tokenizer.pad_token = cfg.pad_token

    ckpt_path = PROJECT_PATH / "checkpoints" / cfg.model
    if cfg.gpus > 1:
        model = torch.nn.DataParallel(create_model(ckpt_path, fp16=use_fp16),
                                      device_ids=list(range(cfg.gpus))).to(device)
    else:
        model = create_model(ckpt_path, fp16=use_fp16).to(device)
    return model, tokenizer


def sample(cfg, model, tokenizer, batch, add_def=False):
    """Run a model on a batch of contexts for a particular task."""
    device = torch.device(cfg.device)

    input_ids_len = batch["input_ids"].shape[1]
    assert input_ids_len < cfg.max_length
    with torch.no_grad():
        batch = batch.to(device)
        if cfg.gpus > 1:
            tokens = model.module.generate(
                **batch,
                do_sample=True,
                num_return_sequences=cfg.batch_size,
                temperature=cfg.temp,
                max_length=input_ids_len + cfg.max_length,
                top_p=cfg.top_p,
                pad_token_id=cfg.pad_token,
                use_cache=True,
            )
        else:
            tokens = model.generate(
                **batch,
                do_sample=True,
                num_return_sequences=cfg.batch_size,
                temperature=cfg.temp,
                max_length=input_ids_len + cfg.max_length,
                top_p=cfg.top_p,
                pad_token_id=cfg.pad_token,
                use_cache=True,
            )
        # "input_ids_len:" removes the prompt
        # - 1 adds in "def"
        if add_def:
            input_ids_len -= 1
        text = tokenizer.batch_decode(tokens[:, input_ids_len:, ...])
    return text
