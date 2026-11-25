"""python -m video_reader read <주소|파일> [--backend local|claude] [--visual auto|always|never] [--lang en] [--out D]
python -m video_reader serve [--port 8777] [--data-dir data]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="video_reader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read", help="영상 하나를 끝까지 정리 (서버 없이, 저장은 --out 폴더에)")
    r.add_argument("ref")
    r.add_argument("--backend", choices=["local", "claude"], default="local")
    r.add_argument("--visual", choices=["auto", "always", "never"], default="auto")
    r.add_argument("--lang", help="자막·받아 적기 언어 (없으면 영상 원래 언어)")
    r.add_argument("--out-lang", default="ko")
    r.add_argument("--purpose", choices=["record", "brief", "share", "agent"], default="record",
                   help="쓰임새: 기록·판단(기본) / 빠른 판단 / 공유 글 / 에이전트 입력")
    r.add_argument("--focus", default="", help="중점 한 줄 (비워도 됨)")
    r.add_argument("--out", type=Path, help="결과 폴더 (summary.md, result.json, frames/)")
    r.add_argument("--claude-model")
    s = sub.add_parser("serve", help="제품 화면 + A2A (127.0.0.1)")
    s.add_argument("--port", type=int, default=8777)
    s.add_argument("--data-dir", type=Path, default=ROOT / "data")
    a = ap.parse_args(argv)

    if a.cmd == "read":
        from video_reader.engine import default_engine
        from video_reader.llm import LLMError
        from video_reader.sources import SourceError
        from video_reader.stt_client import SttError
        out = a.out or Path.cwd() / "video_reader_out"
        out.mkdir(parents=True, exist_ok=True)
        eng = default_engine(claude_model=a.claude_model)
        try:
            res = eng.read(a.ref, out, backend=a.backend, visual=a.visual, lang=a.lang, out_lang=a.out_lang,
                           log=lambda m: print(m, file=sys.stderr), purpose=a.purpose, focus=a.focus)
        except (LLMError, SourceError, SttError) as exc:
            print(f"실패 [{exc.kind}]: {exc.message}", file=sys.stderr)
            return 2
        (out / "summary.md").write_text(res["markdown"], encoding="utf-8")
        (out / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(out / "summary.md")
        return 0

    import uvicorn
    from video_reader.app import create_app
    uvicorn.run(create_app(a.data_dir), host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
