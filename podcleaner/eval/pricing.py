"""Model metadata from OpenRouter: price per token, context length, output cap.

Used to turn token counts into a cost per episode, and to size chunks and output
budgets for a model.  The catalogue is fetched from the public ``/models`` endpoint (no
key needed) and cached under ``var/cache/openrouter-models.json`` for a day.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

__all__ = ["ModelInfo", "ModelCatalogue"]

CATALOGUE_URL = "https://openrouter.ai/api/v1/models"


@dataclass(frozen=True)
class ModelInfo:
    id: str
    prompt_price: float          # USD per token
    completion_price: float      # USD per token
    context_length: int
    max_completion_tokens: Optional[int]
    supports_response_format: bool

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return prompt_tokens * self.prompt_price + completion_tokens * self.completion_price

    @property
    def free(self) -> bool:
        return self.prompt_price == 0 and self.completion_price == 0


class ModelCatalogue:
    def __init__(self, models: Dict[str, ModelInfo]) -> None:
        self.models = models

    @classmethod
    def load(cls, cache_path: Path, *, max_age_seconds: float = 86400, offline: bool = False) -> "ModelCatalogue":
        data = None
        if cache_path.exists() and (offline or time.time() - cache_path.stat().st_mtime < max_age_seconds):
            data = json.loads(cache_path.read_text())
        if data is None and not offline:
            import requests

            r = requests.get(CATALOGUE_URL, timeout=30)
            r.raise_for_status()
            data = r.json()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data))
        if data is None:
            return cls({})
        models: Dict[str, ModelInfo] = {}
        for m in data.get("data", []):
            pricing = m.get("pricing") or {}
            try:
                pin = float(pricing.get("prompt", 0) or 0)
                pout = float(pricing.get("completion", 0) or 0)
            except (TypeError, ValueError):
                continue
            top = m.get("top_provider") or {}
            models[m["id"]] = ModelInfo(
                id=m["id"], prompt_price=pin, completion_price=pout,
                context_length=int(m.get("context_length") or 0),
                max_completion_tokens=top.get("max_completion_tokens"),
                supports_response_format="response_format" in (m.get("supported_parameters") or []),
            )
        return cls(models)

    def get(self, model_id: str) -> Optional[ModelInfo]:
        return self.models.get(model_id)

    def cost(self, model_id: str, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
        info = self.get(model_id)
        return None if info is None else info.cost(prompt_tokens, completion_tokens)
