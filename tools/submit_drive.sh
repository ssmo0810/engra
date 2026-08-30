#!/bin/bash
# 제출 폴더(제출/0x. …)를 Drive 팀 폴더 「254. 앙그라쥬」로 동기화한다. 기본은 --dry-run.
#   bash tools/submit_drive.sh go          # 실제 반영 (영상 제외)
#   bash tools/submit_drive.sh go video    # 영상까지 (09-02 촬영본을 제출/02. 결과물/1. 시연 영상/ 에 넣은 뒤)
# 규칙(경모님 2026-08-30): PDF·데이터·캡처·로그·zip 만 올린다. .md/.html 은 올리지 않는다(데이터세트 README·scenarios 는 예외).
# 운영측 README_*.docx 는 절대 건드리지 않는다. 로컬에서 지운 파일은 Drive 에서도 지운다(sync).
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:-dry}"; VIDEO="${2:-}"; FLAG="--dry-run"; [ "$MODE" = "go" ] && FLAG=""
R="gdrive:254. 앙그라쥬"
for d in "01. 과제 기획서" "02. 결과물" "03. 데이터 세트" "04. 제작 과정"; do
  echo "== $d"
  F=(--filter "- README_*.docx" --filter "+ 데이터세트/README_데이터세트.md" --filter "+ 데이터세트/scenarios.md" --filter "- *.md" --filter "- *.html")
  [ "$VIDEO" != "video" ] && F+=(--filter "- 1. 시연 영상/**")
  rclone sync "제출/$d" "$R/$d" --drive-shared-with-me "${F[@]}" $FLAG -v 2>&1 | grep -E "Copied|Deleted|Transferred:|Errors" | tail -6
done
# sync 는 필터로 제외된 원격 파일을 지우지 않는다 → 낡은 .md 는 따로 지운다(데이터세트 안은 제외)
if [ "$MODE" = "go" ]; then
  rclone delete "$R/02. 결과물" --drive-shared-with-me --include "*.md" -q || true
  rclone delete "$R/03. 데이터 세트" --drive-shared-with-me --include "/*.md" -q || true
  rclone delete "$R/04. 제작 과정" --drive-shared-with-me --include "*.md" -q || true
  rclone rmdirs "$R" --drive-shared-with-me --leave-root -q || true
fi
echo "== Drive 파일 수: $(rclone lsf "$R" --drive-shared-with-me -R --files-only 2>/dev/null | grep -v README_0 | wc -l | tr -d ' ')"
