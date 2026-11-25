"""제품 API + 화면 + A2A — create_app(data_dir, *, engine=…) (외부 의존은 주입, 테스트에서 가짜로)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from video_reader import a2a_tasks
from video_reader.engine import Engine, default_engine
from video_reader.service import Service, ServiceError

STATIC = Path(__file__).resolve().parent / "static"


def create_app(data_dir: Path, *, engine: Optional[Engine] = None, autostart: bool = True,
               stt_interval: float = 2.0) -> FastAPI:
    svc = Service(Path(data_dir), engine or default_engine(), autostart=autostart, stt_interval=stt_interval)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        svc.stop()

    app = FastAPI(title="video_reader", lifespan=lifespan)
    app.state.svc = svc

    @app.exception_handler(ServiceError)
    def _err(_: Request, exc: ServiceError):
        return JSONResponse({"error": exc.kind, "message": exc.message}, status_code=exc.status)

    @app.get("/api/status")
    def status(fresh: bool = False):
        return svc.status(fresh)

    @app.get("/api/jobs")
    def jobs():
        return {"jobs": svc.jobs()}

    @app.post("/api/jobs")
    def create(body: dict = Body(...)):
        job, existed = svc.create_job(str(body.get("ref") or ""), backend=str(body.get("backend") or "local"),
                                      visual=str(body.get("visual") or "auto"), lang=body.get("lang"),
                                      out_lang=str(body.get("outLang") or "ko"), origin="screen", force=bool(body.get("force")),
                                      purpose=str(body.get("purpose") or "record"), focus=str(body.get("focus") or ""),
                                      transcribe=str(body.get("transcribe") or "auto"), stt_fresh=bool(body.get("sttFresh")))
        return JSONResponse({"job": job, "existed": existed}, status_code=200 if existed else 201)

    @app.get("/api/jobs/{jid}")
    def detail(jid: str):
        return svc.job_detail(jid)

    @app.get("/api/jobs/{jid}/transcript")
    def transcript(jid: str, section: Optional[int] = None):
        return {"segments": svc.transcript(jid, section)}

    @app.post("/api/jobs/{jid}/cancel")
    def cancel(jid: str):
        return svc.cancel(jid)

    @app.post("/api/jobs/{jid}/retry")
    def retry(jid: str, body: dict = Body(default={})):
        return svc.retry(jid, body.get("backend"))

    @app.delete("/api/jobs/{jid}")
    def delete(jid: str):
        svc.delete(jid)
        return {"ok": True}

    @app.patch("/api/jobs/{jid}/sections/{idx}")
    def edit(jid: str, idx: int, body: dict = Body(...)):
        return svc.edit_section(jid, idx, title=body.get("title"), points=body.get("points"), gist=body.get("gist"))

    @app.post("/api/jobs/{jid}/sections/{idx}/regenerate")
    def regen(jid: str, idx: int, body: dict = Body(default={})):
        return svc.regenerate_section(jid, idx, body.get("backend"))

    @app.post("/api/jobs/{jid}/rewrite")
    def rewrite(jid: str, body: dict = Body(...)):
        return svc.rewrite(jid, str(body.get("purpose") or ""), body.get("backend"))

    @app.post("/api/jobs/{jid}/overview/regenerate")
    def regen_ov(jid: str, body: dict = Body(default={})):
        return svc.regenerate_overview(jid, body.get("backend"))

    @app.get("/api/jobs/{jid}/summary.md")
    def md(jid: str):
        return PlainTextResponse(svc.markdown(jid), media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{jid}.md"'})

    @app.get("/api/jobs/{jid}/frames/{name}")
    def frame(jid: str, name: str):
        return FileResponse(svc.frame_path(jid, name), media_type="image/jpeg")

    @app.get("/api/jobs/{jid}/media")
    def media(jid: str):
        return FileResponse(svc.media_path(jid))

    a2a_tasks.mount(app, svc)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    if STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
