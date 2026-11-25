"""A2A 창구 — 다른 에이전트가 영상 정리를 맡긴다 (Agent Card + JSON-RPC). 배관은 doc_parser.a2a.

기능 summarize-video — 영상 하나를 구간별로 정리한다.
  보내는 것: url part {"url": "https://…" | "file:///절대경로"} 또는 data {"url"} / {"path"}.
  선택 (data): backend("local"|"claude", 기본 local), visual("auto"|"always"|"never"), lang(자막·받아 적기 언어), outLang(기본 ko),
  purpose("record"|"brief"|"share"|"agent", 기본 record — 쓰임새), focus(중점 한 줄),
  transcribe("auto" 기본 | "stt" 자막이 있어도 받아 적기), sttFresh(true 면 stt 가 예전에 받아 적은 것도 새로). 묻지 않고 기본값으로 돈다.
  돌려받는 것: 처음엔 WORKING. GetTask 로 다시 물어 끝나면 COMPLETED + artifact(text = 마크다운 정리, data = 개요·구간)
  + metadata.reviewUrl (사람이 확인·고치는 화면). 화면에서 고친 내용이 그대로 나간다.
  같은 messageId → 처음 작업. 같은 영상·같은 설정이 이미 있으면 그 작업에 붙는다 (다시 정리하지 않음).
"""
from __future__ import annotations

import uuid
from urllib.parse import unquote, urlparse

from doc_parser import a2a

from video_reader.service import Service, ServiceError

SKILLS = ("summarize-video",)
STATE = {"queued": "TASK_STATE_WORKING", "running": "TASK_STATE_WORKING", "done": "TASK_STATE_COMPLETED",
         "failed": "TASK_STATE_FAILED", "canceled": "TASK_STATE_CANCELED"}

CARD = {
    "name": "video_reader",
    "description": "영상(유튜브·yt-dlp 가 여는 주소·이 PC 파일)을 읽고 구간별로 정리한다. 사람이 만든 자막을 먼저 쓰고, 없으면 stt 에이전트로 "
                   "받아 적는다. 화면에만 있는 정보(슬라이드·그래프·코드)는 필요한 시각의 화면을 읽어 넣는다. 결과는 사람이 확인·고치는 화면 링크와 함께.",
    "version": "1.0.0",
    "provider": {"organization": "side_projects", "url": "http://127.0.0.1:8777"},
    "defaultInputModes": ["application/json", "text/plain"],
    "defaultOutputModes": ["text/markdown", "application/json"],
    "skills": [{
        "id": "summarize-video", "name": "영상 정리",
        "description": "영상 하나 → 구간별 정리(시각·요점·화면에서 읽은 것) + 전체 개요. url part 또는 data.url / data.path(이 PC 절대 경로). "
                       "data: backend(local|claude), visual(auto|always|never), lang, outLang, purpose(record|brief|share|agent), "
                       "focus(중점 한 줄). 영상 구성(챕터·질문·항목)을 찾아 그 단위로 정리한다. 끝나면 artifact 에 마크다운(text)과 "
                       "구조(data: 세 줄 결론·구간별 결론·주장/근거·공유 글), metadata.reviewUrl.",
        "tags": ["video", "youtube", "summary", "transcript"],
        "examples": ['{"data": {"url": "https://www.youtube.com/watch?v=…", "backend": "claude"}}'],
        "inputModes": ["application/json"], "outputModes": ["text/markdown", "application/json"],
    }],
}


def _parts(message: dict) -> tuple[str, dict, list[str]]:
    text, data, refs = [], {}, []
    for p in message.get("parts") or []:
        if not isinstance(p, dict):
            continue
        if "text" in p:
            text.append(str(p["text"]))
        elif "data" in p and isinstance(p["data"], dict):
            data.update(p["data"])
        elif "url" in p:
            u = urlparse(str(p["url"]))
            if u.scheme == "file":
                refs.append(unquote(u.path))
            elif u.scheme in ("http", "https"):
                refs.append(str(p["url"]))
            else:
                raise a2a.content_type_not_supported("url parts: https 주소 또는 file:// (이 PC)")
        elif "raw" in p:
            raise a2a.content_type_not_supported("영상 파일 자체는 받지 않습니다 — 주소나 이 PC의 경로(file://, data.path)로 보내세요")
    for k in ("url", "path"):
        if data.get(k):
            refs.append(str(data[k]))
    return "\n".join(text).strip(), data, refs


