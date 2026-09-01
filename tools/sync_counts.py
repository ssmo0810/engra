#!/usr/bin/env python3
"""문서에 손으로 적힌 「커밋 N건」·기간 끝 날짜를 지금 저장소 상태로 맞춘다.

왜 필요한가. rev.2 문서(docs/*_rev2.html)의 수치는 사람이 쓴 값이라 커밋이 늘어도 그대로 남는다.
2026-09-01 실측 — 자동 생성 문서는 264건인데 rev.2 는 248건·기간 끝 08-31 로 굳어 있었다.
마감일에 커밋을 더 하면 또 어긋나므로 refresh_submission.sh 가 업로드 직전에 이걸 돌린다.
읽고 바꾼 자리를 표준출력에 남긴다 — 조용히 고치면 검증이 안 된다.
"""
import re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ["docs/과제기획서_rev2.html", "docs/문서안내_rev2.html",
           "docs/검증결과정리_rev2.html", "docs/결과물원본_rev2.html", "docs/설명서_rev2.html"]

def sh(*a):
    return subprocess.run(a, cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()

def main():
    n = sh("git", "rev-list", "--count", "HEAD")
    first = sh("git", "log", "--reverse", "--format=%ad", "--date=short").splitlines()[0]
    last = sh("git", "log", "-1", "--format=%ad", "--date=short")
    changed = 0
    for rel in TARGETS:
        p = ROOT / rel
        if not p.exists():
            continue
        s = orig = p.read_text(encoding="utf-8")
        s = re.sub(r"커밋 (<b>)?\d+건", lambda m: f"커밋 {m.group(1) or ''}{n}건", s)
        s = re.sub(r"커밋 전체 \(\d+건", f"커밋 전체 ({n}건", s)
        # "2026-08-02부터 08-31까지" 같은 기간 끝을 마지막 커밋 날짜로
        s = re.sub(rf"({re.escape(first)}부터 )\d{{2}}-\d{{2}}(까지)", rf"\g<1>{last[5:]}\g<2>", s)
        if s != orig:
            p.write_text(s, encoding="utf-8")
            changed += 1
            print(f"   {rel} — 커밋 {n}건 · 기간 끝 {last[5:]}")
    print(f"   대상 {len(TARGETS)}개 중 {changed}개 갱신 (현재 커밋 {n}건 · {first}~{last})")
    return 0

if __name__ == "__main__":
    sys.exit(main())
