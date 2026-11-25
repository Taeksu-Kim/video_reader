"""시안 두 장을 실제 실행 결과(ted_result.json — 2026-11-24 Claude 백엔드로 TED 영상 정리)로 채운다.
A = 밀착 인화지 (추천), B = 코넬 노트 (차선). python make_mockups.py → A-contact-sheet.html, B-cornell.html"""
import html
import json
from pathlib import Path

HERE = Path(__file__).parent
R = json.loads((HERE / "ted_result.json").read_text())
src, ov, secs = R["source"], R["overview"], R["sections"]
FONT = "../../../../video_reader/static/fonts"
E = html.escape


def t(s):
    s = int(s)
    return f"{s // 60:02d}:{s % 60:02d}"


JOBS = [  # 왼쪽 목록: 첫 줄은 실제 결과, 나머지는 상태 범위를 보이려는 예 (정리 중·실패·A2A)
    {"title": src["title"], "len": t(src["duration"]), "sub": "업로더 자막", "frames": [len(s["frames"]) > 0 for s in secs], "done": len(secs), "state": "done", "on": True},
    {"title": "2026 반도체 업황 점검 — 3분기 실적 발표 컨퍼런스콜", "len": "58:12", "sub": "stt 받아 적는 중", "frames": [False] * 0, "done": 0, "state": "stt", "on": False},
    {"title": "Attention Is All You Need 논문 리뷰 (코드로 따라가기)", "len": "41:30", "sub": "업로더 자막", "frames": [True, False, True, True, False, False, False, False, False, False, False, False, False, False], "done": 5, "state": "run", "on": False, "a2a": "paper_analyst"},
    {"title": "/mnt/d/videos/team-meeting-1121.mp4", "len": "32:05", "sub": "", "frames": [], "done": 0, "state": "fail", "on": False},
]


def strip_mini(j):
    n = max(len(j["frames"]), 1)
    cells = []
    for i in range(min(n, 14)):
        if i < j["done"]:
            cells.append('<i class="dev"></i>' if j["frames"][i] else '<i class="unexp"></i>')
        else:
            cells.append('<i class="wait"></i>')
    return f'<span class="mini">{"".join(cells)}</span>'


def jobs_html():
    out = []
    for j in JOBS:
        state = {"done": f'{j["done"]}구간', "stt": "stt 받아 적는 중", "run": f'정리 중 {j["done"]}/14', "fail": "멈춤 — stt 서버 꺼짐"}[j["state"]]
        who = f'<span class="from">A2A · {j["a2a"]}</span>' if j.get("a2a") else ""
        out.append(f'''<a class="job{' on' if j['on'] else ''} {j['state']}" href="#">
  <span class="jt">{E(j['title'])}</span>
  <span class="jm"><span class="len">{j['len']}</span><span class="st">{state}</span>{who}</span>
  {strip_mini(j) if j['frames'] else ''}
</a>''')
    return "\n".join(out)


