"""색 후보를 지금 화면(실제 데이터)에 CSS 변수만 바꿔 입혀 캡처한다 (ui-design topics/color.md 5).
값은 palette.py roles/scale 출력에서 가져왔다 (2026-11-25). python color_candidates.py <작업 id>"""
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
JID = sys.argv[1] if len(sys.argv) > 1 else "061e629a90b7"


def dark_shell(shell, text, sub, line, field_bg, sel_bg, primary, primary_ink, current_bg):
    """셸(왼쪽 기둥)을 진하게 칠할 때 바뀌는 것 — 셸 위 글자·입력·선택·주 동작."""
    return f"""
aside{{background:{shell};color:{text}}}
aside .brand, aside .jt{{color:{text}}}
aside .field>span, aside .lab, aside .jm, aside .deps, aside .more, aside .dep button{{color:{sub}}}
aside .feed{{border-bottom-color:{line}}}
aside .feed textarea, aside .more input{{background:{field_bg};border-color:{line};color:{text}}}
aside .feed textarea::placeholder{{color:{sub}}}
aside .seg{{border-color:{line};background:transparent}} aside .seg button{{color:{sub}}} aside .seg button+button{{border-left-color:{line}}}
aside .seg button[aria-pressed=true]{{background:{sel_bg};color:{text};box-shadow:inset 0 0 0 1.5px {text}}}
aside .seg button.locked{{color:{sub}}}
aside .btn.primary{{background:{primary};border-color:{primary};color:{primary_ink}}}
aside .note{{background:{field_bg};color:{text}}} aside .note b{{color:{text}}} aside .note summary{{color:{sub}}}
aside .job:hover{{background:{field_bg}}} aside .job[aria-current=true]{{background:{current_bg}}}
aside hr, aside .sep{{border-color:{line}}}
"""


CANDS = {
    "K0-now": ("지금: 차가운 중립 · 밝은 셸 · 주 색 없음(먹)", ""),
    "K1-teal": ("청록 암실: 회색 청록 물들임 · 진한 청록 셸 · 주 색 청록", """
:root{--table:#E1E6E6;--ink:#181E1F;--ink-2:#485252;--ink-3:#646E6E;--ink-3t:#485252;--line:#DADDDD;--ctl:#C8CDCE;--wash:#EEF2F2;--hover:#E8EEEE;--focus:#017B80}
.sheet .btn.primary,.edit .btn.primary{background:#017B80;border-color:#017B80}
""" + dark_shell("#063D40", "#F2FCFD", "#A8C7C9", "rgba(242,252,253,.22)", "rgba(242,252,253,.07)", "rgba(242,252,253,.14)",
                 "#93DBDF", "#012224", "rgba(242,252,253,.12)")),
    "K2-green": ("초록: 차가운 중립 · 밝은 셸 · 선택·주 동작만 초록", """
:root{--table:#E6E7E9;--ink:#1B1D1F;--ink-2:#4D5053;--ink-3:#696C6F;--ink-3t:#4D5053;--focus:#337D52}
.btn.primary{background:#337D52;border-color:#337D52} .btn.primary:hover{background:#1A5E39}
.seg button[aria-pressed=true]{box-shadow:inset 0 0 0 1.5px #337D52;color:#1A5E39}
.job[aria-current=true]{background:#fff;box-shadow:inset 3px 0 0 #337D52}
"""),
    "K3-violet": ("흑연 셸 + 남보라: 따뜻한 중립 · 흑연 셸(필름 재료) · 주 색 남보라", """
:root{--table:#E7E5E2;--ink:#1E1D1A;--ink-2:#524F4C;--ink-3:#6E6B67;--ink-3t:#524F4C;--line:#DDDCDB;--ctl:#CDCCCA;--wash:#F1F0EE;--hover:#ECEAE7;--focus:#6663A3}
.sheet .btn.primary,.edit .btn.primary{background:#6663A3;border-color:#6663A3}
""" + dark_shell("#1E1D1A", "#F4F3F3", "#B0ADAA", "rgba(244,243,243,.2)", "rgba(244,243,243,.07)", "rgba(244,243,243,.12)",
                 "#A7A6E8", "#1B163C", "rgba(244,243,243,.1)")),
}

shots = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    for key, (label, css) in CANDS.items():
        pg.goto(f"http://127.0.0.1:8777/#job={JID}")
        pg.wait_for_selector(".cut .pts")
        if css:
            pg.add_style_tag(content=css)
        pg.wait_for_timeout(600)
        out = HERE / f"{key}.png"
        pg.screenshot(path=str(out))
        shots.append((out, label))
    b.close()

# 두 장씩 위아래로 (긴 변 1,568 이하로 줄여 디테일은 각 장을 따로 본다)
ims = [Image.open(s) for s, _ in shots]
w, h = ims[0].size
sheet = Image.new("RGB", (w * 2, h * 2), "white")
for i, im in enumerate(ims):
    sheet.paste(im, ((i % 2) * w, (i // 2) * h))
sheet.thumbnail((1568, 1568))
sheet.save(HERE / "compare.png")
print("\n".join(f"{s.name}: {l}" for s, l in shots))
