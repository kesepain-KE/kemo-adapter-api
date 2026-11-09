from __future__ import annotations

from gateway.adapters.builtin.openai_chat import OpenAIChat
from gateway.adapters.contract import AdapterManifest
from gateway.auth import BEARER, NO_AUTH


class PerplexityAgent(OpenAIChat):
    manifest = AdapterManifest(
        id="perplexity-agent", name="Perplexity Sonar", operations=("llm",),
        streaming=True, discovery=False, auth_schemes=(BEARER, NO_AUTH),
        endpoints={"llm": "/chat/completions"}, structured_output=True,
        usage_semantics="prompt includes provider citation_tokens; output excludes reasoning",
    )
    endpoint = "/chat/completions"

    def encode(self, request, context, model, upstream, stream=False):
        body = super().encode(request, context, model, upstream, stream)
        for field in ("presence_penalty", "frequency_penalty", "logit_bias", "seed", "stream_options"):
            body.pop(field, None)
        return body

    @staticmethod
    def usage(raw):
        if not raw:
            return OpenAIChat.usage(raw)
        usage = dict(raw)
        if isinstance(usage.get("prompt_tokens"), int):
            # TODO(verify): confirm citation accounting on a real Sonar response;
            # only add the provider field, never estimate from citation URLs.
            usage["prompt_tokens"] += usage.get("citation_tokens") or 0
        return OpenAIChat.usage(usage)
