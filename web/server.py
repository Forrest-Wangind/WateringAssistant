"""FastAPI 服务：远程 REST 控制（钉钉消息走 Stream 模式，不走 HTTP 回调）。"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from assistant import WateringAssistant

logger = logging.getLogger(__name__)


class WaterRequest(BaseModel):
    channel_id: int
    duration_sec: Optional[int] = None


def create_app(assistant: WateringAssistant) -> FastAPI:
    app = FastAPI(title="多路浇花助理", version="1.0.0")

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/api/status")
    def status():
        return {"channels": assistant.valve.status()}

    @app.post("/api/water")
    def water(req: WaterRequest):
        try:
            return assistant.water_channel(req.channel_id, req.duration_sec, notify=False)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/report")
    def report():
        r = assistant.take_photo_and_report(push=False)
        return {
            "photo": str(r.photo_path),
            "report": str(r.report_path),
            "score": r.score,
            "markdown": r.markdown,
        }

    @app.post("/api/photo")
    def photo():
        return {"photo": assistant.take_photo_only(push=False)}

    return app
