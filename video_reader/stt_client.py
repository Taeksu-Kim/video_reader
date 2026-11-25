"""자막이 없을 때 받아 적기를 stt 에이전트에 A2A 로 맡긴다 (stt 서버 `python -m stt serve`, 8773).

같은 messageId 로 다시 보내면 stt 가 처음 작업을 돌려준다 → 재시작해도 다시 받아 적지 않고 이어서 기다린다.
맡긴 일은 stt 화면 작업 목록에도 보이고, 사람이 거기서 고친 글이 결과로 온다 (reviewUrl).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable, Literal, Optional, Protocol

import httpx

# 켜져 있나 확인용. 이 PC(WSL)는 닫힌 포트가 거부 대신 연결 시간 초과까지 기다려서(2026-11-24 측정 20초) 연결 대기를 짧게.
PROBE_TIMEOUT = httpx.Timeout(3.0, connect=1.0)

DEFAULT_URL = "http://127.0.0.1:8773"
SttState = Literal["working", "done", "failed", "canceled"]


class SttError(Exception):
    """kind: stt_down(서버 꺼짐) / stt_failed(받아 적기 실패) / stt_canceled"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


@dataclass
class SttStatus:
    task_id: str
    state: SttState
    stage: str = ""
    review_url: str = ""
    segments: Optional[list[dict]] = None     # done 일 때 [{start, end, speaker, text}]
    speakers: Optional[dict] = None
    language: Optional[str] = None
    error: str = ""


class SttClient(Protocol):
    def available(self) -> tuple[bool, str]: ...
    def submit(self, message_id: str, *, url: Optional[str] = None, path: Optional[str] = None, lang: Optional[str] = None,
               context: str = "", title: str = "", force: bool = False) -> SttStatus: ...   # force: stt 가 같은 영상을 받아 적었어도 새로
    def status(self, task_id: str) -> SttStatus: ...
    def cancel(self, task_id: str) -> None: ...


HOW_TO = "켜는 법: cd /mnt/d/workspace/side_projects/stt && python -m stt serve (8773)"


class A2ASttClient:
    def __init__(self, base: str = DEFAULT_URL, timeout: float = 30):
        self.base, self.timeout = base.rstrip("/"), timeout

    def _rpc(self, method: str, params: dict) -> dict:
        try:
            r = httpx.post(f"{self.base}/a2a", json={"jsonrpc": "2.0", "id": uuid.uuid4().hex[:8], "method": method,
                                                     "params": params}, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise SttError("stt_down", f"stt 서버에 닿지 않습니다 ({self.base}) — {HOW_TO}") from exc
        body = r.json()
        if "error" in body:
            raise SttError("stt_failed", f"stt 가 거부했습니다: {body['error'].get('message')}")
        return body["result"]

    def available(self) -> tuple[bool, str]:
        try:
            r = httpx.get(f"{self.base}/.well-known/agent-card.json", timeout=PROBE_TIMEOUT)
            ok = r.status_code == 200 and any(s.get("id") == "transcribe" for s in r.json().get("skills", []))
        except (httpx.HTTPError, ValueError):
            ok = False
        return (True, "") if ok else (False, f"{self.base} 에 닿지 않습니다. {HOW_TO}")

    def submit(self, message_id, *, url=None, path=None, lang=None, context="", title="", force=False):
        data = {"skill": "transcribe", "context": context[:1500], "title": title}
        if force:
            data["force"] = True
        if url:
            data["url"] = url
        if path:
            data["path"] = path
        if lang:
            data["lang"] = lang
        msg = {"messageId": message_id, "role": "ROLE_USER", "parts": [{"data": data}],
               "metadata": {"caller": "video_reader"}}
        return _view(self._rpc("SendMessage", {"message": msg}))

    def status(self, task_id):
        return _view(self._rpc("GetTask", {"id": task_id}))

    def cancel(self, task_id):
        try:
            self._rpc("CancelTask", {"id": task_id})
        except SttError:
            pass          # 이미 끝났으면 취소할 것이 없다


def _view(task: dict) -> SttStatus:
    if "task" in task and "status" not in task:    # SendMessage 결과가 {task: …} 로 감싸진 경우
        task = task["task"]
    st = task["status"]["state"]
    meta = task.get("metadata") or {}
    text = " ".join(p.get("text", "") for p in ((task["status"].get("message") or {}).get("parts") or []))
    state: SttState = {"TASK_STATE_COMPLETED": "done", "TASK_STATE_FAILED": "failed", "TASK_STATE_REJECTED": "failed",
                       "TASK_STATE_CANCELED": "canceled"}.get(st, "working")
    out = SttStatus(task_id=task["id"], state=state, stage=meta.get("stage") or text, review_url=meta.get("reviewUrl", ""),
                    error=text if state in ("failed", "canceled") else "")
    if state == "done":
        for a in task.get("artifacts") or []:
            for p in a.get("parts") or []:
                if isinstance(p.get("data"), dict) and "segments" in p["data"]:
                    out.segments = p["data"]["segments"]
                    out.speakers = p["data"].get("speakers") or {}
                    out.language = (p["data"].get("meta") or {}).get("language")
        if out.segments is None:
            out.segments = []
    return out


def wait(client: SttClient, st: SttStatus, *, cancelled: Callable[[], bool], on_stage: Callable[[str], None],
         interval: float = 2.0, sleep: Callable[[float], None] = time.sleep,
         stopping: Callable[[], bool] = lambda: False) -> SttStatus:
    """끝날 때까지 GetTask. 사람이 취소하면 stt 작업도 취소한다. 우리 서버가 꺼지는 중이면(stopping) stt 작업은 두고 나간다
    — 다시 켜면 같은 작업을 이어 기다린다."""
    while st.state == "working":
        if cancelled():
            client.cancel(st.task_id)
            raise SttError("stt_canceled", "취소됨")
        if stopping():
            raise SttError("interrupted", "서버 종료로 멈춤")
        on_stage(st.stage)
        sleep(interval)
        st = client.status(st.task_id)
    if st.state == "failed":
        raise SttError("stt_failed", f"stt 받아 적기 실패: {st.error or '?'}")
    if st.state == "canceled":
        raise SttError("stt_canceled", "stt 쪽에서 취소됨")
    return st