class VideoTasks:
    """TaskHandler: 한 A2A 작업 = 서비스 작업 하나에 붙음. 상태는 서비스 작업에서 읽는다."""

    def __init__(self, svc: Service):
        self.svc, self.store = svc, svc.store

    def send(self, message: dict, configuration: dict, base: str) -> dict:
        if message.get("taskId"):
            raise a2a.invalid_params("이 작업은 더 받을 것이 없습니다 — 새 메시지로 보내세요")
        mid = message.get("messageId")
        if mid:
            old = self.store.task_by_message(str(mid))
            if old:
                return self._view(old, base)
        tid = uuid.uuid4().hex[:16]
        ctx = str(message.get("contextId") or tid)
        try:
            text, data, refs = _parts(message)
            skill = data.get("skill") or (message.get("metadata") or {}).get("skill") or "summarize-video"
            if skill not in SKILLS:
                raise a2a.invalid_params(f"skill 은 {', '.join(SKILLS)}")
            if not refs and text.startswith(("http://", "https://")):
                refs = [text.split()[0]]
            if len(refs) != 1:
                raise a2a.invalid_params("영상 하나를 보내세요 (url part / data.url / data.path)")
            caller = str((message.get("metadata") or {}).get("caller") or data.get("caller") or "") or None
            job, _ = self.svc.create_job(refs[0], backend=str(data.get("backend") or "local"),
                                         visual=str(data.get("visual") or "auto"), lang=data.get("lang"),
                                         out_lang=str(data.get("outLang") or "ko"), origin="a2a", caller=caller,
                                         purpose=str(data.get("purpose") or "record"), focus=str(data.get("focus") or ""),
                                         transcribe=str(data.get("transcribe") or "auto"), stt_fresh=data.get("sttFresh") is True)
        except a2a.A2AError as exc:
            self.store.add_task(tid, ctx, str(mid) if mid else None, None, state="rejected", error=exc.message)
            raise
        except ServiceError as exc:
            self.store.add_task(tid, ctx, str(mid) if mid else None, None, state="rejected", error=exc.message)
            return self._view(self.store.task(tid), base)
        self.store.add_task(tid, ctx, str(mid) if mid else None, job["id"])
        return self._view(self.store.task(tid), base)

    def get(self, task_id: str, base: str) -> dict:
        return self._view(self._task(task_id), base)

    def list(self, params: dict, base: str) -> list[dict]:
        want = params.get("status")
        views = [self._view(t, base) for t in self.store.tasks(params.get("contextId"))]
        return [v for v in views if not want or v["status"]["state"] == want]

    def cancel(self, task_id: str, base: str) -> dict:
        t = self._task(task_id)
        view = self._view(t, base)
        if view["status"]["state"] != "TASK_STATE_WORKING":
            raise a2a.not_cancelable(task_id, view["status"]["state"])
        self.svc.cancel(t["job_id"])
        return self._view(t, base)

    def _task(self, tid: str) -> dict:
        t = self.store.task(tid)
        if not t:
            raise a2a.task_not_found(tid)
        return t

    def _view(self, t: dict, base: str) -> dict:
        if t["state"] == "rejected" or not t["job_id"]:
            return a2a.task(t["id"], t["context_id"], "TASK_STATE_REJECTED", text=t["error"] or "거부됨")
        j = self.store.job(t["job_id"])
        if not j:
            return a2a.task(t["id"], t["context_id"], "TASK_STATE_CANCELED", text="화면에서 지운 작업입니다")
        state = STATE[j["status"]]
        review = f"{base}/#job={j['id']}"
        meta = {"jobId": j["id"], "reviewUrl": review, "status": j["status"], "stage": j["stage"]}
        if state == "TASK_STATE_WORKING":
            return a2a.task(t["id"], t["context_id"], state, text=j["stage"] or "정리하는 중", metadata=meta)
        if state == "TASK_STATE_FAILED":
            return a2a.task(t["id"], t["context_id"], state, text=j["error"] or "실패",
                            metadata={**meta, "errorKind": j["error_kind"]})
        if state == "TASK_STATE_CANCELED":
            return a2a.task(t["id"], t["context_id"], state, text="취소됨", metadata=meta)
        d = self.svc.job_detail(j["id"])
        md = self.svc.markdown(j["id"])
        data = {"title": d["title"], "duration": d["duration"], "source": d["source"], "overview": d["overview"],
                "purpose": d["purpose"], "focus": d["focus"], "structure": d["structure"], "article": d["article"],
                "mentions": d["mentions"], "coverage": d["coverage"], "concepts": d["concepts"], "examples": d["examples"], "story": d["story"],
                "transcript": {k: d["transcript"][k] for k in ("source", "lang", "lines")},
                "sttReviewUrl": (d["stt"] or {}).get("reviewUrl"), "warnings": d["warnings"],
                "sections": [{k: s[k] for k in ("start", "end", "title", "label", "gist", "points", "mentions", "observations", "link", "edited")}
                             for s in d["sections"]]}
        artifact = {"artifactId": f"{j['id']}-summary", "name": "summary",
                    "parts": [{"text": md, "mediaType": "text/markdown"}, {"data": data}]}
        summary = f"{len(d['sections'])}구간 정리 — 확인·고치기: {review}"
        return a2a.task(t["id"], t["context_id"], state, text=summary, artifacts=[artifact], metadata=meta)


def mount(app, svc: Service) -> None:
    a2a.mount_a2a(app, CARD, VideoTasks(svc))
