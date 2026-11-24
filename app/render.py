"""시험지 HTML · PDF 렌더링 (노트북 셀 5 이식, 레이아웃 로직 변경 없음)."""

import html
import re
import os
import base64
from pathlib import Path
from functools import lru_cache

from playwright.async_api import async_playwright

from . import config

CIRCLED = {1: "①", 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥", 7: "⑦", 8: "⑧", 9: "⑨", 10: "⑩"}


def esc(s):
    return html.escape(str(s)) if s is not None else ""


def underline_word(sentence, word):
    if not word:
        return esc(sentence)
    match = re.search(r'(?<![A-Za-z])' + re.escape(word) + r'(?![A-Za-z])', sentence, re.I)
    if not match:
        return esc(sentence)
    idx = match.start()
    a, b, c = sentence[:idx], sentence[idx : idx + len(word)], sentence[idx + len(word) :]
    return f"{esc(a)}<u>{esc(b)}</u>{esc(c)}"


def choice_line(choices, inline=True):
    cls = "choices-inline" if inline else "choices-col"
    items = "".join(
        f'<span class="choice"><span class="cir">{CIRCLED[i+1]}</span>{esc(c)}</span>'
        for i, c in enumerate(choices)
    )
    return f'<div class="{cls}">{items}</div>'


def render_block(q):
    t = q["type"]
    head = (
        f'<div class="q-head"><span class="q-no">{q["no"]}.</span> '
        f'<span class="q-inst">{esc(q["instruction"]).replace("&lt;u&gt;", "<u>").replace("&lt;/u&gt;", "</u>")}</span></div>'
    )
    body = ""

    if t == "pair_ko":
        rows = "".join(
            f'<div class="opt"><span class="cir">{CIRCLED[i+1]}</span>'
            f'<span class="w">{esc(p["word"])}</span><span class="dash">–</span>'
            f'<span class="m">{esc(p["ko"])}</span></div>'
            for i, p in enumerate(q["payload"]["pairs"])
        )
        body = f'<div class="pair-list">{rows}</div>'

    elif t == "sentence_match":
        rows = "".join(
            f'<div class="opt-blk"><span class="cir">{CIRCLED[i+1]}</span>'
            f'<div class="sm"><div class="en">{underline_word(it["en"], it["u"])}</div>'
            f'<div class="ko">{esc(it["ko"])}</div></div></div>'
            for i, it in enumerate(q["payload"]["items"])
        )
        body = f'<div class="sent-list">{rows}</div>'

    elif t == "common_blank":
        sents = "".join(f'<div class="ex">- {esc(s)}</div>' for s in q["payload"]["sentences"])
        body = (
            f'<div class="rule"></div><div class="ex-box">{sents}</div><div class="rule"></div>'
            + choice_line(q["payload"]["choices"], inline=True)
        )

    elif t == "def_match":
        cells = "".join(
            f'<span class="cir">{CIRCLED[i+1]}</span>'
            f'<span class="w">{esc(p["word"])}</span>'
            f'<span class="colon">:</span>'
            f'<span class="def">{esc(p["def"])}</span>'
            for i, p in enumerate(q["payload"]["pairs"])
        )
        body = f'<div class="def-grid">{cells}</div>'

    elif t == "count_correct":
        tb = q["payload"]["table"]
        rows = "".join(
            f'<tr><td class="ti">{chr(97+i)}.</td><td class="tw">{esc(x["word"])}</td>'
            f'<td class="tk">{esc(x["ko"])}</td></tr>'
            for i, x in enumerate(tb)
        )
        table = f'<table class="v-table"><tbody>{rows}</tbody></table>'
        body = table + choice_line(q["payload"]["choices"], inline=True)

    elif t == "blank_ko":
        body = (
            f'<div class="rule"></div>'
            f'<div class="ex">{esc(q["payload"]["sentence"])}</div>'
            f'<div class="ko">{esc(q["payload"]["ko"])}</div>'
            f'<div class="rule"></div>'
            + choice_line(q["payload"]["choices"], inline=True)
        )

    elif t == "word_meaning":
        p = q['payload']
        if p.get('sentence'):
            body = '<div class="rule"></div><div class="ex context-sentence">' + underline_word(p['sentence'], p.get('target', '')) + '</div><div class="rule"></div>'
        body += choice_line(p["choices"], inline=True)

    elif t == "passage_blank":
        body = (
            f'<div class="rule"></div>'
            f'<div class="passage">{esc(q["payload"]["passage"])}</div>'
            f'<div class="rule"></div>'
            + choice_line(q["payload"]["choices"], inline=True)
        )

    return f'<div class="q-block">{head}{body}</div>'


def render_answers(questions):
    left_rows, right_rows = [], []
    for q in questions:
        left_rows.append(f'<div class="ans-line"><b>{q["no"]}.</b> {CIRCLED[q["answer"]]}</div>')
        exp = esc(q.get("explanation", "") or "").replace("\n", "<br>")
        if not exp:
            exp = "(해설 없음)"
        right_rows.append(
            f'<div class="ans-exp"><span class="ans-no">{q["no"]}.</span>'
            f'<span class="ans-tx">{exp}</span></div>'
        )
    left = "".join(left_rows)
    right = "".join(right_rows)
    return (
        '<div class="ans-block">'
        '<div class="ans-head">'
        '<div class="q-head"><span class="q-inst">[정답 및 해설]</span></div>'
        '<div class="rule"></div>'
        "</div>"
        '<div class="ans-two">'
        f'<div class="ans-left">{left}</div>'
        f'<div class="ans-right">{right}</div>'
        "</div>"
        "</div>"
    )


CSS = """
:root{ --ink:#111; --line:#111; --gray:#8a8a8a; --graybg:#8f8f8f; }
*{ box-sizing:border-box; }
body{ margin:0; background:#fff; font-family:'Noto Sans KR','Malgun Gothic',sans-serif; color:var(--ink); }
.sheet{ padding:0 2mm; position:relative; z-index:1; }
.page-break{ break-before:page; -webkit-column-break-before:page; }
.divider{ position:fixed; top:0; bottom:0; left:50%; width:0;
          border-left:1px solid var(--line); z-index:0; }
.titlebar{ background:#111; color:#fff; text-align:center; font-weight:800; font-size:17px;
           padding:7px 0; letter-spacing:1px; }
.masthead{ background:#fff; position:relative; z-index:2; padding-bottom:5mm; }
.info{ display:grid; grid-template-columns:1fr 1.8fr 1fr 1.8fr; border:1px solid var(--line);
       border-top:none; background:#fff; }
.info>div{ border-right:1px solid var(--line); padding:6px 12px; font-size:12px; }
.info>div:last-child{ border-right:none; }
.info .lb{ background:var(--graybg); color:#fff; font-weight:700; text-align:center; }
.cols{ columns:2; column-gap:9mm; column-fill:auto; }
.q-block{ break-inside:avoid; -webkit-column-break-inside:avoid; margin-bottom:5mm; font-size:12px;
          line-height:1.5; text-align:justify; }
.q-head{ margin-bottom:3px; display:flex; align-items:baseline; }
.q-no{ font-weight:600; margin-right:.3em; flex:0 0 auto; }
.q-inst{ font-weight:500; flex:1 1 auto; white-space:pre-wrap; }
.q-inst u,.q-head u{ text-underline-offset:2px; }
.cir{ margin-right:3px; }
.pair-list .opt{ display:grid; grid-template-columns:16px 92px 14px 1fr; align-items:baseline; padding:1px 0; }
.opt-blk{ display:grid; grid-template-columns:16px 1fr; padding:2px 0; }
.def-grid{ display:grid; grid-template-columns:auto auto auto 1fr; column-gap:4px; row-gap:3px;
           align-items:baseline; }
.def-grid .cir{ margin-right:0; }
.def-grid .colon{ text-align:center; padding:0 6px; }
.rule{ border-top:1px solid var(--line); margin:5px 0; }
.ex{ padding:1px 0; }
.ex-box{ padding:2px 0; }
.choices-inline{ display:flex; flex-wrap:wrap; gap:4px 14px; margin-top:4px; }
.choices-inline .choice{ max-width:100%; overflow-wrap:anywhere; }
.choices-col .choice{ display:block; padding:1px 0; }
.v-table{ width:100%; border-collapse:collapse; margin:3px 0 6px; font-size:11.5px; }
.v-table td{ border:1px solid var(--line); padding:3px 6px; }
.v-table td.ti{ width:1%; white-space:nowrap; text-align:center; padding:3px 5px; }
.v-table td.tw{ width:1%; white-space:nowrap; overflow-wrap:normal; word-break:normal; }
.passage{ text-align:justify; white-space:pre-wrap; }
.context-sentence{ white-space:pre-wrap; }

.ans-block{ font-size:12px; line-height:1.55; }
.ans-head{ background:#fff; position:relative; z-index:2; }
.ans-two{ display:flex; align-items:flex-start; }
.ans-left{ width:50%; padding-right:4.5mm; }
.ans-right{ width:50%; padding-left:4.5mm; }
.ans-line{ padding:0; margin-bottom:1.15em; }
.ans-exp{ display:flex; align-items:baseline; margin-bottom:1.15em; break-inside:avoid; }
.q-block,.ans-tx,.def-grid>*{overflow-wrap:anywhere;min-width:0}
.def-grid{grid-template-columns:auto minmax(40px,auto) auto minmax(0,1fr)}
.ans-exp .ans-no{ font-weight:600; margin-right:.3em; flex:0 0 auto; }
.ans-exp .ans-tx{ flex:1 1 auto; text-align:justify; }

@page{ size:A4; }
"""


@lru_cache(maxsize=1)
def font_css():
    folder = Path(__file__).resolve().parents[1] / 'static/fonts'
    return ''.join("@font-face{font-family:'Noto Sans KR';font-style:normal;font-weight:"+str(weight)+";src:url(data:font/ttf;base64,"+base64.b64encode((folder/f'NotoSansKR-{weight}.ttf').read_bytes()).decode()+") format('truetype');}" for weight in (400,700))


def render_html(questions, meta, include_answers=True):
    q_blocks = "".join(render_block(q) for q in questions)
    exam_page = f"""
<section class="sheet">
  <div class="masthead">
    <div class="titlebar">{esc(meta['title'])}</div>
    <div class="info">
      <div class="lb">이름</div><div></div><div class="lb">점수</div><div></div>
    </div>
  </div>
  <div class="cols">{q_blocks}</div>
</section>"""
    answer_page = ""
    if include_answers:
        answer_page = f"""
<section class="sheet page-break">
  {render_answers(questions)}
</section>"""
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(meta['title'])}</title>


