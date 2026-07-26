from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import get_registry
from api.middleware import Principal, authenticated_principal
from core.models import ModelCapabilities
from core.registry import ProviderRegistry


router = APIRouter(tags=["models"])


@router.get("/model/capabilities", response_model=ModelCapabilities)
async def capabilities(
    model: str = Query(min_length=1),
    _: Principal = Depends(authenticated_principal),
    registry: ProviderRegistry = Depends(get_registry),
) -> ModelCapabilities:
    try:
        package = registry.resolve(model)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"未知模型: {model}") from exc
    return await package.capabilities(model)
