"""웹 화면.

표준 라이브러리만 쓴다. 팀원이 아무것도 설치하지 않고
`python3 app/cli.py serve` 한 줄로 띄울 수 있어야 하기 때문이다.

디자인은 `demo/index.html` 목업을 그대로 따른다. 목업과 프로토타입이 서로 다르게
생기면 결선에서 두 화면을 다 설명해야 하고, 화면을 두 번 만들게 된다.
토큰(색·간격·컴포넌트)을 목업에서 옮겨왔으므로 목업이 바뀌면 여기도 같이 바꾼다.
"""
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import approve as approve_mod
import db
import ports
from config import DOCS_DIR

# demo/index.html 에서 옮겨온 디자인 토큰과 컴포넌트
STYLE = """
:root{--ink:#14161a;--sub:#6b7280;--line:#e2e0dc;--bg:#f6f5f3;--card:#fff;
      --accent:#EA002C;--ok:#1a7f37;--warn:#b45309}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Apple SD Gothic Neo","Malgun Gothic",sans-serif;background:var(--bg);
     color:var(--ink);font-size:14px;line-height:1.6}
.wrap{max-width:1180px;margin:0 auto;padding:22px 20px 70px}
.top{display:flex;align-items:baseline;gap:12px;margin-bottom:4px;flex-wrap:wrap}
.nav{display:flex;gap:6px;margin:14px 0 18px;flex-wrap:wrap}
.nav a{flex:1;min-width:150px;text-decoration:none;background:var(--card);
 border:1px solid var(--line);border-radius:8px;padding:9px 13px;color:var(--sub);
 font-size:13px;font-weight:600;transition:.12s}
.nav a small{display:block;font-weight:400;font-size:11px;color:var(--sub);margin-top:2px}
.nav a:hover{border-color:#c9c6c1}
.nav a.on{background:var(--ink);border-color:var(--ink);color:#fff}
.nav a.on small{color:#c9c6c1}
.frame{width:100%;height:78vh;border:1px solid var(--line);border-radius:8px;background:var(--card)}
.top h1{font-size:20px;letter-spacing:-.4px}.top h1 b{color:var(--accent)}
.top span{font-size:12.5px;color:var(--sub)}
.top .eng{margin-left:auto;font-size:11.5px}
.disc{font-size:12px;color:var(--sub);background:#fff8e6;border:1px solid #f0e3bd;
      padding:7px 11px;border-radius:6px;margin:12px 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;
      padding:16px 18px;margin-bottom:14px}
.card h2{font-size:14.5px;margin-bottom:10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pill{font-size:10.5px;font-weight:700;padding:1.5px 7px;border-radius:9px;
      background:#eceae6;color:var(--sub)}
.pill.on{background:#dcefe2;color:var(--ok)}
.pill.red{background:#ffe0e6;color:#b00020}.pill.amber{background:#fdf0d8;color:var(--warn)}
.muted{color:var(--sub)}.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.note{font-size:12.5px;color:var(--sub);margin-bottom:11px}

/* 초안 항목 */
.item{border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:10px;
      background:#fff;transition:.15s}
.item.off{opacity:.5;background:#fafafa}
.item .row1{display:flex;align-items:flex-start;gap:10px}
.item input[type=checkbox]{width:17px;height:17px;margin-top:2px;accent-color:var(--accent);cursor:pointer}
.item .ttl{font-weight:700;font-size:13.5px;flex:1}
.item .meta{font-size:11.5px;color:var(--sub);margin:3px 0 8px 27px}
.item .body{margin-left:27px}
.why{font-size:12px;background:#f7f6f4;border-left:3px solid #cfcdc8;padding:6px 10px;
     border-radius:0 5px 5px 0;margin-bottom:8px}
.why b{color:var(--ink)}
.sug{font-size:12px;background:#f2f7f3;border-left:3px solid #a8ceb5;padding:6px 10px;
     border-radius:0 5px 5px 0;margin-bottom:8px}
.sug .lb{font-size:10.5px;font-weight:700;color:var(--ok);display:block;margin-bottom:2px}
.sug button{font:inherit;font-size:11px;border:1px solid #a8ceb5;background:#fff;color:var(--ok);
            border-radius:4px;padding:1px 7px;cursor:pointer;margin-left:6px}
textarea{width:100%;border:1px solid var(--line);border-radius:5px;padding:6px 9px;font:inherit;
         font-size:12.5px;resize:vertical;min-height:34px}
.bar{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:13px 16px;
     background:#fff;border:1px solid var(--line);border-radius:9px;position:sticky;bottom:12px;
     flex-wrap:wrap}
.bar .cnt{font-size:13px}.bar .cnt b{color:var(--accent);font-size:16px}
.btn{font:inherit;font-weight:700;font-size:13.5px;padding:9px 22px;border-radius:7px;border:none;
     cursor:pointer;background:var(--accent);color:#fff;white-space:nowrap}
.btn.ghost{background:#fff;color:var(--sub);border:1px solid var(--line)}

/* 수동 추가 */
.item.man{border-style:dashed}
.item .tin{flex:1;font:inherit;font-weight:700;font-size:13.5px;border:none;
           border-bottom:1px solid var(--line);padding:1px 2px;background:none}
.item .tin:focus{outline:none;border-bottom-color:var(--accent)}
.item .min{width:100%;font:inherit;font-size:11.5px;color:var(--sub);border:none;
           border-bottom:1px dashed var(--line);padding:1px 2px;margin-bottom:8px;background:none}
.item .min:focus{outline:none;border-bottom-color:var(--accent)}
.del{font:inherit;font-size:11.5px;background:none;border:1px solid var(--line);color:var(--sub);
     border-radius:5px;padding:2px 9px;cursor:pointer}
.del:hover{border-color:var(--accent);color:var(--accent)}

/* 일지 조회 */
.logrow{display:flex;align-items:center;gap:14px;padding:11px 14px;border:1px solid var(--line);
        border-radius:8px;background:#fff;margin-bottom:8px;transition:.12s;
        text-decoration:none;color:inherit}
.logrow:hover{border-color:var(--accent);background:#fffafb}
.logrow .d{font-family:ui-monospace,Menlo,monospace;font-size:13px;font-weight:700;width:104px}
.logrow .s{font-size:12.5px;width:52px;color:var(--sub)}
.logrow .x{flex:1;font-size:12.5px;color:var(--sub);overflow:hidden;text-overflow:ellipsis;
           white-space:nowrap}
.logrow .c{font-size:11.5px}
.logrow .arw{color:#c9c7c2;font-size:15px}
.det h3{font-size:15px;margin-bottom:2px}
.det .sub{font-size:12px;color:var(--sub);margin-bottom:14px}
.ent{border-left:3px solid var(--accent);padding:2px 0 2px 12px;margin-bottom:14px}
.ent .t{font-weight:700;font-size:13px}
.ent .m{font-size:11.5px;color:var(--sub);margin-bottom:4px}
.ent .c{font-size:12.5px;background:#f7f6f4;padding:6px 10px;border-radius:5px}
.ex{border-left:3px solid #d8d6d1;padding:2px 0 2px 12px;margin-bottom:9px;opacity:.72}
.ex .t{font-size:12.5px}
.back{font:inherit;font-size:12.5px;color:var(--accent);text-decoration:none;
      display:inline-block;margin-bottom:12px;font-weight:700}
.empty{color:var(--sub);text-align:center;padding:28px;font-size:13px}
"""

