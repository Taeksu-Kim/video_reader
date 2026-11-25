"""SQLite 한 파일 (data/video_reader.db). 서비스만 쓴다."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, created TEXT, updated TEXT, key TEXT, ref TEXT, kind TEXT,
  backend TEXT, visual TEXT, lang TEXT, out_lang TEXT,
  status TEXT, stage TEXT, progress TEXT, error TEXT, error_kind TEXT,
  origin TEXT, caller TEXT, source TEXT, transcript TEXT, stt TEXT, overview TEXT, overview_backend TEXT,
  warnings TEXT, cost_usd REAL DEFAULT 0, purpose TEXT DEFAULT 'record', focus TEXT DEFAULT '', structure TEXT, article TEXT, polish TEXT,
  transcribe TEXT DEFAULT 'auto', stt_fresh INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sections (
  job_id TEXT, idx INTEGER, start REAL, end_ REAL, first INTEGER, last INTEGER,
  status TEXT, title TEXT, points TEXT, needs_visual INTEGER, cues TEXT, frames TEXT, observations TEXT,
  backend TEXT, edited INTEGER DEFAULT 0, error TEXT, label TEXT, gist TEXT, mentions TEXT, coverage TEXT, PRIMARY KEY (job_id, idx)
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, context_id TEXT, message_id TEXT UNIQUE, job_id TEXT, state TEXT, error TEXT, created TEXT
);
"""
JSON_COLS = {"progress", "source", "transcript", "stt", "overview", "warnings", "points", "cues", "frames", "observations",
             "structure", "article", "mentions", "coverage", "polish"}
MIGRATE = {"jobs": [("purpose", "TEXT DEFAULT 'record'"), ("focus", "TEXT DEFAULT ''"), ("structure", "TEXT"), ("article", "TEXT"), ("polish", "TEXT"),
                    ("transcribe", "TEXT DEFAULT 'auto'"), ("stt_fresh", "INTEGER DEFAULT 0")],
           "sections": [("label", "TEXT"), ("gist", "TEXT"), ("mentions", "TEXT"), ("coverage", "TEXT")]}   # 2026-11-25 구성·쓰임새 — 예전 DB 에 칸을 더한다


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            for table, cols in MIGRATE.items():
                have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                for name, decl in cols:
                    if name not in have:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            self.db.commit()

    @staticmethod
    def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
        if r is None:
            return None
        d = dict(r)
        for k in JSON_COLS & d.keys():
            d[k] = json.loads(d[k]) if d[k] else None
        if "end_" in d:
            d["end"] = d.pop("end_")
        return d

    @staticmethod
    def _val(k: str, v: Any) -> Any:
        return json.dumps(v, ensure_ascii=False) if k in JSON_COLS and v is not None else v

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    # jobs
    def add_job(self, **f) -> dict:
        f.setdefault("created", now())
        f["updated"] = f["created"]
        cols = ",".join(f)
        self._exec(f"INSERT INTO jobs ({cols}) VALUES ({','.join('?' * len(f))})", tuple(self._val(k, v) for k, v in f.items()))
        return self.job(f["id"])

    def job(self, jid: str) -> Optional[dict]:
        with self.lock:
            return self._row(self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    def jobs(self) -> list[dict]:
        with self.lock:
            return [self._row(r) for r in self.db.execute("SELECT * FROM jobs ORDER BY created DESC, rowid DESC")]

    def jobs_by(self, status: str) -> list[dict]:
        with self.lock:
            return [self._row(r) for r in self.db.execute("SELECT * FROM jobs WHERE status=? ORDER BY updated, rowid", (status,))]

    def job_by_key(self, key: str) -> Optional[dict]:
        with self.lock:
            return self._row(self.db.execute("SELECT * FROM jobs WHERE key=? AND status NOT IN ('failed','canceled') "
                                              "ORDER BY created DESC LIMIT 1", (key,)).fetchone())

    def update_job(self, jid: str, **f) -> None:
        f["updated"] = now()
        sets = ",".join(f"{k}=?" for k in f)
        self._exec(f"UPDATE jobs SET {sets} WHERE id=?", tuple(self._val(k, v) for k, v in f.items()) + (jid,))

    def add_cost(self, jid: str, usd: float) -> None:
        self._exec("UPDATE jobs SET cost_usd=COALESCE(cost_usd,0)+? WHERE id=?", (usd, jid))

    def delete_job(self, jid: str) -> None:
        self._exec("DELETE FROM sections WHERE job_id=?", (jid,))
        self._exec("DELETE FROM jobs WHERE id=?", (jid,))

    # sections
    def set_sections(self, jid: str, secs: list[dict]) -> None:
        with self.lock:
            self.db.execute("DELETE FROM sections WHERE job_id=?", (jid,))
            self.db.executemany("INSERT INTO sections (job_id, idx, start, end_, first, last, status, label) VALUES (?,?,?,?,?,?,?,?)",
                                [(jid, i, s["start"], s["end"], s["first"], s["last"], "pending", s.get("label"))
                                 for i, s in enumerate(secs)])
            self.db.commit()

    def sections(self, jid: str) -> list[dict]:
        with self.lock:
            return [self._row(r) for r in self.db.execute("SELECT * FROM sections WHERE job_id=? ORDER BY idx", (jid,))]

    def section(self, jid: str, idx: int) -> Optional[dict]:
        with self.lock:
            return self._row(self.db.execute("SELECT * FROM sections WHERE job_id=? AND idx=?", (jid, idx)).fetchone())

    def update_section(self, jid: str, idx: int, **f) -> None:
        sets = ",".join(f"{'end_' if k == 'end' else k}=?" for k in f)
        self._exec(f"UPDATE sections SET {sets} WHERE job_id=? AND idx=?", tuple(self._val(k, v) for k, v in f.items()) + (jid, idx))

    # a2a tasks
    def add_task(self, tid: str, ctx: str, mid: Optional[str], jid: Optional[str], *, state: str = "ok", error: str = "") -> None:
        self._exec("INSERT INTO tasks (id, context_id, message_id, job_id, state, error, created) VALUES (?,?,?,?,?,?,?)",
                   (tid, ctx, mid, jid, state, error, now()))

    def task(self, tid: str) -> Optional[dict]:
        with self.lock:
            return self._row(self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())

    def task_by_message(self, mid: str) -> Optional[dict]:
        with self.lock:
            return self._row(self.db.execute("SELECT * FROM tasks WHERE message_id=?", (mid,)).fetchone())

    def tasks(self, ctx: Optional[str] = None) -> list[dict]:
        with self.lock:
            q = "SELECT * FROM tasks" + (" WHERE context_id=?" if ctx else "") + " ORDER BY created DESC, rowid DESC"
            return [self._row(r) for r in self.db.execute(q, (ctx,) if ctx else ())]
