"""입력 단계 — 영상이 어디 있든(유튜브·yt-dlp 가 여는 주소·이 PC 파일) 같은 모양으로 만든다.

- probe: 제목·길이·설명 등 (Source)
- captions: 사람이 만든 자막만 찾는다. 유튜브는 업로더 자막(자동 자막은 쓰지 않는다), 파일은 안의 글자 자막 트랙 → 옆의 .srt/.vtt.
  없으면 None (그다음은 stt 에 맡긴다 — engine).
- grab_frame: 그 시각의 한 장 (화면을 봐야 하는 구간에서만 부른다)

네트워크는 yt-dlp 하나라 좁은 ``Remote`` 뒤에 둔다 (테스트에서 가짜로 바꾼다). ffmpeg·파일은 감싸지 않는다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional, Protocol
from urllib.parse import parse_qs, urlparse

SourceKind = Literal["youtube", "url", "file"]
CaptionSource = Literal["uploader", "embedded", "sidecar", "stt"]
TEXT_SUB_CODECS = {"subrip", "srt", "mov_text", "ass", "ssa", "webvtt", "text"}


class SourceError(Exception):
    """kind: bad_source(주소·경로가 틀림) / fetch(받기 실패) / no_tool(yt-dlp·ffmpeg 없음)"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


@dataclass
class Source:
    ref: str                       # 사용자가 넣은 그대로 (주소 또는 절대 경로)
    kind: SourceKind
    title: str = ""
    duration: Optional[float] = None
    description: str = ""
    uploader: str = ""
    language: Optional[str] = None  # 영상 원래 언어 (알 때만)
    webpage_url: str = ""
    video_id: str = ""
    subtitles: dict = field(default_factory=dict)   # 업로더 자막 {언어: [형식…]} (주소일 때만)
    chapters: list = field(default_factory=list)    # 올린 사람이 붙인 챕터 [{start, title}] — 있으면 영상 구성으로 그대로 쓴다

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("ref", "kind", "title", "duration", "description", "uploader", "language",
                                              "webpage_url", "video_id", "chapters")} | {"subtitle_langs": sorted(self.subtitles)}

    @classmethod
    def from_dict(cls, d: dict) -> "Source":
        s = cls(**{k: d.get(k) for k in ("ref", "kind", "title", "duration", "description", "uploader", "language",
                                         "webpage_url", "video_id")})
        s.subtitles = {k: [] for k in d.get("subtitle_langs") or []}
        s.chapters = list(d.get("chapters") or [])
        return s

    def link(self, t: float) -> str:
        """그 시각으로 여는 주소 (유튜브만 시각을 붙인다). 파일은 빈 문자열 — 화면이 자체 재생기로 연다."""
        if self.kind == "youtube" and self.video_id:
            return f"https://www.youtube.com/watch?v={self.video_id}&t={int(t)}s"
        return self.webpage_url or (self.ref if self.kind != "file" else "")


class Remote(Protocol):
    """yt-dlp 가 하는 일 (네트워크). 가짜로 바꿔 테스트한다."""

    def info(self, url: str) -> dict: ...                                          # yt-dlp -J
    def subtitle(self, url: str, lang: str, dest: Path) -> Optional[Path]: ...     # 업로더 자막 → srt 파일
    def stream_url(self, url: str) -> str: ...                                     # 프레임을 뽑을 영상 스트림 주소