SCRIPT = """
function tg(cb){cb.closest('.item').classList.toggle('off',!cb.checked);cnt()}
function cnt(){
  var b=document.querySelectorAll('.item:not(.man) input[type=checkbox]');
  var n=0;b.forEach(function(c){if(c.checked)n++});
  var el=document.getElementById('n');if(el)el.textContent=n;
}
function use(btn,txt){
  var ta=btn.closest('.body').querySelector('textarea');
  ta.value=txt;ta.focus();
}
var mi=0;
function addMan(){
  mi++;
  var d=document.createElement('div');
  d.className='item man';
  d.innerHTML='<div class="row1"><input type="checkbox" checked disabled>'+
    '<input class="tin" name="manual_title" placeholder="제목 — 예: 3번 압축기 소음 증가" required>'+
    '<button type="button" class="del" onclick="this.closest(\\'.item\\').remove()">삭제</button></div>'+
    '<div class="body" style="margin-top:6px"><textarea name="manual_body" '+
    'placeholder="내용 — 언제부터, 무엇을, 다음 근무가 무엇을 봐야 하는지"></textarea></div>';
  document.getElementById('mans').appendChild(d);
  d.querySelector('.tin').focus();
}
document.addEventListener('DOMContentLoaded',cnt);
"""


NAV = (
    ("/dcs", "① DCS", "실시간 감시 · 기존 시스템"),
    ("/rtdb", "② RTDB", "구간 데이터 추출"),
    ("/draft", "③ ENGRA 초안 검토", "핵심 화면 · 직접 승인해 보세요"),
    ("/", "④ 일지 조회", "축적 · 검색"),
)


