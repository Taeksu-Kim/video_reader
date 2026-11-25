"""엔진 — 단계별로 부를 수 있는 함수들 (import 창구). 상태는 갖지 않는다 (상태의 주인은 service).

    probe → transcript(사람 자막 / 없으면 stt) → split → section(정리 + 화면 필요 판단) → frames(필요한 시각만) → read_frames → overview

한 번에 끝까지: ``Engine.read(ref, backend=…)`` (CLI·간단한 import 용, 저장하지 않음).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional, Sequence

from video_reader import sources, stt_client
from video_reader.llm import LLM, Backend, LLMError
from video_reader.sources import CaptionSource, Remote, Source, SourceError
from video_reader.stt_client import SttClient, SttError

VisualMode = Literal["auto", "always", "never"]
VISUAL_MODES: tuple[VisualMode, ...] = ("auto", "always", "never")
SECTION_TOKENS = 1600       # 구간 하나에 넣는 받아 적은 글 (추정 토큰). 영어 약 3분·한국어 약 2분. 로컬 8,192 안에 넉넉히
MIN_SECTION_SEC = 45
MAX_FRAMES = 3


# ---------- 글 다루기 ----------

def fmt_t(t: float) -> str:
    t = int(max(t, 0))
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def est_tokens(text: str) -> int:
    """대략의 토큰 수 — 한글·한자·가나는 글자당 1, 나머지는 3.5글자당 1 (모델 토크나이저 없이 자르기 위한 어림)."""
    wide = len(re.findall(r"[ᄀ-ᇿ぀-ヿ㄰-㆏一-鿿가-힣]", text))
    return wide + int((len(text) - wide) / 3.5) + 1


def line(seg: dict, speakers: Optional[dict] = None) -> str:
    sp = seg.get("speaker")
    who = f" {(speakers or {}).get(sp, sp)}:" if sp else ""
    return f"[{fmt_t(seg['start'])}]{who} {seg['text']}"


def split_sections(segments: list[dict], budget: int = SECTION_TOKENS, min_sec: float = MIN_SECTION_SEC) -> list[dict]:
    """받아 적은 구간들 → 정리 구간들 [{start, end, first, last}] (first/last = segments 의 번호, 포함).
    예산을 넘기 직전에서, 뒤쪽 30% 안의 가장 긴 쉼에서 끊는다 (말이 바뀌는 자리일 가능성이 크다)."""
    out: list[dict] = []
    i, n = 0, len(segments)
    while i < n:
        used, j = 0, i
        while j < n:
            cost = est_tokens(segments[j]["text"]) + 4
            if used + cost > budget and j > i and segments[j - 1]["end"] - segments[i]["start"] >= min_sec:
                break
            used += cost
            j += 1
        last = j - 1
        if j < n:                                           # 끊을 자리를 뒤쪽 30% 의 가장 긴 쉼으로
            lo = i + max(1, int((last - i) * 0.7))
            best, gap = last, -1.0
            for k in range(lo, last + 1):
                g = segments[k + 1]["start"] - segments[k]["end"] if k + 1 < n else 0
                if g > gap:
                    best, gap = k, g
            last = best
        out.append({"start": segments[i]["start"], "end": segments[last]["end"], "first": i, "last": last})
        i = last + 1
    if len(out) >= 2 and out[-1]["end"] - out[-1]["start"] < min_sec / 3:   # 너무 짧은 꼬리는 앞에 붙인다
        tail = out.pop()
        out[-1].update(end=tail["end"], last=tail["last"])
    return out


# ---------- 프롬프트·스키마 ----------

FRAMES_SCHEMA = {
    "type": "object",
    "properties": {"observations": {"type": "array", "maxItems": 6,
                                    "items": {"type": "object", "properties": {"t": {"type": "number"}, "text": {"type": "string"}},
                                              "required": ["t", "text"]}}},
    "required": ["observations"],
}

FRAMES_SYSTEM = """너는 영상의 화면 몇 장을 보고, 받아 적은 말에는 없고 화면에만 있는 정보를 적는다.
슬라이드 제목·표·그래프의 축과 값·코드·수식·화면에 쓰인 글자처럼 내용을 이해하는 데 필요한 것만.
사람 얼굴·옷·무대처럼 내용과 상관없는 것은 적지 않는다. 더할 것이 없으면 observations 는 빈 목록.
t 는 그 화면의 시각(초). 글은 {lang}로, 문장은 "~다"로 끝낸다. 화면의 글자는 원문 그대로 옮겨도 된다."""

LANG_NAME = {"ko": "한국어", "en": "English", "ja": "日本語"}


AT = {"type": "string", "pattern": "^[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?$"}   # 시각은 줄 앞 [mm:ss] 를 그대로 옮긴다 (초로 계산하지 않는다)
MENTION_KINDS = ["책", "강의·자료", "회사·기관", "사람", "도구·기술", "수치", "기타"]
SECTION_SCHEMA_TPL = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "gist": {"type": "string"},
        "items": {"type": "array", "minItems": 1, "maxItems": 16,
                  "items": {"type": "object", "properties": {
                      "at": AT, "text": {"type": "string"},
                      "details": {"type": "array", "maxItems": 6, "items": {"type": "string"}}},
                            "required": ["at", "text", "details"]}},
        "mentions": {"type": "array", "maxItems": 20,
                     "items": {"type": "object", "properties": {
                         "name": {"type": "string"}, "kind": {"type": "string", "enum": MENTION_KINDS}, "at": AT},
                               "required": ["name", "kind", "at"]}},
        "needs_visual": {"type": "boolean"},
        "visual_cues": {"type": "array", "maxItems": MAX_FRAMES,
                        "items": {"type": "object", "properties": {"at": AT, "why": {"type": "string"}},
                                  "required": ["at", "why"]}},
    },
    "required": ["title", "gist", "items", "mentions", "needs_visual", "visual_cues"],
}

# 쓰임새 — 정보 층은 늘 빠짐없이 같고, 정리 층(세 줄 결론·개요·한 줄 답·공유 글)만 쓰임새에 따라 다르다
Purpose = Literal["record", "brief", "share", "agent"]
PURPOSES: tuple[Purpose, ...] = ("record", "brief", "share", "agent")
PURPOSE_LABEL = {"record": "기록·판단", "brief": "빠른 판단", "share": "공유 글", "agent": "에이전트 입력"}

KIND_LABEL = {"qa": "질문", "list": "항목", "steps": "단계", "argument": "주장", "talk": "주제", "chapters": "챕터", "time": "시간 구간"}
KIND_HINT = {"qa": "말한 사람이 질문에 차례로 답하는 영상", "list": "항목을 차례로 다루는 영상", "steps": "따라 하는 단계를 설명하는 영상",
             "argument": "주장과 근거를 펼치는 영상", "talk": "뚜렷한 구성 없는 이야기·대담", "chapters": "올린 사람이 챕터를 붙인 영상",
             "time": "구성을 찾지 못해 시간으로 자른 영상"}

SECTION_SYSTEM = """너는 영상의 한 구간을 받아 적은 말에서 **정보를 빠짐없이** 뽑아 기록한다. 줄이는 것은 정보가 아니라 표현이다.
입력의 각 줄 앞 [mm:ss] 는 그 말이 나온 시각이다.

