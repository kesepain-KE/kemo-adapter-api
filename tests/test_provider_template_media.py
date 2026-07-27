from __future__ import annotations

import pytest

from template.provider.media import MediaSourceError, parse_media_block


def test_parses_kemo_agent_inline_image() -> None:
    parsed = parse_media_block(
        {
            "type": "image",
            "mime_type": "image/jpeg",
            "detail": "high",
            "source": {"kind": "inline_base64", "data": "YWJj"},
        },
        expected_type="image",
        location="input[0].content[1]",
    )
    assert parsed.kind == "inline_base64"
    assert parsed.detail == "high"
    assert parsed.as_data_url() == "data:image/jpeg;base64,YWJj"


def test_parses_url_without_inventing_source_media_type() -> None:
    parsed = parse_media_block(
        {
            "type": "image",
            "source": {"kind": "url", "uri": "https://example.test/image.png"},
        },
        expected_type="image",
        location="input[0].content[0]",
    )
    assert parsed.uri == "https://example.test/image.png"
    assert parsed.mime_type is None


@pytest.mark.parametrize(
    "block",
    [
        {"type": "image", "source": {"kind": "inline_base64", "data": ""}},
        {"type": "image", "source": {"kind": "url", "uri": ""}},
        {
            "type": "image",
            "mime_type": "audio/wav",
            "source": {"kind": "inline_base64", "data": "YWJj"},
        },
        {
            "type": "image",
            "source": {"kind": "data_url", "uri": "data:audio/wav;base64,YWJj"},
        },
    ],
)
def test_rejects_empty_or_mismatched_media(block: dict[str, object]) -> None:
    with pytest.raises(MediaSourceError):
        parse_media_block(
            block,
            expected_type="image",
            location="input[0].content[0]",
        )