def _nav(active):
    """네 화면을 한 주소에서 이어 보게 한다. DCS·RTDB 는 임도영님 원본을 그대로 띄운다."""
    out = []
    for href, label, sub in NAV:
        on = " on" if href == active else ""
        out.append(f'<a class="{on.strip() or ""}" href="{href}">{label}<small>{sub}</small></a>')
    return f'<div class="nav">{"".join(out)}</div>'


def page(title, body, active=None):
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · ENGRA</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%23EA002C'/><text x='16' y='23' font-size='19' font-family='sans-serif' font-weight='700' fill='white' text-anchor='middle'>E</text></svg>">
<style>{STYLE}</style>
<div class="wrap">
<div class="top"><h1><b>ENGRA</b> 교대 인수인계</h1>
<span>운전 데이터가 먼저 쓰고, 근무자가 마무리합니다</span>
<span class="eng pill{' on' if ports.engine_source() == 'engine' else ''}">엔진 {ports.engine_source()}</span></div>
{_nav(active)}
{body}
</div><script>{SCRIPT}</script></html>"""


def esc(v):
    return html.escape(str(v)) if v is not None else ""


def _span(ts):
    """2026-08-24T06:00:00 -> 08-24 06:00"""
    return (ts or "")[5:16].replace("T", " ")


def _sev_pill(sev):
    cls = {"상": "pill red", "중": "pill amber"}.get(sev, "pill")
    return f'<span class="{cls}">중요도 {esc(sev or "-")}</span>'


# --- 화면 -------------------------------------------------------------

ASU_FILE = DOCS_DIR / "asu_dcs_overview.html"

# 임도영님 원본을 그대로 서비스한다. 복사하지 않는 이유 — 그분이 고칠 때마다
# 사본이 뒤처지고, 태그 체계가 어긋나면 심사에서 화면과 데이터가 다르게 보인다.
# 해시(#ovw/#data/#scen)로 원하는 탭을 열도록 클릭 한 줄만 덧붙인다.
_ASU_TAB_JS = """
<script>
(function(){
  function open(){
    var h=(location.hash||'').replace('#','');
    var id={ovw:'tabOvw',data:'tabData',scen:'tabScen'}[h];
    if(!id) return;
    var el=document.getElementById(id);
    if(el && el.click) el.click();
  }
  if(document.readyState==='complete') open();
  else window.addEventListener('load', open);
  window.addEventListener('hashchange', open);
})();
</script>"""


def view_asu():
    """설비 개요·태그 상세 원본 (docs/asu_dcs_overview.html)."""
    if not ASU_FILE.exists():
        return page("설비 화면 없음",
                    '<div class="card"><div class="empty">'
                    'docs/asu_dcs_overview.html 이 없습니다.</div></div>')
    return ASU_FILE.read_text(encoding="utf-8") + _ASU_TAB_JS


def _embed(title, active, tab, note):
    """원본 화면을 우리 이동줄 안에 품는다."""
    return page(title, f"""<div class="card">
