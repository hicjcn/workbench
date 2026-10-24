"""Shared free-model validation, used by the verifier and persistent Runner."""
from decimal import Decimal, InvalidOperation
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import runtime
from .workspace import WorkbenchError


def openrouter_catalog_model(model, fetch=urlopen):
    runtime.openrouter_config(model)
    request = Request("https://openrouter.ai/api/v1/models", headers={"User-Agent": "agent-workbench/0.1"})
    try:
        with fetch(request, timeout=30) as response:
            content = response.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise WorkbenchError("OpenRouter model catalog exceeds the verification limit")
        catalog = json.loads(content)
    except HTTPError as error:
        raise WorkbenchError(f"OpenRouter public model catalog returned HTTP {error.code}; no task submitted") from None
    except (OSError, ValueError):
        raise WorkbenchError("Cannot read OpenRouter public model catalog through the configured network; no task submitted") from None
    if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
        raise WorkbenchError("OpenRouter returned an invalid model catalog; no task submitted")
    model_id = model.removeprefix("openrouter/")
    entry = next((item for item in catalog["data"] if isinstance(item, dict) and item.get("id") == model_id), None)
    if entry is None:
        raise WorkbenchError("Selected OpenRouter free model is not in the public catalog")
    pricing = entry.get("pricing")
    if not isinstance(pricing, dict) or not {"prompt", "completion"} <= pricing.keys():
        raise WorkbenchError("OpenRouter model pricing is incomplete; no task submitted")
    try:
        prices = [Decimal(str(value)) for value in pricing.values()]
        free = all(price.is_finite() and price == 0 for price in prices)
    except (InvalidOperation, ValueError):
        free = False
    if not free:
        raise WorkbenchError("OpenRouter model pricing is not entirely zero; no task submitted")
    return {"id": model_id, "pricing": pricing}