- title: 이 구간의 이름. 구성 단위 이름이 주어지면 다듬어 쓴다 — 질문이면 질문 그대로 짧게("~할까?"), 항목·단계면 그 이름.
- gist: 이 구간의 결론 한두 문장. 질문이면 그 답.
- items: 정보 단위마다 하나 — 이 구간에 나온 답·주장·판단·추천·조건·사례·수치·이름을 **하나도 빠뜨리지 않는다**.
  같은 것에 딸린 이유·사례·수치·자료 이름은 그 항목의 details 로 (한 줄씩). 관계가 들여쓰기로 보이게.
  at 은 그 정보가 처음 나온 줄 앞의 [mm:ss] 를 **글자 그대로** 옮긴다 (계산하지 않는다).
- mentions: 이 구간에 나온 구체적인 이름과 숫자 전부 — 책·강의·자료·회사·기관·사람·도구·기술·수치·연도. 원문 표기 그대로, 처음 나온 at.
- needs_visual: 말만으로는 내용을 알 수 없고 화면에 정보가 있을 때 true ("이 그래프를 보시면", 코드·표·슬라이드를 가리킬 때).
  말이 스스로 완결되면 false. visual_cues: 그때 볼 at 과 이유, 최대 3개.

쓰지 않는 것: 질문을 다시 말하는 항목("질문은 ~이다"), 진행 멘트("이어서 ~를 다룬다", "~를 예고한다"), 인사·광고,
항목마다 붙는 말꼬리("~라고 말한다", "~라고 본다" — 의견이면 처음 한 번만), "말한 사람·화자·말하는 사람" 같은 호칭 (이름을 알면 이름, 아니면 주어를 뺀다).
진행용 구간(인사·목차 예고)이면 items 는 실제 정보만 한두 개.
말에 없는 것은 쓰지 않는다. 항목은 짧은 "~다" 문장이나 명사구 ("~함·~음"으로 끝내지 않는다).
숫자는 아라비아 숫자로 쓴다 ("삼 년" → "3년", "백퍼센트" → "100%", "일곱 개" → "7개" — 순서를 말하는 "첫째·두 번째"는 그대로). 글은 {lang}로, 고유명사·용어는 원문을 괄호로 붙여도 된다."""

FILL_SCHEMA = {"type": "object", "properties": {"items": SECTION_SCHEMA_TPL["properties"]["items"],
                                                "mentions": SECTION_SCHEMA_TPL["properties"]["mentions"]},
               "required": ["items", "mentions"]}

FILL_SYSTEM = """너는 영상 한 구간의 기록에서 빠진 정보를 채운다. 받아 적은 말과 지금의 기록, 그리고 기록에 없는 낱말(숫자·이름)을 받는다.
그 낱말이 나온 말을 찾아, 기록에 없는 정보를 items 로만 돌려준다 (이미 있는 항목을 다시 쓰지 않는다). 그 낱말이 정보가 아니면(말버릇·잘못 받아 적음) 넣지 않는다.
mentions 에는 새로 넣은 이름·숫자를 원문 그대로. at 은 줄 앞 [mm:ss] 를 글자 그대로. 글은 {lang}로, 짧은 "~다" 문장이나 명사구."""

FOCUS_RULE = "\n사용자가 중점으로 본 것: {focus}\n— 이와 관련된 내용은 빠짐없이 자세히, 관련 없는 내용은 짧게 쓴다. 관련 내용이 없으면 없는 대로 둔다 (지어내지 않는다)."

STRUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["qa", "list", "steps", "argument", "talk"]},
        "starts": {"type": "array", "maxItems": 12,
                   "items": {"type": "object", "properties": {"t": {"type": "number"}, "label": {"type": "string"}},
                             "required": ["t", "label"]}},
    },
    "required": ["kind", "starts"],
}

STRUCT_SYSTEM = """너는 영상의 받아 적은 말 일부를 읽고, 이 영상이 스스로 가진 구성의 경계를 찾는다. 정리는 하지 않는다.
- kind: qa(질문에 차례로 답함) / list(항목을 차례로 다룸) / steps(따라 하는 단계) / argument(주장과 근거를 펼침) / talk(뚜렷한 구성 없는 이야기·대담)
- starts: 이 부분에서 새 질문·항목·단계·주제가 **시작되는** 줄의 시각(초 — 그 줄의 [mm:ss])과 이름.
  qa 면 이름은 그 질문을 짧게("FDE는 교육으로 키울 수 있나?"), 다른 것은 항목·단계·주제 이름 (20자 안팎).
  앞 부분에서 이어지는 내용이면 넣지 않는다. 경계가 없으면 빈 목록.