<h2>{esc(title)} <span class="pill">기존 시스템 · ENGRA 개입 없음</span></h2>
<p class="note">{note}</p>
<iframe class="frame" src="/asu#{tab}" title="{esc(title)}"></iframe>
<p class="note" style="margin-top:10px">이 화면은 팀에서 만든 모의 설비 화면
(<code>docs/asu_dcs_overview.html</code>)을 그대로 띄운 것입니다. 태그 체계·정상범위·
알람 한계는 <code>docs/tag_master.csv</code>(계측 태그 53점)를 정본으로 씁니다.
모든 값은 가상 데이터입니다.</p>
</div>""", active=active)


def view_dcs():
    return _embed("제어시스템 공정 개요 화면", "/dcs", "ovw",
                  "근무자가 근무 중 주시하는 화면입니다. 공정 흐름도 위에 태그값이 직접 "
                  "표시되고 임계값을 넘으면 색으로 경보가 뜹니다. 실시간 감시와 알람은 "
                  "전적으로 DCS 의 역할이며 ENGRA 는 이 화면에 나타나지 않습니다.")


def view_rtdb():
    return _embed("실시간 데이터베이스 — 태그 상세", "/rtdb", "data",
                  "DCS 가 감시하는 태그값이 2초 주기로 쌓이는 원본입니다. 태그를 고르면 "
                  "추이 파형이 그려집니다. 12시간이면 태그당 21,600점, 태그 53점이면 "
                  "114만 점이라 사람이 훑어 이상을 찾는 것은 불가능합니다 — ENGRA 는 "
                  "구간이 닫히는 시각에 이 전체를 한 번에 검토합니다.")


def view_draft():
    """승인 대기 중인 가장 최근 근무의 초안을 띄운다.

    이동줄에서 초안 검토를 누를 때 근무 ID 를 몰라도 되게 하려는 것이다.
    대기 중인 것이 없으면 (전부 확정됐으면) 가장 최근 근무를 보여준다.
    """
    with db.connect() as conn:
        rows = db.list_shifts(conn)
        pending = None
        newest = None
        for r in rows:
            if newest is None:
                newest = r["id"]
            if db.load_handover(conn, r["id"]) is None and db.load_draft(conn, r["id"]):
                pending = r["id"]
                break
    target = pending or newest
    if not target:
        return page("초안", '<div class="card"><div class="empty">'
                            '아직 근무 구간이 없습니다.</div></div>', active="/draft")
    return view_shift(target, active="/draft")


def view_index():
    with db.connect() as conn:
        rows = db.list_shifts(conn)
        summaries = {
            r["id"]: [e["tag"] for e in db.load_events(conn, r["id"])] for r in rows
        }

    if not rows:
        return page("일지", '<div class="card"><div class="empty">등록된 근무 구간이 없습니다.<br>'
                            '<code class="mono">python3 app/cli.py ingest &lt;csv&gt;</code> 로 '
                            '데이터를 넣으세요.</div></div>')

    out = []
    for r in rows:
        tags = summaries.get(r["id"]) or []
        gist = " · ".join(tags[:3]) + (f" 외 {len(tags) - 3}건" if len(tags) > 3 else "")
        status = r["draft_status"] or ""
        if status == "confirmed":
            badge = f'<span class="pill on">채택 {r["adopted_count"]}건</span>'
        elif status == "pending":
            badge = '<span class="pill amber">승인 대기</span>'
        else:
            badge = '<span class="pill">미생성</span>'
        date, kind = r["id"].rsplit("-", 1)
        out.append(
            f'<a class="logrow" href="/shift/{esc(r["id"])}">'
            f'<span class="d">{esc(date)}</span>'
            f'<span class="s">{"주간" if kind == "day" else "야간"}</span>'
            f'<span class="x">{esc(gist) or "감지 항목 없음"}</span>'
            f'<span class="c">{badge}</span><span class="arw">›</span></a>'
        )

    body = (f'<div class="card" style="padding:14px 18px">'
            f'<h2>근무 일지</h2>'
            f'<p class="note" style="margin:0">근무를 누르면 초안 검토 또는 확정 일지로 들어갑니다. '
            f'분석 구간은 교대 1시간 전을 경계로 나뉩니다.</p></div>'
            f'{"".join(out)}')
    return page("일지 조회", body, active="/")


def _view_confirmed(shift_id, draft, handover, active="/"):
    adopted = [i for i in draft["items"] if i["adopted"] == 1]
    excluded = [i for i in draft["items"] if i["adopted"] == 0]

    ents = []
    for it in adopted:
        man = ' <span class="pill">직접 추가</span>' if it["origin"] == "manual" else ""
        comment = (f'<div class="c">{esc(it["comment"])} '
                   f'<span class="muted">— {esc(handover["confirmed_by"])}</span></div>'
                   if it.get("comment")
                   else '<div class="c muted">코멘트 없음</div>')
        ents.append(
            f'<div class="ent"><div class="t">{esc(it["title"])} {_sev_pill(it["severity"])}{man}</div>'
            f'<div class="m">{esc(it["evidence"] or it["body"])}</div>{comment}</div>'
        )
    if not ents:
        ents.append('<div class="empty">채택된 항목이 없습니다.</div>')

    ex = ""
    if excluded:
        rows = "".join(
            f'<div class="ex"><div class="t">{esc(i["title"])} {_sev_pill(i["severity"])}</div>'
            f'<div class="m">{esc(i["evidence"] or i["body"])} · <b>제외</b></div></div>'
            for i in excluded
        )
        ex = (f'<div style="border-top:1px solid var(--line);margin:18px 0 12px"></div>'
              f'<p class="note" style="margin-bottom:8px"><b style="color:var(--ink)">제외된 항목</b>'
              f' — 근무자가 전달 대상이 아니라고 판단한 항목도 기록으로 남습니다.</p>{rows}'
              f'<p class="note" style="margin-top:14px">이 기록은 감지 감도 조정의 근거로 사용되며, '
              f'같은 유형이 반복 제외될 경우 해당 검출기의 임계값을 상향합니다.</p>')

    date, kind = shift_id.rsplit("-", 1)
    return page(shift_id, f"""<a class="back" href="/">‹ 목록으로</a>
