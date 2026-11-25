"""제품 API 진입점 — 입력 종류별 글 확보, 화면 읽기, 사람 개입, 부정 케이스."""
from __future__ import annotations

from conftest import FakeLLM, make_client, wait_job

YT = "https://www.youtube.com/watch?v=abc123XYZ00"


def create(c, ref, **kw):
    r = c.post("/api/jobs", json={"ref": ref, **kw})
    assert r.status_code in (200, 201), r.text
    return r.json()


# ---------- 입력 검사 (부정 먼저) ----------

def test_bad_inputs(client, tmp_path):
    for ref, kind in [("", "bad_source"), ("relative/video.mp4", "bad_source"), (str(tmp_path / "없음.mp4"), "bad_source")]:
        r = client.post("/api/jobs", json={"ref": ref})
        assert r.status_code == 400 and r.json()["error"] == kind, r.text
    r = client.post("/api/jobs", json={"ref": YT, "backend": "gpt"})
    assert r.status_code == 400
    r = client.post("/api/jobs", json={"ref": YT, "visual": "sometimes"})
    assert r.status_code == 400
    assert client.get("/api/jobs").json()["jobs"] == []


def test_broken_url_fails_with_reason(client):
    j = create(client, "https://www.youtube.com/watch?v=broken0000")["job"]
    d = wait_job(client, j["id"])
    assert d["status"] == "failed" and d["errorKind"] == "fetch" and "404" in d["error"]


# ---------- 글 확보 경로 ----------

def test_youtube_uploader_subs_no_stt(client, fakes):
    out = create(client, YT, backend="claude")
    d = wait_job(client, out["job"]["id"])
    assert d["status"] == "done", d
    assert d["transcript"]["source"] == "uploader" and d["transcript"]["lang"] == "ko"
    assert fakes["stt"].submits == 0                            # 자막이 있으면 stt 를 부르지 않는다
    assert d["overview"]["summary"].startswith("claude")
    secs = d["sections"]
    assert secs and all(s["status"] == "done" for s in secs)
    vis = [s for s in secs if s["needsVisual"]]
    assert vis and vis[0]["frames"] and vis[0]["observations"]   # 그래프를 가리킨 구간은 화면을 읽었다
    assert all(f["read"] is True for f in vis[0]["frames"])
    assert client.get(vis[0]["frames"][0]["url"]).headers["content-type"] == "image/jpeg"
    assert secs[0]["link"].startswith("https://www.youtube.com/watch?v=abc123XYZ00&t=")
    assert d["costUsd"] > 0
    md = client.get(f"/api/jobs/{d['id']}/summary.md").text
    assert "## 개요" in md and "화면 " in md and "업로더 자막" in md


def test_youtube_without_uploader_subs_uses_stt(tmp_path, fakes, media):
    from conftest import FakeRemote
    fakes["remote"] = FakeRemote(media, subs={})                 # 자동 자막만 있는 영상 = 업로더 자막 없음
    with make_client(tmp_path, fakes) as c:
        d = wait_job(c, create(c, YT)["job"]["id"])
        assert d["status"] == "done", d
        assert d["transcript"]["source"] == "stt" and d["stt"]["reviewUrl"].startswith("http://stt/")
        assert d["transcript"]["speakers"] == {"S1": "진행자", "S2": "발표자"}
        seg = c.get(f"/api/jobs/{d['id']}/transcript", params={"section": 0}).json()["segments"]
        assert seg[0]["speaker"] == "S1"


