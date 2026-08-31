#!/usr/bin/env python3
"""제출용 「04. 제작 과정」 문서를 자동 생성한다.

본선 배점에서 04 폴더는 15점(문제해결 과정의 집요함)이고, 평가 기준에 이렇게 적혀 있다.

    "결과물만으로는 드러나지 않는 부분이라 별도로 기록해 주셔야 확인할 수 있습니다."

그래서 따로 일지를 쓰는 게 아니라, **일하면서 이미 남기는 것**을 모아 문서로 만든다.

    커밋 메시지  → 개발 일지 · 난관과 극복 사례
    GitHub Issue → 회의록(결정 기록)
    Co-Authored-By → 사용 도구 내역 (FAQ Q5 가 제작 과정에 남기라고 요구)

즉 **좋은 커밋 메시지를 쓰는 것이 곧 개발 일지를 쓰는 것**이다. 마감에 몰아서
되살릴 수 없는 자료라, 진행하면서 자동으로 쌓이게 만드는 것이 요점이다.

    python3 tools/journal.py            # docs/제작과정/ 에 생성
    python3 tools/journal.py --out DIR  # 다른 곳에 생성 (Drive 폴더 등)
"""
import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEP = "\x1e"  # 커밋 구분자 (메시지에 나올 일 없는 문자)
FIELD = "\x1f"

# 난관으로 분류할 신호. 커밋 메시지에 이 표현이 있으면 문제를 만나 넘은 기록이다.
TROUBLE = re.compile(
    r"발견|결함|버그|고쳤|고침|틀렸|틀림|오류|실패|깨졌|깨짐|위반|재현|반증|"
    r"놓쳤|누락|막혔|충돌|되돌|원복|복구"
)


def sh(*args):
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"실패: {' '.join(args)}\n{r.stderr.strip()}")
    return r.stdout


def commits():
    fmt = FIELD.join(["%H", "%ad", "%an", "%s", "%b"]) + SEP
    raw = sh("git", "log", "--reverse", "--date=format:%Y-%m-%d", f"--pretty=format:{fmt}")
    out = []
    for chunk in raw.split(SEP):
        if not chunk.strip():
            continue
        h, date, author, subject, body = (chunk.lstrip("\n").split(FIELD) + [""] * 5)[:5]
        tools = re.findall(r"Co-Authored-By:\s*([^<\n]+)", body)
        body = re.sub(r"Co-Authored-By:.*", "", body).strip()
        stat = sh("git", "show", "--stat", "--format=", h).strip().splitlines()
        out.append({
            "hash": h[:7], "date": date, "author": author.strip(),
            "subject": subject.strip(), "body": body,
            "tools": [t.strip() for t in tools],
            "files": len([s for s in stat if "|" in s]),
        })
    return out


def issues():
    """GitHub Issue = 우리 팀의 회의록. 결정과 근거가 여기 남는다."""
    try:
        raw = sh("gh", "issue", "list", "--state", "all", "--limit", "200", "--json",
                 "number,title,body,state,createdAt,closedAt,author,assignees,comments,labels")
    except SystemExit:
        print("  ⚠ gh 로 이슈를 읽지 못했습니다. 회의록은 건너뜁니다.", file=sys.stderr)
        return []
    data = json.loads(raw)
    return sorted(data, key=lambda i: i["number"])


def h(level, text):
    return f"{'#' * level} {text}\n"


# ── 개발 일지 ─────────────────────────────────────────────────────────

def dev_journal(cs):
    by_date = defaultdict(list)
    for c in cs:
        by_date[c["date"]].append(c)

    L = [h(1, "개발 일지"),
         "커밋 기록에서 자동 생성했습니다. 각 항목은 실제 변경 한 건이며, "
         "무엇을 왜 바꿨는지와 무엇으로 확인했는지를 그때 남긴 내용 그대로입니다.\n",
         f"기간 {cs[0]['date']} ~ {cs[-1]['date']} · 총 {len(cs)}건\n"]

    authors = defaultdict(int)
    for c in cs:
        authors[c["author"]] += 1
    L.append(h(2, "담당 분포"))
    L.append("| 작성자 | 커밋 |\n| --- | --- |\n")
    for a, n in sorted(authors.items(), key=lambda x: -x[1]):
        L.append(f"| {a} | {n}건 |\n")
    L.append("\n")

    for date in sorted(by_date):
        L.append(h(2, date))
        for c in by_date[date]:
            L.append(f"**{c['subject']}** — {c['author']} · `{c['hash']}` · 파일 {c['files']}개\n\n")
            if c["body"]:
                for line in c["body"].splitlines():
                    L.append(f"> {line}\n" if line.strip() else ">\n")
                L.append("\n")
    return "".join(L)


# ── 회의록 (결정 기록) ────────────────────────────────────────────────

def minutes(iss):
    L = [h(1, "회의록 · 결정 기록"),
         "팀원이 서로 다른 시각에 일하는 구조라, 회의와 결정을 GitHub Issue 로 남겼습니다.\n"
         "여기 있는 내용은 그 원본을 자동으로 모은 것이며, 누가 무엇을 요청하고 어떻게 "
         "정해졌는지가 시각과 함께 남아 있습니다.\n",
         f"총 {len(iss)}건 (열림 {sum(1 for i in iss if i['state'] == 'OPEN')} · "
         f"닫힘 {sum(1 for i in iss if i['state'] != 'OPEN')})\n"]

    for i in iss:
        state = "처리 완료" if i["state"] != "OPEN" else "진행 중"
        who = ", ".join(a["login"] for a in i["assignees"]) or "미지정"
        L.append(h(2, f"#{i['number']} {i['title']}"))
        L.append(f"제기 {i['author']['login']} · 담당 {who} · {i['createdAt'][:10]} · **{state}**\n\n")
        if i["body"]:
            body = i["body"].strip()
            L.append("\n".join("> " + ln if ln.strip() else ">" for ln in body.splitlines()) + "\n\n")
        if i["comments"]:
            L.append("**논의와 결론**\n\n")
            for cm in i["comments"]:
                when = cm.get("createdAt", "")[:10]
                L.append(f"- *{cm['author']['login']}* ({when}) — "
                         f"{' '.join(cm['body'].split())[:400]}\n")
            L.append("\n")
        if i["closedAt"]:
            L.append(f"→ {i['closedAt'][:10]} 처리 완료\n\n")
    return "".join(L)


