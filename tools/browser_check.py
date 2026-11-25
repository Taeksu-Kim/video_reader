"""실제 브라우저로 제품 화면 과업을 눌러 본다. 서버는 이 스크립트가 직접 띄운다 — 모델·네트워크(yt-dlp·stt)만 가짜, ffmpeg·SQLite·화면은 진짜.

    python tools/browser_check.py [--shots 폴더] [--headed]

과업: 첫 화면 / 여러 줄 넣기 / 같은 요청 2회 / A2A 로 맡긴 일이 목록에 / 끝난 인화지(칸·그림·시각→뷰어) / 받아 적은 글 /
고치기(고침 표시·마크다운 반영) / 잠긴 쪽 메뉴 / 취소 / 모델 꺼짐 → 다른 쪽으로 이어서 / 재시작 후 이어서 / 좁은 폭 / 콘솔 오류 0.
"""
from __future__ import annotations

import argparse
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import conftest as C  # noqa: E402  (가짜 Remote·Stt·LLM 과 시험 영상 만들기를 테스트와 같이 쓴다)
from video_reader.app import create_app  # noqa: E402
from video_reader.engine import Engine  # noqa: E402

YT = "https://www.youtube.com/watch?v=abc123XYZ00"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, data: Path, fakes: dict, port: int):
        self.data, self.fakes, self.port = data, fakes, port

    def start(self):
        eng = Engine(remote=self.fakes["remote"], stt=self.fakes["stt"], llms=self.fakes["llms"])
        self.app = create_app(self.data, engine=eng, stt_interval=0.2)
        self.srv = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.th = threading.Thread(target=self.srv.run, daemon=True)
        self.th.start()
        for _ in range(100):
            try:
                httpx.get(f"http://127.0.0.1:{self.port}/api/jobs", timeout=1)
                return self
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("서버가 뜨지 않음")

    def crash(self):
        """갑자기 꺼진 것처럼 — 일꾼을 멈추고 서버를 내린다 (작업 상태는 그대로 남음)."""
        self.app.state.svc._stop = True
        self.srv.should_exit = True
        self.th.join(timeout=10)


