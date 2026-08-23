"""웹 화면.

표준 라이브러리만 쓴다. 팀원이 아무것도 설치하지 않고
`python3 app/cli.py serve` 한 줄로 띄울 수 있어야 하기 때문이다.

지금 화면은 **동작 확인용**이다. 디자인은 `demo/index.html` 쪽이 이미 있으니,
2주 차에 그 화면을 여기 API 에 붙인다.
"""
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import approve as approve_mod
import db
import ports

STYLE = """
*{box-sizing:border-box}
body{margin:0;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif;
     background:#f5f6f8;color:#1a1a1a}
header{background:#EA002C;color:#fff;padding:14px 24px;display:flex;align-items:baseline;gap:12px}
header b{font-size:18px;letter-spacing:.5px}
header span{opacity:.85;font-size:13px}
main{max-width:920px;margin:24px auto;padding:0 20px}
.card{background:#fff;border:1px solid #e3e5e9;border-radius:10px;padding:18px 20px;margin-bottom:14px}
.card h3{margin:0 0 6px;font-size:16px}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden}
th,td{padding:11px 14px;text-align:left;border-bottom:1px solid #eceef1;font-size:14px}
th{background:#fafbfc;font-weight:600;color:#5b616e}
tr:last-child td{border-bottom:none}
a{color:#0a58ca;text-decoration:none}
a:hover{text-decoration:underline}
.sev{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;font-weight:600}
.s-상{background:#fdeaec;color:#c0113a}.s-중{background:#fff5e0;color:#9a6800}
.s-하{background:#eef1f5;color:#5b616e}
.ev{color:#5b616e;font-size:13px;margin-top:5px}
.pre{background:#f2f7ff;border-left:3px solid #7aa7e8;padding:8px 11px;margin-top:8px;
     font-size:13px;color:#33465e;border-radius:0 5px 5px 0}
input[type=text]{width:100%;padding:8px 10px;border:1px solid #d6d9de;border-radius:6px;font:inherit}
label.chk{display:flex;gap:9px;align-items:flex-start;cursor:pointer}
button{background:#EA002C;color:#fff;border:0;padding:10px 22px;border-radius:7px;
       font:inherit;font-weight:600;cursor:pointer;white-space:nowrap}
button.ghost{background:#fff;color:#5b616e;border:1px solid #d6d9de;font-weight:400;padding:7px 14px}
.bar{position:sticky;bottom:0;background:#fff;border-top:1px solid #e3e5e9;padding:14px 20px;
     display:flex;justify-content:space-between;align-items:center;margin:0 -20px -18px;
     border-radius:0 0 10px 10px}
.tag{font-family:ui-monospace,Menlo,monospace;font-size:13px}
.empty{color:#8a909c;text-align:center;padding:28px}
pre.log{white-space:pre-wrap;font:13px/1.7 ui-monospace,Menlo,monospace;
        background:#fafbfc;border:1px solid #eceef1;border-radius:7px;padding:14px;margin:0}
.warn{background:#fff8e5;border:1px solid #f0dda8;color:#7a5c00;padding:9px 13px;
      border-radius:7px;font-size:13px;margin-bottom:14px}
"""


def page(title, body):
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · ENGRA</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%23EA002C'/><text x='16' y='23' font-size='19' font-family='sans-serif' font-weight='700' fill='white' text-anchor='middle'>E</text></svg>">
<style>{STYLE}</style>
<header><b>ENGRA</b><span>교대 인수인계 초안</span>
<span style="margin-left:auto">엔진: {ports.engine_source()}</span></header>
<main>{body}</main></html>"""


def esc(v):
    return html.escape(str(v)) if v is not None else ""


def _span(ts):
    """2026-08-24T06:00:00 -> 08-24 06:00"""
    return (ts or "")[5:16].replace("T", " ")


# --- 화면 -------------------------------------------------------------

def view_index():
    with db.connect() as conn:
        rows = db.list_shifts(conn)
    if not rows:
        body = ('<div class="card"><div class="empty">등록된 근무 구간이 없습니다.<br>'
                '<code>python3 app/cli.py ingest &lt;csv&gt;</code> 로 데이터를 넣으세요.</div></div>')
        return page("일지", body)

    tr = []
    for r in rows:
        status = r["draft_status"] or "-"
        label = {"pending": "승인 대기", "confirmed": f"확정 · 채택 {r['adopted_count']}건"}.get(status, "미생성")
        tr.append(
            f"<tr><td><a href='/shift/{esc(r['id'])}'>{esc(r['id'])}</a></td>"
            f"<td>{esc(_span(r['window_start']))} ~ {esc(_span(r['window_end']))}</td>"
            f"<td>{r['event_count']}건</td><td>{esc(label)}</td></tr>"
        )
    body = (f"<h2 style='font-size:19px;margin:0 0 14px'>근무 일지</h2>"
            f"<table><tr><th>근무</th><th>분석 구간</th><th>감지</th><th>상태</th></tr>"
            f"{''.join(tr)}</table>")
    return page("일지 조회", body)


def view_shift(shift_id):
    with db.connect() as conn:
        draft = db.load_draft(conn, shift_id)
        handover = db.load_handover(conn, shift_id)

    if draft is None:
        return page(shift_id, f"<div class='card'><div class='empty'>{esc(shift_id)} 의 초안이 없습니다.<br>"
                              f"<code>python3 app/cli.py run {esc(shift_id)}</code></div></div>")

    if handover:
        excluded = [i for i in draft["items"] if i["adopted"] == 0]
        ex = ""
        if excluded:
            rows = "".join(
                f"<li><span class='tag'>{esc(i['tag'])}</span> {esc(i['title'])}</li>"
                for i in excluded
            )
            ex = (f"<div class='card'><h3>제외한 항목 {len(excluded)}건</h3>"
                  f"<div class='ev'>왜 걸러졌는지가 감도 조정의 재료가 됩니다.</div>"
                  f"<ul style='margin:9px 0 0;padding-left:20px;font-size:14px'>{rows}</ul></div>")
        return page(shift_id, (
            f"<p><a href='/'>← 일지 목록</a></p>"
            f"<div class='card'><h3>{esc(shift_id)} 확정 일지</h3>"
            f"<div class='ev'>{esc(handover['confirmed_by'])} · "
            f"{esc((handover['confirmed_at'] or '').replace('T', ' '))} · "
            f"채택 {handover['adopted_count']} / 제외 {handover['excluded_count']}</div>"
            f"<pre class='log' style='margin-top:12px'>{esc(handover['body'])}</pre></div>{ex}"))

    cards = []
    for it in draft["items"]:
        pre = ""
        if it["suggested_action"]:
            pre = (f"<div class='pre'><b>과거 조치</b> — {esc(it['suggested_action'])}"
                   f"<br><button type='button' class='ghost' style='margin-top:7px'"
                   f" onclick=\"document.getElementById('c{it['id']}').value="
                   f"this.parentNode.textContent.split('—')[1].trim()\">코멘트로 사용</button></div>")
        cards.append(f"""<div class="card">
