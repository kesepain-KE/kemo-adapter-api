"""Public names are URL-safe aliases; upstream model identifiers stay unchanged."""
from __future__ import annotations

import re


TEMPLATE_VENDORS = {
    "dashscope": "qwen", "zhipu": "z-ai", "moonshot": "moonshot",
    "volcengine": "bytedance", "byteplus": "bytedance", "qianfan": "baidu",
    "hunyuan": "tencent", "openai-responses": "openai", "openai-embedding": "openai",
    "cohere-rerank": "cohere",
}


def public_model_name(provider, remote, template=None):
    parts = remote.removeprefix("@cf/").split("/")
    vendor = parts.pop(0) if len(parts) > 1 else TEMPLATE_VENDORS.get(template, template or "custom")
    value = f"{template or provider}-{vendor}-{'-'.join(parts)}"
    return re.sub(r"[\s/\\]+", "-", value.strip()).strip("-")[:200]