def test_local_files_embedded_sidecar_none(client, fakes, media):
    e = wait_job(client, create(client, str(media["embedded"]))["job"]["id"])
    s = wait_job(client, create(client, str(media["sidecar"]))["job"]["id"])
    n = wait_job(client, create(client, str(media["plain"]))["job"]["id"])
    assert (e["transcript"]["source"], s["transcript"]["source"], n["transcript"]["source"]) == ("embedded", "sidecar", "stt")
    assert s["transcript"]["lang"] == "ko"
    assert fakes["stt"].submits == 1
    # 로컬 LLM 은 그림을 못 읽는다 → 그림만 붙이고 경고 (조용히 빼지 않는다)
    vis = [x for x in e["sections"] if x["needsVisual"]]
    assert vis and vis[0]["frames"] and not vis[0]["observations"]
    assert all(f["read"] is False for f in vis[0]["frames"])     # 읽지 못한 것과 "읽었는데 더할 것 없음"을 화면이 가를 수 있게
    assert any("화면 읽기 잠김" in w for w in e["warnings"])
    assert client.get(f"/api/jobs/{e['id']}/media").status_code == 200


def test_audio_only_file_frames_warning(client, fakes, media):
    d = wait_job(client, create(client, str(media["audio"]), backend="claude", visual="always")["job"]["id"])
    assert d["status"] == "done", d
    assert any("화면을 뽑지 못했습니다" in w for w in d["warnings"])
    assert all(not s["frames"] for s in d["sections"])


def test_visual_never_and_always(client, fakes, media):
    nv = wait_job(client, create(client, str(media["embedded"]), backend="claude", visual="never")["job"]["id"])
    assert all(not s["frames"] for s in nv["sections"])
    al = wait_job(client, create(client, str(media["sidecar"]), backend="claude", visual="always")["job"]["id"])
    assert all(s["frames"] for s in al["sections"])


# ---------- 같은 요청 2회 ----------

def test_same_request_twice_returns_existing(client, fakes):
    a = create(client, YT)
    b = create(client, YT.replace("watch?v=", "watch?feature=share&v="))
    assert a["existed"] is False and b["existed"] is True and a["job"]["id"] == b["job"]["id"]
    c = create(client, YT, backend="claude")                     # 설정이 다르면 다른 작업
    assert c["job"]["id"] != a["job"]["id"]
    f = create(client, YT, force=True)
    assert f["existed"] is False and f["job"]["id"] != a["job"]["id"]
    for x in (a, c, f):
        wait_job(client, x["job"]["id"])


# ---------- 꺼진 의존 ----------

def test_llm_down_then_retry_other_backend(tmp_path, fakes):
    fakes["llms"]["local"] = FakeLLM("local", up=False)
    with make_client(tmp_path, fakes) as c:
        st = c.get("/api/status").json()
        assert st["backends"]["local"]["ok"] is False and "켜는 법" in st["backends"]["local"]["why"]
        d = wait_job(c, create(c, YT)["job"]["id"])
        assert d["status"] == "failed" and d["errorKind"] == "llm_down"
        assert d["transcript"]["source"] == "uploader"           # 글은 이미 받아 둠 — 다시 받지 않는다
        calls = list(fakes["remote"].calls)
        r = c.post(f"/api/jobs/{d['id']}/retry", json={"backend": "claude"})
        assert r.status_code == 200
        d = wait_job(c, d["id"])
        assert d["status"] == "done" and d["backend"] == "claude"
        again = fakes["remote"].calls[len(calls):]
        assert "info" not in again and not any(x.startswith("subtitle") for x in again)   # 영상 정보·자막을 다시 받지 않음


def test_stt_down_fails_with_how_to(tmp_path, fakes, media):
    fakes["stt"].up = False
    with make_client(tmp_path, fakes) as c:
        d = wait_job(c, create(c, str(media["plain"]))["job"]["id"])
        assert d["status"] == "failed" and d["errorKind"] == "stt_down" and "켜" in d["error"]
        assert c.get("/api/status").json()["stt"]["ok"] is False


# ---------- 취소·재시작 ----------