class YtDlp:
    def __init__(self, binary: Optional[str] = None, timeout: int = 120):
        self.bin = binary or shutil.which("yt-dlp")
        self.timeout = timeout

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        if not self.bin:
            raise SourceError("no_tool", "yt-dlp 가 없습니다 — pip install yt-dlp")
        try:
            return subprocess.run([self.bin, "--no-playlist", "--no-warnings", *args], capture_output=True, text=True,
                                  timeout=self.timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise SourceError("fetch", f"yt-dlp 시간 초과 ({self.timeout}초)") from exc

    @staticmethod
    def _last(r: subprocess.CompletedProcess) -> str:
        return (r.stderr.strip().splitlines() or ["?"])[-1][:200]

    def info(self, url: str) -> dict:
        r = self._run(["-J", url])
        if r.returncode != 0:
            raise SourceError("fetch", f"주소를 열 수 없습니다: {self._last(r)}")
        return json.loads(r.stdout)

    def subtitle(self, url: str, lang: str, dest: Path) -> Optional[Path]:
        dest.mkdir(parents=True, exist_ok=True)
        for old in dest.glob("uploader.*"):
            old.unlink()
        r = self._run(["-q", "--skip-download", "--write-subs", "--sub-langs", lang, "--sub-format", "srt/vtt/best",
                       "--convert-subs", "srt", "-o", str(dest / "uploader"), url])
        got = sorted(dest.glob("uploader.*.srt"))
        if r.returncode != 0 or not got:
            raise SourceError("fetch", f"업로더 자막을 받지 못했습니다: {self._last(r)}")
        return got[0]

    def stream_url(self, url: str) -> str:
        r = self._run(["-q", "-f", "bv*[height<=720]/b[height<=720]/bv*/b", "-g", url])
        if r.returncode != 0 or not r.stdout.strip():
            raise SourceError("fetch", f"영상 스트림 주소를 얻지 못했습니다: {self._last(r)}")
        return r.stdout.strip().splitlines()[0]


def kind_of(ref: str) -> SourceKind:
    u = urlparse(ref)
    if u.scheme in ("http", "https"):
        host = (u.hostname or "").lower()
        return "youtube" if host.endswith(("youtube.com", "youtu.be")) else "url"
    return "file"


def youtube_id(url: str) -> str:
    u = urlparse(url)
    if (u.hostname or "").endswith("youtu.be"):
        return u.path.strip("/").split("/")[0]
    if u.path.startswith(("/shorts/", "/live/", "/embed/")):
        return u.path.split("/")[2]
    return (parse_qs(u.query).get("v") or [""])[0]


def check_ref(ref: str) -> str:
    """입력 검사 — 주소는 http(s), 파일은 이 PC의 절대 경로 + 존재. 틀리면 SourceError(bad_source)."""
    ref = (ref or "").strip()
    if not ref:
        raise SourceError("bad_source", "영상 주소나 파일 경로를 넣으세요")
    if ref.startswith("file://"):
        ref = urlparse(ref).path
    if kind_of(ref) != "file":
        return ref
    p = Path(ref).expanduser()
    if not p.is_absolute():
        raise SourceError("bad_source", f"파일은 이 PC의 절대 경로로 넣으세요: {ref}")
    if not p.is_file():
        raise SourceError("bad_source", f"파일이 없습니다: {ref}")
    return str(p)


def probe(ref: str, remote: Remote) -> Source:
    ref = check_ref(ref)
    kind = kind_of(ref)
    if kind == "file":
        return Source(ref=ref, kind="file", title=Path(ref).stem, duration=media_duration(Path(ref)))
    d = remote.info(ref)
    if d.get("is_live"):
        raise SourceError("bad_source", "생방송 중인 주소입니다 — 끝난 뒤 다시 넣으세요")
    subs = {k: v for k, v in (d.get("subtitles") or {}).items() if k != "live_chat"}   # 업로더 자막만
    return Source(ref=ref, kind=kind, title=d.get("title") or "", duration=d.get("duration"),
                  description=(d.get("description") or "")[:3000], uploader=d.get("uploader") or d.get("channel") or "",
                  language=d.get("language"), webpage_url=d.get("webpage_url") or ref,
                  video_id=(d.get("id") or youtube_id(ref)) if kind == "youtube" else (d.get("id") or ""), subtitles=subs,
                  chapters=[{"start": float(c.get("start_time") or 0), "title": str(c.get("title") or "").strip()}
                            for c in d.get("chapters") or [] if isinstance(c, dict)])


# ---------- 자막 ----------

_TS = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})|(\d{2}):(\d{2})[.,](\d{1,3})")


def _sec(s: str) -> float:
    m = _TS.search(s)
    if not m:
        raise ValueError(s)
    if m.group(1) is not None:
        h, mi, se, ms = m.group(1), m.group(2), m.group(3), m.group(4)
    else:
        h, mi, se, ms = "0", m.group(5), m.group(6), m.group(7)
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000


