#!/bin/bash
# 라이브(Vultr)로 코드를 올리고 서비스를 다시 띄운다.
#   bash tools/deploy.sh            # 확인만 (무엇이 바뀌는지)
#   bash tools/deploy.sh go         # 실제 배포
#
# 왜 스크립트인가: 손으로 rsync 를 치다 제외 옵션을 빠뜨려 라이브를 깬 적이 있다(2026-08-31).
#   ① `-a` 는 소유권까지 복사한다 → 맥의 uid 501 이 서버 파일 주인이 되어 앱 계정이 못 쓴다.
#      실제로 `.qa_active` 가 501:staff 가 되어 「다시 만들기」가 500 을 냈다. --no-owner --no-group 필수.
#   ② DB·uploads 를 덮으면 라이브 데이터가 날아간다. --delete 는 쓰지 않는다(서버에만 있는 파일 보호).
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:-dry}"
H=root@64.176.227.85
EX=(--exclude '__pycache__/' --exclude '*.db' --exclude '*.db-*' --exclude '*.db-wal' --exclude '*.db-shm'
    --exclude 'uploads/' --exclude '.qa_active' --exclude '.env' --exclude '*.pyc')
OPT=(-rlptz --no-owner --no-group "${EX[@]}")

if [ "$MODE" != "go" ]; then
  echo "== 바뀌는 것 (dry-run)"; rsync -n --itemize-changes "${OPT[@]}" app engine tools docs "$H:/opt/engra/" | grep -vE '^\.[fd]\.\.\.' | head -30
  echo "== 실제로 올리려면: bash tools/deploy.sh go"; exit 0
fi

R=$(curl -s --max-time 10 https://engra.64-176-227-85.sslip.io/api/job | python3 -c 'import sys,json;print(json.load(sys.stdin).get("running"))' 2>/dev/null || echo unknown)
[ "$R" = "True" ] && { echo "!! 작업이 돌고 있다(running=True). 끝난 뒤 다시 실행하라."; exit 1; }
echo "== 작업 없음(running=$R) — 올린다"
rsync "${OPT[@]}" app engine tools docs "$H:/opt/engra/"
ssh "$H" 'chown -R engra:engra /opt/engra/app /opt/engra/engine /opt/engra/tools /opt/engra/docs && systemctl restart engra.service'
echo "== 기동 대기 (uploads 재적재 때문에 20~30초 걸린다)"
for i in $(seq 1 20); do
  sleep 3
  C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://engra.64-176-227-85.sslip.io/ || echo 000)
  [ "$C" = "200" ] && { echo "== 200 OK ($((i*3))초)"; break; }
  [ "$i" = "20" ] && { echo "!! 60초 안에 안 떴다 — journalctl -u engra.service 확인"; exit 1; }
done
for p in /draft /admin /pipeline; do echo "   $p → $(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://engra.64-176-227-85.sslip.io$p")"; done