def test_cancel_queued_and_during_stt(client, fakes, media):
    fakes["stt"].gate.clear()                                     # stt 가 끝나지 않게 잡아 둔다
    a = create(client, str(media["plain"]))["job"]
    b = create(client, str(media["audio"]))["job"]               # a 뒤 줄에서 기다림
    wait_job(client, a["id"], want=("running",))
    import time
    for _ in range(100):
        if "stt" in client.get(f"/api/jobs/{a['id']}").json()["stage"]:
            break
        time.sleep(0.05)
    assert client.post(f"/api/jobs/{b['id']}/cancel").json()["status"] == "canceled"
    client.post(f"/api/jobs/{a['id']}/cancel")
    d = wait_job(client, a["id"])
    assert d["status"] == "canceled" and fakes["stt"].canceled     # stt 쪽 작업도 취소했다
    r = client.post(f"/api/jobs/{a['id']}/cancel")
    assert r.status_code == 409


def test_restart_resumes_stt_without_resubmit(tmp_path, fakes, media):
    fakes["stt"].gate.clear()
    with make_client(tmp_path, fakes) as c:
        jid = create(c, str(media["plain"]))["job"]["id"]
        import time
        for _ in range(200):
            if (c.get(f"/api/jobs/{jid}").json().get("stt") or {}).get("taskId"):
                break
            time.sleep(0.05)
        c.app.state.svc._stop = True                             # 서버가 갑자기 꺼진 것처럼: 일꾼만 멈춘다 (상태는 running)
    assert fakes["stt"].submits == 1
    fakes["stt"].gate.set()
    with make_client(tmp_path, fakes) as c:                      # 같은 data_dir 로 다시 켠다
        d = wait_job(c, jid)
        assert d["status"] == "done" and d["transcript"]["source"] == "stt"
    assert len(fakes["stt"].tasks) == 1                          # stt 작업을 새로 만들지 않았다


# ---------- 사람 개입: 고치기·다시 만들기 ----------

def test_edit_and_regenerate(client, fakes):
    d = wait_job(client, create(client, YT)["job"]["id"])
    jid = d["id"]
    r = client.patch(f"/api/jobs/{jid}/sections/0", json={"title": "고친 제목", "points": [{"t": 999, "text": "고친 요점"}, {"text": " "}]})
    s0 = r.json()["sections"][0]
    assert s0["title"] == "고친 제목" and s0["edited"] and s0["points"] == [{"t": s0["end"], "text": "고친 요점", "sub": []}]
    assert client.patch(f"/api/jobs/{jid}/sections/0", json={"points": [{"text": ""}]}).status_code == 400
    assert client.patch(f"/api/jobs/{jid}/sections/99", json={"title": "x"}).status_code == 404
    assert "고친 요점" in client.get(f"/api/jobs/{jid}/summary.md").text
    r = client.post(f"/api/jobs/{jid}/sections/0/regenerate", json={"backend": "claude"})
    assert r.status_code == 200
    d = wait_job(client, jid)
    s0 = d["sections"][0]
    assert s0["title"].startswith("claude") and s0["backend"] == "claude" and not s0["edited"]
    client.post(f"/api/jobs/{jid}/overview/regenerate", json={"backend": "claude"})
    d = wait_job(client, jid)
    assert d["overview"]["summary"].startswith("claude")
    assert client.delete(f"/api/jobs/{jid}").status_code == 200
    assert client.get(f"/api/jobs/{jid}").status_code == 404


# ---------- 구성·쓰임새·중점 ----------

SRT_QA = "\n\n".join(f"{i + 1}\n00:{i // 2:02d}:{(i % 2) * 30:02d},000 --> 00:{i // 2:02d}:{(i % 2) * 30 + 20:02d},000\n{t}" for i, t in enumerate(
    ["안녕하세요 오늘은 후기를 나눕니다", "첫 번째 질문 교육으로 키울 수 있나", "기준선은 그을 수 있다고 봅니다", "수강생 열 명 중 여덟 명이 끝냈습니다",
     "두 번째 질문 무엇이 차이를 만드나", "결국 끈기라고 생각합니다", "세 번째 질문 연차가 중요한가", "연차보다 문제 해결력입니다"])) + "\n"