def parse_subs(text: str) -> list[dict]:
    """SRT·VTT → [{start, end, text}]. 태그를 지우고, 바로 앞과 같은 글은 합친다 (굴러가는 자막)."""
    out: list[dict] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("﻿", "")):
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        k = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if k is None:
            continue
        a, _, b = lines[k].partition("-->")
        try:
            start, end = _sec(a), _sec(b)
        except ValueError:
            continue
        body = " ".join(re.sub(r"<[^>]+>|\{\\[^}]*\}", "", ln).strip() for ln in lines[k + 1:]).strip()
        body = re.sub(r"\s+", " ", body)
        if not body:
            continue
        if out and body == out[-1]["text"]:
            out[-1]["end"] = max(out[-1]["end"], end)
            continue
        out.append({"start": round(start, 3), "end": round(end, 3), "text": body})
    return out


def pick_lang(available: list[str], want: Optional[str], original: Optional[str]) -> Optional[str]:
    """원하는 언어 → 영상 원래 언어 → ko → en → 첫 번째. 'en' 은 'en-US' 같은 것과도 맞는다."""
    if not available:
        return None
    for cand in (want, original, "ko", "en"):
        if not cand:
            continue
        c = cand.lower()
        exact = [a for a in available if a.lower() == c]
        if exact:
            return exact[0]
        pre = [a for a in available if a.lower().split("-")[0] == c.split("-")[0]]
        if pre:
            return sorted(pre, key=len)[0]
    return sorted(available)[0]


def captions(src: Source, work: Path, remote: Remote, lang: Optional[str] = None) -> Optional[tuple[CaptionSource, str, list[dict]]]:
    """사람이 만든 자막 → (출처, 언어, 구간). 없으면 None."""
    if src.kind != "file":
        code = pick_lang(sorted(src.subtitles), lang, src.language)
        if not code:
            return None
        segs = parse_subs(remote.subtitle(src.ref, code, work).read_text(encoding="utf-8", errors="replace"))
        return ("uploader", code, segs) if segs else None
    path = Path(src.ref)
    got = _embedded(path, work, lang)
    if got:
        return got
    side = _sidecar(path, lang)
    if side:
        segs = parse_subs(side[1].read_text(encoding="utf-8", errors="replace"))
        if segs:
            return "sidecar", side[0], segs
    return None


def _embedded(path: Path, work: Path, lang: Optional[str]) -> Optional[tuple[CaptionSource, str, list[dict]]]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
                        "stream=index,codec_name:stream_tags=language", "-of", "json", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    streams = [s for s in json.loads(r.stdout or "{}").get("streams", []) if s.get("codec_name") in TEXT_SUB_CODECS]
    if not streams:
        return None
    langs = [(s.get("tags") or {}).get("language") or "und" for s in streams]
    code = pick_lang(langs, lang, None)
    s = streams[langs.index(code)]
    work.mkdir(parents=True, exist_ok=True)
    out = work / "embedded.srt"
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(path), "-map", f"0:{s['index']}", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        return None
    segs = parse_subs(out.read_text(encoding="utf-8", errors="replace"))
    return ("embedded", code, segs) if segs else None


def _sidecar(path: Path, lang: Optional[str]) -> Optional[tuple[str, Path]]:
    cands: dict[str, Path] = {}
    for ext in (".srt", ".vtt"):
        if path.with_suffix(ext).is_file():
            cands.setdefault("und", path.with_suffix(ext))
        for p in path.parent.glob(f"{glob_escape(path.stem)}.*{ext}"):
            code = p.name[len(path.stem) + 1:-len(ext)]
            if code and "." not in code:
                cands.setdefault(code, p)
    if not cands:
        return None
    code = pick_lang(sorted(cands), lang, None)
    return code, cands[code]


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", s)


# ---------- 프레임 ----------

def media_duration(path: Path) -> Optional[float]:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True)
    try:
        return round(float(r.stdout.strip()), 3)
    except ValueError:
        return None


def has_video(path: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                        str(path)], capture_output=True, text=True)
    return "video" in r.stdout


def grab_frame(input_: str, t: float, out: Path, width: int = 960) -> Path:
    """ffmpeg 로 t 초의 한 장 (input_ = 파일 경로 또는 스트림 주소). 실패하면 SourceError(fetch)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{max(t, 0):.2f}", "-i", input_, "-frames:v", "1",
                        "-vf", f"scale='min({width},iw)':-2", "-q:v", "3", str(out)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise SourceError("fetch", f"{t:.0f}초 화면을 뽑지 못했습니다: {r.stderr.strip()[-160:] or '영상 트랙 없음'}")
    return out