def contact_sheet():
    rows = []
    n = 0
    for k, s in enumerate(secs):
        frames = s["frames"]
        cells = []
        if frames:
            for f in frames:
                n += 1
                cells.append(f'''<figure class="frame"><img src="frames/{f['file']}" alt=""><figcaption><b>{n:02d}</b><span>{t(f['t'])}</span></figcaption></figure>''')
        else:
            n += 1
            cells.append(f'<figure class="frame unexp"><div class="win">말만 있는 구간</div><figcaption><b>{n:02d}</b><span>{t(s["start"])}</span></figcaption></figure>')
        pts = "".join(f'<li><span class="txt">{E(p["text"])}</span><a class="tc" href="#">{t(p["t"])}</a></li>' for p in s["points"])
        obs = "".join(f'<li><a class="tc" href="#">{t(o["t"])}</a><span class="txt">{E(o["text"])}</span></li>' for o in s.get("observations", []))
        edited = k == 1
        rows.append(f'''<section class="cut{' edited' if edited else ''}">
  <div class="strip">{''.join(cells)}</div>
  <div class="notes">
    <header><a class="range" href="#">{t(s['start'])} – {t(s['end'])}</a>{'<span class="mark">고침</span>' if edited else ''}
      <span class="acts"><button>고치기</button><button>다시 만들기 ▾</button><button>받아 적은 글</button></span></header>
    <h2>{E(s['title'])}</h2>
    <ul class="pts">{pts}</ul>
    {f'<div class="seen"><h3>화면에서 읽은 것</h3><ul>{obs}</ul></div>' if obs else ''}
  </div>
</section>''')
    take = "".join(f"<li>{E(x)}</li>" for x in ov["takeaways"])
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>video_reader 시안 A</title>
<style>
@font-face{{font-family:P;src:url({FONT}/Pretendard-Regular.ttf);font-weight:400}}
@font-face{{font-family:P;src:url({FONT}/Pretendard-Medium.ttf);font-weight:500}}
@font-face{{font-family:P;src:url({FONT}/Pretendard-SemiBold.ttf);font-weight:600}}
:root{{--table:#E2E4E7;--sheet:#FFFFFF;--film:#1A1B1E;--film2:#2A2C31;--edge:#E9A23B;--ink:#16181C;--ink2:#4A4F57;--ink3:#737985;--line:#E6E8EB;--time:#8A5300;--pencil:#D2322D;}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--table);font:15px/1.5 P,system-ui,sans-serif;color:var(--ink);-webkit-font-smoothing:antialiased}}
.app{{display:grid;grid-template-columns:316px 1fr;min-height:100vh}}
aside{{padding:20px 16px 20px 20px;display:flex;flex-direction:column;gap:18px}}
.brand{{display:flex;align-items:center;gap:8px;font-weight:600;font-size:15px}} .brand i{{width:18px;height:22px;background:var(--film);border-radius:2px;box-shadow:inset 3px 0 0 -1px var(--film),inset 0 0 0 4px var(--film);position:relative}}
.brand i:after{{content:"";position:absolute;inset:5px 4px;background:var(--edge);border-radius:1px}}
.feed{{background:var(--sheet);border-radius:10px;padding:12px;box-shadow:0 1px 3px rgba(20,22,26,.08)}}
.feed textarea{{width:100%;border:1px solid #CDD1D6;border-radius:6px;padding:9px 10px;font:14px/1.4 P;resize:none;height:62px}}
.opts{{display:flex;gap:6px;margin:8px 0;flex-wrap:wrap}} .seg{{display:inline-flex;border:1px solid #CDD1D6;border-radius:6px;overflow:hidden;font-size:13px}}
.seg span{{padding:5px 9px}} .seg .on{{background:var(--ink);color:#fff}} .seg .lock{{color:var(--ink3)}}
.go{{width:100%;background:var(--ink);color:#fff;border:0;border-radius:6px;padding:9px;font:600 14px P}}
.lab{{font-size:13px;color:var(--ink3);margin:2px 2px -8px}}
.job{{display:block;text-decoration:none;color:inherit;padding:10px 10px 11px;border-radius:8px}} .job:hover{{background:rgba(255,255,255,.5)}}
.job.on{{background:var(--sheet);box-shadow:0 1px 3px rgba(20,22,26,.08)}}
.jt{{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-weight:500;font-size:14px;line-height:1.4}}
.jm{{display:flex;gap:10px;font-size:12.5px;color:var(--ink3);margin-top:3px;font-variant-numeric:tabular-nums}} .fail .st{{color:var(--ink)}} .from{{color:var(--ink2)}}
.mini{{display:flex;gap:2px;margin-top:7px;background:var(--film);padding:3px;border-radius:2px;width:max-content}}
.mini i{{width:13px;height:8px;border-radius:1px;background:#3B3E45}} .mini .dev{{background:#B9BDC4}} .mini .wait{{background:transparent;outline:1px dashed #555A62;outline-offset:-1px}}
main{{padding:20px 28px 60px 8px}}
.sheet{{background:var(--sheet);max-width:1060px;border-radius:4px;box-shadow:0 1px 2px rgba(20,22,26,.06),0 8px 24px rgba(20,22,26,.07)}}
.label{{padding:26px 36px 22px;border-bottom:1px solid var(--line);display:grid;grid-template-columns:1fr auto;gap:8px 24px}}
h1{{margin:0;font-size:22px;line-height:1.35;font-weight:600;letter-spacing:-.01em;text-wrap:balance}}
dl{{grid-column:1;display:flex;gap:22px;margin:6px 0 0;font-size:13px}} dt{{color:var(--ink3)}} dd{{margin:0 0 0 6px;font-variant-numeric:tabular-nums}} dl div{{display:flex}}
.top{{grid-row:1/3;grid-column:2;display:flex;gap:8px;align-items:flex-start}}
.top button,.acts button{{font:500 13px P;border:1px solid #CDD1D6;background:#fff;border-radius:6px;padding:6px 10px;color:var(--ink)}}
.over{{padding:22px 36px 26px;border-bottom:1px solid var(--line);display:grid;grid-template-columns:1fr 320px;gap:32px}}
.over p{{margin:0;font-size:16px;line-height:1.8;max-width:62ch}} .over h3,.seen h3{{margin:0 0 8px;font-size:13px;font-weight:600;color:var(--ink2)}}
.over ul{{margin:0;padding-left:18px;font-size:14px;line-height:1.6}} .over li{{margin-bottom:6px}}
.cut{{display:grid;grid-template-columns:232px 1fr;border-bottom:1px solid var(--line)}}
.strip{{background:var(--film);padding:14px 22px;position:relative;
  background-image:radial-gradient(circle at 50% 50%,#E2E4E7 0 0) ,linear-gradient(var(--film),var(--film));}}
.strip:before,.strip:after{{content:"";position:absolute;top:0;bottom:0;width:8px;background:repeating-linear-gradient(to bottom,transparent 0 6px,#5A5E66 6px 11px,transparent 11px 16px)}}
.strip:before{{left:7px}} .strip:after{{right:7px}}
.frame{{margin:0 0 14px}} .frame img,.frame .win{{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:1px}}
.frame .win{{background:var(--film2);color:#8D929B;font-size:12px;display:grid;place-items:center}}
.frame figcaption{{display:flex;justify-content:space-between;color:var(--edge);font-size:11.5px;margin-top:4px;font-variant-numeric:tabular-nums;letter-spacing:.02em}}
.frame b{{font-weight:600}}
.edited .strip figure:first-child figcaption b{{outline:2px solid var(--pencil);outline-offset:2px;border-radius:50%;padding:0 3px;color:#fff}}
.notes{{padding:18px 36px 22px 28px}}
.notes header{{display:flex;align-items:center;gap:10px;font-size:13px}} .range{{color:var(--time);font-variant-numeric:tabular-nums;font-weight:500;text-decoration:none}}
.mark{{color:var(--pencil);font-weight:600}} .acts{{margin-left:auto;display:flex;gap:6px}} .acts button{{padding:4px 8px;font-size:12.5px;color:var(--ink2)}}
h2{{margin:6px 0 10px;font-size:18px;font-weight:600;line-height:1.4}}
.pts{{margin:0;padding:0;list-style:none}} .pts li{{display:grid;grid-template-columns:1fr auto;gap:16px;padding:6px 0;line-height:1.7;font-size:15px}}
.tc{{color:var(--time);text-decoration:none;font-size:13px;font-variant-numeric:tabular-nums;padding-top:2px}}
.seen{{margin-top:12px;padding:12px 14px;background:#F4F5F7;border-radius:6px}} .seen ul{{margin:0;padding:0;list-style:none}}
.seen li{{display:grid;grid-template-columns:44px 1fr;gap:8px;font-size:14px;line-height:1.65;padding:3px 0}}
</style>
<div class="app"><aside>
<div class="brand"><i></i>video_reader</div>
<div class="feed"><textarea>https://www.youtube.com/watch?v=…</textarea>
<div class="opts"><span class="seg"><span>로컬 LLM</span><span class="on">Claude Code</span></span><span class="seg"><span class="on">화면 자동</span><span>항상</span><span>안 봄</span></span></div>
<button class="go">정리하기</button></div>
<div class="lab">정리한 영상</div>
{jobs_html()}
</aside><main><article class="sheet">
<div class="label"><h1>{E(src['title'])}</h1><dl><div><dt>길이</dt><dd>{t(src['duration'])}</dd></div><div><dt>글</dt><dd>업로더 자막 (en)</dd></div><div><dt>정리</dt><dd>Claude Code</dd></div><div><dt>구간</dt><dd>{len(secs)}</dd></div></dl>
<div class="top"><button>원본 열기</button><button>마크다운</button><button>⋯</button></div></div>
<div class="over"><div><h3>개요</h3><p>{E(ov['summary'])}</p></div><div><h3>가져갈 것</h3><ul>{take}</ul></div></div>
{''.join(rows)}
</article></main></div>'''


def cornell():
    rows = []
    for s in secs:
        thumbs = "".join(f'<img src="frames/{f["file"]}" alt="">' for f in s["frames"][:2])
        pts = "".join(f'<li>{E(p["text"])} <a href="#">{t(p["t"])}</a></li>' for p in s["points"])
        obs = "".join(f'<li class="o">화면 {t(o["t"])} — {E(o["text"])}</li>' for o in s.get("observations", []))
        rows.append(f'''<section><div class="cue"><a href="#" class="ct">{t(s['start'])}</a><b>{E(s['title'])}</b>{thumbs}</div>
<div class="note"><ul>{pts}{obs}</ul></div></section>''')
    take = "".join(f"<li>{E(x)}</li>" for x in ov["takeaways"])
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>video_reader 시안 B</title>
<style>
@font-face{{font-family:P;src:url({FONT}/Pretendard-Regular.ttf);font-weight:400}}
@font-face{{font-family:P;src:url({FONT}/Pretendard-SemiBold.ttf);font-weight:600}}
:root{{--bg:#E9ECEF;--paper:#fff;--rule:#DCE3EC;--margin:#E0625B;--ink:#1C1F24;--ink3:#6B717C;--blue:#2F5DA8}}
body{{margin:0;background:var(--bg);font:15px/1.6 P,system-ui;color:var(--ink)}}
.app{{display:grid;grid-template-columns:280px 1fr}} aside{{padding:20px;font-size:14px}} aside .j{{padding:10px 0;border-bottom:1px solid #D5D9DE}} aside .on{{font-weight:600}}
.paper{{background:var(--paper);margin:20px 28px;max-width:1040px;box-shadow:0 6px 22px rgba(0,0,0,.08);
 background-image:repeating-linear-gradient(to bottom,transparent 0 31px,var(--rule) 31px 32px)}}
.head{{padding:24px 32px 16px;background:var(--paper);border-bottom:2px solid var(--ink)}} h1{{margin:0;font-size:22px}} .meta{{color:var(--ink3);font-size:13px;margin-top:4px}}
section{{display:grid;grid-template-columns:250px 1fr;border-bottom:1px solid var(--rule)}}
.cue{{padding:16px 18px 16px 32px;border-right:2px solid var(--margin);display:flex;flex-direction:column;gap:6px;background:rgba(255,255,255,.7)}}
.cue b{{font-size:15px;line-height:1.45}} .ct{{color:var(--blue);font-size:13px;text-decoration:none}} .cue img{{width:100%;border-radius:2px}}
.note{{padding:14px 32px 14px 24px}} .note ul{{margin:0;padding-left:16px;line-height:2}} .note a{{color:var(--blue);font-size:12.5px;text-decoration:none}} .o{{color:#3E5578}}
.sum{{border-top:2px solid var(--ink);padding:18px 32px 26px;background:var(--paper)}} .sum h3{{margin:0 0 6px;font-size:14px}} .sum p{{margin:0 0 10px;line-height:1.8}}
</style>
<div class="app"><aside><b>video_reader</b><p style="color:#6B717C">주소를 넣고 정리하기</p>
<div class="j on">{E(src['title'])}</div><div class="j">2026 반도체 업황 점검 — 3분기 실적 발표…</div><div class="j">Attention Is All You Need 논문 리뷰…</div></aside>
<main><article class="paper"><div class="head"><h1>{E(src['title'])}</h1><div class="meta">길이 {t(src['duration'])}　글 업로더 자막(en)　정리 Claude Code</div></div>
{''.join(rows)}
<div class="sum"><h3>요약</h3><p>{E(ov['summary'])}</p><ul>{take}</ul></div></article></main></div>'''


(HERE / "A-contact-sheet.html").write_text(contact_sheet(), encoding="utf-8")
(HERE / "B-cornell.html").write_text(cornell(), encoding="utf-8")
print("ok")
