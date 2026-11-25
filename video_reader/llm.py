"""정리하는 쪽 두 가지 — 로컬 LLM(vLLM, OpenAI 호환)과 Claude Code headless (`claude -p`). 같은 좁은 Protocol.

둘 다 "시스템 지시 + 글 (+ 그림) → 스키마에 맞는 JSON 하나" 만 한다. 도구 호출·대화는 없다.
- local: 기본 http://127.0.0.1:8002/v1. 컨텍스트 8,192 토큰이라 엔진이 구간을 작게 자른다.
  그림은 서버가 받을 때만 (`vision=True`). 지금 띄우는 명령(REF/LLM.md)은 `--limit-mm-per-prompt image:0` 이라 기본 꺼짐.
- claude: 설정·CLAUDE.md·MCP·슬래시 명령을 싣지 않고(격리), 시스템 프롬프트를 우리 것으로 바꾼다. 그림은 Read 도구로 그 폴더만.
  첫 측정(2026-11-24): 그림 한 장 + 짧은 지시 5.7초, $0.025.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Literal, Optional, Protocol, Sequence

import httpx

# 켜져 있나 확인용. 이 PC(WSL)는 닫힌 포트가 거부 대신 연결 시간 초과까지 기다려서(2026-11-24 측정 20초) 연결 대기를 짧게.
PROBE_TIMEOUT = httpx.Timeout(3.0, connect=1.0)

Backend = Literal["local", "claude"]
BACKENDS: tuple[Backend, ...] = ("local", "claude")
PKG_ROOT = Path(__file__).resolve().parent.parent


class LLMError(Exception):
    """kind: llm_down(꺼짐·없음) / overflow(컨텍스트 초과) / bad_output(형식 틀림) / llm_error"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


class LLM(Protocol):
    name: Backend

    def available(self) -> tuple[bool, str]: ...       # (지금 쓸 수 있나, 아니면 이유와 켜는 법)
    def vision(self) -> tuple[bool, str]: ...          # 그림을 읽을 수 있나
    def json(self, system: str, user: str, schema: dict, images: Sequence[Path] = ()) -> dict: ...


LOCAL_HOW_TO = ("켜는 법: REF/LLM.md 2절 'LLM만' 명령 (vllm serve ~/models/Qwen3.8-27B-INT4 … --port 8002). "
                "stt 와 GPU 를 같이 쓰므로 받아 적기가 끝난 뒤에 켭니다")


