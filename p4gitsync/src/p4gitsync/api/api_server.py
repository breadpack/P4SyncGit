import logging
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("p4gitsync.api")

app = FastAPI(title="P4GitSync API")

_last_trigger_time: datetime | None = None
_trigger_secret: str = ""


class TriggerPayload(BaseModel):
    changelist: int
    user: str


class HealthResponse(BaseModel):
    status: str
    last_trigger_time: str | None


def configure(secret: str) -> None:
    global _trigger_secret
    _trigger_secret = secret


@app.post("/api/trigger", status_code=202)
async def trigger(
    payload: TriggerPayload,
    x_trigger_secret: str = Header(default=""),
):
    global _last_trigger_time

    if _trigger_secret and x_trigger_secret != _trigger_secret:
        raise HTTPException(status_code=403, detail="Invalid trigger secret")

    _last_trigger_time = datetime.now()
    logger.info("트리거 수신: CL %d, user=%s", payload.changelist, payload.user)

    return {"status": "accepted", "changelist": payload.changelist}


@app.get("/api/health")
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        last_trigger_time=_last_trigger_time.isoformat() if _last_trigger_time else None,
    )
