#!/bin/bash
# 제출 폴더(제출/0x. …)를 Drive 팀 폴더 「254. 앙그라쥬」의 같은 이름 폴더로 올린다. 기본은 --dry-run(무엇이 올라갈지만).
# 실제 업로드: bash tools/submit_drive.sh go     — 마감(09-03 23:59) 전 마지막 날에 경모님 확인 뒤 실행.
# rclone 리모트 gdrive: 는 경모님 계정. README_0x.docx(운영측 안내문)는 건드리지 않는다(--exclude).
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:-dry}"; FLAG="--dry-run"; [ "$MODE" = "go" ] && FLAG=""
for d in "01. 과제 기획서" "02. 결과물" "03. 데이터 세트" "04. 제작 과정"; do
  echo "== $d"
  rclone copy "제출/$d" "gdrive:254. 앙그라쥬/$d" --drive-shared-with-me --exclude "README_*.docx" --exclude "*.html" $FLAG -v 2>&1 | grep -E "Copied|Transferred:|Errors|Skipped|NOTICE: .*: Copied" | tail -8
done
