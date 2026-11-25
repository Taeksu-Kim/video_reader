"""서비스 — 상태의 유일한 주인. 제품 API(app.py)와 A2A(a2a_tasks.py)가 같은 함수를 부른다.

작업 하나 = 영상 하나. 일꾼 스레드 하나가 줄의 작업을 차례로 돌린다 (stt·로컬 LLM 이 GPU 를 같이 써서 동시에 돌리지 않는다).
파이프라인은 "아직 없는 것만 만든다" — 그래서 재시작·다시 시도·구간 다시 만들기가 모두 같은 길이다:
  source 없음 → probe / transcript 없음 → 사람 자막, 없으면 stt(같은 messageId 라 재시작해도 이어 기다림)
  sections 없음 → split / pending 구간 → 정리(+화면) / overview 없음 → 개요
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

from video_reader import engine as E
from video_reader import sources, stt_client
from video_reader.engine import Engine, VISUAL_MODES
from video_reader.llm import BACKENDS, LLMError
from video_reader.sources import Source, SourceError
from video_reader.store import Store
from video_reader.stt_client import SttError

log = logging.getLogger("video_reader")
ACTIVE = ("queued", "running")
LABEL = {"local": "로컬 LLM", "claude": "Claude Code"}


class ServiceError(Exception):
    def __init__(self, kind: str, message: str, status: int = 400):
        super().__init__(message)
        self.kind, self.message, self.status = kind, message, status


class Canceled(Exception):
    pass


class Interrupted(Exception):
    """우리 서버가 꺼지는 중 — 작업은 취소가 아니라 다시 켜면 이어서."""


class Service:
    def __init__(self, data_dir: Path, engine: Engine, *, autostart: bool = True, stt_interval: float = 2.0):
        self.data_dir = Path(data_dir)
        self.store = Store(self.data_dir / "video_reader.db")
        self.engine = engine
        self.stt_interval = stt_interval
        self._cancel: dict[str, threading.Event] = {}
        self._wake = threading.Condition()
        self._stop = False
        self._status_cache: tuple[float, dict] = (0.0, {})
        for j in self.store.jobs_by("running"):          # 재시작: 돌던 작업은 줄로 되돌려 이어서
            self.store.update_job(j["id"], status="queued", stage="재시작 후 이어서")
        self.worker: Optional[threading.Thread] = None
        if autostart:
            self.start()

    # ---------- 바깥 상태 ----------

    def status(self, fresh: bool = False) -> dict:
        """의존(LLM 두 쪽·stt·도구)이 켜져 있나. 확인은 동시에 하고 10초 캐시 — 꺼진 의존이 화면을 붙잡지 않게."""
        t, cached = self._status_cache
        if cached and not fresh and time.time() - t < 10:
            return cached
        from concurrent.futures import ThreadPoolExecutor

        def one(name: str) -> dict:
            llm = self.engine.llms.get(name)
            if llm is None:
                return {"ok": False, "why": "설정되지 않음", "vision": False, "visionWhy": ""}
            ok, why = llm.available()
            v_ok, v_why = llm.vision() if ok else (False, why)
            return {"ok": ok, "why": why, "vision": v_ok, "visionWhy": v_why}

        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = {name: ex.submit(one, name) for name in BACKENDS}
            stt_f = ex.submit(self.engine.stt.available)
            out: dict = {"backends": {name: f.result() for name, f in futs.items()}}
            ok, why = stt_f.result()
        out["stt"] = {"ok": ok, "why": why}
        out["tools"] = {"yt-dlp": bool(shutil.which("yt-dlp")), "ffmpeg": bool(shutil.which("ffmpeg"))}
        self._status_cache = (time.time(), out)
        return out

    # ---------- 작업 만들기·조회 ----------

    @staticmethod
    def key_of(ref: str, backend: str, visual: str, lang: Optional[str], out_lang: str, purpose: str = "record",
               focus: str = "", transcribe: str = "auto") -> str:
        kind = sources.kind_of(ref)
        base = f"youtube:{sources.youtube_id(ref)}" if kind == "youtube" and sources.youtube_id(ref) else ref
        return "|".join([base, backend, visual, lang or "", out_lang, purpose, focus] + ([transcribe] if transcribe != "auto" else []))

    def create_job(self, ref: str, *, backend: str = "local", visual: str = "auto", lang: Optional[str] = None,
                   out_lang: str = "ko", origin: str = "screen", caller: Optional[str] = None,
                   force: bool = False, purpose: str = "record", focus: str = "", transcribe: str = "auto",
                   stt_fresh: bool = False) -> tuple[dict, bool]:
        """→ (작업, 이미 있던 것인가). 같은 영상·같은 설정으로 진행 중이거나 끝난 작업이 있으면 그걸 돌려준다 (force 면 새로)."""
        if backend not in BACKENDS:
            raise ServiceError("bad_request", f"backend 는 {', '.join(BACKENDS)}")
        if visual not in VISUAL_MODES:
            raise ServiceError("bad_request", f"visual 은 {', '.join(VISUAL_MODES)}")
        try:
            ref = sources.check_ref(ref)
        except SourceError as exc:
            raise ServiceError(exc.kind, exc.message) from exc
        if purpose not in E.PURPOSES:
            raise ServiceError("bad_request", f"purpose 는 {', '.join(E.PURPOSES)}")
        if transcribe not in ("auto", "stt"):
            raise ServiceError("bad_request", "transcribe 는 auto(사람 자막 먼저) 또는 stt(자막이 있어도 받아 적기)")
        focus = " ".join((focus or "").split())
        if len(focus) > 200:
            raise ServiceError("bad_request", "중점은 200자 안으로 적으세요")
        lang = (lang or "").strip() or None
        key = self.key_of(ref, backend, visual, lang, out_lang, purpose, focus, transcribe)
        if not force and not stt_fresh:
            old = self.store.job_by_key(key)
            if old:
                return self.job_detail(old["id"]), True
        jid = uuid.uuid4().hex[:12]
        self.store.add_job(id=jid, key=key, ref=ref, kind=sources.kind_of(ref), backend=backend, visual=visual, lang=lang,
                           out_lang=out_lang, status="queued", stage="줄에서 기다리는 중", origin=origin, caller=caller,
                           warnings=[], purpose=purpose, focus=focus, transcribe=transcribe, stt_fresh=int(bool(stt_fresh)))
        self._kick()
        return self.job_detail(jid), False

    def jobs(self) -> list[dict]:
        out = []
        for j in self.store.jobs():
            secs = self.store.sections(j["id"])
            cells = ["wait" if s["status"] != "done" else
                     ("lit" if any(f.get("read", True) for f in s["frames"]) else "unread") if s["frames"] else "done" for s in secs]
            out.append({**self._brief(j), "sections": {"total": len(secs), "done": sum(s["status"] == "done" for s in secs),
                                                       "cells": cells}})
        return out

    def _brief(self, j: dict) -> dict:
        src = j["source"] or {}
        return {"id": j["id"], "ref": j["ref"], "kind": j["kind"], "title": src.get("title") or "", "duration": src.get("duration"),
                "status": j["status"], "stage": j["stage"], "progress": j["progress"], "backend": j["backend"],
                "visual": j["visual"], "lang": j["lang"], "origin": j["origin"], "caller": j["caller"], "created": j["created"],
                "updated": j["updated"], "error": j["error"], "errorKind": j["error_kind"],
                "transcriptSource": (j["transcript"] or {}).get("source"), "costUsd": round(j["cost_usd"] or 0, 4),
                "purpose": j["purpose"] or "record", "focus": j["focus"] or "",
                "transcribe": j["transcribe"] or "auto", "sttFresh": bool(j["stt_fresh"])}

    def _job(self, jid: str) -> dict:
        j = self.store.job(jid)
        if not j:
            raise ServiceError("not_found", "작업이 없습니다", 404)
        return j

    def job_detail(self, jid: str, *, with_transcript: bool = False) -> dict:
        j = self._job(jid)
        tr = j["transcript"] or {}
        secs = self.store.sections(jid)
        src = Source.from_dict(j["source"]) if j["source"] else None
        out = {**self._brief(j), "source": j["source"], "overview": j["overview"], "warnings": j["warnings"] or [],
               "structure": j["structure"], "structureLabel": E.structure_label(j["structure"], len(secs)) if secs else "",
               "article": j["article"],
               "mentions": (j["polish"] or {}).get("mentions") if j["polish"] else
                           E.merge_mentions([{"mentions": s["mentions"] or []} for s in secs if s["status"] == "done"]),
               "style": (j["polish"] or {}).get("style"),
               "coverage": {"checked": sum((s["coverage"] or {}).get("checked", 0) for s in secs),
                            "missing": [m for s in secs for m in (s["coverage"] or {}).get("missing", [])]},
               "transcript": {"source": tr.get("source"), "lang": tr.get("lang"), "lines": len(tr.get("segments") or []),
                              "speakers": tr.get("speakers") or {}},
               "stt": j["stt"],
               "sections": [{"idx": s["idx"], "start": s["start"], "end": s["end"], "status": s["status"], "title": s["title"],
                             "label": s["label"], "gist": s["gist"] or "", "mentions": s["mentions"] or [],
                             "coverage": s["coverage"],
                             "points": s["points"] or [], "needsVisual": bool(s["needs_visual"]), "cues": s["cues"] or [],
                             "frames": [{**f, "url": f"/api/jobs/{jid}/frames/{f['file']}"} for f in (s["frames"] or [])],
                             "observations": s["observations"] or [], "backend": s["backend"], "edited": bool(s["edited"]),
                             "error": s["error"], "link": src.link(s["start"]) if src else ""} for s in secs]}
        if with_transcript:
            out["transcript"]["segments"] = tr.get("segments") or []
        return out

    def transcript(self, jid: str, idx: Optional[int] = None) -> list[dict]:
        j = self._job(jid)
        segs = (j["transcript"] or {}).get("segments") or []
        if idx is None:
            return segs
        s = self.store.section(jid, idx)
        if not s:
            raise ServiceError("not_found", "구간이 없습니다", 404)
        return segs[s["first"]:s["last"] + 1]

    def markdown(self, jid: str) -> str:
        j = self._job(jid)
        if not j["source"]:
            raise ServiceError("not_ready", "아직 영상을 읽지 못했습니다", 409)
        secs = [self._sec_view(s) for s in self.store.sections(jid) if s["status"] == "done"]
        tr = j["transcript"] or {}
        return E.to_markdown(Source.from_dict(j["source"]), j["overview"], secs, transcript_source=tr.get("source") or "",
                             lang=tr.get("lang") or "", structure=j["structure"], art=j["article"],
                             mentions=(j["polish"] or {}).get("mentions") if j["polish"] else None)

    @staticmethod
    def _sec_view(s: dict) -> dict:
        return {"start": s["start"], "end": s["end"], "title": s["title"] or "", "gist": s["gist"] or "",
                "points": s["points"] or [], "observations": s["observations"] or [], "mentions": s["mentions"] or [],
                "edited": bool(s["edited"])}

    def work_dir(self, jid: str) -> Path:
        return self.data_dir / "jobs" / jid

    def frame_path(self, jid: str, name: str) -> Path:
        self._job(jid)
        p = (self.work_dir(jid) / "frames" / name).resolve()
        if p.parent != (self.work_dir(jid) / "frames").resolve() or not p.is_file():
            raise ServiceError("not_found", "그림이 없습니다", 404)
        return p

    def media_path(self, jid: str) -> Path:
        j = self._job(jid)
        if j["kind"] != "file" or not Path(j["ref"]).is_file():
            raise ServiceError("not_found", "이 PC 파일로 넣은 작업만 재생할 수 있습니다", 404)
        return Path(j["ref"])

    # ---------- 사람 개입 ----------

    def cancel(self, jid: str) -> dict:
        j = self._job(jid)
        if j["status"] == "queued":
            self.store.update_job(jid, status="canceled", stage="취소됨")
        elif j["status"] == "running":
            self._cancel.setdefault(jid, threading.Event()).set()
            self.store.update_job(jid, stage="취소하는 중")
        else:
            raise ServiceError("not_cancelable", f"진행 중이 아닙니다 ({j['status']})", 409)
        return self.job_detail(jid)

    def retry(self, jid: str, backend: Optional[str] = None) -> dict:
        """실패·취소된 작업을 멈춘 자리부터 다시. backend 를 주면 남은 일은 그쪽으로."""
        j = self._job(jid)
        if j["status"] in ACTIVE:
            raise ServiceError("busy", "이미 진행 중입니다", 409)
        f: dict = {"status": "queued", "stage": "줄에서 기다리는 중", "error": None, "error_kind": None}
        if backend:
            if backend not in BACKENDS:
                raise ServiceError("bad_request", f"backend 는 {', '.join(BACKENDS)}")
            f["backend"] = backend
            f["key"] = self.key_of(j["ref"], backend, j["visual"], j["lang"], j["out_lang"], j["purpose"] or "record", j["focus"] or "", j["transcribe"] or "auto")
        for s in self.store.sections(jid):
            if s["status"] == "failed":
                self.store.update_section(jid, s["idx"], status="pending", error=None)
        self.store.update_job(jid, **f)
        self._kick()
        return self.job_detail(jid)

    def edit_section(self, jid: str, idx: int, *, title: Optional[str] = None, points: Optional[list] = None,
                     gist: Optional[str] = None) -> dict:
        self._job(jid)
        s = self.store.section(jid, idx)
        if not s:
            raise ServiceError("not_found", "구간이 없습니다", 404)
        if s["status"] != "done":
            raise ServiceError("not_ready", "정리가 끝난 구간만 고칠 수 있습니다", 409)
        f: dict = {"edited": 1}
        if title is not None:
            if not title.strip():
                raise ServiceError("bad_request", "제목이 비었습니다")
            f["title"] = title.strip()
        if gist is not None:
            f["gist"] = gist.strip()
        if points is not None:
            clean = []
            for p in points:
                if not isinstance(p, dict) or not str(p.get("text", "")).strip():
                    continue
                t = p.get("t")
                clean.append({"t": E._clamp(t if t is not None else s["start"], s["start"], s["end"]), "text": str(p["text"]).strip(),
                              "sub": [str(x).strip() for x in p.get("sub") or [] if str(x).strip()]})
            if not clean:
                raise ServiceError("bad_request", "요점이 하나는 있어야 합니다")
            f["points"] = clean
        self.store.update_section(jid, idx, **f)
        return self.job_detail(jid)

    def regenerate_section(self, jid: str, idx: int, backend: Optional[str] = None) -> dict:
        j = self._job(jid)
        if j["status"] in ACTIVE:
            raise ServiceError("busy", "작업이 진행 중입니다 — 끝난 뒤에", 409)
        if not self.store.section(jid, idx):
            raise ServiceError("not_found", "구간이 없습니다", 404)
        if backend and backend not in BACKENDS:
            raise ServiceError("bad_request", f"backend 는 {', '.join(BACKENDS)}")
        self.store.update_section(jid, idx, status="pending", backend=backend or j["backend"], error=None, edited=0)
        self.store.update_job(jid, polish=None)
        self.store.update_job(jid, status="queued", stage=f"구간 {idx + 1} 다시 만들기 기다리는 중", error=None, error_kind=None)
        self._kick()
        return self.job_detail(jid)

    def rewrite(self, jid: str, purpose: str, backend: Optional[str] = None) -> dict:
        """다른 쓰임새로 글 층(세 줄 결론·개요·공유 글)만 다시 쓴다. 글 확보·구성·구간 정리·화면은 그대로 (비용이 작다)."""
        j = self._job(jid)
        if j["status"] in ACTIVE:
            raise ServiceError("busy", "작업이 진행 중입니다 — 끝난 뒤에", 409)
        if purpose not in E.PURPOSES:
            raise ServiceError("bad_request", f"purpose 는 {', '.join(E.PURPOSES)}")
        if backend and backend not in BACKENDS:
            raise ServiceError("bad_request", f"backend 는 {', '.join(BACKENDS)}")
        self.store.update_job(jid, purpose=purpose, overview=None, article=None, overview_backend=backend or j["backend"],
                              key=self.key_of(j["ref"], j["backend"], j["visual"], j["lang"], j["out_lang"], purpose, j["focus"] or "", j["transcribe"] or "auto"),
                              status="queued", stage=f"{E.PURPOSE_LABEL[purpose]}로 다시 쓰기 기다리는 중", error=None, error_kind=None)
        self._kick()
        return self.job_detail(jid)

    def regenerate_overview(self, jid: str, backend: Optional[str] = None) -> dict:
        j = self._job(jid)
        if j["status"] in ACTIVE:
            raise ServiceError("busy", "작업이 진행 중입니다 — 끝난 뒤에", 409)
        if backend and backend not in BACKENDS:
            raise ServiceError("bad_request", f"backend 는 {', '.join(BACKENDS)}")
        self.store.update_job(jid, overview=None, article=None, overview_backend=backend or j["backend"], status="queued",
                              stage="개요 다시 만들기 기다리는 중", error=None, error_kind=None)
        self._kick()
        return self.job_detail(jid)

    def delete(self, jid: str) -> None:
        j = self._job(jid)
        if j["status"] == "running":
            raise ServiceError("busy", "진행 중인 작업은 먼저 취소하세요", 409)
        self.store.delete_job(jid)
        shutil.rmtree(self.work_dir(jid), ignore_errors=True)

    # ---------- 일꾼 ----------

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self._stop = False
        self.worker = threading.Thread(target=self._loop, name="video_reader-worker", daemon=True)
        self.worker.start()

    def stop(self) -> None:
        """일꾼을 멈춘다. 진행 중이던 작업은 취소하지 않고 줄로 되돌린다 (다시 켜면 이어서, stt 작업도 그대로 둔다)."""
        self._stop = True
        self._kick()
        if self.worker:
            self.worker.join(timeout=10)

    def _kick(self) -> None:
        with self._wake:
            self._wake.notify_all()

    def _loop(self) -> None:
        while not self._stop:
            q = self.store.jobs_by("queued")
            if not q:
                with self._wake:
                    self._wake.wait(timeout=2)
                continue
            self.run_job(q[0]["id"])

    def run_job(self, jid: str) -> None:
        """한 작업을 할 수 있는 데까지. 일꾼 스레드에서 (테스트는 직접 불러도 된다)."""
        ev = self._cancel.setdefault(jid, threading.Event())
        ev.clear()
        self.store.update_job(jid, status="running")
        try:
            self._pipeline(jid, ev)
            self.store.update_job(jid, status="done", stage="끝", progress=None)
        except Canceled:
            self.store.update_job(jid, status="canceled", stage="취소됨")
        except Interrupted:
            self.store.update_job(jid, status="queued", stage="서버가 꺼져 멈춤 — 다시 켜면 이어서")
        except (SourceError, SttError, LLMError) as exc:
            kind = "canceled" if exc.kind == "stt_canceled" else exc.kind
            if kind == "interrupted":
                self.store.update_job(jid, status="queued", stage="서버가 꺼져 멈춤 — 다시 켜면 이어서")
            elif kind == "canceled":
                self.store.update_job(jid, status="canceled", stage="취소됨")
            else:
                self.store.update_job(jid, status="failed", stage="멈춤", error=exc.message, error_kind=kind)
        except Exception as exc:   # 일꾼이 죽지 않게 — 위에서 알려진 실패는 이미 처리했다
            log.error("작업 %s 내부 오류\n%s", jid, traceback.format_exc())
            self.store.update_job(jid, status="failed", stage="멈춤", error=f"내부 오류: {type(exc).__name__}: {exc}"[:300],
                                  error_kind="internal")
        finally:
            self._cancel.pop(jid, None)

    def _check(self, ev: threading.Event) -> None:
        if ev.is_set():
            raise Canceled()
        if self._stop:
            raise Interrupted()

    def _stage(self, jid: str, stage: str, **progress) -> None:
        self.store.update_job(jid, stage=stage, progress=progress or None)

    def _warn(self, jid: str, msg: str) -> None:
        w = self.store.job(jid)["warnings"] or []
        if msg not in w:
            self.store.update_job(jid, warnings=w + [msg])

    def _charge(self, jid: str, llm) -> None:
        cost = (getattr(llm, "last", None) or {}).get("cost_usd")
        if cost:
            self.store.add_cost(jid, float(cost))
            llm.last = {}

    def _pipeline(self, jid: str, ev: threading.Event) -> None:
        j = self.store.job(jid)
        work = self.work_dir(jid)
        work.mkdir(parents=True, exist_ok=True)
        # 1. 영상 정보
        if not j["source"]:
            self._stage(jid, "영상 정보 읽는 중")
            src = self.engine.probe(j["ref"])
            self.store.update_job(jid, source=src.to_dict())
            j = self.store.job(jid)
        src = Source.from_dict(j["source"])          # 업로더 자막 언어 목록도 들어 있다 (자막은 언어 코드로 받는다)
        self._check(ev)
        # 2. 글
        if not j["transcript"]:
            tr = None
            if not j["stt"] and (j["transcribe"] or "auto") == "auto":
                self._stage(jid, "사람이 만든 자막 찾는 중")
                tr = self.engine.captions(src, work, j["lang"])
            if tr is None:
                st = None
                if j["stt"] and j["stt"].get("taskId"):
                    self._stage(jid, "stt 받아 적기 이어서 기다리는 중")
                    try:
                        st = self.engine.stt.status(j["stt"]["taskId"])
                    except SttError:
                        st = None
                if st is None:
                    self._stage(jid, "자막 없음 → stt 에 받아 적기 맡기는 중")
                    self._stage(jid, "stt 에 받아 적기 맡기는 중" + (" (예전 결과 말고 새로)" if j["stt_fresh"] else ""))
                    st = self.engine.submit_stt(src, f"video_reader-{jid}", j["lang"], force=bool(j["stt_fresh"]))
                self.store.update_job(jid, stt={"taskId": st.task_id, "reviewUrl": st.review_url})
                st = stt_client.wait(self.engine.stt, st, cancelled=ev.is_set, interval=self.stt_interval,
                                     stopping=lambda: self._stop,
                                     on_stage=lambda s: self._stage(jid, f"stt 받아 적는 중 — {s}" if s else "stt 받아 적는 중"))
                tr = self.engine.stt_transcript(st)
            if not tr.segments:
                raise SourceError("fetch", "말소리·자막이 없습니다 — 정리할 글이 없습니다")
            self.store.update_job(jid, transcript={"source": tr.source, "lang": tr.lang, "segments": tr.segments,
                                                   "speakers": tr.speakers}, **({"stt": tr.stt} if tr.stt else {}))
            j = self.store.job(jid)
        self._check(ev)
        segs = j["transcript"]["segments"]
        speakers = j["transcript"].get("speakers") or {}
        # 3. 구성 → 구간 (올린 사람의 챕터 → LLM 이 경계 찾기 → 시간)
        if not self.store.sections(jid):
            self._stage(jid, "영상 구성 찾는 중")
            struct = E.structure_from_chapters(src)
            if struct is None:
                llm = self.engine.llm(j["backend"])
                ok, why = llm.available()
                if not ok:
                    raise LLMError("llm_down", why)
                struct = E.detect_structure(llm, src, segs, out_lang=j["out_lang"] or "ko", speakers=speakers)
                self._charge(jid, llm)
            self._check(ev)
            self.store.update_job(jid, structure=struct)
            self.store.set_sections(jid, E.units_from_structure(segs, struct))
            j = self.store.job(jid)
        # 4. 구간 정리 (+ 화면)
        secs = self.store.sections(jid)
        todo = [s for s in secs if s["status"] != "done"]
        for n, s in enumerate(todo):
            self._check(ev)
            self._stage(jid, f"구간 정리 {s['idx'] + 1}/{len(secs)}", done=len(secs) - len(todo) + n, total=len(secs))
            self._section(jid, j, src, segs, speakers, s, ev)
        # 5. 다듬기 — 정보는 그대로, 표현만: 문체 위반 줄 고치기 + 언급된 것 정리 (사람이 고친 구간은 건드리지 않는다)
        j = self.store.job(jid)
        if not j["polish"]:
            self._check(ev)
            self._stage(jid, "다듬는 중 — 문체·언급된 것")
            llm = self.engine.llm(j["backend"])
            ok, why = llm.available()
            if not ok:
                raise LLMError("llm_down", why)
            rows = self.store.sections(jid)
            views = [self._sec_view(s) for s in rows]
            fixed, style = E.fix_style(llm, src, views, out_lang=j["out_lang"] or "ko")
            for s, before, after in zip(rows, views, fixed):
                if after["points"] != before["points"] or after["gist"] != before["gist"]:
                    self.store.update_section(jid, s["idx"], points=after["points"], gist=after["gist"])
            asr = (j["transcript"] or {}).get("source") == "stt"
            screen = " ".join(o["text"] for s in rows for o in (s["observations"] or []))
            ms = E.polish_mentions(llm, src, E.merge_mentions(fixed), out_lang=j["out_lang"] or "ko", asr=asr, seen_on_screen=screen)
            self._charge(jid, llm)
            self.store.update_job(jid, polish={"mentions": ms, "style": style})
        # 6. 개요
        j = self.store.job(jid)
        if not j["overview"]:
            self._check(ev)
            self._stage(jid, "전체 개요 쓰는 중")
            llm = self.engine.llm(j["overview_backend"] or j["backend"])
            done = [{**self._sec_view(s), "observations": s["observations"] or []} for s in self.store.sections(jid)]
            ov = E.overview(llm, src, done, out_lang=j["out_lang"] or "ko", purpose=j["purpose"] or "record", focus=j["focus"] or "")
            self._charge(jid, llm)
            self.store.update_job(jid, overview=ov)
            j = self.store.job(jid)
        # 7. 공유 글 (쓰임새가 공유일 때만)
        if (j["purpose"] or "record") == "share" and not j["article"]:
            self._check(ev)
            self._stage(jid, "공유 글 쓰는 중")
            llm = self.engine.llm(j["overview_backend"] or j["backend"])
            done = [{**self._sec_view(s), "observations": s["observations"] or []} for s in self.store.sections(jid)]
            art = E.article(llm, src, done, j["overview"], out_lang=j["out_lang"] or "ko", focus=j["focus"] or "")
            self._charge(jid, llm)
            self.store.update_job(jid, article=art)
        self.store.update_job(jid, overview_backend=None)

    def _section(self, jid: str, j: dict, src: Source, segs: list, speakers: dict, s: dict, ev: threading.Event) -> None:
        backend = s["backend"] or j["backend"]
        llm = self.engine.llm(backend)
        ok, why = llm.available()
        if not ok:
            raise LLMError("llm_down", why)
        prev = self.store.section(jid, s["idx"] - 1) if s["idx"] else None
        sec = {"start": s["start"], "end": s["end"], "first": s["first"], "last": s["last"], "label": s["label"]}
        r = E.summarize_section(llm, src, segs, sec, speakers=speakers, out_lang=j["out_lang"] or "ko",
                                prev_title=(prev or {}).get("title") or "", purpose=j["purpose"] or "record",
                                focus=j["focus"] or "", kind=(j["structure"] or {}).get("kind") or "time",
                                asr=(j["transcript"] or {}).get("source") == "stt")
        self._charge(jid, llm)
        frames, obs = [], []
        want = E.frame_times(sec, r, j["visual"])
        if want:
            self._check(ev)
            try:
                shots = self.engine.grab(src, [c["t"] for c in want], self.work_dir(jid) / "frames")
            except SourceError as exc:
                self._warn(jid, f"화면을 뽑지 못했습니다: {exc.message}")
                shots = []
            v_ok, v_why = llm.vision()
            frames = [{"t": t, "file": p.name, "why": c["why"], "read": bool(v_ok)} for (t, p), c in zip(shots, want)]
            if shots and v_ok:
                self._check(ev)
                obs = E.read_frames(llm, src, {**sec, **r}, shots, out_lang=j["out_lang"] or "ko")
                self._charge(jid, llm)
            elif shots:
                self._warn(jid, f"화면 읽기 잠김 ({LABEL[backend]}) — 화면은 붙였지만 읽지 않았습니다. 다시 만들기에서 그림을 읽는 쪽을 고르세요")
        self.store.update_section(jid, s["idx"], status="done", title=r["title"], gist=r["gist"], points=r["points"],
                                  mentions=r.get("mentions") or [], coverage=r.get("coverage"),
                                  needs_visual=int(r["needs_visual"]), cues=r["visual_cues"], frames=frames, observations=obs,
                                  backend=backend, error=None)
