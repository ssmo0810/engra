#!/bin/bash
# AI 시드가 끝난 뒤 한 번 실행. 지금 DB 를 기준선(seed.db)으로 굳히고 서비스를 올린다.
set -eu
cd /opt/engra
if pgrep -f "python3 tools/seed.py" >/dev/null; then echo "시드가 아직 돌고 있습니다. 끝난 뒤 실행하세요."; exit 2; fi
n=$(sudo -u engra python3 -c "
import sys; sys.path.insert(0,\"app\"); import db
with db.connect() as c:
    rows=db.list_shifts(c); print(sum(1 for r in rows if db.load_handover(c,r[\"id\"])), len(rows))")
echo "확정/근무 = $n"
sudo -u engra cp app/engra.db app/seed.db
rm -f app/seed-v3.db app/engra.db-wal app/engra.db-shm
chown engra:engra app/seed.db
systemctl start engra.service && sleep 2 && systemctl is-active engra.service
echo "seed.db $(du -h app/seed.db | cut -f1) 저장 · 서비스 기동"