class Check:
    def __init__(self):
        self.ok, self.fail = [], []

    def __call__(self, name: str, cond: bool, detail: str = ""):
        (self.ok if cond else self.fail).append(name)
        print(("  통과 " if cond else "  실패 ") + name + (f" — {detail}" if detail and not cond else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", type=Path)
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="vr_check_"))
    media_dir = tmp / "media"
    media_dir.mkdir()

    media = C.make_media(media_dir)
    fakes = {"remote": C.FakeRemote(media), "stt": C.FakeStt(),
             "llms": {"local": C.FakeLLM("local", up=False), "claude": C.FakeLLM("claude")}}
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    srv = Server(tmp / "data", fakes, port).start()
    ck = Check()
    shots = a.shots
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    def shot(pg, name):
        if shots:
            pg.screenshot(path=str(shots / f"{name}.png"))

    with sync_playwright() as p:
        b = p.chromium.launch(headless=not a.headed)
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        errs: list[str] = []
        # 넣기 검증의 400 은 의도한 응답이라 콘솔 자원 실패 줄은 빼고, 응답을 직접 본다: 5xx 또는 화면이 읽는 GET 의 실패만 오류
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "youtube" not in m.text
              and not m.text.startswith("Failed to load resource") else None)
        pg.on("response", lambda r: errs.append(f"{r.request.method} {r.url} {r.status}")
              if r.url.startswith(base) and (r.status >= 500 or (r.status >= 400 and r.request.method == "GET")) else None)
        pg.on("pageerror", lambda e: errs.append(str(e)))

        def wait_text(sel, text, timeout=20000):
            pg.wait_for_function("([s,t]) => (document.querySelector(s)?.innerText || '').includes(t)", arg=[sel, text], timeout=timeout)

        print("1. 첫 화면")
        pg.goto(base + "/")
        wait_text("#main", "인화지 한 장")
        ck("빈 화면이 사용법을 말함", "자동 자막은 쓰지 않습니다" in pg.inner_text("#main"))
        ck("꺼진 의존(로컬 LLM)이 '켜는 법'과 함께 보임", "로컬 LLM 꺼짐" in pg.inner_text("#deps") and "켜는 법" in pg.inner_text("#deps"))
        ck("저장된 선택이 없으면 켜진 쪽(Claude Code)이 골라짐", pg.get_attribute("#backend [data-b=claude]", "aria-pressed") == "true")
        shot(pg, "c1-empty")

        print("2. 빈 입력 / 여러 줄 넣기")
        pg.click("#go")
        ck("빈 입력은 이유를 말하고 넣지 않음", "넣으세요" in pg.inner_text("#feedErr"))
        pg.fill("#ref", f"{YT}\n{media['embedded']}\n상대경로/영상.mp4")
        pg.click("#go")
        wait_text("#jobs", "캐시 강의")
        pg.wait_for_timeout(500)
        ck("두 줄은 두 작업으로 목록에", pg.locator(".job").count() == 2, str(pg.locator(".job").count()))
        ck("틀린 줄만 남기고 이유를 보임", "절대 경로" in pg.inner_text("#feedErr") and pg.input_value("#ref").strip() == "상대경로/영상.mp4")

        print("3. 끝난 인화지")
        jobs = httpx.get(base + "/api/jobs").json()["jobs"]
        yid = next(j["id"] for j in jobs if j["kind"] == "youtube")
        jid = next(j["id"] for j in jobs if j["kind"] == "file")
        pg.goto(f"{base}/#job={yid}")
        wait_text("#main", "가져갈 것")
        ck("끝나면 정리 탭이 먼저 — 세 줄 결론·구간별 한 줄", pg.get_attribute("#tab-summary", "aria-selected") == "true"
           and pg.locator(".tldr li").count() == 3 and pg.locator(".answers li").count() >= 1)
        pg.locator(".answers .jump").first.click()
        pg.wait_for_selector("#cut-0", state="visible")
        ck("한 줄 답을 누르면 정보 탭의 그 구간으로", pg.get_attribute("#tab-info", "aria-selected") == "true")
        ck("정보 탭에 언급된 것과 누락 검사", pg.locator(".mentions .mn").count() >= 1 and "숫자·이름" in pg.inner_text(".mentions"))
        ck("항목 아래 딸린 이유·사례", pg.locator(".pts .sub li").count() >= 1)
        shot(pg, "c3-info")
        pg.locator(".pts .tc").first.click()
        pg.wait_for_selector(".viewer iframe", timeout=5000)
        ck("유튜브는 시각을 누르면 그 시각부터 재생", "start=" in (pg.get_attribute(".viewer iframe", "src") or ""))
        pg.click("#vclose")
        pg.goto(f"{base}/#job={jid}")
        wait_text("#main", "가져갈 것")
        pg.click("#tab-info")
        pg.wait_for_timeout(500)
        ck("구간마다 띠 칸이 있음", pg.locator(".cut .strip .frame").count() >= pg.locator(".cut").count() >= 1)
        imgs = pg.eval_on_selector_all(".strip img", "els => els.map(e => e.complete && e.naturalWidth > 0)")
        ck("화면 칸에 실제 그림이 뜸", bool(imgs) and all(imgs), str(imgs))
        ck("화면에서 읽은 것이 보임", pg.locator(".seen").count() >= 1)
        shot(pg, "c3-done")
        pg.locator(".pts .tc").first.click()
        pg.wait_for_selector(".viewer video", timeout=5000)
        ck("파일은 시각을 누르면 그 시각으로 재생", "#t=" in (pg.get_attribute(".viewer video", "src") or ""))
        pg.click("#vclose")
        pg.locator("[data-act=tr]").first.click()
        pg.wait_for_selector("#cut-0 .tr li", timeout=5000)
        ck("받아 적은 글이 펼쳐짐", pg.locator("#cut-0 .tr li").count() >= 1)

        print("4. 고치기")
        pg.locator("#cut-0 [data-act=edit]").click()
        pg.fill("#et", "사람이 고친 제목")
        pg.locator("#cut-0 .edit textarea").first.fill("사람이 고친 요점")
        pg.keyboard.press("Control+Enter")
        wait_text("#cut-0", "고침")
        ck("고친 구간에 '고침' 표시", pg.locator("#cut-0.edited").count() == 1)
        md = httpx.get(f"{base}/api/jobs/{jid}/summary.md").text
        ck("마크다운에 고친 내용이 반영", "사람이 고친 제목" in md and "사람이 고친 요점" in md)
        pg.locator("#cut-0 [data-act=regen]").click()
        pg.wait_for_selector(".menu")
        locked = pg.locator(".menu button[disabled]").inner_text() if pg.locator(".menu button[disabled]").count() else ""
        ck("꺼진 쪽은 메뉴에서 잠기고 이유가 있음", "로컬 LLM" in locked and "꺼져" in locked, locked)
        shot(pg, "c4-menu")
        pg.keyboard.press("Escape")

        print("5. 같은 요청 2회")
        n = pg.locator(".job").count()
        pg.fill("#ref", YT)
        pg.click("#go")
        pg.wait_for_selector(".toast:has-text('이미 정리한')", timeout=5000)
        ck("같은 설정이면 새로 만들지 않고 그 결과를 엶", pg.locator(".job").count() == n)

        print("6. A2A 로 맡긴 일")
        r = httpx.post(base + "/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
            "messageId": "check-a2a", "role": "ROLE_USER", "parts": [{"data": {"path": str(media["sidecar"]), "backend": "claude"}}],
            "metadata": {"caller": "paper_analyst"}}}}).json()
        review = r["result"]["task"]["metadata"]["reviewUrl"]
        wait_text("#jobs", "paper_analyst가 맡김")
        ck("A2A 로 맡긴 일이 목록에 맡긴 곳과 함께", True)
        pg.goto(review)
        wait_text("#main", "맡긴 에이전트")
        ck("reviewUrl 로 그 작업이 열림", "lecture" in pg.inner_text("h1"))

        print("7. 취소")
        fakes["stt"].gate.clear()
        pg.fill("#ref", str(media["plain"]))
        pg.click("#go")
        wait_text("#main", "stt 받아 적는 중")
        shot(pg, "c7-running")
        pg.click("[data-act=cancel]")
        wait_text("#main", "취소했습니다")
        ck("취소하면 '이어서 정리'가 남음", pg.locator("[data-act=retry]").count() == 1)
        ck("stt 쪽 작업도 취소됨", bool(fakes["stt"].canceled))
        fakes["stt"].gate.set()

        print("8. 모델 꺼짐 → 다른 쪽으로 이어서")
        pg.click("#backend [data-b=local]")
        ck("꺼진 쪽을 고르면 넣기 전에 알려 줌", "꺼져 있습니다" in pg.inner_text("#lockNote"))
        pg.fill("#ref", str(media["sidecar"]))
        pg.click("#go")
        wait_text("#main", "꺼져 있어 멈췄습니다")
        ck("멈춘 이유와 받아 둔 것을 말함", "글은 받아 두었습니다" in pg.inner_text(".band"))
        shot(pg, "c8-llm-down")
        pg.click("[data-act=retry][data-b=claude]")
        wait_text("#main", "가져갈 것")
        ck("다른 쪽으로 이어서 끝남", "Claude Code" in pg.inner_text(".facts"))

        print("9. 재시작 후 이어서")
        fakes["stt"].gate.clear()
        pg.click("#backend [data-b=claude]")
        pg.fill("#ref", str(media["audio"]))
        pg.click("#go")
        wait_text("#main", "stt 받아 적는 중")
        rid = httpx.get(base + "/api/jobs").json()["jobs"][0]["id"]
        srv.crash()
        fakes["stt"].gate.set()
        srv = Server(tmp / "data", fakes, port).start()
        pg.goto(f"{base}/#job={rid}")
        wait_text("#main", "가져갈 것", timeout=30000)
        ck("재시작 뒤 같은 stt 작업을 이어 받아 끝남", len(fakes["stt"].tasks) == len(fakes["stt"].by_mid))

        print("10. 좁은 폭")
        pg.set_viewport_size({"width": 390, "height": 844})
        pg.wait_for_timeout(300)
        sw = pg.evaluate("document.documentElement.scrollWidth")
        ck("390 폭에서 가로 스크롤 없음", sw <= 390, f"scrollWidth {sw}")
        shot(pg, "c10-narrow")
        ck("콘솔 오류 0", not errs, "; ".join(errs[:3]))
        b.close()
    srv.crash()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(ck.ok)}/{len(ck.ok) + len(ck.fail)} 통과" + (f" — 실패: {', '.join(ck.fail)}" if ck.fail else ""))
    return 1 if ck.fail else 0


if __name__ == "__main__":
    sys.exit(main())
