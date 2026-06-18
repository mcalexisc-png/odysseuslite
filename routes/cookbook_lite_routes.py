"""Lite Cookbook API — Odysseus Lite overlay (Phase 3).

A tiny, honest first-run model recommender for the non-technical lite user.
Surfaces a hardware-aware recommendation and an OPT-IN Ollama bootstrap.

Endpoints (all under /api/lite/cookbook):
  GET  /recommend       -> hardware-tiered model recommendation
  GET  /ollama          -> Ollama reachability + installed models
  POST /autopull        -> pull a default model (requires confirm + opt-in env)
"""
import logging

from fastapi import APIRouter, Request

from services import cookbook_lite

logger = logging.getLogger(__name__)


def setup_cookbook_lite_routes() -> APIRouter:
    router = APIRouter(prefix="/api/lite/cookbook", tags=["lite-cookbook"])

    @router.get("/recommend")
    async def recommend():
        """Detected hardware + curated model recommendation for this machine."""
        return cookbook_lite.recommend()

    @router.get("/ollama")
    async def ollama():
        """Ollama reachability and whether an autopull is currently allowed."""
        return cookbook_lite.can_autopull()

    @router.post("/autopull")
    async def autopull(request: Request):
        """Pull a default model via Ollama. Never silent: needs confirm=true in
        the body AND LITE_AUTOPULL_MODEL=true. Returns a clear refusal otherwise."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        model = (body or {}).get("model")
        confirm = bool((body or {}).get("confirm"))
        result = cookbook_lite.autopull(model=model, confirm=confirm)
        return result

    return router
