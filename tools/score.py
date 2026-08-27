#!/usr/bin/env python3
"""정답지 대조 — 검출 이벤트가 주입된 시나리오를 몇 종 잡았는지 센다.

    python3 tools/score.py                          # data/asu_answer_key.json 기준
    python3 tools/score.py data/sim/asu_answer_all.json

`engine/selftest.py` 와 다른 점: 저쪽은 검출기를 만든 사람이 만든 픽스처로 재는
**예비치**고, 이쪽은 임도영님 생성기가 독립적으로 만든 데이터와 정답지로 재는
**제출용 수치**다. 두 숫자를 섞지 않는다.

판정 기준은 `docs/scenarios.md` "전파 경로와 기대 탐지 결과" 를 따른다 —
주입 구간과 시간이 겹치는 이벤트가 **영향 태그(affected_tags) 중 하나**에서
나오면 탐지로 센다. 정답지가 영향 태그를 직접 적어 주므로 전파 추정이 필요 없다.

오탐은 "어느 주입 구간·영향 태그와도 겹치지 않는 이벤트" 다. 동시 발생(overlaps)
구간은 정답지 note 대로 두 시나리오 어느 쪽에 붙어도 탐지로 인정한다.

DB 는 이미 `ingest → run` 이 끝난 상태여야 한다. 이 도구는 읽기만 한다.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import db  # noqa: E402


def _ts(s):
    return datetime.fromisoformat(s)


def _overlap(a0, a1, b0, b1):
    return not (a1 < b0 or b1 < a0)


def score(answer_path, only=None):
    key = json.loads(Path(answer_path).read_text(encoding="utf-8"))
    shifts = key["shift_list"] if "shift_list" in key else [key]   # 묶음 / 근무 하나짜리(새 생성기) 둘 다
    if only is not None:
        shifts = [sh for sh in shifts if sh["shift_id"] in only]
    return score_shifts(shifts)


def score_shifts(shifts):
    """정답지(근무 dict 목록)를 DB 의 이벤트와 대조한다. 화면(app/jobs.py)과 CLI 가 같은 함수를 쓴다."""
    per_shift = []
    hit_ids, inj_ids = set(), set()
    total_inj = total_hit = total_events = total_fp = 0
    lead_times, missed, false_pos, tracking = [], [], [], []
    per_shift_trk = {}   # shift_id -> 추적 중 건수
    detail = {}          # shift_id -> {"injected": [...주입+매칭 이벤트], "fp": [...오탐 이벤트], "overlaps": [...]}

    with db.connect() as conn:
        for sh in shifts:
            sid = sh["shift_id"]
            events = [dict(e) for e in db.load_events(conn, sid)]
            if not events:
                per_shift.append((sid, None, len(sh["injected"]), 0, 0))
                continue
            total_events += len(events)
            used = set()
            hits = 0
            dshift = {"injected": [], "fp": [], "overlaps": sh.get("overlaps") or []}
            detail[sid] = dshift
            trk = 0
            for inj in sh["injected"]:
                a0, a1 = _ts(inj["start"]), _ts(inj["end"])
                tags = set(inj["affected_tags"]) | {inj["trigger_tag"]}
                found = [
                    (i, e) for i, e in enumerate(events)
                    if e["tag"] in tags and e.get("start_ts") and e.get("end_ts")
                    and _overlap(a0, a1, _ts(e["start_ts"]), _ts(e["end_ts"]))
                ]
                # 근무 끝까지 이어지는 주입(continues_next)을 이 근무에서 못 잡았으면 '놓침' 이 아니라 '추적 중' —
                # 다음 근무의 carried_in 항목에서 판정된다 (경모님 2026-08-27: "실제로 검출 못 해도 놓침이라 뜨면 오답").
                status = "hit" if found else ("tracking" if inj.get("continues_next") else "miss")
                if status != "tracking":
                    total_inj += 1
                    inj_ids.add(inj["scenario_id"])   # 추적 중은 종 커버 분모에도 안 넣는다 — 다음 근무에서 센다 (Codex)
                dshift["injected"].append({**inj, "hit": bool(found), "status": status,
                                           "matched": [{"id": e.get("id"), "tag": e["tag"], "kind": e["kind"],
                                                        "start": e["start_ts"][11:16], "end": e["end_ts"][11:16],
                                                        "evidence": e.get("evidence")} for _, e in found]})
                if found:
                    hits += 1
                    hit_ids.add(inj["scenario_id"])
                    used.update(i for i, _ in found)
                    if str(inj.get("dcs_alarm", "")).startswith("발생"):
                        first = min(_ts(e["start_ts"]) for _, e in found)
                        alarm_hits = [e for _, e in found if "초과" in e["kind"] or "미달" in e["kind"]]
                        if alarm_hits:
                            alarm_at = min(_ts(e["start_ts"]) for e in alarm_hits)
                            lead_times.append((inj["scenario_id"], (alarm_at - first).total_seconds() / 60))
                elif status == "tracking":
                    trk += 1
                    tracking.append((sid, inj["scenario_id"], inj["name"], inj["trigger_tag"],
                                     inj["start"][11:16], inj["end"][11:16]))
                else:
                    missed.append((sid, inj["scenario_id"], inj["name"], inj["trigger_tag"],
                                   inj["start"][11:16], inj["end"][11:16]))
            total_hit += hits
            fp = [e for i, e in enumerate(events) if i not in used]
            dshift["fp"] = [{"id": e.get("id"), "tag": e["tag"], "kind": e["kind"], "start": e["start_ts"][11:16],
                            "end": e["end_ts"][11:16], "evidence": e.get("evidence")} for e in fp]
            total_fp += len(fp)
            false_pos.extend((sid, e["tag"], e["kind"], e["start_ts"][11:16], e["end_ts"][11:16]) for e in fp)
            per_shift_trk[sid] = trk
            per_shift.append((sid, len(events), len(sh["injected"]) - trk, hits, len(fp)))

    return {
        "per_shift": per_shift, "inj": total_inj, "hit": total_hit, "tracking": tracking, "per_shift_trk": per_shift_trk,
        "cov_inj": sorted(inj_ids), "cov_hit": sorted(hit_ids),
        "events": total_events, "fp": total_fp,
        "missed": missed, "false_pos": false_pos, "lead": lead_times, "shifts": len(shifts),
        "detail": detail, "shift_list": shifts,
    }


def main(argv):
    path = argv[1] if len(argv) > 1 else str(ROOT / "data" / "asu_answer_key.json")
    r = score(path)
    print(f"정답지: {path}")
    print("-" * 78)
    print(f"{'근무':<20}{'이벤트':>7}{'주입':>6}{'탐지':>6}{'오탐':>6}")
    for sid, ev, inj, hit, fp in r["per_shift"]:
        if ev is None:
            print(f"{sid:<20}{'—':>7}{inj:>6}{'—':>6}{'—':>6}   ← DB 에 없음. ingest → run 먼저")
        else:
            print(f"{sid:<20}{ev:>7}{inj:>6}{hit:>6}{fp:>6}")
    print("-" * 78)
    if not r["events"]:
        print("검출 이벤트가 없습니다. 근무별로 ingest → run 을 먼저 실행하세요.")
        return 1
    pct = 100 * r["hit"] / r["inj"] if r["inj"] else 0
    print(f"주입 건 포함률   {r['hit']}/{r['inj']}  ({pct:.0f}%)")
    cov_n, cov_d = len(r["cov_hit"]), len(r["cov_inj"])
    miss_ids = sorted(set(r["cov_inj"]) - set(r["cov_hit"]))
    print(f"시나리오 종 커버  {cov_n}/{cov_d} 종" + (f"  · 놓친 종: {miss_ids}" if miss_ids else ""))
    per = r["fp"] / r["shifts"] if r["shifts"] else 0
    print(f"오탐             {r['fp']}건 / 이벤트 {r['events']}건  (근무당 {per:.1f}건)")
    if r["lead"]:
        early = [m for _, m in r["lead"] if m > 0]
        print(f"알람 선행 검출    {len(early)}/{len(r['lead'])} 건이 알람선 도달보다 먼저 잡힘"
              + (f"  (최대 +{max(early):.0f}분)" if early else ""))
    if r.get("tracking"):
        print(f"추적 중 {len(r['tracking'])}건 (근무 끝까지 이어지는 주입 — 다음 근무에서 판정, 포함률 분모에서 제외)")
        for sid, no, name, tag, s_, e_ in r["tracking"]:
            print(f"  {sid}  #{no} {name:<18} {tag:<8} {s_}~{e_}")
    if r["missed"]:
        print(f"\n놓친 주입 {len(r['missed'])}건")
        for sid, n, name, tag, s, e in r["missed"]:
            print(f"  {sid}  #{n:<3}{name:<22}{tag:<9}{s}~{e}")
    if r["false_pos"]:
        print(f"\n오탐 {len(r['false_pos'])}건 (어느 주입 구간·영향 태그와도 안 겹침)")
        for sid, tag, kind, s, e in r["false_pos"][:12]:
            print(f"  {sid}  {tag:<9}{kind:<12}{s}~{e}")
        if len(r["false_pos"]) > 12:
            print(f"  … 외 {len(r['false_pos']) - 12}건")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
