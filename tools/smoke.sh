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

# 이 스크립트는 app/engra.db 를 지우고 다시 만든다. 시드(tools/seed.py)나 AI 초안 생성이
# 돌고 있으면 그 작업이 "no such table" 로 죽는다 — 2026-08-27 실제로 40분짜리 AI 시드를
# 스모크 한 번으로 날렸다. 다른 파이썬이 DB 를 쓰고 있으면 멈춘다.
# pgrep 은 자기 자신(이 셸)도 잡으므로 python 프로세스만 본다 — 처음 넣었을 때 스모크가 스모크를 막았다.
busy=$(ps -eo pid=,args= 2>/dev/null | grep -E "python3? [^ ]*(tools/seed\.py|app/cli\.py (run|ingest)) " | grep -vE "smoke\.sh|grep" || true)
if [ -n "$busy" ]; then
  echo "✗ 다른 작업이 app/engra.db 를 쓰고 있습니다 (seed/run/ingest). 끝난 뒤 다시 실행하세요."
  echo "$busy" | cut -c1-90
  exit 2
fi

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
# 화면 렌더 — 라이브에서 실제로 열리는 페이지가 500 없이 뜨는지. 2026-08-27 밤 server.py 의 import 누락이 컴파일·배선 시험을
# 다 통과하고 라이브 ④ 만 500 이었다. 파이썬 문법/배선이 아니라 "페이지가 뜨나" 를 직접 본다.
PORT=$((20000 + RANDOM % 20000))
ENGRA_LLM=off python3 app/cli.py serve --port "$PORT" >/tmp/engra_smoke_serve.log 2>&1 &
SPID=$!; sleep 1.5
SID=$(python3 -c "import sqlite3,os; c=sqlite3.connect(os.environ.get('ENGRA_DB','app/engra.db')); r=c.execute('select id from shift order by id desc limit 1').fetchone(); print(r[0] if r else '')")
pages="/ /pipeline /draft /admin"
[ -n "$SID" ] && pages="$pages /shift/$SID /answer/$SID"
bad=0
for pg in $pages; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "http://127.0.0.1:$PORT$pg")
  case "$code" in 200|303) ;; *) echo "✗ 페이지 $pg → $code"; bad=1;; esac
done
kill $SPID 2>/dev/null; wait $SPID 2>/dev/null
if [ "$bad" = 0 ]; then echo "✓ 화면 렌더 ($pages)"; else fail=1; fi

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