<label class="chk"><input type="checkbox" name="item" value="{it['id']}" checked style="margin-top:5px">
<span><b>{esc(it['title'])}</b>
<span class="sev s-{esc(it['severity'] or '하')}">{esc(it['severity'] or '-')}</span>
<div class="ev">{esc(it['body'])}</div>
<div class="ev">근거: {esc(it['evidence'])}</div></span></label>
{pre}
<input type="text" id="c{it['id']}" name="comment_{it['id']}" placeholder="코멘트 (선택)" style="margin-top:10px">
</div>""")

    if not cards:
        cards.append("<div class='card'><div class='empty'>감지된 항목이 없습니다. "
                     "직접 추가할 수 있습니다.</div></div>")

    warn = ""
    if ports.engine_source() == "stub":
        warn = ("<div class='warn'>임시 엔진으로 돌고 있습니다. 알람 한계를 넘은 것만 잡히고, "
                "헌팅·드리프트 같은 신호는 감지되지 않습니다.</div>")

    body = f"""<p><a href="/">← 일지 목록</a></p>
{warn}
<h2 style="font-size:19px;margin:0 0 4px">{esc(shift_id)} 인수인계 초안</h2>
<p class="ev" style="margin:0 0 14px">감지된 {len(draft['items'])}건을 전부 보여줍니다.
적을 것을 고르고, 필요하면 코멘트를 답니다.</p>
<form method="post" action="/approve">
<input type="hidden" name="shift_id" value="{esc(shift_id)}">
{''.join(cards)}
<div class="card"><h3>직접 추가</h3>
<div class="ev">감지되지 않았지만 넘겨야 할 것을 적습니다.</div>
<input type="text" name="manual_title" placeholder="제목" style="margin-top:9px">
<input type="text" name="manual_body" placeholder="내용" style="margin-top:7px"></div>
<div class="card" style="padding-bottom:0">
<div class="bar"><span class="ev">확정하면 다음 근무의 참고 자료가 됩니다.</span>
<button type="submit">승인하고 확정</button></div></div>
</form>"""
    return page(shift_id, body)


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
            elif path.startswith("/shift/"):
                self._send(200, view_shift(path[len("/shift/"):]))
            elif path == "/api/shifts":
                with db.connect() as conn:
                    self._json([dict(r) for r in db.list_shifts(conn)])
            elif path.startswith("/api/draft/"):
                with db.connect() as conn:
                    self._json(db.load_draft(conn, path[len("/api/draft/"):]))
            else:
                self._send(404, page("없음", "<div class='card'><div class='empty'>없는 주소입니다.</div></div>"))
        except Exception as exc:  # 화면에 그대로 드러낸다 — 조용히 넘기지 않는다
            self._send(500, page("오류", f"<div class='card'><h3>오류</h3><pre class='log'>{esc(exc)}</pre></div>"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        try:
            if urlparse(self.path).path != "/approve":
                self._send(404, page("없음", "<div class='card'>없는 주소입니다.</div>"))
                return

            shift_id = form["shift_id"][0]
            chosen = {int(v) for v in form.get("item", [])}

            title = (form.get("manual_title", [""])[0] or "").strip()
            if title:
                approve_mod.add_manual(
                    shift_id, title, (form.get("manual_body", [""])[0] or "").strip()
                )

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
            self._send(500, page("오류", f"<div class='card'><h3>오류</h3><pre class='log'>{esc(exc)}</pre></div>"))


def serve(host, port):
    db.init()
    print(f"ENGRA 화면: http://{host}:{port}  (엔진: {ports.engine_source()})")
    print("멈추려면 Ctrl+C")
    try:
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
