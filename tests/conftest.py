"""진입점 테스트 공통 — 가짜는 네트워크(yt-dlp·stt)와 모델뿐. ffmpeg·파일·SQLite 는 진짜."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from video_reader.app import create_app
from video_reader.engine import Engine
from video_reader.llm import LLMError
from video_reader.stt_client import SttError, SttStatus

SRT_KO = "\n\n".join(f"{i + 1}\n00:00:{i * 2:02d},000 --> 00:00:{i * 2 + 2:02d},000\n{t}" for i, t in enumerate(
    ["안녕하세요 오늘은 캐시 이야기입니다", "이 그래프를 보시면 화면에 지연 시간이 나옵니다", "캐시가 맞으면 빨라집니다",
     "틀리면 원본까지 갑니다", "정리하면 적중률이 핵심입니다"])) + "\n"


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict:
    return make_media(tmp_path_factory.mktemp("media"))


def make_media(d: Path) -> dict:
    """시험 영상들 (ffmpeg 로 만든다): 자막 없는 영상 / 안에 자막 트랙 / 옆에 .ko.srt / 소리만."""
    srt = d / "subs.srt"
    srt.write_text(SRT_KO, encoding="utf-8")
    base = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=12",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=12"]
    plain = d / "plain.mp4"
    run(base + ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(plain)])
    emb = d / "embedded.mp4"
    run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(plain), "-i", str(srt), "-map", "0", "-map", "1", "-c", "copy",
         "-c:s", "mov_text", "-metadata:s:s:0", "language=kor", str(emb)])
    side = d / "lecture.mp4"
    side.write_bytes(plain.read_bytes())
    (d / "lecture.ko.srt").write_text(SRT_KO, encoding="utf-8")
    audio = d / "talk.m4a"
    run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=8", str(audio)])
    return {"plain": plain, "embedded": emb, "sidecar": side, "audio": audio, "srt": srt}


class FakeRemote:
    """yt-dlp 대신. subtitles 가 있으면 업로더 자막, stream_url 은 로컬 영상."""

    def __init__(self, media: dict, subs: Optional[dict] = None, srt: str = SRT_KO, chapters: Optional[list] = None):
        self.media, self.subs = media, subs if subs is not None else {"ko": [{"ext": "srt"}]}
        self.srt, self.chapters = srt, chapters or []
        self.calls: list[str] = []

    def info(self, url):
        self.calls.append("info")
        if "broken" in url:
            raise __import__("video_reader.sources", fromlist=["SourceError"]).SourceError("fetch", "주소를 열 수 없습니다: 404")
        return {"id": "abc123XYZ00", "title": "캐시 강의", "duration": 12, "description": "캐시 설명", "uploader": "채널",
                "language": "ko", "webpage_url": url, "subtitles": self.subs, "chapters": self.chapters}

    def subtitle(self, url, lang, dest):
        self.calls.append(f"subtitle:{lang}")
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / f"uploader.{lang}.srt"
        p.write_text(self.srt, encoding="utf-8")
        return p

    def stream_url(self, url):
        self.calls.append("stream")
        return str(self.media["plain"])


class FakeStt:
    """stt A2A 대신. submit 은 같은 messageId 면 같은 작업. gate 가 닫혀 있으면 계속 working."""

    def __init__(self, up: bool = True):
        self.up = up
        self.tasks: dict[str, dict] = {}
        self.by_mid: dict[str, str] = {}
        self.submits = 0
        self.gate = threading.Event()
        self.gate.set()
        self.canceled: list[str] = []

    def available(self):
        return (True, "") if self.up else (False, "stt 서버 꺼짐 — 켜세요")

    def submit(self, message_id, *, url=None, path=None, lang=None, context="", title="", force=False):
        if not self.up:
            raise SttError("stt_down", "stt 서버 꺼짐")
        self.submits += 1
        self.forced = getattr(self, "forced", []) + [force]
        if message_id not in self.by_mid:
            tid = f"stt{len(self.tasks) + 1}"
            self.tasks[tid] = {"polls": 0, "state": "working"}
            self.by_mid[message_id] = tid
        return self.status(self.by_mid[message_id])

    def status(self, task_id):
        t = self.tasks[task_id]
        if t["state"] == "working" and self.gate.is_set():
            t["polls"] += 1
            if t["polls"] >= 2:
                t["state"] = "done"
        segs = [{"start": i * 3.0, "end": i * 3.0 + 2.5, "speaker": "S1" if i % 2 == 0 else "S2", "text": x}
                for i, x in enumerate(["회의를 시작합니다", "이 화면의 표를 보시면 매출이 나옵니다", "다음 분기 계획입니다", "끝"])]
        return SttStatus(task_id=task_id, state=t["state"], stage="받아 적는 중", review_url=f"http://stt/#job={task_id}",
                         segments=segs if t["state"] == "done" else None, speakers={"S1": "진행자", "S2": "발표자"},
                         language="ko")

    def cancel(self, task_id):
        self.canceled.append(task_id)
        self.tasks[task_id]["state"] = "canceled"


class FakeLLM:
    def __init__(self, name: str, *, up: bool = True, vision: bool = True):
        self.name, self.up, self._vision = name, up, vision
        self.calls: list[str] = []
        self.prompts: list[str] = []
        self.last: dict = {}

    def available(self):
        return (True, "") if self.up else (False, f"{self.name} 꺼짐 — 켜는 법")

    def vision(self):
        return (True, "") if self._vision else (False, "그림을 받지 않게 떠 있음")

    def json(self, system, user, schema, images=()):
        if not self.up:
            raise LLMError("llm_down", f"{self.name} 꺼짐")
        props = schema["properties"]
        self.prompts.append(system + "\n" + user)
        if "starts" in props:                             # 구성 찾기: "질문" 이 든 줄이 경계
            self.calls.append("structure")
            import re
            starts = [{"t": int(m[0]) * 60 + int(m[1]), "label": m[2][:20]}
                      for m in re.findall(r"\[(\d+):(\d+)\][^\n]*?(\S*질문[^\n]*)", user)]
            return {"kind": "qa" if starts else "talk", "starts": starts}
        if "body" in props:
            self.calls.append("article")
            return {"title": "공유 글 제목", "body": "공유 글 본문입니다."}
        if "lines" in props:                              # 문체 고치기: "~봄" → "~본다"
            self.calls.append("style")
            import re
            got = re.findall(r"^(\d+)\. (.*)$", user, re.M)
            return {"lines": [{"n": int(n), "text": t.replace("봄", "본다").replace("함", "한다")} for n, t in got]}
        if "groups" in props:                             # 언급된 것 묶기: 이름이 같으면 한 묶음, '주제어'는 뺀다
            self.calls.append("mentions")
            import re
            got = re.findall(r"^(\d+)\. (\S+)", user, re.M)
            groups = {}
            for n, name in got:
                if name != "주제어":
                    groups.setdefault(name, []).append(int(n))
            return {"groups": [{"ids": ids, "name": nm, "kind": "기타", "note": "메모" if nm == "2001년생" else ""} for nm, ids in groups.items()]}
        if "items" in props and "title" not in props:      # 누락 채우기
            self.calls.append("fill")
            import re
            miss = user.rsplit("기록에 없는 낱말:", 1)[-1].strip()
            return {"items": [{"at": "00:02", "text": f"빠졌던 것: {miss}", "details": []}], "mentions": []}
        if "needs_visual" in props:
            self.calls.append("section")
            import re
            ts = [int(m[0]) * 60 + int(m[1]) for m in re.findall(r"\[(\d+):(\d+)\]", user)]
            lines = [ln.split("] ", 1)[1] for ln in user.splitlines() if ln.startswith("[")]
            vis = "화면" in user or "그래프" in user
            self.last = {"cost_usd": 0.01} if self.name == "claude" else {}
            at = lambda t: f"{t // 60:02d}:{t % 60:02d}"
            return {"title": f"{self.name} 구간 {ts[0] if ts else 0}", "gist": "이 구간의 결론이다.",
                    "items": [{"at": at(t), "text": x, "details": ["딸린 근거"] if k == 0 else []} for k, (t, x) in enumerate(zip(ts, lines))][:4],
                    "mentions": [{"name": "캐시", "kind": "도구·기술", "at": at(ts[0]) if ts else "00:00"}],
                    "needs_visual": vis, "visual_cues": [{"t": ts[1] if len(ts) > 1 else 0, "why": "화면을 가리킴"}] if vis else []}
        if "observations" in props:
            self.calls.append(f"frames:{len(images)}")
            assert all(p.is_file() for p in images)
            return {"observations": [{"t": 2, "text": "슬라이드: 지연 시간 표"}]}
        self.calls.append("overview")
        return {"tldr": [{"text": "결론 하나", "from": [1]}, {"text": "결론 둘", "from": [1, 99]}, {"text": "결론 셋", "from": [1]}],
                "summary": f"{self.name} 가 쓴 개요입니다.",
                "answers": [{"n": 1, "line": "첫 구간의 답"}], "takeaways": [{"text": "적중률이 핵심", "from": [1]}]}


@pytest.fixture
def fakes(media):
    return {"remote": FakeRemote(media), "stt": FakeStt(),
            "llms": {"local": FakeLLM("local", vision=False), "claude": FakeLLM("claude")}}


def make_client(tmp_path: Path, fakes: dict, **kw) -> TestClient:
    eng = Engine(remote=fakes["remote"], stt=fakes["stt"], llms=fakes["llms"])
    return TestClient(create_app(tmp_path / "data", engine=eng, stt_interval=0.05, **kw))


@pytest.fixture
def client(tmp_path, fakes):
    with make_client(tmp_path, fakes) as c:
        yield c


def wait_job(c: TestClient, jid: str, want=("done", "failed", "canceled"), timeout: float = 20) -> dict:
    t = time.time()
    while time.time() - t < timeout:
        d = c.get(f"/api/jobs/{jid}").json()
        if d["status"] in want:
            return d
        time.sleep(0.05)
    raise AssertionError(f"작업이 {want} 가 되지 않음: {d['status']} / {d['stage']}")
