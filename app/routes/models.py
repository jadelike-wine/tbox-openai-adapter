"""
GET /v1/models

Returns the list of "models" exposed by this adapter.
In v1 this is a static list driven by ADAPTER_MODEL_ID from config.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.schemas.openai import ModelCard, ModelList

router = APIRouter()


@router.get("/v1/models", response_model=ModelList)
async def list_models(settings: Settings = Depends(get_settings)) -> ModelList:
    """Return OpenAI-compatible model list.

    Only one model is advertised in v1 (the adapter model).
    Add more entries here if you later expose multiple TBox workflows
    under different model IDs.
    """
    card = ModelCard(
        id=settings.adapter_model_id,
        object="model",
        created=int(time.time()),
        owned_by="tbox",
    )
    return ModelList(object="list", data=[card])
