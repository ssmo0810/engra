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

**포함률은 「태그를 맞혔는가」에 가까운 값이다.** 구간이 1초라도 겹치면 세기 때문에,
정답지를 60분 옮겨도 100% 가 나온다(적대적 시험 B, 2026-08-29). 인계 목적으로는 그것으로
충분하지만 숫자를 쓸 때는 정의를 밝혀야 하므로 **IoU 를 함께 낸다** — 정답 구간과 검출
구간의 겹침 비율이고, 1.0 이면 시각까지 맞은 것이다. 이 지표가 10번 시나리오의 가짜
탐지(IoU 0.03)를 드러냈다.

DB 는 이미 `ingest → run` 이 끝난 상태여야 한다. 이 도구는 읽기만 한다.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import db  # noqa: E402


# 엔진이 판정하려면 30초 블록이 최소 20개(10분) 있어야 한다 (engine/detectors.py 의 n < 20 가드).
# 그보다 짧은 조각은 원리상 검출 대상이 아니다.
MIN_DETECTABLE_SEC = 600

# 주입이 끝나고 값이 원래대로 돌아오는 구간. 정답지는 「주입 시작~종료」 만 적지만 실제
# 신호는 복귀까지가 한 사건이라, 복귀를 잡은 이벤트가 오탐으로 집계된다. 실측(2026-08-29)
# 에서 `data/sim` 6근무 오탐 7건이 **전부** 주입 종료 0~8분 뒤 같은 태그에서 나왔고,
# 시나리오를 하나도 안 넣은 14근무는 이벤트가 0건이었다 — 엔진 오작동이 아니다.
#
# 오탐 수를 줄여 보이려는 것이 아니라 **성격을 나눠 보여주려는 것**이다. 전체 오탐 수는
# 그대로 내고, 그중 복귀가 몇 건인지를 함께 낸다.
RECOVERY_WINDOW_SEC = 600


def _ts(s):
    return datetime.fromisoformat(s)


def _overlap(a0, a1, b0, b1):
    return not (a1 < b0 or b1 < a0)


# 「관찰 창」 을 적는 검출과 「사건 구간」 을 적는 검출을 나눈다.
#
# 드리프트는 창(60·120·240·720분)을 통째로 놓고 회귀선을 그어 기울기를 보는 방식이라,
# 결과 구간이 **그 창 전체**가 된다. "12시간 내내 이상이었다" 가 아니라 "12시간 창으로
# 봤다" 는 뜻이다. 그래서 103분짜리 정답과 IoU 를 재면 원리상 낮게 나온다 — 엔진이
# 시각을 못 짚은 것이 아니라 자를 잘못 댄 것이다.
#
# 실측 구간 길이(2026-08-29): 드리프트 평균 247분·최대 720분, 나머지는 전부 최대 120분.
# 그래서 드리프트만 추세형으로 본다.
TREND_KINDS = ("드리프트",)


def _iou(a0, a1, b0, b1):
    """두 구간의 겹침 비율. 1.0 이면 시각까지 정확히 맞은 것."""
    inter = (min(a1, b1) - max(a0, b0)).total_seconds()
    if inter <= 0:
        return 0.0
    union = (max(a1, b1) - min(a0, b0)).total_seconds()
    return inter / union if union > 0 else 0.0


