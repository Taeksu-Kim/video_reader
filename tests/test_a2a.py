"""A2A 진입점 — 맡긴 일이 화면 목록에 보이는지, 같은 messageId, 취소, 형식 오류."""
from __future__ import annotations

import time

from conftest import wait_job

YT = "https://www.youtube.com/watch?v=abc123XYZ00"


def rpc(c, method, params):
    return c.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).json()


def send(c, parts, mid="m1", **meta):
    r = rpc(c, "SendMessage", {"message": {"messageId": mid, "role": "ROLE_USER", "parts": parts, "metadata": meta}})
    if "result" in r:
        r["result"] = r["result"]["task"]
    return r


def poll(c, tid, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        r = rpc(c, "GetTask", {"id": tid})["result"]
        if r["status"]["state"] != "TASK_STATE_WORKING":
            return r
        time.sleep(0.05)
    raise AssertionError("끝나지 않음")


def test_card(client):
    card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "video_reader" and card["skills"][0]["id"] == "summarize-video"


def test_a2a_job_visible_on_screen_and_result(client, fakes):
    r = send(client, [{"data": {"url": YT, "backend": "claude"}}], caller="paper_analyst")
    task = r["result"]
    assert task["status"]["state"] == "TASK_STATE_WORKING"
    jobs = client.get("/api/jobs").json()["jobs"]                 # 화면 목록에 바로 보인다
    assert len(jobs) == 1 and jobs[0]["origin"] == "a2a" and jobs[0]["caller"] == "paper_analyst"
    done = poll(client, task["id"])
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert done["metadata"]["reviewUrl"].endswith(f"/#job={jobs[0]['id']}")
    parts = done["artifacts"][0]["parts"]
    assert parts[0]["text"].startswith("# 캐시 강의") and parts[1]["data"]["sections"]
    # 화면에서 고치면 A2A 결과에도 그대로
    client.patch(f"/api/jobs/{jobs[0]['id']}/sections/0", json={"title": "사람이 고친 제목"})
    again = rpc(client, "GetTask", {"id": task["id"]})["result"]
    assert "사람이 고친 제목" in again["artifacts"][0]["parts"][0]["text"]


def test_same_message_twice_and_same_video_from_screen(client, fakes):
    a = send(client, [{"url": YT}], mid="same")["result"]
    b = send(client, [{"url": YT}], mid="same")["result"]
    assert a["id"] == b["id"]
    c = send(client, [{"url": YT}], mid="other")["result"]        # 다른 메시지라도 같은 영상·설정이면 같은 작업에 붙는다
    assert c["metadata"]["jobId"] == a["metadata"]["jobId"]
    assert len(client.get("/api/jobs").json()["jobs"]) == 1
    poll(client, a["id"])


def test_bad_messages(client):
    r = send(client, [{"text": "정리해 줘"}], mid="x1")
    assert r["error"]["code"] == -32602
    r = send(client, [{"url": YT}, {"data": {"path": "/tmp/a.mp4"}}], mid="x2")
    assert r["error"]["code"] == -32602
    r = send(client, [{"raw": "AAAA", "filename": "a.mp4"}], mid="x3")
    assert "error" in r
    r = send(client, [{"data": {"path": "/없는/파일.mp4"}}], mid="x4")["result"]
    assert r["status"]["state"] == "TASK_STATE_REJECTED"
    r = rpc(client, "GetTask", {"id": "nope"})
    assert r["error"]["code"] == -32001
    assert client.get("/api/jobs").json()["jobs"] == []


def test_a2a_cancel(client, fakes, media):
    fakes["stt"].gate.clear()
    t = send(client, [{"data": {"path": str(media["plain"])}}], mid="c1")["result"]
    for _ in range(200):
        if "stt" in (rpc(client, "GetTask", {"id": t["id"]})["result"]["status"].get("message") or {}).get("parts", [{}])[0].get("text", ""):
            break
        time.sleep(0.05)
    r = rpc(client, "CancelTask", {"id": t["id"]})["result"]
    done = poll(client, t["id"])
    assert done["status"]["state"] == "TASK_STATE_CANCELED"
    r = rpc(client, "CancelTask", {"id": t["id"]})
    assert r["error"]["code"] == -32002
    wait_job(client, done["metadata"]["jobId"])
