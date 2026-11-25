"""층을 정리한 화면에 색 후보를 변수만 바꿔 입혀 캡처한다 (ui-design topics/color.md 5). 후보마다 페이지를 새로 연다
(같은 페이지에서 #만 바꾸면 새로고침이 안 돼 앞 후보 CSS 가 남는다 — 11-24 비교에서 겪음).
값은 palette.py roles/scale(청록 H200·남보라 H285) 출력에서 (2026-11-25). python color_candidates.py [작업 id]"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
JID = sys.argv[1] if len(sys.argv) > 1 else "061e629a90b7"
FONT = "/mnt/d/workspace/side_projects/video_reader/video_reader/static/fonts/Pretendard-SemiBold.ttf"

CANDS = [
    ("C1-graphite-teal", "C1 흑연 보관함 + 청록 (지금)", ""),
    ("C2-graphite-mono", "C2 흑연 보관함 + 무채색 (색은 시각·고침에만)", """:root{
      --accent:#16181C;--accent-hover:#2B2F36;--accent-soft:#EEF0F2;--accent-on-console:#E6E7E9;--accent-ink:#16181C}
      aside .btn.primary:hover{background:#FFFFFF}"""),
    ("C3-teal-console", "C3 청록으로 물들인 보관함·바닥 + 청록", """:root{
      --table:#DCE3E3;--console:#0F2C2E;--console-2:#17393B;--console-line:#5A7C7E;--on-console:#EEF6F6;--on-console-2:#A6C3C4;
      --sheet-head:#F3F7F7;--line-2:#D3DCDC}
      .feed{border-bottom-color:#24484A} .feed textarea,.more input{background:#0A2224} .job:hover{background:#163638}"""),
    ("C4-warm-violet", "C4 따뜻한 바닥·보관함 + 남보라", """:root{
      --table:#E4E1DC;--console:#22201D;--console-2:#2E2B27;--console-line:#6E6A64;--on-console:#F4F2EF;--on-console-2:#B3AEA7;
      --sheet-head:#F8F7F5;--line-2:#DDD9D3;--accent:#5E5AA0;--accent-hover:#4B4780;--accent-soft:#F0F0FB;--accent-on-console:#ABA9EC;--accent-ink:#1B163C}
      .feed{border-bottom-color:#3A3632} .feed textarea,.more input{background:#1A1816} .job:hover{background:#2A2724}"""),
]

shots = []
with sync_playwright() as p:
    b = p.chromium.launch()
    for key, label, css in CANDS:
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(f"http://127.0.0.1:8777/#job={JID}")
        pg.wait_for_selector(".cut .pts")
        if css:
            pg.add_style_tag(content=css)
        pg.wait_for_timeout(600)
        out = HERE / f"{key}.png"
        pg.screenshot(path=str(out))
        shots.append((out, label))
        pg.close()
    b.close()

ims = [Image.open(s) for s, _ in shots]
w, h = ims[0].size
lab = 56
sheet = Image.new("RGB", (w * 2, (h + lab) * 2), "white")
d = ImageDraw.Draw(sheet)
f = ImageFont.truetype(FONT, 30)
for i, (im, (_, label)) in enumerate(zip(ims, shots)):
    x, y = (i % 2) * w, (i // 2) * (h + lab)
    d.text((x + 24, y + 12), label, fill="#16181C", font=f)
    sheet.paste(im, (x, y + lab))
sheet.thumbnail((1568, 1568))
sheet.save(HERE / "compare.png")
print("\n".join(f"{s.name}: {l}" for s, l in shots))