def _iou_union(a0, a1, ivs):
    """정답 구간과 매칭 이벤트 **전체의 합집합** 사이의 IoU.

    `_iou` 가 「가장 잘 맞은 이벤트 하나」를 보는 값이라면 이쪽은 검출 묶음 전체가
    얼마나 군더더기 없는지를 본다. 근무 전체에 걸친 드리프트 이벤트가 같이 붙으면
    이 값이 크게 떨어진다 — 그것이 의도한 신호다.
    """
    merged = []
    for b0, b1 in sorted(ivs):
        if merged and b0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b1)
        else:
            merged.append([b0, b1])
    inter = sum(max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds()) for b0, b1 in merged)
    if inter <= 0:
        return 0.0
    cover = sum((b1 - b0).total_seconds() for b0, b1 in merged)
    union = (a1 - a0).total_seconds() + cover - inter
    return inter / union if union > 0 else 0.0


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
    tails = []          # 앞 근무에서 넘어온, 검출 최소 길이보다 짧은 조각
    ious = []           # (시나리오, 최선 IoU, 합집합 IoU, 시작오차분) — 시각까지 맞혔는지
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
                #
                # 그 반대쪽도 같은 이유로 세지 않는다. 앞 근무에서 넘어온 조각(carried_in)이 검출
                # 최소 길이보다 짧으면 **원리상 잡을 수 없다** — 엔진은 30초 블록 20개(10분)가
                # 있어야 판정한다. 실측 예: #1 순도 헌팅이 주간에 17:30:38~18:00 으로 잡혀 일지에
                # 들어갔는데, 야간으로 넘어온 38초 꼬리를 정답지가 별도 주입 1건으로 세어
                # 미탐지로 집계했다(2026-08-29). 이미 앞 근무에서 판정된 건이다.
                too_short = (
                    inj.get("carried_in")
                    and (a1 - a0).total_seconds() < MIN_DETECTABLE_SEC
                )
                status = ("hit" if found
                          else "carried_tail" if too_short
                          else "tracking" if inj.get("continues_next")
                          else "miss")
                if status not in ("tracking", "carried_tail"):
                    total_inj += 1
                    inj_ids.add(inj["scenario_id"])   # 추적 중은 종 커버 분모에도 안 넣는다 — 다음 근무에서 센다 (Codex)
                iou_best = iou_uni = None
                start_err = None
                if found:
                    ivs = [(_ts(e["start_ts"]), _ts(e["end_ts"])) for _, e in found]
                    iou_best = max(_iou(a0, a1, b0, b1) for b0, b1 in ivs)
                    iou_uni = _iou_union(a0, a1, ivs)
                    # 시작 오차는 「가장 잘 맞은」 이벤트 기준 — 근무 전체 드리프트가 아니라 그 구간을 짚은 이벤트를 본다
                    best_i = max(range(len(ivs)), key=lambda k: _iou(a0, a1, ivs[k][0], ivs[k][1]))
                    best = ivs[best_i]
                    start_err = (best[0] - a0).total_seconds() / 60
                    best_kind = found[best_i][1]["kind"]
                    is_trend = best_kind in TREND_KINDS
                    ious.append((inj["scenario_id"], iou_best, iou_uni, start_err, best_kind, is_trend))
                dshift["injected"].append({**inj, "hit": bool(found), "status": status,
                                           "iou": iou_best, "iou_union": iou_uni, "start_err_min": start_err,
                                           "iou_kind": (locals().get("best_kind") if found else None),
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
                elif status == "carried_tail":
                    tails.append((sid, inj["scenario_id"], inj["name"], inj["trigger_tag"],
                                  inj["start"][11:19], inj["end"][11:19],
                                  (a1 - a0).total_seconds()))
                elif status == "tracking":
                    trk += 1
                    tracking.append((sid, inj["scenario_id"], inj["name"], inj["trigger_tag"],
                                     inj["start"][11:16], inj["end"][11:16]))
                else:
                    missed.append((sid, inj["scenario_id"], inj["name"], inj["trigger_tag"],
                                   inj["start"][11:16], inj["end"][11:16]))
            total_hit += hits
            fp = [e for i, e in enumerate(events) if i not in used]
            # 복귀 구간 표시 — 같은 태그의 주입이 끝난 직후에 시작한 이벤트
            ends = []
            for inj2 in sh["injected"]:
                tg = set(inj2["affected_tags"]) | {inj2["trigger_tag"]}
                ends.append((_ts(inj2["end"]), tg, inj2["scenario_id"]))
            for e in fp:
                e_start = _ts(e["start_ts"])
                e["_recovery"] = next(
                    (sid_ for t_end, tg, sid_ in ends
                     if e["tag"] in tg and 0 <= (e_start - t_end).total_seconds() <= RECOVERY_WINDOW_SEC),
                    None)
            dshift["fp"] = [{"id": e.get("id"), "tag": e["tag"], "kind": e["kind"], "start": e["start_ts"][11:16],
                            "end": e["end_ts"][11:16], "evidence": e.get("evidence")} for e in fp]
            total_fp += len(fp)
            false_pos.extend((sid, e["tag"], e["kind"], e["start_ts"][11:16], e["end_ts"][11:16],
                              e.get("_recovery")) for e in fp)
            per_shift_trk[sid] = trk
            per_shift.append((sid, len(events), len(sh["injected"]) - trk, hits, len(fp)))

    return {
        "per_shift": per_shift, "inj": total_inj, "hit": total_hit, "tracking": tracking, "per_shift_trk": per_shift_trk,
        "cov_inj": sorted(inj_ids), "cov_hit": sorted(hit_ids),
        "events": total_events, "fp": total_fp,
        "missed": missed, "false_pos": false_pos, "lead": lead_times, "shifts": len(shifts),
        "tails": tails,
        "iou": ious,
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
    if r.get("iou"):
        def _agg(rows):
            v = sorted(x[1] for x in rows)
            if not v:
                return None
            mid = v[len(v)//2] if len(v) % 2 else (v[len(v)//2 - 1] + v[len(v)//2]) / 2
            return len(v), sum(v)/len(v), mid, sum(1 for x in v if x >= 0.5)

        allr = r["iou"]
        epis = [x for x in allr if not x[5]]        # 국소형 — 사건 구간을 적는 검출
        trnd = [x for x in allr if x[5]]            # 추세형 — 관찰 창을 적는 검출

        a = _agg(allr)
        print(f"시각 일치도 IoU  전체 {a[0]}건 평균 {a[1]:.2f} · 중앙 {a[2]:.2f} · IoU≥0.5 {a[3]}/{a[0]}")
        e = _agg(epis)
        if e:
            print(f"  ├ 국소형 {e[0]}건  평균 {e[1]:.2f} · 중앙 {e[2]:.2f} · IoU≥0.5 {e[3]}/{e[0]}"
                  f"   ← 시각까지 짚는다")
        t = _agg(trnd)
        if t:
            print(f"  └ 추세형 {t[0]}건  평균 {t[1]:.2f} · 중앙 {t[2]:.2f} · IoU≥0.5 {t[3]}/{t[0]}"
                  f"   ← 관찰 창을 적으므로 IoU 로 재는 지표가 아니다")
            for sid, v, _u, err, kind, _ in sorted(trnd, key=lambda x: x[1]):
                print(f"      #{sid:<3} IoU {v:.2f}  시작 오차 {err:+.0f}분  [{kind}]")
        loose = sorted((v, sid, err) for sid, v, _u, err, _k, tr in allr if v < 0.5 and not tr)
        if loose:
            print(f"  국소형인데 시각이 헐거운 건 {len(loose)}건 — 태그는 맞았지만 구간이 어긋남")
            for v, sid, err in loose[:6]:
                print(f"    #{sid:<3} IoU {v:.2f}  시작 오차 {err:+.0f}분")
            if len(loose) > 6:
                print(f"    … 외 {len(loose) - 6}건")
    per = r["fp"] / r["shifts"] if r["shifts"] else 0
    rec = sum(1 for x in r["false_pos"] if len(x) > 5 and x[5] is not None)
    line = f"오탐             {r['fp']}건 / 이벤트 {r['events']}건  (근무당 {per:.1f}건)"
    if rec:
        rest = r["fp"] - rec
        line += (f"\n  └ 그중 {rec}건이 주입 종료 직후 복귀 — "
                 f"복귀를 빼면 {rest}건 (근무당 {rest/r['shifts']:.1f}건)")
    print(line)
    if r["lead"]:
        early = [m for _, m in r["lead"] if m > 0]
        print(f"알람 선행 검출    {len(early)}/{len(r['lead'])} 건이 알람선 도달보다 먼저 잡힘"
              + (f"  (최대 +{max(early):.0f}분)" if early else ""))
    if r.get("tails"):
        print(f"이월 꼬리 {len(r['tails'])}건 (앞 근무에서 넘어온 {MIN_DETECTABLE_SEC//60}분 미만 조각 — "
              f"엔진 최소 판정 길이 미달이라 분모에서 제외, 앞 근무에서 이미 판정됨)")
        for sid, no, name, tag, s_, e_, dur in r["tails"]:
            print(f"  {sid}  #{no} {name:<18} {tag:<8} {s_}~{e_}  {dur:.0f}초")
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
        for row in r["false_pos"][:12]:
            sid, tag, kind, s, e = row[:5]
            note = f"  ← #{row[5]} 복귀" if len(row) > 5 and row[5] is not None else ""
            print(f"  {sid}  {tag:<9}{kind:<12}{s}~{e}{note}")
        if len(r["false_pos"]) > 12:
            print(f"  … 외 {len(r['false_pos']) - 12}건")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