<div class="card det">
<h3>{esc(date)} {"주간조" if kind == "day" else "야간조"} 인수인계서</h3>
<div class="sub">분석 구간 {esc(_span(draft.get("window_start")))} · 작성 ENGRA
{esc((draft["generated_at"] or "").replace("T", " "))} · 승인 {esc(handover["confirmed_by"])}
{esc((handover["confirmed_at"] or "").replace("T", " "))} · 감지 {len(draft["items"])}건 중
<b>{handover["adopted_count"]}건 채택 / {handover["excluded_count"]}건 제외</b></div>
{"".join(ents)}{ex}
</div>""", active=active)


def _view_pending(shift_id, draft, active="/"):
    items = []
    for it in draft["items"]:
        sug = ""
        if it["suggested_action"]:
            quoted = json.dumps(it["suggested_action"], ensure_ascii=False)
            n = len(it["precedents"]) or 1
            sug = (f'<div class="sug"><span class="lb">과거 조치 추천 — 유사 사례 {n}건</span>'
                   f'{esc(it["suggested_action"])}'
                   f'<button type="button" onclick="use(this,{html.escape(quoted, quote=True)})">'
                   f'코멘트로 사용</button></div>')
        items.append(f"""<div class="item">
<div class="row1"><input type="checkbox" name="item" value="{it['id']}" checked onchange="tg(this)">
<div class="ttl">{esc(it['title'])} {_sev_pill(it['severity'])}</div></div>
<div class="meta">{esc(it['tag'])} · {esc(it['body'])}</div>
<div class="body">
<div class="why"><b>감지 근거</b> — {esc(it['evidence'])}</div>
{sug}
<textarea name="comment_{it['id']}" placeholder="코멘트 (선택)"></textarea>
</div></div>""")

    if not items:
        items.append('<div class="card"><div class="empty">감지된 항목이 없습니다. '
                     '아래에서 직접 추가할 수 있습니다.</div></div>')

    disc = ""
    if ports.engine_source() == "stub":
        disc = ('<div class="disc">임시 엔진으로 돌고 있습니다. 알람 한계를 넘은 것만 잡히고, '
                '헌팅·드리프트·상관 붕괴 같은 신호는 감지되지 않습니다. '
                '검출 엔진이 연결되면 이 목록이 달라집니다.</div>')

    date, kind = shift_id.rsplit("-", 1)
    n = len(draft["items"])
    return page(shift_id, f"""<a class="back" href="/">‹ 목록으로</a>
{disc}
<div class="card" style="display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;
     align-items:flex-start">
<div><h2 style="margin-bottom:4px">{esc(date)} {"주간조" if kind == "day" else "야간조"}
인수인계 초안</h2>
<p class="note" style="margin:0">감지된 항목을 <b style="color:var(--ink)">전부</b> 보여줍니다.
적을 것을 고르고, 필요하면 코멘트를 답니다. 최종 판단은 근무자가 합니다.</p></div>
<div class="muted" style="font-size:12px;text-align:right">감지
<b style="color:var(--ink)">{n}</b>건<br>전체 표시 (걸러내지 않음)</div>
</div>
<form method="post" action="/approve">
<input type="hidden" name="shift_id" value="{esc(shift_id)}">
{"".join(items)}
<div id="mans"></div>
<div class="card" style="padding:12px 16px;display:flex;justify-content:space-between;
     gap:12px;flex-wrap:wrap;align-items:center">