def test_qa_video_split_by_questions(tmp_path, fakes, media):
    from conftest import FakeRemote
    fakes["remote"] = FakeRemote(media, srt=SRT_QA)
    with make_client(tmp_path, fakes) as c:
        d = wait_job(c, create(c, YT, backend="claude")["job"]["id"])
        assert d["status"] == "done", d
        assert d["structure"]["kind"] == "qa" and d["structureLabel"] == "질문 3개"
        labels = [s["label"] for s in d["sections"]]
        assert labels[0] == "들어가며" and all("질문" in (x or "") for x in labels[1:]) and len(labels) == 4
        s1 = d["sections"][1]
        assert s1["gist"] and s1["points"][0]["sub"] == ["딸린 근거"] and s1["mentions"][0]["name"] == "캐시"
        assert d["mentions"] and d["overview"]["answers"][0] == {"idx": 0, "line": "첫 구간의 답"}
        assert [x["text"] for x in d["overview"]["tldr"]] == ["결론 하나", "결론 둘", "결론 셋"]
        assert d["overview"]["tldr"][1]["from"] == [0]                     # 없는 구간 번호(99)는 버린다
        md = c.get(f"/api/jobs/{d['id']}/summary.md").text
        assert "# 정리" in md and "# 정보" in md and "## 세 줄 결론" in md and "구성: 질문 3개" in md
        assert "> 이 구간의 결론이다." in md and "  - 딸린 근거" in md and "## 언급된 것" in md and "## 구간별 한 줄" in md


def test_chapters_used_without_llm_structure(tmp_path, fakes, media):
    from conftest import FakeRemote
    fakes["remote"] = FakeRemote(media, chapters=[{"start_time": 0, "title": "도입"}, {"start_time": 4, "title": "본론"}])
    with make_client(tmp_path, fakes) as c:
        d = wait_job(c, create(c, YT, backend="claude")["job"]["id"])
        assert d["structure"]["source"] == "chapters" and [s["label"] for s in d["sections"]] == ["도입", "본론"]
        assert "structure" not in fakes["llms"]["claude"].calls        # 챕터가 있으면 구성 찾기를 부르지 않는다


def test_focus_reaches_prompts_and_bad_inputs(client, fakes):
    r = client.post("/api/jobs", json={"ref": YT, "purpose": "poem"})
    assert r.status_code == 400
    r = client.post("/api/jobs", json={"ref": YT, "focus": "가" * 201})
    assert r.status_code == 400
    d = wait_job(client, create(client, YT, backend="claude", focus="교육 커리큘럼")["job"]["id"])
    assert d["focus"] == "교육 커리큘럼"
    assert any("중점으로 본 것: 교육 커리큘럼" in p for p in fakes["llms"]["claude"].prompts)
    other = create(client, YT, backend="claude")                        # 중점이 다르면 다른 작업
    assert other["job"]["id"] != d["id"]
    wait_job(client, other["job"]["id"])


def test_share_purpose_and_rewrite_only_writing_layer(client, fakes):
    d = wait_job(client, create(client, YT, backend="claude")["job"]["id"])
    assert d["article"] is None
    llm = fakes["llms"]["claude"]
    before = list(llm.calls)
    r = client.post(f"/api/jobs/{d['id']}/rewrite", json={"purpose": "share"})
    assert r.status_code == 200
    d = wait_job(client, d["id"])
    assert d["purpose"] == "share" and d["article"]["title"] == "공유 글 제목"
    again = llm.calls[len(before):]
    assert again == ["overview", "article"]                             # 구간 정리·구성 찾기·화면을 다시 하지 않는다
    assert "## 공유 글: 공유 글 제목" in client.get(f"/api/jobs/{d['id']}/summary.md").text
    assert client.post(f"/api/jobs/{d['id']}/rewrite", json={"purpose": "x"}).status_code == 400
    dup = create(client, YT, backend="claude", purpose="share")          # 다시 쓴 작업이 그 쓰임새의 작업이 된다
    assert dup["existed"] is True and dup["job"]["id"] == d["id"]


