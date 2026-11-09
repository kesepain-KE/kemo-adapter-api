import asyncio
import unicodedata

from gateway.plugins.sdk import Plugin, text_capabilities, text_result


class TextPlugin(Plugin):
    async def models_list(self, params):
        return [text_capabilities("local-text", "transform")]

    async def models_probe(self, params):
        text = self.transform("Kemo")
        return {"available": bool(text), "operation": self.config.get("mode", "uppercase")}

    def transform(self, text):
        text = unicodedata.normalize("NFC", text)
        mode = self.config.get("mode", "uppercase")
        value = {"uppercase": str.upper, "lowercase": str.lower, "title": str.title,
                 "reverse": lambda value: value[::-1]}[mode](text)
        return self.config.get("prefix", "") + value

    async def models_execute(self, params):
        text = "\n".join(block["text"] for item in params["request"]["input"]
                         for block in item.get("content", []) if block.get("type") == "text")
        return text_result(self.transform(text))

    async def models_stream(self, params):
        result = await self.models_execute(params)
        text = result["output"][0]["content"][0]["text"]
        for offset in range(0, len(text), 64):
            yield {"kind": "output_text.delta", "item_id": "msg_plugin", "content_index": 0,
                   "delta": text[offset:offset + 64]}
            await asyncio.sleep(0)
        yield {"kind": "provider.completed", "result": result}

    async def ui_actions_invoke(self, params):
        return {"status": "ready", "result": self.transform("Kemo Gateway")}

    async def diagnostics(self, params):
        return {"status": "ready", "mode": self.config.get("mode", "uppercase"),
                "usage": "unavailable", "network": "not used"}


def create_plugin():
    return TextPlugin()
