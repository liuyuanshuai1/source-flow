from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.analyzer import analyze_repository, analyzer_name
from app.demo_data import sample_reports
from app.models import AnalyzeRequest, HealthResponse, OpportunityReport


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="OpenRadar MVP", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(analyzer=analyzer_name())


@app.get("/api/opportunities", response_model=list[OpportunityReport])
async def opportunities() -> list[OpportunityReport]:
    return sample_reports()


@app.post("/api/analyze", response_model=OpportunityReport)
async def analyze(request: AnalyzeRequest) -> OpportunityReport:
    try:
        return await analyze_repository(str(request.repository_url))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
