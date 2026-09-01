#!/bin/bash
# 과제 기획서 rev.2 (docs/과제기획서_rev2.html) → PDF.
# md2pdf.py 를 쓰지 않는다 — 첫 면 포스터와 SK 레드 포인트가 손수 만든 마크다운 파서로는
# 표현되지 않아, 기획서만 HTML 로 직접 쓰고 헤드리스 크롬으로 인쇄한다.
# 다른 제출 문서(03·04)는 그대로 md2pdf.py 를 쓴다 — 이 스크립트는 01 만 건드린다.
#
#   bash tools/plan2pdf.sh                          # 기획서 → 저장소 루트에 과제기획서_앙그라쥬.pdf
#   bash tools/plan2pdf.sh /경로/이름.pdf            # 출력 위치 지정
#   bash tools/plan2pdf.sh out.pdf docs/결과물원본_rev2.html   # 다른 원본
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="${2:-$PWD/docs/과제기획서_rev2.html}"
OUT="${1:-$PWD/과제기획서_앙그라쥬.pdf}"
# 크롬은 상대 경로 출력을 못 쓴다 — 저장소 루트 기준으로 절대화한다 (refresh_submission ⑤ 가 상대로 넘긴다)
case "$SRC" in /*|[A-Za-z]:*) ;; *) SRC="$PWD/$SRC" ;; esac
case "$OUT" in /*|[A-Za-z]:*) ;; *) OUT="$PWD/$OUT" ;; esac
[ -f "$SRC" ] || { echo "원본이 없습니다: $SRC" >&2; exit 1; }

CHROME=""
for C in \
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  "/c/Program Files/Google/Chrome/Application/chrome.exe" \
  "/c/Program Files (x86)/Google/Chrome/Application/chrome.exe" \
  "$(command -v google-chrome 2>/dev/null || true)" \
  "$(command -v chromium 2>/dev/null || true)"
do
  if [ -n "$C" ] && [ -x "$C" ]; then CHROME="$C"; break; fi
done
[ -n "$CHROME" ] || { echo "크롬을 찾지 못했습니다. 경로를 이 스크립트에 추가하세요." >&2; exit 1; }

# Git Bash 의 /c/... 경로는 크롬이 읽지 못한다 — cygpath 가 있으면 윈도 경로로 바꾼다.
if command -v cygpath >/dev/null 2>&1; then
  SRC_URL="file:///$(cygpath -m "$SRC")"
  OUT_ARG="$(cygpath -w "$OUT")"
else
  SRC_URL="file://$SRC"
  OUT_ARG="$OUT"
fi

"$CHROME" --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
  --print-to-pdf="$OUT_ARG" "$SRC_URL" >/dev/null 2>&1

[ -s "$OUT" ] || { echo "PDF 가 만들어지지 않았습니다." >&2; exit 1; }
echo "생성: $OUT  ($(du -k "$OUT" | cut -f1)KB)"
echo "확인할 것 — 원본이 $(basename "$SRC") 다. 기획서는 1면이 표지로 끝나고 2면이 「1. 팀 소개」로 시작해야 한다."
echo "  파일 크기가 40KB 아래면 경로를 못 읽어 빈 PDF 가 나온 것이다."