<style>{font_css()}{CSS}</style>
</head><body>
<div class="divider"></div>{exam_page}{answer_page}
</body></html>"""


async def html_to_pdf(html_str: str, header_html: str, footer_html: str) -> bytes:
    """HTML 문자열을 바로 PDF 바이트로 렌더링한다 (디스크에 쓰지 않음)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"], executable_path=os.environ.get("PDF_BROWSER_EXECUTABLE"))
        page = await browser.new_page()
        await page.route("**/*", lambda route: route.abort())
        await page.emulate_media(media="print")
        await page.set_content(html_str, wait_until="load")
        await page.evaluate("document.fonts.ready")
        pdf_bytes = await page.pdf(
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template=header_html,
            footer_template=footer_html,
            margin={"top": "16mm", "bottom": "14mm", "left": "8mm", "right": "8mm"},
        )
        await browser.close()
        return pdf_bytes


def header_template():
    return (
        '<div style="width:100%; font-family:\'Malgun Gothic\',sans-serif; font-size:11px;'
        ' padding:0 12mm; text-align:right; color:#111;">'
        f'<span style="font-weight:800;">{esc(config.BRAND)}</span></div>'
    )


def footer_template():
    return (
        '<div style="width:100%; font-family:\'Malgun Gothic\',sans-serif; font-size:10px;'
        ' text-align:center; color:#111;"> - <span class="pageNumber"></span> - </div>'
    )
