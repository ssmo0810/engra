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

**라이브 DB 를 직접 지우지 않는다.** 별도 파일(seed_build.db)에 전부 만든 뒤 마지막에 한 번
교체한다. 서비스는 그동안 계속 켜져 있다 — 2026-08-27 시드하려고 서비스를 30분 넘게 내렸다가
공개 URL 이 502 였다. 평가 기준이 "URL 이 안 열리면 구현 완성도를 확인할 수 없다" 다.
"""
import os
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
    # 시드는 조치가 끝난 과거 일지다. 진행중으로 넣으면 뒤의 모든 근무 초안에 이월 묶음이 쌓인다.
    run("approve", sid, "--all", "--status", "완료", "--comment", comment)
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

    # 빌드는 별도 파일에. cli.py 가 ENGRA_DB 를 읽어 그 파일을 쓴다. 라이브(engra.db)는 건드리지 않는다.
    build = ROOT / "app" / "seed_build.db"
    for f in (ROOT / "app").glob("seed_build.db*"):
        f.unlink()
    os.environ["ENGRA_DB"] = str(build)
    db.DB_PATH = build          # 이 프로세스의 db 모듈도 같은 파일을 보게
    import config
    config.DB_PATH = build
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

    conn = db.connect()
    try:
        pending = [r["id"] for r in db.list_shifts(conn) if db.load_handover(conn, r["id"]) is None]
    finally:
        conn.close()
    print(f"\n빌드 완료. 승인 대기: {pending}")

    # 교체 — 기준선(seed.db)과 라이브(engra.db)를 한 번에. 서비스는 다음 요청부터 새 파일을 읽는다.
    live, seed = ROOT / "app" / "engra.db", ROOT / "app" / "seed.db"
    swap_in(build, live, seed)
    print(f"교체 완료 → {live.name} (기준선 {seed.name} 갱신). 서비스 재시작 불필요 — 다음 요청부터 반영.")
    return 0


def swap_in(build, live, seed):
    """빌드 DB 를 라이브(engra.db)와 기준선(seed.db)으로 바꾼다.

    교체 전에 WAL 에 남은 쓰기를 본 파일로 옮긴다 — 본 파일만 복사·교체하고 -wal 을 지우면 마지막 근무(승인 대기 초안)가
    통째로 사라졌다(2026-09-14 실측: 로그엔 「초안 10 · 승인 대기」 인데 engra.db 엔 그 근무의 초안·이벤트가 없었다). 다 못 옮기면 교체하지 않는다.
    """
    import shutil
    import sqlite3
    ck = sqlite3.connect(build)
    try:
        busy, frames, moved = ck.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        ck.close()
    if busy or frames != moved:
        raise SystemExit(f"빌드 DB 의 WAL 을 본 파일로 다 옮기지 못했다(busy={busy}, {moved}/{frames}) — 교체하지 않는다.")
    # 기준선은 임시 파일에 복사한 뒤 한 번에 바꾼다. 그 자리에 덮어쓰면 남아 있던 곁파일(-wal · -shm)이 새 파일에 얹혀,
    # 멈춘 프로세스가 뒤늦게 닫히며 옛 행을 새 기준선에 써 넣는다(codex 지적). 복사 도중에 읽으면 반쪽 파일을 보는 것도 함께 막는다.
    tmp = seed.with_name(seed.name + ".new")
    shutil.copyfile(build, tmp)
    for suf in ("-wal", "-shm"):
        q = seed.with_name(seed.name + suf)
        if q.exists():
            q.unlink()
    os.replace(tmp, seed)
    for suf in ("-wal", "-shm"):
        p = live.with_name(live.name + suf)
        if p.exists():
            p.unlink()
    os.replace(build, live)     # 원자적 교체
    for suf in ("-wal", "-shm"):            # 빌드 부산물 정리
        p = build.with_name(build.name + suf)
        if p.exists():
            p.unlink()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