# ── 난관과 극복 ───────────────────────────────────────────────────────

def troubles(cs):
    """난관과 극복 — 사람이 쓴 기록을 앞에, 커밋에서 자동 수집한 목록은 부록으로.
    자동 수집 100여 건을 앞에 두면 심사위원이 5요소가 갖춰진 기록에 닿기 전에 지친다(경모님 2026-08-30)."""
    hits = [c for c in cs if TROUBLE.search(c["subject"] + c["body"])]
    L = [h(1, "난관과 극복 사례"),
         "앞부분은 담당자가 직접 정리한 기록(무엇이 예상과 달랐나 → 시도 순서 → 각 결과 → 남긴 것과 포기한 것 → 새로 알게 된 것)이고, "
         "부록은 커밋 메시지에서 문제·수정 서술이 있는 건을 자동으로 뽑은 원문입니다.\n\n"]
    eng = ROOT / "engine" / "난관기록.md"
    if eng.exists():
        L.append(h(2, "검출 엔진 — 정기영 (engine/난관기록.md)"))
        L.append(eng.read_text(encoding="utf-8").strip() + "\n\n")
    try:
        raw = sh("gh", "issue", "view", "15", "--json", "body,title", "-q", ".body")
        if raw.strip():
            L.append(h(2, "데이터·시나리오 — 임도영 (이슈 #15)"))
            L.append(raw.strip() + "\n\n")
    except Exception as exc:   # gh 없이도 문서는 만들어진다 — 그 사실을 남긴다
        L.append(f"(이슈 #15 본문을 읽지 못했습니다: {exc})\n\n")
    L.append(h(2, "배선·AI 계층·사용자 QA — 박경모 (과제 기획서 5-3)"))
    L.append("과제 기획서 5-3 절에 6건을 5요소 형식으로 정리했습니다(공개 URL 배포 실패의 오진과 정정 · 물리 불가능 값 · 수치 복사 사고 · "
             "사용자 QA 가 뒤집은 구조 4건 등). 여기서는 중복하지 않고 그 문서를 가리킵니다.\n\n")
    # 부록은 목록만 둔다 — 커밋 본문 전문은 「개발 일지」에 날짜별로 같은 글이 실려 있어
    # 전문을 다시 실으면 이 문서의 83% 가 중복이 된다(임도영 이슈 #32 실측, 2026-08-31).
    L.append(h(1, "부록 — 커밋에서 자동 수집한 문제·수정 기록"))
    L.append(f"총 {len(hits)}건 / 전체 커밋 {len(cs)}건입니다. "
             "각 건의 경위(무엇이 문제였고 무엇으로 확인했는지)는 커밋 본문에 그대로 남아 있고, "
             "그 원문은 같은 폴더의 「개발 일지」에 날짜순으로 실려 있습니다. "
             "여기서는 문제축으로 한눈에 보도록 목록만 둡니다.\n\n")
    L.append("| 날짜 | 문제·수정 | 커밋 |\n| --- | --- | --- |\n")
    for c in hits:
        subj = c["subject"].replace("|", "\\|")
        L.append(f"| {c['date']} | {subj} | `{c['hash']}` |\n")
    L.append("\n")
    return "".join(L)


# ── 사용 도구 ─────────────────────────────────────────────────────────

def tools_used(cs):
    counts = defaultdict(int)
    for c in cs:
        for t in c["tools"]:
            counts[t] += 1
    L = [h(1, "사용 도구"),
         "FAQ Q5 에 따라 사용한 도구와 방식을 남깁니다. 커밋의 Co-Authored-By 기록에서 "
         "자동 집계했습니다.\n"]
    if counts:
        L.append("| 도구 | 참여 커밋 |\n| --- | --- |\n")
        for t, n in sorted(counts.items(), key=lambda x: -x[1]):
            L.append(f"| {t} | {n}건 / {len(cs)}건 |\n")
    else:
        L.append("\n기록된 도구가 없습니다.\n")
    L.append("\n사람이 판단하고 결정했으며, 코드 작성과 검증에 위 도구를 사용했습니다. "
             "모든 변경은 커밋 단위로 무엇을 왜 바꿨는지와 무엇으로 확인했는지를 함께 남겼습니다.\n")
    return "".join(L)


def main():
    ap = argparse.ArgumentParser(description="「04. 제작 과정」 문서 자동 생성")
    ap.add_argument("--out", default=str(ROOT / "docs" / "제작과정"))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cs = commits()
    if not cs:
        raise SystemExit("커밋이 없습니다.")
    iss = issues()

    files = {
        "개발일지.md": dev_journal(cs),
        "회의록_결정기록.md": minutes(iss),
        "난관과_극복사례.md": troubles(cs),
        "사용도구.md": tools_used(cs),
    }
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
        print(f"  {name}  {len(text):,}자")

    print(f"\n생성 위치: {out}")
    print(f"커밋 {len(cs)}건 · 이슈 {len(iss)}건 기준")


if __name__ == "__main__":
    main()
