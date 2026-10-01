"""Kemo 2.0 public models loaded from the pinned protocol artifact."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

_ROOT = Path(__file__).resolve().parents[1]
_LOCK_PATH = _ROOT / "vendor" / "kemo-protocol.lock.json"
_LOCK = json.loads(_LOCK_PATH.read_text(encoding="utf-8"))
_ARTIFACT = _LOCK_PATH.parent / _LOCK["artifact"]
if hashlib.sha256(_ARTIFACT.read_bytes()).hexdigest() != _LOCK["sha256"]:
    raise RuntimeError("Kemo 协议制品摘要不匹配，拒绝加载")
if str(_ARTIFACT) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT))

from provider.protocol.enums import StreamEventType  # noqa: E402
from provider.protocol.models import *  # noqa: E402,F403
from provider.protocol.models import (  # noqa: E402
    AssetDescriptor,
    ProtocolModel,
    UnifiedError,
    Usage,
)
from provider.protocol.streaming import ProviderStreamEvent  # noqa: E402

StrictModel = ProtocolModel
ErrorObject = UnifiedError
UsageMeasurement = Measurement


class CompatibleModelItem(ProtocolModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default=0, ge=0)
    owned_by: str


class CompatibleModelList(ProtocolModel):
    object: Literal["list"] = "list"
    data: list[CompatibleModelItem]


# Keep the gateway's historical name while making the pinned artifact the sole
# source of the wire event contract.
SSEEvent = ProviderStreamEvent
