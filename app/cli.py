#!/usr/bin/env python3
"""ENGRA 명령줄.

    python3 app/cli.py init                      저장소 생성
    python3 app/cli.py sample                    스모크 테스트용 CSV 생성
    python3 app/cli.py ingest <csv>              데이터 적재
    python3 app/cli.py shifts                    근무 구간 목록
    python3 app/cli.py run <근무id>              요약·검출·초안 생성
    python3 app/cli.py draft <근무id>            초안 보기
    python3 app/cli.py approve <근무id> [--items 1,3] [--all]
    python3 app/cli.py handover <근무id>         확정 일지 보기
    python3 app/cli.py serve                     웹 화면
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approve as approve_mod  # noqa: E402
import collect  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import pipeline  # noqa: E402
import ports  # noqa: E402
from config import APP_DIR, DB_PATH, HOST, PORT  # noqa: E402


def cmd_init(_):
    path = db.init()
    print(f"저장소 준비 완료: {path}")
    print(f"검출 엔진: {ports.engine_source()}")
    print(f"AI: {llm.status()}")


def cmd_sample(args):
    import sample_data  # noqa: PLC0415
    from datetime import date as _date  # noqa: PLC0415
    out = Path(args.out) if args.out else APP_DIR / "sample_shift.csv"
    d = _date.fromisoformat(args.date) if args.date else _date.today()
    path, rows = sample_data.generate(out, kind=args.kind, date=d, minutes=args.minutes)
    rel = path.relative_to(APP_DIR.parent) if APP_DIR.parent in path.parents else path
    print(f"스모크 테스트용 CSV 생성: {path} ({rows:,}행)")
    # 근무 ID 는 날짜에서 나온다. 안 알려주면 다음 명령을 몰라 헤맨다.
    print(f"다음: python3 app/cli.py ingest {rel}")
    print(f"그다음: python3 app/cli.py run {d}-{args.kind}")
    print("실제 검증은 임도영님 생성기의 시나리오 CSV + 정답지로 합니다.")


def cmd_ingest(args):
    db.init()
    bad = collect.BadRows(max_ratio=collect.MAX_BAD_RATIO) if args.skip_bad_rows else None
    source = collect.source_for(args.csv, bad)   # 파일 경로 또는 URL (#12)
    counts = collect.ingest(source)
    if bad and bad.count:
        # 건너뛴 것을 숨기지 않는다. 몇 행을 왜 버렸는지가 검증 기록이다.
        print(f"⚠ 깨진 행 {bad.count:,}개 건너뜀 ({bad.count/bad.total:.2%}). 예:")
        for s_ in bad.samples:
            print(f"    {s_}")
    if not counts:
        print("적재된 데이터가 없습니다.")
        return
    with db.connect() as conn:
        for sid, n in sorted(counts.items()):
            cov = collect.coverage(conn, sid)
            print(f"{sid}: {n:,}점 적재 · 태그 {cov['tags']}개 · 구간 충족률 {cov['ratio']:.1%}")
    print(f"다음: python3 app/cli.py run {sorted(counts)[0]}")


def cmd_shifts(_):
    with db.connect() as conn:
        rows = db.list_shifts(conn)
    if not rows:
        print("등록된 근무 구간이 없습니다. ingest 를 먼저 실행하세요.")
        return
    print(f"{'근무 ID':<22}{'구간':<38}{'이벤트':>6}{'상태':>12}")
    for r in rows:
        span = f"{r['window_start'][5:16]} ~ {r['window_end'][5:16]}"
        status = r["draft_status"] or "-"
        if status == "confirmed":
            status = f"확정({r['adopted_count']})"
        print(f"{r['id']:<22}{span:<38}{r['event_count']:>6}{status:>12}")


def prepare_latest(sid, redo):
    """타이머 실행(--latest)의 사전 판정. 초안·확정이 있으면 사용자의 검토를 덮지 않는다. 원본이 없거나 앞이
    잘렸으면(3일 회전) 업로드된 파일이 있을 때만 거기서 다시 적재하고, 없으면 할 일이 없다. True = 실행 진행."""
    with db.connect() as conn:
        if not redo and (db.load_draft(conn, sid) or db.load_handover(conn, sid)):
            print(f"  {sid} 는 이미 초안/확정이 있습니다 — 건너뜀 (다시 만들려면 --redo)"); return False
        complete = db.raw_complete(conn, sid)
    if complete:
        return True
    import jobs   # 서버와 같은 정의 — shift.source 로 uploads/ 의 원본 파일을 찾는다
    csv = jobs.csv_for(sid)
    if csv is None:
        print(f"  {sid} 는 적재된 원본이 없습니다(또는 보관 기간이 지나 정리됨) — 올린 파일도 없어 할 일 없음"); return False
    with db.connect() as conn:
        n = db.forget_raw(conn, sid)
    print(f"  {sid} 원본 {n:,}점 정리됨 — 올린 파일 {csv.name} 에서 다시 적재")
    collect.ingest(collect.source_for(str(csv), collect.BadRows(max_ratio=collect.MAX_BAD_RATIO)))
    return True


def cmd_run(args):
    if args.latest:
        # 교대 시각(06:00/18:00) 직후에 돈다고 가정하고 "지금 끝난 근무" 를 고른다.
        # now-1초가 속하는 근무가 그것이다 — 18:00:00 정각은 이미 야간조 시작이라 1초를 뺀다.
        from datetime import datetime, timedelta
        sid, _kind, _s, _e = collect.shift_id_for(datetime.now() - timedelta(seconds=1))
        args.shift_id = sid
        print(f"--latest → {sid}")
        if not prepare_latest(sid, args.redo):
            return
    if not args.shift_id:
        raise SystemExit("근무 ID 를 주거나 --latest 를 쓰세요.")
    print(f"[{args.shift_id}] 실행 — 엔진: {ports.engine_source()}")
    r = pipeline.run(args.shift_id, redo=args.redo)
    if r["baseline_provisional"]:
        print("  ⚠ 기준선이 잠정입니다. 근무가 쌓이면 정확해집니다.")
    print(f"완료: 이벤트 {r['events']}건 · 초안 {r['items']}개 항목")


def cmd_draft(args):
    with db.connect() as conn:
        d = db.load_draft(conn, args.shift_id)
    if d is None:
        print(f"{args.shift_id} 의 초안이 없습니다. run 을 먼저 실행하세요.")
        return
    print(f"[{args.shift_id}] 초안 — {d['status']} · 생성 {d['generator']} · {d['generated_at']}")
    if not d["items"]:
        print("  감지된 항목이 없습니다.")
    for it in d["items"]:
        mark = {1: "채택", 0: "제외"}.get(it["adopted"], "미결정")
        print(f"\n  #{it['id']} [{it['severity'] or '-'}] {it['title']}   ({mark})")
        print(f"      {it['body']}")
        if it["evidence"]:
            print(f"      근거: {it['evidence']}")
        if it["suggested_action"]:
            print(f"      과거 조치: {it['suggested_action']}")
        # 화면에서 최하단으로 내려가는 항목은 명령줄에서도 그 사실이 보여야 한다 (#30)
        pe = it.get("prev_excluded")
        if pe:
            rep = f" · {pe['times']}근무 연속" if (pe.get("times") or 1) > 1 else ""
            print(f"      이전 제외: {pe.get('shift_id') or '-'} "
                  f"{(pe.get('by') or '앞 근무자')} 님{rep} — 최하단 「이전에 제외한 것」")


def cmd_approve(args):
    with db.connect() as conn:
        d = db.load_draft(conn, args.shift_id)
    if d is None:
        print(f"{args.shift_id} 의 초안이 없습니다.")
        return

    known = {it["id"] for it in d["items"]}
    if args.all:
        chosen = set(known)
    elif args.items:
        try:
            chosen = {int(x) for x in args.items.split(",") if x.strip()}
        except ValueError:
            print(f"--items 는 쉼표로 구분한 항목 번호입니다. 받은 것: {args.items!r}")
            return
        # 없는 번호를 그냥 두면 아무것도 채택되지 않은 빈 일지가 조용히 확정된다.
        # 오타 하나로 인수인계가 통째로 비는 것이 가장 나쁜 결과라 여기서 막는다.
        unknown = sorted(chosen - known)
        if unknown:
            print(f"이 초안에 없는 항목입니다: {unknown}")
            print(f"이 근무의 항목 번호: {sorted(known)}   (python3 app/cli.py draft {args.shift_id} 로 확인)")
            return
    else:
        print("--all 또는 --items 1,3 으로 채택할 항목을 지정하세요.")
        return

    decisions = {
        it["id"]: {"adopted": it["id"] in chosen, "comment": args.comment}
        for it in d["items"]
    }
    r = approve_mod.decide(args.shift_id, decisions, confirmed_by=args.by)
    if r.get("prev_round"):
        print(f"※ 이미 확정된 근무입니다. 이전 확정본을 이력 {r['prev_round']}회차로 남기고 다시 확정합니다.")
    print(f"확정: 채택 {r['adopted']}건 · 제외 {r['excluded']}건\n")
    print(r["body"])


def cmd_handover(args):
    with db.connect() as conn:
        h = db.load_handover(conn, args.shift_id)
    if h is None:
        print(f"{args.shift_id} 의 확정 일지가 없습니다.")
        return
    print(f"확정자 {h['confirmed_by']} · {h['confirmed_at']} · "
          f"채택 {h['adopted_count']} / 제외 {h['excluded_count']}\n")
    print(h["body"])


def cmd_serve(args):
    import server  # noqa: PLC0415
    server.serve(args.host, args.port)


def build_parser():
    p = argparse.ArgumentParser(prog="engra", description="ENGRA 인수인계 초안 시스템")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="저장소 생성").set_defaults(fn=cmd_init)

    s = sub.add_parser("sample", help="스모크 테스트용 CSV 생성")
    s.add_argument("--out")
    s.add_argument("--kind", choices=["day", "night"], default="day")
    s.add_argument("--minutes", type=int, default=60)
    s.add_argument("--date", help="근무 날짜 YYYY-MM-DD (기본: 오늘)")
    s.set_defaults(fn=cmd_sample)

    s = sub.add_parser("ingest", help="CSV 적재")
    s.add_argument("csv", help="CSV 파일 경로 또는 http(s) URL")
    s.add_argument("--skip-bad-rows", action="store_true",
                   help="형식이 깨진 행을 건너뛴다 (건수·예시를 보고, 1%% 초과면 거부). 기본은 첫 오류에서 멈춤")
    s.set_defaults(fn=cmd_ingest)

    sub.add_parser("shifts", help="근무 구간 목록").set_defaults(fn=cmd_shifts)

    s = sub.add_parser("run", help="요약·검출·초안 생성")
    s.add_argument("shift_id", nargs="?", help="근무 ID (YYYY-MM-DD-day|night). --latest 면 생략")
    s.add_argument("--latest", action="store_true",
                   help="지금 막 끝난 근무를 고른다. 스케줄러(교대 시각)에서 쓴다 — 06:00 에 돌면 전날 야간, 18:00 에 돌면 당일 주간")
    s.add_argument("--redo", action="store_true", help="확정된 일지를 지우고 다시 만든다")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("draft", help="초안 보기")
    s.add_argument("shift_id")
    s.set_defaults(fn=cmd_draft)

    s = sub.add_parser("approve", help="승인 처리")
    s.add_argument("shift_id")
    s.add_argument("--items", help="채택할 항목 번호, 쉼표 구분")
    s.add_argument("--all", action="store_true", help="전부 채택")
    s.add_argument("--comment")
    s.add_argument("--by", default="근무자")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("handover", help="확정 일지 보기")
    s.add_argument("shift_id")
    s.set_defaults(fn=cmd_handover)

    s = sub.add_parser("serve", help="웹 화면")
    s.add_argument("--host", default=HOST)
    s.add_argument("--port", type=int, default=PORT)
    s.set_defaults(fn=cmd_serve)

    return p


def main():
    args = build_parser().parse_args()
    if args.cmd != "init" and not DB_PATH.exists():
        print("저장소가 없습니다. 먼저 실행하세요:  python3 app/cli.py init")
        sys.exit(1)
    try:
        args.fn(args)
    except (ValueError, KeyError) as exc:
        # 사용법 문제는 한 줄로 알린다. 그 외 오류는 추적을 그대로 보여준다.
        print(f"오류: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
