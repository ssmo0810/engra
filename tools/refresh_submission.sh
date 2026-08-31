#!/bin/bash
# 제출/ 폴더의 파생물을 저장소 정본에서 다시 만든다. Drive 업로드(submit_drive.sh) 직전에 돌린다.
#   bash tools/refresh_submission.sh            # 전부
#   bash tools/refresh_submission.sh nopdf      # PDF 생성은 건너뛴다(빠른 점검용)
# 왜 필요한가: 데이터·소스·04 문서는 저장소가 정본인데 제출/ 는 사본이라 손으로 맞추면 어긋난다.
#   2026-08-31 실제로 어긋났다 — 소스 zip 52커밋·데이터세트 4일(LI-806 수정 전 판) 뒤처짐(이슈 #33).
set -euo pipefail
cd "$(dirname "$0")/.."
PDF="${1:-pdf}"
H=$(git log -1 --format=%h)

echo "== ① 데이터세트 ← data/ · docs/"
D="제출/03. 데이터 세트/데이터세트"
cp data/asu_shift_day_12h.csv data/asu_shift_night_12h.csv data/asu_answer_key.json "$D/"
cp data/sim/asu_2026-08-2*.csv data/sim/asu_answer_all.json "$D/sim_3일_6근무/"
cp docs/tag_master.csv docs/scenarios.md "$D/"
echo "   CSV 8 · 정답지 2 · tag_master · scenarios"

echo "== ② 소스 스냅샷 zip (현재 커밋 $H · data/ 제외 — 03 에 따로 올라간다)"
OUT="제출/02. 결과물/2. 결과물 원본"
rm -f "$OUT"/소스스냅샷_engra_*.zip
Z="$OUT/소스스냅샷_engra_${H}_앙그라쥬.zip"
git archive --format=zip -o "$Z" HEAD
zip -d "$Z" "data/*" -q >/dev/null 2>&1 || true
echo "   $(basename "$Z") $(du -h "$Z" | cut -f1)"
if unzip -l "$Z" | grep -qiE "\.env|serviceAccount|\.pem|id_rsa"; then echo "   !! 비밀 파일이 들어갔다 — 중단"; exit 1; fi

echo "== ③ 04 제작 과정 ← journal.py 재생성"
python3 tools/journal.py >/dev/null
P="제출/04. 제작 과정"
cp docs/제작과정/개발일지.md        "$P/개발일지_앙그라쥬.md"
cp docs/제작과정/난관과_극복사례.md "$P/난관과극복사례_앙그라쥬.md"
cp docs/제작과정/사용도구.md        "$P/사용도구_앙그라쥬.md"
cp docs/제작과정/회의록_결정기록.md "$P/이슈결정기록_앙그라쥬.md"
echo "   md 4개 (회의록_앙그라쥬.md 는 팀원 원문 병합본이라 손대지 않는다)"

if [ "$PDF" != "nopdf" ]; then
  echo "== ④ PDF 재생성 (몇 분 걸린다)"
  for m in "제출/01_과제기획서.md" "$P"/*.md "제출/02. 결과물/2. 결과물 원본/결과물원본_접속주소_앙그라쥬.md" \
           "제출/02. 결과물/3. 설명서·매뉴얼/설명서_앙그라쥬.md" "제출/03. 데이터 세트/검증결과정리_앙그라쥬.md"; do
    [ -f "$m" ] || continue
    python3 tools/md2pdf.py "$m" >/dev/null 2>&1 && echo "   $(basename "${m%.md}.pdf")"
  done
fi
echo "== 완료 — 이어서: bash tools/submit_drive.sh go [video]"
