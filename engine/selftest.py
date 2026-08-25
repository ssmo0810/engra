"""자체 점검 — 시나리오 20종을 넣고 몇 종을 잡는지 센다.

    python3 engine/selftest.py              # 20종 전부
    python3 engine/selftest.py 1 3 11       # 고른 것만
    python3 engine/selftest.py --csv out/   # 회차별 CSV 를 남긴다 (app 통합 확인용)

**여기서 나오는 숫자는 예비치다.** 제출용 포함률은 임도영님 생성기가 만든
`asu_shift_*.csv` + `asu_answer_key.json` 로 재야 한다(Issue #5). 이 점검은
문턱값을 바꿀 때마다 **즉시 되돌려 보기 위한** 개발 도구다.

판정 기준은 `docs/scenarios.md` "전파 경로와 기대 탐지 결과" 를 따른다 —
주입 구간과 겹치는 이벤트가 **영향 태그 중 하나에서** 나오면 탐지로 센다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import _fixture, api                      # noqa: E402
from engine.core import epoch, hhmm, spec_of          # noqa: E402


def _overlap(a0, a1, b0, b1):
    return not (a1 < b0 or b1 < a0)


def _with_propagation(tags):
    """주입 태그 + 태그 마스터 links 로 1홉 전파되는 태그.

    `docs/scenarios.md` 의 '전파' 열이 기대하는 범위다. 예를 들어 시나리오 1은
    AI-707 만 주입하지만 links 에 `AI-803:0.80:180` 이 있어 LOX 순도가 실제로 함께
    흔들린다. 그것을 오탐으로 세면 **전파를 잡아낸 것을 벌주는 셈**이 된다.
    """
    out = set(tags)
    for t in list(tags):
        for other, _g, _l in spec_of(t).get("links", ()):
            out.add(other)
    return out


def _alarm_time(points, spec):
    """원본에서 알람 한계선을 처음 넘은 시각. 없으면 None."""
    first = None
    for key, above in (("HH", True), ("H", True), ("LL", False), ("L", False)):
        limit = spec.get(key)
        if limit is None:
            continue
        for ts, v in points:
            if (v > limit) if above else (v < limit):
                t = epoch(ts)
                if first is None or t < first:
                    first = t
                break
    return first


def run_round(placements, seed, start, csv_dir=None, index=0):
    series, answer = _fixture.generate(placements, seed=seed, start=start)
    if csv_dir:
        path = Path(csv_dir) / f"fixture_{index:02d}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        _fixture.to_csv(series, path)

    summaries = api.summarize(series)
    baselines = api.build_baseline({t: [s] for t, s in summaries.items()})
    events = api.detect(series, baselines)

    rows = []
    hit_ids = set()
    for inj in answer["injected"]:
        b0, b1 = epoch(inj["start"]), epoch(inj["end"])
        tags = _with_propagation(inj["affected_tags"])
        found = [e for e in events
                 if e["tag"] in tags
                 and _overlap(epoch(e["start_ts"]), epoch(e["end_ts"]), b0, b1)]
        # 알람보다 먼저 잡았는가 — 알람이 울리는 종에서만 의미가 있다
        at = _alarm_time(series.get(inj["trigger_tag"], []), spec_of(inj["trigger_tag"]))
        early = None
        if at is not None and found:
            first = min(epoch(e["start_ts"]) for e in found)
            early = round((at - first) / 60)
        if found:
            hit_ids.add(inj["scenario_id"])
        rows.append({
            "id": inj["scenario_id"], "name": inj["name"], "tag": inj["trigger_tag"],
            "window": f'{hhmm(inj["start"])}~{hhmm(inj["end"])}',
            "hit": bool(found),
            "kinds": sorted({e["kind"] for e in found}),
            "tags_hit": sorted({e["tag"] for e in found}),
            "alarm": inj["dcs_alarm"],
            "lead_min": early,
        })

    # 오탐 — 어느 주입 구간과도 겹치지 않는 **사건**.
    # 이벤트가 아니라 group 단위로 센다. 초안에 한 줄로 나가는 단위가 group 이고,
    # 한 사건이 검출기 네 개에 걸렸다고 오탐 네 건으로 세면 실제보다 부풀려진다.
    # 사건이 끝나고 값이 되돌아오는 구간이 레벨시프트로 다시 잡히는 것도
    # 같은 group 에 들어가므로 여기서 함께 정리된다.
    spans = [(epoch(i["start"]), epoch(i["end"]), _with_propagation(i["affected_tags"]))
             for i in answer["injected"]]
    groups = {}
    for e in events:
        groups.setdefault((e.get("metrics") or {}).get("group", id(e)), []).append(e)
    false_pos = []
    for members in groups.values():
        if any(e["tag"] in tg and
               _overlap(epoch(e["start_ts"]), epoch(e["end_ts"]), s, t)
               for e in members for s, t, tg in spans):
            continue
        false_pos.append(max(members, key=lambda e: e["score"]))
    return rows, events, false_pos, series, answer


def main(argv):
    csv_dir = None
    if "--csv" in argv:
        k = argv.index("--csv")
        csv_dir = argv[k + 1] if len(argv) > k + 1 else "engine/_out"
        argv = argv[:k] + argv[k + 2:]
    ids = [int(a) for a in argv if a.isdigit()] or None

    rounds = _fixture.pack(ids)
    all_rows, all_fp, total_events = [], [], 0
    for k, placements in enumerate(rounds):
        start = f"2026-08-{22 + k:02d}T06:00:00"
        rows, events, fp, _s, _a = run_round(placements, seed=1 + k, start=start,
                                             csv_dir=csv_dir, index=k)
        all_rows += rows
        all_fp += fp
        total_events += len(events)
        print(f"[회차 {k + 1}/{len(rounds)}] {start[:10]} · 시나리오 "
              f"{len(rows)}종 · 이벤트 {len(events)}건")

    all_rows.sort(key=lambda r: r["id"])
    print()
    print(f"{'#':>3} {'시나리오':<22} {'태그':<9} {'구간':<12} {'판정':<5} "
          f"{'검출 유형':<22} {'알람 대비':<9}")
    print("-" * 92)
    for r in all_rows:
        mark = "잡힘" if r["hit"] else "놓침"
        kinds = ",".join(r["kinds"])[:21] or "-"
        lead = f"+{r['lead_min']}분 먼저" if r["lead_min"] is not None else \
               ("-" if r["alarm"] == "미발생" else "동시/이후")
        print(f'{r["id"]:>3} {r["name"]:<22} {r["tag"]:<9} {r["window"]:<12} '
              f'{mark:<5} {kinds:<22} {lead:<9}')

    hit = sum(1 for r in all_rows if r["hit"])
    silent = [r for r in all_rows if r["alarm"] == "미발생"]
    silent_hit = sum(1 for r in silent if r["hit"])
    print("-" * 92)
    print(f"포함률   {hit}/{len(all_rows)}  ({hit / len(all_rows) * 100:.0f}%)")
    print(f"알람 미발생 종  {silent_hit}/{len(silent)}  "
          f"← 임시 엔진은 구조적으로 0/{len(silent)}")
    print(f"오탐     {len(all_fp)}건 / 이벤트 {total_events}건 "
          f"(회차 {len(rounds)}개, 근무당 {len(all_fp) / len(rounds):.1f}건)")
    print()
    print("※ 예비치입니다. 제출용 포함률은 임도영님 정답지로 측정합니다 (Issue #5).")

    if all_fp:
        print("\n오탐 상위 10건")
        for e in sorted(all_fp, key=lambda x: -x["score"])[:10]:
            print(f'  {e["tag"]:<9} {e["kind"]:<8} {hhmm(e["start_ts"])}~'
                  f'{hhmm(e["end_ts"])}  {e["evidence"][:64]}')
    return 0 if hit == len(all_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