def test_old_db_migrates(tmp_path, fakes):
    import sqlite3
    data = tmp_path / "data"
    data.mkdir()
    db = sqlite3.connect(data / "video_reader.db")
    db.executescript("""CREATE TABLE jobs (id TEXT PRIMARY KEY, created TEXT, updated TEXT, key TEXT, ref TEXT, kind TEXT,
      backend TEXT, visual TEXT, lang TEXT, out_lang TEXT, status TEXT, stage TEXT, progress TEXT, error TEXT, error_kind TEXT,
      origin TEXT, caller TEXT, source TEXT, transcript TEXT, stt TEXT, overview TEXT, overview_backend TEXT, warnings TEXT, cost_usd REAL DEFAULT 0);
      CREATE TABLE sections (job_id TEXT, idx INTEGER, start REAL, end_ REAL, first INTEGER, last INTEGER, status TEXT, title TEXT,
      points TEXT, needs_visual INTEGER, cues TEXT, frames TEXT, observations TEXT, backend TEXT, edited INTEGER DEFAULT 0, error TEXT,
      PRIMARY KEY (job_id, idx));""")
    db.commit()
    db.close()
    with make_client(tmp_path, fakes) as c:
        d = wait_job(c, create(c, YT, backend="claude")["job"]["id"])
        assert d["status"] == "done" and d["purpose"] == "record"


def test_coverage_fills_missing_numbers_once(tmp_path, fakes, media):
    """원문의 숫자·영문이 정리에 없으면 한 번만 채운다 — 가짜 LLM 은 '2001' 같은 낱말을 항목에 안 넣는다."""
    from conftest import FakeRemote
    lines = ["오늘 이야기를 시작한다", "현장 이야기를 한다", "역량 이야기를 한다", "질문에 답한다", "현장에는 2001년생이 많다"]
    srt = "\n\n".join(f"{i + 1}\n00:00:{i * 2:02d},000 --> 00:00:{i * 2 + 1:02d},000\n{t}" for i, t in enumerate(lines)) + "\n"   # 가짜 LLM 은 앞 4줄만 옮긴다
    fakes["remote"] = FakeRemote(media, srt=srt)
    llm = fakes["llms"]["claude"]
    with make_client(tmp_path, fakes) as c:
        d = wait_job(c, create(c, YT, backend="claude")["job"]["id"])
        assert d["status"] == "done", d
        assert llm.calls.count("fill") == 1                                  # 한 번만
        texts = " ".join(p["text"] for s in d["sections"] for p in s["points"])
        assert "2001" in texts and d["coverage"]["missing"] == []


def test_boundary_line_just_before_chapter_stays_in_previous():
    """챕터 시각 0.1초 앞에서 시작한 줄은 앞 구간의 끝이다 (가장 가까운 줄로 고르면 다음 구간으로 넘어갔다)."""
    from video_reader.engine import units_from_structure
    segs = [{"start": 0.0, "end": 1.0, "text": "가"}, {"start": 3.9, "end": 4.5, "text": "앞 구간의 마지막 문장"},
            {"start": 4.6, "end": 6.0, "text": "다음 질문"}]
    u = units_from_structure(segs, {"starts": [{"t": 0, "label": "하나"}, {"t": 4, "label": "둘"}]})
    assert [(x["first"], x["last"], x["label"]) for x in u] == [(0, 1, "하나"), (2, 2, "둘")]


