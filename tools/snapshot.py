#!/usr/bin/env python3
"""돌아가는 프로토타입 화면을 정적 페이지(`sample/`)로 뜬다.

팀원과 심사위원이 파이썬 없이 링크만 열어도 지금 무엇이 만들어졌는지 보게 하는 것이
목적이다. 스크린샷이 아니라 서버가 실제로 만든 HTML 이라 값과 문구가 진짜다.

    python3 tools/snapshot.py

서버를 직접 띄우고, 세 화면을 받아 정적으로 고친 뒤 서버를 내린다.
화면(`app/server.py`)을 고쳤으면 이걸 다시 돌려야 `sample/` 이 갱신된다.
"""
import datetime as _dt
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "sample"

# 스냅샷에 담을 근무. 주간조는 확정 일지, 야간조는 승인 대기 화면이 되도록 만든다.
# 고정 날짜를 쓰면 원본 보관 기간(RAW_RETENTION_DAYS)을 지나 run 이 깨진다.
# smoke.sh 에서 같은 병을 두 번 겪었다. 항상 보관 기간 안에 있는 오늘을 쓴다.
DAY = _dt.date.today().isoformat()
# 왼쪽 목록 + 오른쪽 내용이 한 화면이라 /draft 가 곧 초안 검토 화면이다 — 따로 뜨던 draft.html 은 없앴다.
PAGES = {
    "index.html": "/draft",                   # / 도 여기로 303. 가장 최근 근무(야간 초안)가 펴져 있다
    "handover.html": f"/shift/{DAY}-day",     # 확정 일지
}
# 정적본에서 링크가 갈 곳. 야간 줄은 index.html 이 이미 그 화면이라 제자리로 돌아온다.
LINKS = {
    "/draft": "index.html",
    f"/shift/{DAY}-night": "index.html",
    f"/shift/{DAY}-day": "handover.html",
}
NOTES = {
    "index.html": (
        "<b>여기가 근무자가 보는 한 화면입니다.</b> 왼쪽에서 근무를 고르면 오른쪽에 그 근무가 열립니다 — "
        f"지금 열려 있는 <b>{DAY}-night</b> 는 아직 승인 전인 <b>초안 검토</b> 이고, 왼쪽의 "
        f"<b>{DAY}-day</b> 줄을 누르면 이미 승인된 <b>확정 일지</b> 입니다. "
        "각 항목의 초록 칸(<b>과거 조치</b>)을 보세요 — 이건 우리가 넣어 둔 문구가 아니라, "
        "<b>앞 근무자가 승인하면서 직접 쓴 코멘트</b>가 저장됐다가 돌아온 것입니다. "
        "이 순환이 ENGRA 의 핵심입니다."
    ),
    "handover.html": (
        "<b>승인이 끝나면 이렇게 확정됩니다.</b> 여기 적힌 코멘트가 곧 다음 근무 초안의 "
        "'과거 조치' 로 나타납니다."
    ),
}


def banner(note):
    return f"""<div class="card" style="background:#eef3fb;border-color:#c9d9ef;color:#25405f;
font-size:12.5px;line-height:1.75;padding:13px 17px">
<b>실제로 돌아가는 프로토타입 화면입니다.</b> 스크린샷이 아니라 서버가 만든 화면을 그대로 떠 온
것이라 값과 문구가 전부 진짜입니다. 다만 이 페이지는 정적이라 <b>버튼은 눌러도 반응하지
않습니다.</b> 직접 돌려 보시려면 저장소를 받아 <code class="mono">python3 app/cli.py serve</code>
를 실행하세요 (설치할 것 없음).
<div style="margin-top:9px;padding-top:9px;border-top:1px solid #d6e2f2">{note}</div>
</div>"""


def cli(*args):
    r = subprocess.run([sys.executable, "app/cli.py", *args],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"실패: cli.py {' '.join(args)}\n{r.stdout}{r.stderr}")
    return r.stdout


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_data():
    """주간조 확정 → 야간조 초안 대기. 순환이 화면에 보이게 하는 최소 구성."""
    for f in ("engra.db", "engra.db-wal", "engra.db-shm",
              "sample_day.csv", "sample_night.csv"):
        (ROOT / "app" / f).unlink(missing_ok=True)

    cli("init")
    # ISO 문자열을 그대로 넘긴다. "2026, 08, 22" 로 조립하면 08 이 8진수로 해석돼 깨진다.
    code = (
        "import sys; sys.path.insert(0, 'app')\n"
        "from datetime import date\n"
        "from pathlib import Path\n"
        "import sample_data\n"
        "d = date.fromisoformat(sys.argv[1])\n"
        "sample_data.generate(Path('app/sample_day.csv'),   kind='day',"
        "   date=d, minutes=240, seed=42)\n"
        "sample_data.generate(Path('app/sample_night.csv'), kind='night',"
        " date=d, minutes=240, seed=17)\n"
    )
    r = subprocess.run([sys.executable, "-c", code, DAY], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"샘플 생성 실패\n{r.stderr}")

    cli("ingest", "app/sample_day.csv")
    cli("ingest", "app/sample_night.csv")
    cli("run", f"{DAY}-day")
    cli("approve", f"{DAY}-day", "--all", "--status", "완료", "--by", "박경모",
        "--comment", "샘플링 재확인 결과 정상. 분석기 셀 청소 후 안정화 확인")
    cli("run", f"{DAY}-night")


