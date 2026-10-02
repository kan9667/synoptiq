"""FastAPI application for frozen historical replay assets."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from bust.api import store
from bust.api.schemas import EvaluationResponse, HealthResponse, RegionResponse, ReplayResponse

app = FastAPI(
    title="Synoptiq Replay API",
    version="0.1.0",
    description="Read-only historical replay API.",
)


def _not_found(kind: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"message": f"Unknown {kind}", "available_inits": store.available_inits()},
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(data_mode=store.load_store()["data_mode"])


@app.get("/v1/replay", response_model=ReplayResponse)
def get_replay(
    init: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    lead: int = Query(..., ge=1, le=10),
) -> ReplayResponse:
    payload = store.replay(init, lead)
    if payload is None:
        raise _not_found("replay initialization or lead")
    return ReplayResponse.model_validate(payload)


@app.get("/v1/region/{region_id}", response_model=RegionResponse)
def get_region(
    region_id: str,
    init: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    lead: int = Query(..., ge=1, le=10),
) -> RegionResponse:
    payload = store.region(init, lead, region_id)
    if payload is None:
        raise _not_found("region or replay")
    return RegionResponse.model_validate(payload)


@app.get("/v1/evaluation", response_model=EvaluationResponse)
def get_evaluation() -> EvaluationResponse:
    return EvaluationResponse.model_validate(store.evaluation())


web_dist = Path(__file__).resolve().parents[3] / "web" / "dist"
if web_dist.is_dir():
    app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
