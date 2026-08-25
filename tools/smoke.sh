#!/bin/bash
# 배선이 살아 있는지 한 번에 확인한다. 작업 시작 전과 커밋 전에 돌린다.
#
#   bash tools/smoke.sh
#
# 근무 ID 는 날짜에서 나오므로(YYYY-MM-DD-day) --date 로 고정한다.
# 하드코딩한 날짜를 쓰면 하루만 지나도 깨진다 — 실제로 두 번 겪었다.
#
# 두 번째는 이 파일 자신이었다. DAY 에 고정 날짜(2026-08-22)를 적어 뒀는데
# 그 날짜가 RAW_RETENTION_DAYS(3일)를 지나자, 첫 run 이 원본을 정리한 뒤
# --redo 가 읽을 데이터가 없어 실패했다. 검출 엔진 도착과 겹쳐 엔진 문제로
# 오진할 수 있었다. 그래서 항상 보관 기간 안에 있는 오늘 날짜를 쓴다.
set -u
cd "$(dirname "$0")/.." || exit 1

DAY=$(date +%F)
SID="$DAY-day"
fail=0

rm -f app/engra.db app/engra.db-shm app/engra.db-wal app/sample_*.csv
rm -rf app/__pycache__

step() {
  desc="$1"; shift
  out=$(python3 app/cli.py "$@" 2>&1); code=$?
  if [ $code -ne 0 ]; then
    fail=1; echo "✗ $desc (exit $code)"; echo "$out" | tail -3
  else
    echo "✓ $desc"
  fi
}

echo "== 적재부터 확정 일지까지 =="
step "저장소 생성"   init
step "샘플 생성"     sample --date "$DAY"
step "적재"          ingest app/sample_shift.csv
step "검출·초안"     run "$SID"
step "초안 조회"     draft "$SID"
step "승인"          approve "$SID" --all --comment "샘플링 재확인"
step "확정 일지"     handover "$SID"
step "근무 목록"     shifts

echo "== 안전장치 =="
if python3 app/cli.py run "$SID" >/dev/null 2>&1; then
  echo "✗ 확정된 근무 재실행이 막히지 않음"; fail=1
else
  echo "✓ 확정 근무 재실행 차단"
fi
step "강제 재실행"   run "$SID" --redo

echo "== 제작 과정 문서 생성 =="
if python3 tools/journal.py >/dev/null 2>&1; then
  echo "✓ journal.py"
else
  echo "✗ journal.py"; fail=1
fi

# 남겨두면 다음 실행에서 헷갈린다
rm -f app/engra.db app/engra.db-shm app/engra.db-wal app/sample_*.csv

echo "---"
if [ $fail -eq 0 ]; then echo "전체 통과"; else echo "실패 있음"; fi
exit $fail