def fetch(base, path):
    for _ in range(60):
        try:
            return urllib.request.urlopen(base + path, timeout=5).read().decode("utf-8")
        except Exception:
            time.sleep(0.25)
    raise SystemExit(f"서버 응답 없음: {path}")


def to_static(html, name):
    for src, dst in LINKS.items():
        for q in ('"', "'"):
            html = html.replace(f"href={q}{src}{q}", f"href={q}{dst}{q}")

    # 동작하지 않는 폼은 제출 경로를 없앤다 — 승인 폼만이 아니라 확정 화면의 재검토 폼까지 POST 폼 전부.
    # 폼 태그가 바뀌면 치환이 조용히 빗나가 정적 페이지에 제출 경로가 남는다(onsubmit 이 붙었을 때 실제로 그랬다) — 남으면 멈춘다.
    # 대소문자·따옴표·공백 표기가 달라도 같은 POST 폼이다 — METHOD="post" · method='post' · method=post 가 빠져나갔다(반증 워커).
    post = r'method\s*=\s*["\']?post\b'
    html = re.sub(r'<form\b[^>]*\b' + post + r'[^>]*>', '<form onsubmit="return false">', html, flags=re.I)
    if re.search(post, html, re.I):
        raise SystemExit(f"{name}: POST 폼을 정적화하지 못했다 — app/server.py 의 폼 태그가 바뀌었으면 여기 치환도 고쳐라")
    html = re.sub(r'<button type="submit" class="btn">.*?</button>',
                  '<span class="muted" style="font-size:12px">'
                  '승인 버튼은 실제 프로토타입에서 동작합니다</span>', html)

    # 버튼이 안 먹는데 승인 바가 따라다니면 본문만 가린다. 실제 앱에서는 sticky 가 맞다.
    html = html.replace("</style>",
                        ".bar{position:static !important;bottom:auto !important}</style>", 1)

    # 안내 배너를 본문 맨 앞에 넣는다. 앵커는 page() 의 본문 시작 태그다 —
    # 옛 이동줄(<div class="nav">…</div>)의 닫는 태그를 앵커로 쓰다가, 이동줄을 없앤 새 page() 에서
    # 매치가 0 이 되어 안내가 말없이 빠졌다(반증 워커 실측). re.sub 는 매치 0 을 알려주지 않으니 세고 멈춘다.
    html, n = re.subn(r'<main class="wrap">',
                      lambda m: m.group(0) + banner(NOTES[name]), html, count=1)
    if n != 1:
        raise SystemExit(f"{name}: 안내 배너를 넣을 자리를 못 찾았다 — app/server.py 의 page() 본문 태그가 바뀌었으면 여기 앵커도 고쳐라")
    return html


def main():
    build_data()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    srv = subprocess.Popen([sys.executable, "app/cli.py", "serve", "--port", str(port)],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        OUT.mkdir(exist_ok=True)
        for name, path in PAGES.items():
            html = to_static(fetch(base, path), name)
            (OUT / name).write_text(html, encoding="utf-8")
            print(f"  {name}  {len(html):,}자")
    finally:
        srv.terminate()
        srv.wait(timeout=10)

    # 저장소에 남기지 않는다 (app/.gitignore)
    for f in ("engra.db", "engra.db-wal", "engra.db-shm",
              "sample_day.csv", "sample_night.csv"):
        (ROOT / "app" / f).unlink(missing_ok=True)
    shutil.rmtree(ROOT / "app" / "__pycache__", ignore_errors=True)

    # 목록 주소가 / 에서 /draft 로 바뀌며 「‹ 목록으로」 치환이 조용히 빗나갔다(조각 1 반증) — /draft 링크가 남아도 멈춘다
    leftover = [n for n in PAGES if any(s in (OUT / n).read_text(encoding="utf-8") for s in ("/shift/", 'href="/draft"'))]
    if leftover:
        raise SystemExit(f"정적 링크로 안 바뀐 페이지: {leftover}")
    print(f"\n생성 위치: {OUT}  (링크·폼 정적화 확인)")


if __name__ == "__main__":
    main()
