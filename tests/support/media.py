"""Offline binary samples and public media request helpers."""
from __future__ import annotations

from email.parser import BytesParser
from email.policy import default
import hashlib
import io
import wave


def wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)
    return output.getvalue()


WAV = wav()
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\0" * 32
TXT = b"KEMO-FILE-5821"


def multipart(request):
    envelope = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
    return {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
            for part in envelope.iter_parts()}


def call(client, token, identity, operation, modalities, *, blocks=(), stream=False, config=None):
    return client.post("/model/responses", headers={
        "Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0", "Idempotency-Key": identity,
    }, json={"protocol_version": "2.0", "request_id": identity, "model": "media", "stream": stream,
             "metadata": {"capability": operation},
             "input": [{"type": "message", "id": "msg_user", "role": "user",
                        "content": [{"type": "text", "text": "Say hello."}, *blocks]}],
             "output": {"modalities": modalities, **(config or {})}})


def assert_asset(client, token, block, content):
    headers = {"Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0"}
    assert block["checksum_sha256"] == hashlib.sha256(content).hexdigest()
    download = client.get(f"/assets/{block['asset_id']}/content", headers=headers)
    assert download.status_code == 200, download.text
    assert download.content == content
