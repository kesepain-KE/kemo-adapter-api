from __future__ import annotations

import mimetypes

from core.provider_contract import ProviderEvent as Event, ProviderEventKind as Kind
from gateway.adapters.builtin.common import unsupported
from gateway.adapters.builtin.media import download, native_id, result
from gateway.adapters.builtin.openai_responses import OpenAIResponses
from gateway.adapters.contract import AdapterManifest
from gateway.transport import upstream_error


class OpenAIFiles(OpenAIResponses):
    manifest = AdapterManifest(
        id="openai-files", name="Responses File Generation", operations=("llm",),
        tools=False, streaming=True, media={"input": ("text", "image", "file"), "output": ("text", "file")},
        probe_operation="file_generation",
        endpoints={"llm": "/responses", "discovery": "/models"},
        body_fields=OpenAIResponses.manifest.body_fields,
        usage_semantics=OpenAIResponses.manifest.usage_semantics,
    )

    def encode(self, request, context, model, stream=False):
        if request.metadata.get("capability") not in {None, "conversation"}:
            raise unsupported("文件生成使用 conversation；生成文件需包含 file 输出模态")
        body = super().encode(request, context, model, stream=stream)
        if "file" in request.output.modalities:
            body["tools"] = [{"type": "code_interpreter", "container": {"type": "auto"}}]
            body["tool_choice"] = "required"
            config = request.output.file
            if config and (config.filename or config.mime_type):
                body["input"].append({"role": "user", "content": [{
                    "type": "input_text",
                    "text": "Create a downloadable file. Requested filename: "
                    + (config.filename or "output") + "; MIME type: " + (config.mime_type or "unspecified"),
                }]})
        return body

    async def execute(self, request, context, model, upstream, headers):
        raw = await self.transport.json(upstream, "/responses", headers, body=self.encode(request, context, model))
        if raw.get("status") not in {"completed", "incomplete"}:
            raise upstream_error(code="UPSTREAM_INVALID_RESPONSE", retryable=False)
        complete = self.result(raw)
        if "file" not in request.output.modalities:
            return complete
        references = []
        for item in raw.get("output", []):
            for block in item.get("content", []):
                for annotation in block.get("annotations", []):
                    if annotation.get("type") == "container_file_citation":
                        reference = (native_id(annotation.get("container_id")), native_id(annotation.get("file_id")),
                                     annotation.get("filename"))
                        if reference not in references:
                            references.append(reference)
        if not references:
            raise upstream_error(code="UPSTREAM_FILE_NOT_GENERATED", retryable=False)
        blocks = []
        for container, identity, filename in references:
            if not isinstance(filename, str) or not filename:
                raise upstream_error(code="UPSTREAM_INVALID_FILE_RESPONSE", retryable=False)
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            blocks.append(await download(
                self.transport, upstream, f"/containers/{container}/files/{identity}/content", headers,
                context, "file", filename=filename, mime_type=mime,
                metadata={"provider_file_id": identity, "provider_container_id": container},
            ))
        if "text" not in request.output.modalities:
            complete.output = []
        if complete.output and complete.output[-1]["type"] == "message":
            complete.output[-1]["content"].extend(blocks)
        else:
            complete.output.extend(result(blocks).output)
        if raw["status"] == "completed" and complete.incomplete_details == {"reason": "empty_output"}:
            complete.status, complete.incomplete_details = "completed", None
        return complete

    async def stream(self, request, context, model, upstream, headers):
        if "file" not in request.output.modalities:
            async for event in super().stream(request, context, model, upstream, headers):
                yield event
            return
        complete = await self.execute(request, context, model, upstream, headers)
        for item in complete.output:
            for index, block in enumerate(item.get("content", [])):
                if block["type"] == "file":
                    yield Event(Kind.MEDIA_COMPLETED, item_id=item["id"], content_index=index, item=item)
        yield Event(Kind.USAGE, usage=complete.usage)
        yield Event(Kind.INCOMPLETE if complete.status == "incomplete" else Kind.COMPLETED, result=complete)