def test_polish_fixes_style_and_mentions_but_not_human_edits(tmp_path, fakes, media):
    from conftest import FakeRemote
    lines = ["강점이 다르다고 봄", "현장에는 2001년생이 많다"]
    srt = "\n\n".join(f"{i + 1}\n00:00:{i * 2:02d},000 --> 00:00:{i * 2 + 1:02d},000\n{t}" for i, t in enumerate(lines)) + "\n"
    fakes["remote"] = FakeRemote(media, srt=srt)
    llm = fakes["llms"]["claude"]
    with make_client(tmp_path, fakes) as c:
        d = wait_job(c, create(c, YT, backend="claude")["job"]["id"])
        texts = [p["text"] for s in d["sections"] for p in s["points"]]
        assert "강점이 다르다고 본다" in texts and not any(t.endswith("봄") for t in texts)
        assert d["style"]["found"] >= 1 and d["style"]["left"] == 0
        assert llm.calls.count("style") == 1 and llm.calls.count("mentions") == 1
        # 사람이 "~함"으로 고친 구간은 다시 만들기 전까지 다듬기가 건드리지 않는다
        c.patch(f"/api/jobs/{d['id']}/sections/0", json={"points": [{"t": 0, "text": "사람이 쓴 메모함"}]})
        c.post(f"/api/jobs/{d['id']}/overview/regenerate", json={})
        d = wait_job(c, d["id"])
        assert d["sections"][0]["points"][0]["text"] == "사람이 쓴 메모함"
        md = c.get(f"/api/jobs/{d['id']}/summary.md").text
        assert "결론 하나 — 구간 00:00" in md


def test_force_stt_even_with_captions_and_fresh(client, fakes):
    """자막이 있어도 stt 로(transcribe=stt), stt 예전 결과 말고 새로(sttFresh → stt force). 기본은 그대로 자막."""
    a = wait_job(client, create(client, YT, backend="claude")["job"]["id"])
    assert a["transcriptSource"] == "uploader" and fakes["stt"].submits == 0
    b = create(client, YT, backend="claude", transcribe="stt", sttFresh=True)
    assert b["existed"] is False                                      # 새로 원하면 같은 설정이 있어도 새 작업
    d = wait_job(client, b["job"]["id"])
    assert d["transcriptSource"] == "stt" and d["transcribe"] == "stt" and d["sttFresh"] is True
    assert fakes["stt"].forced == [True]
    assert client.post("/api/jobs", json={"ref": YT, "transcribe": "whisper"}).status_code == 400


def test_asr_glossary_and_check_rules():
    from video_reader.engine import glossary, needs_check
    from video_reader.sources import Source
    src = Source(ref="x", kind="youtube", title="FDE 후기", description="자료\n- 『프로덕트 오너』, SVPG\n- Stanford CS329Z\n:arrow_forward: https://fastcampus.info/zqx\n- 결론이 중요합니다.")
    g = glossary(src)
    assert "SVPG" in g and "CS329Z" in g and "프로덕트 오너" in g
    assert not any(x in g for x in ("arrow", "forward", "fastcampus.info", "결론이 중요합니다"))
    chk = lambda n, k: needs_check({"name": n, "kind": k}, src)
    assert chk("CSV PG", "강의·자료") and chk("리벨리온", "회사·기관")           # 근거 없는 이름
    assert not chk("SVPG", "회사·기관") and not chk("스탠포드 CS 329Z", "강의·자료")  # 설명에 같은 표기
    assert not chk("클로드(Claude)", "도구·기술") and not chk("삼 년", "수치")       # 일반어·숫자는 보지 않음


def test_asr_rule_only_for_stt_transcripts(tmp_path, fakes, media):
    with make_client(tmp_path, fakes) as c:
        wait_job(c, create(c, YT, backend="claude")["job"]["id"])                       # 업로더 자막
        cap = list(fakes["llms"]["claude"].prompts)
        wait_job(c, create(c, YT, backend="claude", transcribe="stt")["job"]["id"])     # stt
        st = fakes["llms"]["claude"].prompts[len(cap):]
    assert not any("소리 인식(stt)" in p for p in cap) and any("소리 인식(stt)" in p for p in st)