class LocalLLM:
    name: Backend = "local"

    def __init__(self, base: str = "http://127.0.0.1:8002/v1", model: Optional[str] = None, *, vision: bool = False,
                 timeout: float = 300, max_tokens: int = 1500):
        self.base, self._model, self._vision, self.timeout, self.max_tokens = base.rstrip("/"), model, vision, timeout, max_tokens

    def _models(self) -> list[str]:
        r = httpx.get(f"{self.base}/models", timeout=PROBE_TIMEOUT)
        return [m["id"] for m in r.json().get("data", [])]

    def available(self):
        try:
            ids = self._models()
        except (httpx.HTTPError, ValueError, KeyError):
            return False, f"{self.base} 에 닿지 않습니다. {LOCAL_HOW_TO}"
        if not ids:
            return False, "로컬 LLM 서버에 모델이 없습니다"
        self._model = self._model or ids[0]
        return True, ""

    def vision(self):
        if self._vision:
            return True, ""
        return False, ("그림을 받지 않게 떠 있습니다 (--limit-mm-per-prompt image:0). 화면 읽기는 Claude Code 로, "
                       "또는 image:N 으로 다시 띄우고 VR_LOCAL_VISION=1")

    def json(self, system, user, schema, images=()):
        ok, why = self.available()
        if not ok:
            raise LLMError("llm_down", why)
        content: list | str = user
        if images:
            if not self._vision:
                raise LLMError("llm_error", self.vision()[1])
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()}}
                for p in images]
        body = {"model": self._model, "temperature": 0, "max_tokens": self.max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}}
        try:
            r = httpx.post(f"{self.base}/chat/completions", json=body, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise LLMError("llm_down", f"로컬 LLM 호출 실패 ({type(exc).__name__}). {LOCAL_HOW_TO}") from exc
        if r.status_code == 400 and "context length" in r.text:
            raise LLMError("overflow", "로컬 LLM 컨텍스트(8,192 토큰)를 넘었습니다")
        if r.status_code != 200:
            raise LLMError("llm_error", f"로컬 LLM 오류 {r.status_code}: {r.text[:200]}")
        choice = r.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise LLMError("overflow", "로컬 LLM 출력이 max_tokens 에서 잘렸습니다")
        try:
            return json.loads(choice["message"]["content"])
        except (ValueError, TypeError) as exc:
            raise LLMError("bad_output", f"로컬 LLM 이 JSON 이 아닌 답을 냈습니다: {str(choice['message'].get('content'))[:160]}") from exc


CLAUDE_HOW_TO = "Claude Code 를 설치하고 로그인하세요 (npm i -g @anthropic-ai/claude-code → claude)"


class ClaudeLLM:
    name: Backend = "claude"

    def __init__(self, binary: Optional[str] = None, *, model: Optional[str] = None, max_budget_usd: float = 0.5,
                 timeout: int = 300):
        self.bin = binary if binary is not None else shutil.which("claude")
        self.model, self.max_budget_usd, self.timeout = model, max_budget_usd, timeout
        self.last: dict = {}

    def available(self):
        if not self.bin:
            return False, f"claude 명령이 없습니다 — {CLAUDE_HOW_TO}"
        return True, ""

    def vision(self):
        ok, why = self.available()
        return (True, "") if ok else (False, why)

    def command(self, system: str, user: str, schema: dict, images: Sequence[Path]) -> list[str]:
        cmd = [self.bin or "claude", "-p", user, "--output-format", "json",
               "--json-schema", json.dumps(schema, ensure_ascii=False),
               "--system-prompt", system, "--setting-sources", "", "--disable-slash-commands", "--strict-mcp-config",
               "--no-session-persistence", "--max-budget-usd", str(self.max_budget_usd)]
        if images:
            dirs = sorted({str(p.parent) for p in images})
            cmd += ["--tools", "Read", "--allowedTools", "Read", "--add-dir", *dirs]
        else:
            cmd += ["--tools", ""]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def json(self, system, user, schema, images=()):
        ok, why = self.available()
        if not ok:
            raise LLMError("llm_down", why)
        if images:
            user += "\n\n화면 그림 (Read 도구로 모두 열어 본다):\n" + "\n".join(f"- {p}" for p in images)
        try:
            r = subprocess.run(self.command(system, user, schema, images), capture_output=True, text=True,
                               timeout=self.timeout, cwd=PKG_ROOT, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise LLMError("llm_error", f"claude 시간 초과 ({self.timeout}초)") from exc
        try:
            out = json.loads(r.stdout)
        except ValueError as exc:
            raise LLMError("llm_error", f"claude 가 오류로 끝남 (코드 {r.returncode}): "
                                        f"{(r.stderr or r.stdout).strip()[-200:] or '로그인 상태 확인: claude'}") from exc
        self.last = {"cost_usd": out.get("total_cost_usd"), "seconds": (out.get("duration_ms") or 0) / 1000}
        if out.get("is_error") or out.get("subtype") != "success":
            raise LLMError("llm_error", f"claude 실패: {str(out.get('result') or out.get('subtype'))[:200]}")
        so = out.get("structured_output")
        if isinstance(so, dict):
            return so
        try:
            return json.loads(out.get("result") or "")
        except ValueError as exc:
            raise LLMError("bad_output", f"claude 가 JSON 이 아닌 답을 냈습니다: {str(out.get('result'))[:160]}") from exc