<span class="note" style="margin:0">감지되지 않았지만 넘겨야 할 것이 있으면 직접 추가합니다.
여러 건을 넣을 수 있습니다.</span>
<button type="button" class="btn ghost" onclick="addMan()">+ 항목 직접 추가</button></div>
<div class="bar"><div class="cnt">채택 <b id="n">{n}</b> / <span>{n}</span>건
<span class="muted" style="font-size:12px">· 제외 항목도 기록으로 남습니다</span></div>
<button type="submit" class="btn">승인하고 확정</button></div>
</form>""", active=active)


def view_shift(shift_id, active="/"):
    with db.connect() as conn:
        draft = db.load_draft(conn, shift_id)
        handover = db.load_handover(conn, shift_id)
        row = conn.execute(
            "SELECT window_start FROM shift WHERE id = ?", (shift_id,)
        ).fetchone()

    if draft is None:
        return page(shift_id, f'<a class="back" href="/">‹ 목록으로</a>'
                              f'<div class="card"><div class="empty">{esc(shift_id)} 의 초안이 '
                              f'없습니다.<br><code class="mono">python3 app/cli.py run '
                              f'{esc(shift_id)}</code></div></div>', active=active)

    draft["window_start"] = row["window_start"] if row else None
    if handover:
        return _view_confirmed(shift_id, draft, handover, active)
    return _view_pending(shift_id, draft, active)


# --- 서버 -------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ENGRA"

    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path}")

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send(200, view_index())
            elif path == "/draft":
                self._send(200, view_draft())
            elif path == "/dcs":
                self._send(200, view_dcs())
            elif path == "/rtdb":
                self._send(200, view_rtdb())
            elif path == "/asu":
                self._send(200, view_asu())
            elif path.startswith("/shift/"):
                self._send(200, view_shift(path[len("/shift/"):]))
            elif path == "/api/shifts":
                with db.connect() as conn:
                    self._json([dict(r) for r in db.list_shifts(conn)])
            elif path.startswith("/api/draft/"):
                with db.connect() as conn:
                    self._json(db.load_draft(conn, path[len("/api/draft/"):]))
            else:
                self._send(404, page("없음", '<div class="card"><div class="empty">'
                                             '없는 주소입니다.</div></div>'))
        except Exception as exc:  # 화면에 그대로 드러낸다 — 조용히 넘기지 않는다
            self._send(500, page("오류", f'<div class="card"><h2>오류</h2>'
                                        f'<pre class="mono">{esc(exc)}</pre></div>'))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        try:
            if urlparse(self.path).path != "/approve":
                self._send(404, page("없음", '<div class="card">없는 주소입니다.</div>'))
                return

            shift_id = form["shift_id"][0]
            chosen = {int(v) for v in form.get("item", [])}

            # 직접 추가는 여러 건이 올 수 있다. 같은 이름으로 반복 전송된다.
            titles = form.get("manual_title", [])
            bodies = form.get("manual_body", [])
            for i, title in enumerate(titles):
                title = title.strip()
                if title:
                    body = bodies[i].strip() if i < len(bodies) else ""
                    approve_mod.add_manual(shift_id, title, body)

            with db.connect() as conn:
                draft = db.load_draft(conn, shift_id)
            decisions = {
                it["id"]: {
                    "adopted": it["id"] in chosen or it["origin"] == "manual",
                    "comment": (form.get(f"comment_{it['id']}", [""])[0] or "").strip() or None,
                }
                for it in draft["items"]
            }
            approve_mod.decide(shift_id, decisions)

            self.send_response(303)
            self.send_header("Location", f"/shift/{shift_id}")
            self.end_headers()
        except Exception as exc:
            self._send(500, page("오류", f'<div class="card"><h2>오류</h2>'
                                        f'<pre class="mono">{esc(exc)}</pre></div>'))


def serve(host, port):
    db.init()
    print(f"ENGRA 화면: http://{host}:{port}  (엔진: {ports.engine_source()})")
    print("멈추려면 Ctrl+C")
    try:
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