- 말한 사람이 구성을 밝히는 말("첫 번째 질문은", "다음으로", "2단계는")을 먼저 본다. 영상 설명에 목차·질문 목록이 있으면 참고한다.
글은 {lang}로 쓴다."""

SOURCED = {"type": "object", "properties": {"text": {"type": "string"},
                                           "from": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "integer"}}},
           "required": ["text", "from"]}   # 정리 문장 + 그 문장이 기대는 정보 기록의 구간 번호

OVERVIEW_SCHEMA = {
    "type": "object",
    "properties": {"tldr": {"type": "array", "minItems": 1, "maxItems": 3, "items": SOURCED},
                   "summary": {"type": "string"},
                   "answers": {"type": "array", "maxItems": 40,
                               "items": {"type": "object", "properties": {"n": {"type": "integer"}, "line": {"type": "string"}},
                                         "required": ["n", "line"]}},
                   "takeaways": {"type": "array", "minItems": 1, "maxItems": 7, "items": SOURCED}},
    "required": ["tldr", "summary", "answers", "takeaways"],
}

OVERVIEW_SYSTEM = """너는 영상의 정보 기록(구간마다 결론과 항목)을 받아, 영상을 보지 않은 사람이 읽을 **정리**를 쓴다. 기록에 없는 내용은 쓰지 않는다.
- tldr: 이 영상의 결론 세 줄. 한 줄에 한 가지, 말한 사람이 실제로 내린 결론으로 (주제 소개 말고).
  tldr·takeaways 의 줄마다 from 에 그 문장이 기대는 기록의 구간 번호(## 번호)를 적는다. 기댈 구간이 없는 문장은 쓰지 않는다.
- summary: {summary_len}. 결론과 그 근거가 이어지게 — tldr 를 되풀이하지 말고 이어서 설명한다.
- answers: 구간 번호(n, 기록의 ## 번호)마다 한 줄 — 질문이면 그 답, 아니면 그 구간의 핵심. {answers_len}
- takeaways: {takeaways_len}. tldr·summary·answers 에 쓴 문장을 되풀이하지 않는다 — 실제로 써먹을 것(방법·자료·판단 기준·주의할 점)으로.
말한 사람의 의견은 의견으로 쓰되 이름은 처음 한 번만 ("말한 사람·화자" 같은 호칭은 쓰지 않는다). 글은 {lang}로, 문장은 "~다"로 끝낸다."""

OVERVIEW_SHAPE = {"record": ("3~6문장", "진행용 구간(인사·목차)은 건너뛴다.", "가져갈 것 3~7개 (한 줄씩)"),
                  "brief": ("2~3문장", "핵심 구간만, 최대 6줄.", "가져갈 것 1~3개 (한 줄씩)"),
                  "share": ("3~6문장", "진행용 구간은 건너뛴다.", "가져갈 것 3~5개 (한 줄씩)"),
                  "agent": ("3~4문장, 사실 위주", "모든 구간, 짧게.", "가져갈 것 3~7개 (짧게)")}

ARTICLE_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                  "required": ["title", "body"]}

ARTICLE_SYSTEM = """너는 영상 정리를 재료로, 영상을 안 본 사람과 나눌 글 한 편을 쓴다 (메신저·블로그·사내 공유).
- title: 글 제목 한 줄 (낚시·과장 없이, 영상의 결론이 드러나게).
- body: 마크다운. 첫 문단 2~3문장에 무엇에 대한 영상이고 결론이 무엇인지. 그다음 영상 구성을 따라 짧은 소제목 또는 문단.
  말한 사람의 주장은 그 사람의 것으로 쓴다 ("○○는 ~라고 본다"). 근거(경험·수치·사례)는 구체적으로 남긴다.
  재료에 없는 사실·평가를 더하지 않는다. 번역투·과장·이모지 없이, 사람이 쓴 것처럼. 600~1,200자.
  출처 링크는 붙이지 않는다 (따로 붙인다). 글은 {lang}로."""


def _lang(out_lang: str) -> str:
    return LANG_NAME.get(out_lang, out_lang)


def _head(src: Source) -> str:
    bits = [f"영상 제목: {src.title}"] if src.title else []
    if src.uploader:
        bits.append(f"올린 사람: {src.uploader}")
    return "\n".join(bits)


def _unescape(text: str) -> str:
    """모델이 화면의 코드 줄바꿈을 글자 그대로 `\\n` 으로 돌려줄 때가 있다 (2026-11-24 Claude, 코드 슬라이드) → 실제 줄바꿈."""
    return text.replace("\\n", "\n")


def parse_at(v) -> Optional[float]:
    """"mm:ss" / "h:mm:ss" / 숫자 → 초. 못 읽으면 None (구간 시작으로 덮어쓰지 않고 호출한 쪽이 판단)."""
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s*", str(v or ""))
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _clamp(t, lo: float, hi: float) -> float:
    try:
        t = float(t)
    except (TypeError, ValueError):
        return lo
    return round(min(max(t, lo), hi), 1)


ASR_RULE = """
받아 적은 말은 소리 인식(stt)이라 이름·약어·외래어가 틀리게 적혔을 수 있다. 아래 '영상 설명·제목의 표기'에 같은 대상으로 보이는 것이 있으면
(소리가 비슷하고 맥락이 맞을 때 — 예: "CSV PG"가 사이트 이름으로 나오고 표기 목록에 "SVPG"가 있으면) 그 표기를 쓴다.
같은 대상이라는 근거가 없으면 받아 적은 그대로 둔다 (지어서 고치지 않는다).
영상 설명·제목의 표기: {glossary}"""


def glossary(src: Source, limit: int = 40) -> list[str]:
    """영상 제목·설명에서 이름 표기 후보 — 영문·숫자 낱말(두 글자 이상, 질문 번호 Q1 같은 것 제외), 『』 제목, 목록 줄(- …)의 짧은 항목.
    링크·이모지 코드(:arrow_forward:)는 먼저 지우고, 문장으로 끝나는 조각은 넣지 않는다. 받아 적기 오류를 바로잡는 근거로만 쓴다."""
    text = f"{src.title}\n{src.uploader}\n{src.description or ''}"
    text = re.sub(r"https?://\S+|www\.\S+|:[a-z_]+:", " ", text)
    out: list[str] = []

    def add(x: str):
        x = x.strip(" ·,/()[]『』「」<>\"'.")
        if 2 <= len(x) <= 30 and x not in out and not re.fullmatch(r"Q\d+", x) and not re.search(r"(다|요|죠)$", x):
            out.append(x)
    for w in re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]*[A-Za-z0-9]", text):
        add(w)
    for t in _TITLE.findall(text):
        add(t)
    for ln in text.splitlines():
        if ln.strip().startswith(("-", "•", "·")):
            for part in re.split(r"[,，、]", ln.strip().lstrip("-•· ")):
                if len(part.strip()) <= 20 and not part.strip().endswith("."):
                    add(part)
    return out[:limit]


def _asr(src: Source, asr: bool) -> str:
    if not asr:
        return ""
    g = glossary(src)
    return ASR_RULE.replace("{glossary}", ", ".join(g) if g else "(없음)")


def _focus(focus: str) -> str:
    focus = (focus or "").strip()
    return FOCUS_RULE.replace("{focus}", focus[:200]) if focus else ""


# ---------- 단계: 구성 ----------

def structure_from_chapters(src: Source) -> Optional[dict]:
    """올린 사람의 챕터가 2개 이상이면 그대로 구성으로."""
    ch = [c for c in src.chapters if c.get("title")]
    if len(ch) < 2:
        return None
    return {"kind": "chapters", "source": "chapters", "starts": [{"t": float(c["start"]), "label": c["title"]} for c in ch]}


def detect_structure(llm: LLM, src: Source, segments: list[dict], *, out_lang: str = "ko",
                     speakers: Optional[dict] = None) -> dict:
    """받아 적은 글을 시간 조각으로 읽으며 구성 경계를 찾는다 → {kind, source, starts[{t,label}]}.
    조각마다 한 번 부른다 (로컬 8,192 토큰 안). 경계가 2개 미만이면 kind=time (시간으로 자름)."""
    desc = (src.description or "")[:800]
    kinds: dict[str, int] = {}
    starts: list[dict] = []
    for ch in split_sections(segments):
        body = "\n".join(line(s, speakers) for s in segments[ch["first"]:ch["last"] + 1])
        user = (f"{_head(src)}" + (f"\n영상 설명: {desc}" if desc else "")
                + f"\n이 부분: {fmt_t(ch['start'])} ~ {fmt_t(ch['end'])}\n\n받아 적은 말:\n{body}")
        raw = _json_retry(llm, STRUCT_SYSTEM.replace("{lang}", _lang(out_lang)), user, STRUCT_SCHEMA)
        k = str(raw.get("kind") or "talk")
        found = [{"t": _clamp(x.get("t"), ch["start"], ch["end"]), "label": str(x.get("label") or "").strip()}
                 for x in raw.get("starts") or [] if isinstance(x, dict) and str(x.get("label") or "").strip()]
        kinds[k] = kinds.get(k, 0) + len(found) + (1 if k != "talk" else 0)
        starts += found
    starts.sort(key=lambda x: x["t"])
    merged: list[dict] = []
    for st in starts:                                   # 15초 안에 붙은 경계는 하나로 (조각 경계에서 같은 질문을 두 번 짚을 때)
        if merged and st["t"] - merged[-1]["t"] < 15:
            continue
        merged.append(st)
    kind = max(kinds, key=kinds.get) if kinds else "talk"
    if len(merged) < 2:
        return {"kind": "time", "source": "time", "starts": []}
    return {"kind": kind, "source": "llm", "starts": merged}


def units_from_structure(segments: list[dict], structure: dict, budget: int = SECTION_TOKENS) -> list[dict]:
    """구성 경계 → 정리 구간 [{start, end, first, last, label}]. 너무 긴 단위는 시간으로 더 나누고 "(이어서)"를 붙인다."""
    starts = structure.get("starts") or []
    if len(starts) < 2:
        return [{**s, "label": None} for s in split_sections(segments, budget)]
    idx = []
    for st in starts:                                   # 경계 시각 → 그 시각 이후에 시작하는 첫 줄
        # (가장 가까운 줄로 고르면 경계 직전에 시작한 앞 구간의 마지막 문장이 다음 구간으로 넘어갔다 — 2026-11-25 FDE 영상 07:20·10:52)
        i = next((k for k, sg in enumerate(segments) if sg["start"] >= st["t"] - 0.05), None)
        if i is None:
            continue
        if not idx or i > idx[-1][0]:
            idx.append((i, st["label"]))
    units = []
    if idx[0][0] > 0:
        units.append((0, idx[0][0] - 1, "들어가며"))
    for n, (i, lab) in enumerate(idx):
        last = idx[n + 1][0] - 1 if n + 1 < len(idx) else len(segments) - 1
        units.append((i, last, lab))
    out = []
    for first, last, lab in units:
        part = segments[first:last + 1]
        if sum(est_tokens(s["text"]) + 4 for s in part) <= budget * 1.5:
            out.append({"start": part[0]["start"], "end": part[-1]["end"], "first": first, "last": last, "label": lab})
            continue
        for k, sub in enumerate(split_sections(part, budget)):
            out.append({"start": sub["start"], "end": sub["end"], "first": first + sub["first"], "last": first + sub["last"],
                        "label": lab if k == 0 else f"{lab} (이어서)"})
    return out


# ---------- 단계: 구간 ----------

_NUM = re.compile(r"\d[\d,.]*\s*(?:년생|년|주차|주|개월|개|명|퍼센트|%|억|만|천|살|시간|분|초|배|위|차|기|권|번째)?")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]+")
_TITLE = re.compile(r"[『「<《]([^』」>》]{1,40})[』」>》]")


def key_tokens(text: str) -> set[str]:
    """누락 검사용 낱말 — 숫자(두 자리 이상이거나 단위가 붙은 것)·영문 낱말·『』 제목. 스크립트로 센다."""
    out: set[str] = set()
    for m in _NUM.finditer(text):
        tok = m.group(0).replace(",", "").replace(" ", "")
        digits = re.match(r"[\d.]+", tok).group(0).rstrip(".")
        if len(digits.replace(".", "")) >= 2 or tok != digits:
            out.add(digits)
    out |= {w.lower().rstrip(".-") for w in _LATIN.findall(text) if len(w.rstrip(".-")) >= 2}
    out |= {t.strip() for t in _TITLE.findall(text)}
    return {t for t in out if t}


def _section_text(r: dict) -> str:
    bits = [r.get("title", ""), r.get("gist", "")]
    for it in r.get("points") or []:
        bits.append(it["text"])
        bits += it.get("sub") or []
    bits += [m["name"] for m in r.get("mentions") or []]
    return "\n".join(bits)


def coverage(segments_text: str, r: dict) -> list[str]:
    """원문의 숫자·영문·제목 중 정리에 없는 것 (영문은 대소문자 무시)."""
    have = _section_text(r)
    low = have.lower()
    return sorted(t for t in key_tokens(segments_text) if (t.lower() not in low))


def _items(raw_items, lo: float, hi: float) -> list[dict]:
    out = []
    for it in raw_items or []:
        if not isinstance(it, dict) or not str(it.get("text", "")).strip():
            continue
        t = parse_at(it.get("at", it.get("t")))
        out.append({"t": _clamp(t if t is not None else lo, lo, hi), "text": str(it["text"]).strip(),
                    "sub": [str(x).strip() for x in it.get("details") or [] if str(x).strip()],
                    **({"at_unread": True} if t is None else {})})
    return out


def _mentions(raw, lo: float, hi: float) -> list[dict]:
    out = []
    for m in raw or []:
        if isinstance(m, dict) and str(m.get("name", "")).strip():
            t = parse_at(m.get("at"))
            out.append({"name": str(m["name"]).strip(), "kind": m.get("kind") if m.get("kind") in MENTION_KINDS else "기타",
                        "t": _clamp(t if t is not None else lo, lo, hi)})
    return out


def summarize_section(llm: LLM, src: Source, segments: list[dict], sec: dict, *, speakers: Optional[dict] = None,
                      out_lang: str = "ko", prev_title: str = "", purpose: Purpose = "record", focus: str = "",
                      kind: str = "time", fill: bool = True, asr: bool = False) -> dict:
    """구간 하나 → 정보 층 {title, gist, points[{t,text,sub}], mentions, coverage, needs_visual, visual_cues[{t,why}]}.
    숫자·영문·제목 누락을 세고, 빠진 게 있으면 한 번만 채운다. 로컬 컨텍스트를 넘으면 반으로 나눠 합친다."""
    part = segments[sec["first"]:sec["last"] + 1]
    body = "\n".join(line(s, speakers) for s in part)
    label = sec.get("label")
    user = (f"{_head(src)}\n영상 구성: {KIND_HINT.get(kind, KIND_HINT['time'])}"
            + (f"\n이 구간의 구성 단위: {label}" if label else "")
            + f"\n구간: {fmt_t(sec['start'])} ~ {fmt_t(sec['end'])}"
            + (f"\n바로 앞 구간: {prev_title}" if prev_title else "") + f"\n\n받아 적은 말:\n{body}")
    system = SECTION_SYSTEM.replace("{lang}", _lang(out_lang)) + _focus(focus) + _asr(src, asr)
    try:
        raw = _json_retry(llm, system, user, SECTION_SCHEMA_TPL)
    except LLMError as exc:
        if exc.kind != "overflow" or sec["last"] - sec["first"] < 2:
            raise
        mid = (sec["first"] + sec["last"]) // 2
        kw = dict(speakers=speakers, out_lang=out_lang, purpose=purpose, focus=focus, kind=kind, fill=fill, asr=asr)
        a = summarize_section(llm, src, segments, {**sec, "last": mid, "end": segments[mid]["end"]}, prev_title=prev_title, **kw)
        b = summarize_section(llm, src, segments, {**sec, "first": mid + 1, "start": segments[mid + 1]["start"]},
                              prev_title=a["title"], **kw)
        return {"title": a["title"], "gist": " ".join(x for x in (a["gist"], b["gist"]) if x), "points": a["points"] + b["points"],
                "mentions": a["mentions"] + b["mentions"], "coverage": {"checked": a["coverage"]["checked"] + b["coverage"]["checked"],
                                                                        "missing": a["coverage"]["missing"] + b["coverage"]["missing"], "filled": a["coverage"]["filled"] or b["coverage"]["filled"]},
                "needs_visual": a["needs_visual"] or b["needs_visual"], "visual_cues": (a["visual_cues"] + b["visual_cues"])[:MAX_FRAMES]}
    lo, hi = sec["start"], sec["end"]
    pts = _items(raw.get("items"), lo, hi)
    if not pts:
        raise LLMError("bad_output", "구간 정리에 항목이 없습니다")
    r = {"title": str(raw.get("title") or "").strip() or label or f"{fmt_t(lo)} 구간", "gist": str(raw.get("gist") or "").strip(),
         "points": pts, "mentions": _mentions(raw.get("mentions"), lo, hi)}
    src_text = "\n".join(x["text"] for x in part)
    checked = len(key_tokens(src_text))
    missing = coverage(src_text, r)
    filled = False
    if missing and fill:                                  # 한 번만 채운다 (대원칙: 재시도 상한)
        have = "\n".join(f"- [{fmt_t(it['t'])}] {it['text']}" + "".join(f"\n  - {x}" for x in it["sub"]) for it in pts)
        fu = (f"{user}\n\n지금의 기록:\n{have}\n\n기록에 없는 낱말: {', '.join(missing[:40])}")
        extra = _json_retry(llm, FILL_SYSTEM.replace("{lang}", _lang(out_lang)) + _asr(src, asr), fu, FILL_SCHEMA)
        more = _items(extra.get("items"), lo, hi)
        if more:
            r["points"] = sorted(pts + more, key=lambda x: x["t"])
            filled = True
        r["mentions"] += _mentions(extra.get("mentions"), lo, hi)
        missing = coverage(src_text, r)
    r["coverage"] = {"checked": checked, "missing": missing, "filled": filled}
    cues = []
    for c in raw.get("visual_cues") or []:
        if isinstance(c, dict):
            t = parse_at(c.get("at", c.get("t")))
            cues.append({"t": _clamp(t if t is not None else lo, lo, hi), "why": str(c.get("why", "")).strip()})
    cues = cues[:MAX_FRAMES]
    need = bool(raw.get("needs_visual")) and bool(cues)
    return {**r, "needs_visual": need, "visual_cues": cues if need else []}


def frame_times(sec: dict, result: dict, mode: VisualMode) -> list[dict]:
    """볼 시각 [{t, why}] — auto: 모델이 짚은 것, always: 짚은 것 없으면 구간 1/3·2/3, never: 없음."""
    if mode == "never":
        return []
    cues = list(result.get("visual_cues") or [])
    if mode == "always" and not cues:
        span = sec["end"] - sec["start"]
        cues = [{"t": round(sec["start"] + span * f, 1), "why": "항상 보기"} for f in (1 / 3, 2 / 3)]
    seen, out = set(), []
    for c in cues:                                         # 2초 안에 겹치는 시각은 하나로
        key = int(c["t"] // 2)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out[:MAX_FRAMES]


def read_frames(llm: LLM, src: Source, section: dict, frames: Sequence[tuple[float, Path]], *, out_lang: str = "ko") -> list[dict]:
    """화면 몇 장 → [{t, text}] (말에 없고 화면에만 있는 정보)."""
    if not frames:
        return []
    pts = "\n".join(f"- [{fmt_t(p['t'])}] {p['text']}" for p in section["points"])
    times = "\n".join(f"- 그림 {i + 1}: {fmt_t(t)} ({t:.1f}초) — {p.name}" for i, (t, p) in enumerate(frames))
    user = f"{_head(src)}\n구간 제목: {section['title']}\n이 구간에서 말한 것:\n{pts}\n\n화면:\n{times}"
    raw = _json_retry(llm, FRAMES_SYSTEM.replace("{lang}", _lang(out_lang)), user, FRAMES_SCHEMA, [p for _, p in frames])
    ts = [t for t, _ in frames]
    return [{"t": min(ts, key=lambda x: abs(x - _clamp(o.get("t"), 0, 1e9))), "text": _unescape(str(o.get("text", "")).strip())}
            for o in raw.get("observations") or [] if isinstance(o, dict) and str(o.get("text", "")).strip()]


# ---------- 단계: 글 층 (쓰임새마다 다르다 — 다시 쓰기는 여기만) ----------

def _sections_digest(src: Source, sections: list[dict], budget: int) -> str:
    """정보 층을 정리 층의 재료로 — 구간 번호(##n)를 붙이고, 길면 구간마다 앞쪽 항목만."""
    for keep in (16, 8, 5, 3, 2, 1):
        parts = []
        for n, s in enumerate(sections, 1):
            lines = []
            for it in s["points"][:keep]:
                lines.append(f"- {it['text']}")
                lines += [f"  - {x}" for x in (it.get("sub") or [])[:3]]
            lines += [f"- (화면) {o['text']}" for o in (s.get("observations") or [])[:2]]
            head = f"## {n}. [{fmt_t(s['start'])}] {s['title']}" + (f"\n결론: {s['gist']}" if s.get("gist") else "")
            parts.append(head + "\n" + "\n".join(lines))
        body = "\n\n".join(parts)
        if est_tokens(body) <= budget:
            return body
    return body


def overview(llm: LLM, src: Source, sections: list[dict], *, out_lang: str = "ko", budget: int = 4200,
             purpose: Purpose = "record", focus: str = "") -> dict:
    """정보 층 → 정리 층 {tldr[3], summary, answers[{n, line}], takeaways}."""
    desc = (src.description or "")[:600]
    body = _sections_digest(src, sections, budget)
    s_len, a_len, t_len = OVERVIEW_SHAPE.get(purpose, OVERVIEW_SHAPE["record"])
    system = (OVERVIEW_SYSTEM.replace("{lang}", _lang(out_lang)).replace("{summary_len}", s_len)
              .replace("{answers_len}", a_len).replace("{takeaways_len}", t_len) + _focus(focus))
    user = f"{_head(src)}" + (f"\n영상 설명: {desc}" if desc else "") + f"\n\n정보 기록:\n{body}"
    raw = _json_retry(llm, system, user, OVERVIEW_SCHEMA)
    if not str(raw.get("summary") or "").strip():
        raise LLMError("bad_output", "개요가 비었습니다")
    answers = []
    for x in raw.get("answers") or []:
        try:
            n = int(x.get("n"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= n <= len(sections) and str(x.get("line") or "").strip():
            answers.append({"idx": n - 1, "line": str(x["line"]).strip()})
    def sourced(xs) -> list[dict]:
        out = []
        for x in xs or []:
            text = str(x.get("text") if isinstance(x, dict) else x).strip()
            if not text:
                continue
            frm = sorted({int(n) - 1 for n in (x.get("from") if isinstance(x, dict) else []) or []
                          if str(n).lstrip("-").isdigit() and 1 <= int(n) <= len(sections)})
            out.append({"text": text, "from": frm})
        return out
    return {"tldr": sourced(raw.get("tldr"))[:3], "summary": str(raw["summary"]).strip(), "answers": answers,
            "takeaways": sourced(raw.get("takeaways"))}


def article(llm: LLM, src: Source, sections: list[dict], ov: dict, *, out_lang: str = "ko", budget: int = 3600,
            focus: str = "") -> dict:
    """공유 글 한 편 → {title, body(마크다운)}. 출처 줄은 to_markdown 이 붙인다."""
    body = _sections_digest(src, sections, budget)
    lead = "\n".join(f"- {_line(x)}" for x in ov.get("tldr") or [])
    user = f"{_head(src)}\n\n세 줄 결론:\n{lead}\n\n개요: {ov.get('summary', '')}\n\n구간별 정리:\n{body}"
    raw = _json_retry(llm, ARTICLE_SYSTEM.replace("{lang}", _lang(out_lang)) + _focus(focus), user, ARTICLE_SCHEMA)
    if not str(raw.get("body") or "").strip():
        raise LLMError("bad_output", "공유 글이 비었습니다")
    return {"title": str(raw.get("title") or src.title).strip(), "body": _unescape(str(raw["body"]).strip())}


def _json_retry(llm: LLM, system: str, user: str, schema: dict, images: Sequence[Path] = ()) -> dict:
    """형식이 틀리면 한 번 더 (같은 입력). 두 번째도 틀리면 그대로 올린다."""
    try:
        out = llm.json(system, user, schema, images)
        if isinstance(out, dict):
            return out
        raise LLMError("bad_output", "JSON 객체가 아닙니다")
    except LLMError as exc:
        if exc.kind != "bad_output":
            raise
        out = llm.json(system, user, schema, images)
        if not isinstance(out, dict):
            raise LLMError("bad_output", "JSON 객체가 아닙니다")
        return out


def structure_label(structure: Optional[dict], n_sections: int) -> str:
    """구성 한 줄 — "질문 10개" / "챕터 7개" / "시간 구간 6개"."""
    st = structure or {}
    kind = st.get("kind") or "time"
    n = len(st.get("starts") or []) or n_sections
    return f"{KIND_LABEL.get(kind, '주제')} {n}개" if kind != "time" else f"시간 구간 {n_sections}개"


# ---------- 단계: 다듬기 (정보 층을 바꾸지 않고 표현만) ----------

# "~다고 봄·~라고 함" / "~있음·~했음" / 동사 뒤 "~함·~됨" / 띄어 쓴 "봄·함" 으로 끝나는 줄, 그리고 호칭.
# "다음·마음·처음" 같은 명사는 잡지 않는다 ("~음" 은 있·없·했·였… 뒤에서만).
STYLE_BAD = re.compile(r"(?:(?:다|라|자)고\s?(?:함|봄|말함|생각함|설명함|강조함)|(?:있|없|했|였|됐|었|겠|았)음|[가-힣](?:함|됨)|\s(?:봄|함|됨))\.?\s*$"
                       r"|말한 사람|말하는 사람|화자[는가의]")


def style_violations(sections: list[dict]) -> list[dict]:
    """문체 위반 줄 — "~함·~음·~봄"으로 끝남, "말한 사람·화자" 호칭. [{sec, kind: gist|item|sub, i, j, text}]"""
    out = []
    for k, s in enumerate(sections):
        if s.get("edited"):                                # 사람이 고친 구간은 건드리지 않는다
            continue
        if s.get("gist") and STYLE_BAD.search(s["gist"]):
            out.append({"sec": k, "kind": "gist", "i": -1, "j": -1, "text": s["gist"]})
        for i, it in enumerate(s.get("points") or []):
            if STYLE_BAD.search(it["text"]):
                out.append({"sec": k, "kind": "item", "i": i, "j": -1, "text": it["text"]})
            for j, x in enumerate(it.get("sub") or []):
                if STYLE_BAD.search(x):
                    out.append({"sec": k, "kind": "sub", "i": i, "j": j, "text": x})
    return out


STYLE_SCHEMA = {"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "object", "properties": {
    "n": {"type": "integer"}, "text": {"type": "string"}}, "required": ["n", "text"]}}}, "required": ["lines"]}
STYLE_SYSTEM = """너는 기록의 문장 몇 줄을 고친다. 뜻·정보·숫자는 그대로 두고 표현만:
"~함·~음·~봄·~됨" 같은 명사형 끝은 "~한다·~이다·~본다" 같은 "~다" 문장으로 (짧은 명사구로 둬도 된다),
"말한 사람·말하는 사람·화자"는 이름이 주어지면 그 이름으로, 아니면 주어를 뺀다. 줄 번호(n)마다 고친 줄 하나. 글은 {lang}로."""


def fix_style(llm: LLM, src: Source, sections: list[dict], *, out_lang: str = "ko") -> tuple[list[dict], dict]:
    """문체 위반 줄만 한 번에 고친다 → (고친 sections, {found, fixed})."""
    bad = style_violations(sections)
    if not bad:
        return sections, {"found": 0, "fixed": 0}
    speaker = src.uploader or ""
    user = (f"{_head(src)}" + (f"\n이름: {speaker}" if speaker else "") + "\n\n고칠 줄:\n"
            + "\n".join(f"{n}. {b['text']}" for n, b in enumerate(bad[:60], 1)))
    raw = _json_retry(llm, STYLE_SYSTEM.replace("{lang}", _lang(out_lang)), user, STYLE_SCHEMA)
    import copy
    out = copy.deepcopy(sections)
    fixed = 0
    for ln in raw.get("lines") or []:
        try:
            b = bad[int(ln.get("n")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        text = str(ln.get("text") or "").strip()
        if not text or text == b["text"]:
            continue
        sec = out[b["sec"]]
        if b["kind"] == "gist":
            sec["gist"] = text
        elif b["kind"] == "item":
            sec["points"][b["i"]]["text"] = text
        else:
            sec["points"][b["i"]]["sub"][b["j"]] = text
        fixed += 1
    return out, {"found": len(bad), "fixed": fixed, "left": len(style_violations(out))}


MENTIONS_SCHEMA = {"type": "object", "properties": {"groups": {"type": "array", "maxItems": 60, "items": {"type": "object", "properties": {
    "ids": {"type": "array", "minItems": 1, "items": {"type": "integer"}}, "name": {"type": "string"},
    "kind": {"type": "string", "enum": MENTION_KINDS}, "note": {"type": "string"}}, "required": ["ids", "name", "kind", "note"]}}},
                   "required": ["groups"]}
MENTIONS_SYSTEM = """너는 영상에서 뽑은 '언급된 것' 목록(번호·이름·종류·시각)을 다듬는다. 새 이름을 지어내지 않는다.
- 같은 대상을 가리키는 것은 한 묶음으로 (ids 에 번호를 모두, name 은 가장 정확한 표기 하나). 다른 대상(예: 회사와 그 회사의 제품)은 합치지 않는다.
- 영상의 주제어 자체(제목에 나오는 핵심어)와 누구나 아는 일반어(AI 처럼 영상 전체에 깔린 말)는 빼고, 그 번호는 어느 묶음에도 넣지 않는다.
- 숫자는 무엇의 숫자인지 note 에 짧게 (예: "4~6주차 — 기술·비즈니스 최신 내용"). 쪼개진 숫자 조각은 한 묶음으로.
- note 는 그 줄에 붙은 '맥락' 에 적힌 사실로만 쓴다. 맥락에 없으면 짐작하지 말고 빈 글자로 둔다.
  40자 안팎의 명사구나 짧은 "~다" 문장 하나 ("~함·~음·~배움"처럼 명사형으로 끝내지 않는다). 이름이 스스로 뜻이 분명하면 비운다.
- kind 는 바르게 고쳐도 된다. 글은 {lang}로."""


def polish_mentions(llm: LLM, src: Source, merged: list[dict], *, out_lang: str = "ko", asr: bool = False,
                    seen_on_screen: str = "") -> list[dict]:
    """merge_mentions 결과 → 같은 것 합치고 주제어·일반어 빼고 숫자에 맥락. 시각은 묶음의 가장 이른 것."""
    if not merged:
        return []
    user = f"{_head(src)}\n\n목록:\n" + "\n".join(f"{n}. {m['name']} ({m['kind']}, {fmt_t(m['t'])})"
                                                 + (f" — 맥락: {m['ctx'][:120]}" if m.get("ctx") else "") for n, m in enumerate(merged, 1))
    raw = _json_retry(llm, MENTIONS_SYSTEM.replace("{lang}", _lang(out_lang)) + _asr(src, asr), user, MENTIONS_SCHEMA)
    out, used = [], set()
    for g in raw.get("groups") or []:
        ids = [int(i) - 1 for i in g.get("ids") or [] if str(i).isdigit() and 1 <= int(i) <= len(merged) and int(i) - 1 not in used]
        if not ids or not str(g.get("name") or "").strip():
            continue
        used |= set(ids)
        out.append({"name": str(g["name"]).strip(), "kind": g.get("kind") if g.get("kind") in MENTION_KINDS else merged[ids[0]]["kind"],
                    "t": min(merged[i]["t"] for i in ids), "note": str(g.get("note") or "").strip()})
    if asr:
        for m in out:
            if needs_check(m, src, seen_on_screen):
                m["check"] = True
    order = {k: i for i, k in enumerate(MENTION_KINDS)}
    return sorted(out, key=lambda m: (order.get(m["kind"], 99), m["t"]))


CHECK_KINDS = {"책", "강의·자료", "회사·기관", "사람"}


def needs_check(m: dict, src: Source, seen_on_screen: str = "") -> bool:
    """받아 적기 확인 필요 — 글이 stt 일 때, 사람·회사·책·강의 이름인데
    영상 제목·설명·화면 어디에도 같은 표기가 없는 것 (바로잡을 근거가 없는 이름). 숫자·도구 일반어("Claude")는 보지 않는다.
    같은 표기: 공백을 뺀 이름이 있거나, 이름 안의 영문·숫자 낱말이 모두 있으면 (예: "스탠포드 CS 329Z" ↔ "Stanford CS329Z")."""
    if m.get("kind") == "수치":
        return False
    if m.get("kind") not in CHECK_KINDS:                # 대문자 약어 전부로 넓히자 PHP·B2B 같은 흔한 말까지 걸렸다 (2026-11-25 FDE stt)
        return False
    hay = re.sub(r"\s+", "", f"{src.title}{src.uploader}{src.description or ''}{seen_on_screen}").lower()
    name = re.sub(r"\s+|\(.*?\)", "", m["name"]).lower()
    parts = [name] + [re.sub(r"\s+", "", x).lower() for x in re.findall(r"\(([^)]+)\)", m["name"])]   # "클로드(Claude)" 는 둘 다
    if any(p and p in hay for p in parts):
        return False
    latin = re.sub(r"[^a-z0-9]", "", "".join(re.findall(r"[A-Za-z0-9 ]+", m["name"])).lower())
    return not (len(latin) >= 2 and latin in hay)


def merge_mentions(sections: list[dict]) -> list[dict]:
    """구간마다 뽑은 '언급된 것'을 이름으로 합친다 — 처음 나온 시각, 종류 순."""
    seen: dict[str, dict] = {}
    for s in sections:
        lines = [it["text"] + (" / " + " / ".join(it.get("sub") or []) if it.get("sub") else "") for it in s.get("points") or []]
        for m in s.get("mentions") or []:
            k = re.sub(r"\s+", "", m["name"]).lower()
            if k not in seen or m["t"] < seen[k]["t"]:
                ctx = next((ln for ln in lines if m["name"] in ln), "")   # 다듬기에서 메모를 짐작하지 않게 그 이름이 나온 기록 줄
                seen[k] = {**m, **({"ctx": ctx} if ctx else {})}
    order = {k: i for i, k in enumerate(MENTION_KINDS)}
    return sorted(seen.values(), key=lambda m: (order.get(m["kind"], 99), m["t"]))


def _line(x) -> str:
    return x["text"] if isinstance(x, dict) else str(x)


def _cite(x, sections: list[dict]) -> str:
    frm = x.get("from") if isinstance(x, dict) else None
    return (" — 구간 " + ", ".join(fmt_t(sections[i]["start"]) for i in frm if i < len(sections))) if frm else ""


def to_markdown(src: Source, ov: Optional[dict], sections: list[dict], *, transcript_source: str = "", lang: str = "",
                structure: Optional[dict] = None, art: Optional[dict] = None, mentions: Optional[list] = None) -> str:
    link = lambda t: f"[{fmt_t(t)}]({src.link(t)})" if src.link(t) else f"[{fmt_t(t)}]"
    out = [f"# {src.title or src.ref}", "", f"- 출처: {src.webpage_url or src.ref}"]
    if src.duration:
        out.append(f"- 길이: {fmt_t(src.duration)}")
    if transcript_source:
        out.append(f"- 글: {CAPTION_LABEL.get(transcript_source, transcript_source)}" + (f" ({lang})" if lang else ""))
    out += [f"- 구성: {structure_label(structure, len(sections))}", ""]
    out += ["# 정리", ""]
    if ov:
        if ov.get("tldr"):
            out += ["## 세 줄 결론", ""] + [f"{i + 1}. {_line(x)}{_cite(x, sections)}" for i, x in enumerate(ov["tldr"])] + [""]
        out += ["## 개요", "", ov["summary"], ""]
        if ov.get("answers"):
            out += ["## 구간별 한 줄", ""]
            for a in ov["answers"]:
                if a["idx"] < len(sections):
                    s = sections[a["idx"]]
                    out.append(f"- {link(s['start'])} **{s['title']}** — {a['line']}")
            out.append("")
        if ov.get("takeaways"):
            out += ["## 가져갈 것", ""] + [f"- {_line(x)}{_cite(x, sections)}" for x in ov["takeaways"]] + [""]
    else:
        out += ["(아직 정리 전)", ""]
    if art:
        out += [f"## 공유 글: {art['title']}", "", art["body"], "", f"출처: {src.title} — {src.webpage_url or src.ref}", ""]
    out += ["# 정보", ""]
    ms = mentions if mentions is not None else merge_mentions(sections)
    if ms:
        out += ["## 언급된 것", ""]
        for k in MENTION_KINDS:
            group = [m for m in ms if m["kind"] == k]
            if group:
                out.append(f"- {k}: " + ", ".join(f"{m['name']}" + (" [받아 적기 확인 필요]" if m.get("check") else "")
                                                  + (f" — {m['note']}" if m.get("note") else "") + f" ({fmt_t(m['t'])})"
                                                  for m in group))
        out.append("")
    for s in sections:
        out += [f"## {link(s['start'])} {s['title']}", ""]
        if s.get("gist"):
            out += [f"> {s['gist']}", ""]
        for it in s["points"]:
            out.append(f"- {it['text']} ({fmt_t(it['t'])})")
            out += [f"  - {x}" for x in it.get("sub") or []]
        for o in s.get("observations") or []:
            out.append(f"- 화면 {fmt_t(o['t'])}: {o['text']}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


CAPTION_LABEL = {"uploader": "업로더 자막", "embedded": "파일 안 자막", "sidecar": "옆 자막 파일", "stt": "stt 받아 적기"}


# ---------- 의존을 묶은 엔진 ----------

@dataclass
class Transcript:
    source: CaptionSource
    lang: Optional[str]
    segments: list[dict]
    speakers: dict = field(default_factory=dict)
    stt: dict = field(default_factory=dict)       # {taskId, reviewUrl} — stt 를 썼을 때


class Engine:
    def __init__(self, *, remote: Remote, stt: SttClient, llms: dict[Backend, LLM]):
        self.remote, self.stt, self.llms = remote, stt, llms
        self._streams: dict[str, str] = {}

    def probe(self, ref: str) -> Source:
        return sources.probe(ref, self.remote)

    def captions(self, src: Source, work: Path, lang: Optional[str] = None) -> Optional[Transcript]:
        got = sources.captions(src, work, self.remote, lang)
        return Transcript(source=got[0], lang=got[1], segments=got[2]) if got else None

    def submit_stt(self, src: Source, message_id: str, lang: Optional[str] = None, force: bool = False) -> stt_client.SttStatus:
        ok, why = self.stt.available()
        if not ok:
            raise SttError("stt_down", why)
        ctx = "\n".join(x for x in (src.title, src.uploader, (src.description or "")[:800]) if x)
        if src.kind == "file":
            return self.stt.submit(message_id, path=src.ref, lang=lang, context=ctx, title=src.title, force=force)
        return self.stt.submit(message_id, url=src.ref, lang=lang, context=ctx, title=src.title, force=force)

    @staticmethod
    def stt_transcript(st: stt_client.SttStatus) -> Transcript:
        segs = [{"start": s["start"], "end": s["end"], "text": s["text"], **({"speaker": s["speaker"]} if s.get("speaker") else {})}
                for s in st.segments or [] if str(s.get("text", "")).strip()]
        return Transcript(source="stt", lang=st.language, segments=segs, speakers=st.speakers or {},
                          stt={"taskId": st.task_id, "reviewUrl": st.review_url})

    def frame_input(self, src: Source) -> str:
        if src.kind == "file":
            if not sources.has_video(Path(src.ref)):
                raise SourceError("fetch", "영상 트랙이 없는 파일입니다 (소리만)")
            return src.ref
        if src.ref not in self._streams:
            self._streams[src.ref] = self.remote.stream_url(src.ref)
        return self._streams[src.ref]

    def grab(self, src: Source, times: Sequence[float], dest: Path) -> list[tuple[float, Path]]:
        inp = self.frame_input(src)
        return [(t, sources.grab_frame(inp, t, dest / f"f{int(round(t * 10)):06d}.jpg")) for t in times]

    def llm(self, backend: Backend) -> LLM:
        if backend not in self.llms:
            raise LLMError("llm_down", f"정리하는 쪽 '{backend}' 이 없습니다")
        return self.llms[backend]

    def structure(self, llm: LLM, src: Source, tr: "Transcript", out_lang: str = "ko") -> dict:
        """구성: 올린 사람의 챕터 → 없으면 LLM 이 경계를 찾음 → 못 찾으면 시간."""
        return structure_from_chapters(src) or detect_structure(llm, src, tr.segments, out_lang=out_lang, speakers=tr.speakers)

    def read(self, ref: str, work: Path, *, backend: Backend = "local", visual: VisualMode = "auto", lang: Optional[str] = None,
             out_lang: str = "ko", log: Callable[[str], None] = print, message_id: Optional[str] = None,
             purpose: Purpose = "record", focus: str = "") -> dict:
        """처음부터 끝까지 한 번에 (저장 없음). 서비스는 단계를 하나씩 부르고 결과를 저장한다."""
        llm = self.llm(backend)
        ok, why = llm.available()
        if not ok:
            raise LLMError("llm_down", why)
        src = self.probe(ref)
        log(f"영상: {src.title} ({fmt_t(src.duration or 0)})")
        tr = self.captions(src, work, lang)
        if tr is None:
            log("사람이 만든 자막 없음 → stt 에 받아 적기를 맡김")
            st = self.submit_stt(src, message_id or f"video_reader-cli-{abs(hash(src.ref))}", lang)
            st = stt_client.wait(self.stt, st, cancelled=lambda: False, on_stage=lambda s: log(f"  stt: {s}"))
            tr = self.stt_transcript(st)
        log(f"글: {CAPTION_LABEL[tr.source]} {tr.lang or ''} — {len(tr.segments)}줄")
        struct = self.structure(llm, src, tr, out_lang)
        units = units_from_structure(tr.segments, struct)
        log(f"구성: {structure_label(struct, len(units))} ({struct['source']})")
        vis_ok, vis_why = llm.vision()
        secs, prev = [], ""
        for k, sec in enumerate(units):
            r = summarize_section(llm, src, tr.segments, sec, speakers=tr.speakers, out_lang=out_lang, prev_title=prev,
                                  purpose=purpose, focus=focus, kind=struct["kind"])
            s = {**sec, **r, "frames": [], "observations": []}
            want = frame_times(sec, r, visual)
            if want:
                shots = self.grab(src, [c["t"] for c in want], work / "frames")
                s["frames"] = [{"t": t, "file": p.name, "why": c["why"], "read": bool(vis_ok)} for (t, p), c in zip(shots, want)]
                if vis_ok:
                    s["observations"] = read_frames(llm, src, s, shots, out_lang=out_lang)
                else:
                    log(f"  화면 읽기 잠김: {vis_why}")
            log(f"  구간 {k + 1}: [{fmt_t(sec['start'])}] {s['title']} — 요점 {len(s['points'])}, 화면 {len(s['frames'])}")
            secs.append(s)
            prev = s["title"]
        ov = overview(llm, src, secs, out_lang=out_lang, purpose=purpose, focus=focus)
        art = article(llm, src, secs, ov, out_lang=out_lang, focus=focus) if purpose == "share" else None
        return {"source": src.to_dict(), "transcript": {"source": tr.source, "lang": tr.lang, "stt": tr.stt},
                "structure": struct, "purpose": purpose, "focus": focus, "overview": ov, "article": art, "sections": secs,
                "markdown": to_markdown(src, ov, secs, transcript_source=tr.source, lang=tr.lang or "", structure=struct, art=art)}


def default_engine(*, stt_url: Optional[str] = None, local_url: Optional[str] = None, local_vision: Optional[bool] = None,
                   claude_model: Optional[str] = None) -> Engine:
    """실제 의존으로 엔진을 만든다. 인자가 없으면 환경 변수: VR_STT_URL · VR_LOCAL_URL · VR_LOCAL_VISION=1 · VR_CLAUDE_MODEL."""
    import os
    from video_reader.llm import ClaudeLLM, LocalLLM
    env = os.environ.get
    return Engine(remote=sources.YtDlp(), stt=stt_client.A2ASttClient(stt_url or env("VR_STT_URL") or stt_client.DEFAULT_URL),
                  llms={"local": LocalLLM(local_url or env("VR_LOCAL_URL") or "http://127.0.0.1:8002/v1",
                                          vision=local_vision if local_vision is not None else env("VR_LOCAL_VISION") == "1"),
                        "claude": ClaudeLLM(model=claude_model or env("VR_CLAUDE_MODEL") or None)})


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False)
