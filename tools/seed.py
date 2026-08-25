#!/usr/bin/env python3
"""데모 시드 — 팀 정본 데이터(data/)로 근무를 순서대로 적재·검출·확정한다.

    python3 tools/seed.py                # data/ 루트 2근무 + data/sim 6근무 → 마지막 하나만 승인 대기
    python3 tools/seed.py --only-root    # 루트 2근무만

왜 스크립트로 두나 — 시드는 근무별로 **ingest → run → approve 순서**를 지켜야 한다.
셋을 다 run 한 뒤 승인하면 과거 조치가 하나도 회수되지 않는다(사례 검색이 초안
생성 시점에 일어난다). 손으로 하면 이 순서를 두 번 틀렸다(HANDOFF §7).

승인 코멘트는 `docs/scenarios.md` 「조치 문구」 표에서 태그별로 가져온다 — 임도영님이
"모의 과거 일지 시드" 용도로 만든 문구라, 그대로 넣으면 첫 화면의 「과거 조치 추천」
파란 칸에 현장 문장이 뜬다. 표에 없는 태그는 중립 문구를 쓴다.

마지막 근무는 승인하지 않고 남긴다. 심사위원이 직접 채택·코멘트·승인해 봐야 하기 때문이다.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import db  # noqa: E402

CLI = [sys.executable, str(ROOT / "app" / "cli.py")]
SCENARIOS = ROOT / "docs" / "scenarios.md"
FALLBACK = "현장 확인 후 이상 없음. 다음 근무 계속 관찰 요청"


def action_texts():
    """scenarios.md 「조치 문구」 표 → {태그: 조치 문구}. 여러 태그가 적힌 행은 첫 태그에 붙인다."""
    out = {}
    text = SCENARIOS.read_text(encoding="utf-8")
    sec = text.split("## 조치 문구", 1)
    if len(sec) < 2:
        return out
    for line in sec[1].splitlines():
        m = re.match(r"\|\s*\d+\s*\|\s*`([A-Z]+-\d+)`[^|]*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", line)
        if m:
            tag, act, handoff = m.group(1), m.group(2).strip(), m.group(3).strip()
            out.setdefault(tag, f"{act}. 인계: {handoff}")
    return out


def run(*args, quiet=True):
    r = subprocess.run(CLI + list(args), capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print(r.stdout[-400:], r.stderr[-400:], file=sys.stderr)
        raise SystemExit(f"실패: cli.py {' '.join(args)}")
    return r.stdout


def shift_id_of(path):
    """asu_shift_day_12h.csv 처럼 이름에 근무 ID 가 없는 파일은 첫 줄 timestamp 로 계산한다."""
    with open(path, encoding="utf-8-sig") as f:
        f.readline()
        ts = f.readline().split(",")[0]
    from datetime import datetime
    from collect import shift_id_for  # noqa: E402  — 근무 ID 규칙은 collect 가 정본
    return shift_id_for(datetime.fromisoformat(ts))[0]   # (id, kind, start, end) 중 id


def approve_with_texts(sid, texts):
    """항목별 태그에 맞는 조치 문구로 승인한다. 태그마다 문구가 다르면 그만큼 자연스럽다."""
    with db.connect() as conn:
        draft = db.load_draft(conn, sid)
    items = (draft or {}).get("items") or []
    tags = sorted({it.get("tag") for it in items if it.get("tag")})
    comment = next((texts[t] for t in tags if t in texts), FALLBACK)
    run("approve", sid, "--all", "--comment", comment)
    return comment[:50]


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-root", action="store_true")
    a = ap.parse_args(argv[1:])

    files = sorted((ROOT / "data").glob("asu_shift_*_12h.csv"))
    if not a.only_root:
        files += sorted((ROOT / "data" / "sim").glob("asu_*_12h.csv"))
    if not files:
        raise SystemExit("data/ 에 CSV 가 없습니다.")

    texts = action_texts()
    print(f"조치 문구 {len(texts)}개 로드 · 근무 {len(files)}건")

    # 시간순으로 — 파일명이 아니라 첫 timestamp 기준
    files.sort(key=lambda p: shift_id_of(p))
    for f in (ROOT / "app").glob("engra.db*"):
        f.unlink()
    run("init")

    for i, f in enumerate(files):
        sid = shift_id_of(f)
        run("ingest", str(f))
        out = run("run", sid)
        m = re.search(r"이벤트 (\d+)건 · 초안 (\d+)개", out)
        ev, it = (m.group(1), m.group(2)) if m else ("?", "?")
        if i < len(files) - 1:
            c = approve_with_texts(sid, texts)
            print(f"  {sid}  이벤트 {ev} · 초안 {it} · 확정  「{c}…」")
        else:
            print(f"  {sid}  이벤트 {ev} · 초안 {it} · **승인 대기** (심사위원용)")

    with db.connect() as conn:
        pending = [r["id"] for r in db.list_shifts(conn) if db.load_handover(conn, r["id"]) is None]
    print(f"\n완료. 승인 대기: {pending}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
