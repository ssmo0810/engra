#!/bin/bash
# 정시 리셋 → **완전 빈 상태** (경모님 2026-08-27: "싹 다 없는 DB, 완전 비워진 상태로 전체가 시작").
# 사용자가 최근 2시간 안에 리셋·실행 버튼을 눌렀으면(app/.qa_active) 건너뛴다 — QA 도중 되돌림 사고 방지.
# 기준선(8근무 시드)은 ⑤ 화면의 「기준선으로」 버튼으로만.
M=/opt/engra/app/.qa_active
if [ -f "$M" ]; then age=$(( $(date +%s) - $(cat "$M") )); if [ "$age" -lt 7200 ]; then echo "QA 활동 ${age}s 전 — 리셋 건너뜀"; exit 0; fi; fi
cd /opt/engra && sudo -u engra env ENGRA_LLM=cli python3 -c "import sys; sys.path.insert(0,'app'); import db; print('빈 상태:', db.reset(empty=True))"
