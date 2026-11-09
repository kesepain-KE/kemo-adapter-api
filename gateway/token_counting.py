"""Offline estimates. Provider usage remains authoritative, including cache billing."""
from __future__ import annotations

from functools import lru_cache
import base64
import gzip
import json
from pathlib import Path
import unicodedata

import tiktoken
from tiktoken.load import load_tiktoken_bpe
from tiktoken.model import encoding_name_for_model
from tokenizers import Tokenizer
from sentencepiece import SentencePieceProcessor
import yaml

from core.models import Usage, UsageMeasurement


ROOT = Path(__file__).with_name("tokenizers")
TOKENIZER_CATALOG = yaml.safe_load((ROOT / "catalog.yaml").read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def tokenizer(name):
    if name in TOKENIZER_CATALOG:
        config = TOKENIZER_CATALOG[name]
        if config["kind"] == "openai":
            return tokenizer("o200k_base")
        path = ROOT / config["file"]
        if config["kind"] == "huggingface":
            return Tokenizer.from_str(gzip.decompress(path.read_bytes()).decode())
        if config["kind"] == "sentencepiece":
            return SentencePieceProcessor(model_file=str(path))
        if config["kind"] == "anthropic":
            value = json.loads(gzip.decompress(path.read_bytes()))
            ranks = {}
            for line in value["bpe_ranks"].splitlines():
                if line.strip():
                    _, offset, *tokens = line.split()
                    ranks.update({base64.b64decode(token): int(offset) + index for index, token in enumerate(tokens)})
            return tiktoken.Encoding(name=name, pat_str=value["pat_str"], special_tokens=value["special_tokens"],
                                     mergeable_ranks=ranks)
        if config["kind"] == "tiktoken":
            return tiktoken.Encoding(name=name, pat_str=config["pat_str"], special_tokens=config["special_tokens"],
                                     mergeable_ranks=load_tiktoken_bpe(str(path)))
    config = yaml.safe_load((ROOT / "openai.yaml").read_text(encoding="utf-8"))[name]
    return tiktoken.Encoding(
        name=name, mergeable_ranks=load_tiktoken_bpe(str(ROOT / f"{name}.tiktoken")), **config,
    )


def select_tokenizer(setting, model):
    model = model.rsplit("/", 1)[-1]
    if setting not in {"auto", "openai"}:
        return setting
    if setting in {"auto", "openai"}:
        try:
            name = encoding_name_for_model(model)
        except KeyError:
            name = None
        if name in {"cl100k_base", "o200k_base"}:
            return name
        if setting == "openai":
            return "o200k_base"
    for name, config in TOKENIZER_CATALOG.items():
        if any(model.lower().startswith(prefix) for prefix in config["patterns"]):
            return name
    return "deepseek"


def count_text(text, name):
    encoder = tokenizer(name)
    if isinstance(encoder, Tokenizer):
        return len(encoder.encode(text, add_special_tokens=False).ids)
    if isinstance(encoder, SentencePieceProcessor):
        return len(encoder.encode(text, out_type=int))
    if name == "claude-legacy":
        text = unicodedata.normalize("NFKC", text)
        return len(encoder.encode(text, allowed_special="all"))
    return len(encoder.encode(text, disallowed_special=()))


def contains_media(value):
    if isinstance(value, list):
        return any(contains_media(item) for item in value)
    if isinstance(value, dict):
        return value.get("type") in {"image", "audio", "video", "file"} or any(
            contains_media(item) for item in value.values()
        )
    return False


def output_text(value):
    if isinstance(value, list):
        return "\n".join(output_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(
            str(item) if key in {"text", "arguments", "refusal"} and item is not None
            else output_text(item)
            for key, item in value.items()
            if key in {"text", "arguments", "refusal", "content", "output", "responses", "result"}
        )
    return value if isinstance(value, str) else ""


def estimate_usage(usage, request, result, *, model, setting, task, system_prompt=""):
    """Fill absent text counts for successful attempts, never cache or reasoning counts."""
    if task not in {"llm", "embedding"} or request is None:
        return usage, None
    if usage.input_tokens is not None and usage.output_tokens is not None:
        return usage, None
    name = select_tokenizer(setting, model)
    values = usage.model_dump()
    estimated = list(usage.measurement.estimated_fields)
    if usage.input_tokens is None and not contains_media(request):
        content = {k: request[k] for k in ("system_prompt", "input", "inputs", "tools") if request.get(k)}
        values["input_tokens"] = count_text(
            system_prompt + json.dumps(content, ensure_ascii=False, separators=(",", ":")), name,
        )
        estimated.append("input_tokens")
    if usage.output_tokens is None and result is not None:
        output = result.get("output") or result.get("responses")
        if task == "embedding":
            values["output_tokens"] = 0
        elif output is not None and not contains_media(output):
            values["output_tokens"] = count_text(output_text(output), name)
        if values["output_tokens"] is not None:
            estimated.append("output_tokens")
    if not estimated:
        return usage, None
    if values["input_tokens"] is not None and values["output_tokens"] is not None:
        values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
        estimated.append("total_tokens")
    values["measurement"] = UsageMeasurement(
        mode="mixed" if usage.measurement.exact_fields else "estimated", exact=False,
        exact_fields=usage.measurement.exact_fields, estimated_fields=estimated,
    )
    return Usage.model_validate(values), name
