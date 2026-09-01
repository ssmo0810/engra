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

# rev.2 — 01 기획서·02-2 접속주소·02-3 설명서는 저장소 정본(docs/*_rev2.html)에서 렌더한다.
# 위 md 루프 **뒤**라서 rev.2 가 최종본이 된다 (09-01 임도영 결정 — 세 문서 rev.2 사용).
# 표지 포스터·표 머리행 반복은 md2pdf 파서로는 안 되어 HTML 직접 렌더(헤드리스 크롬)로 간다.
if [ "$PDF" != "nopdf" ]; then
  echo "== ⑤ rev.2 문서 → PDF (docs/*_rev2.html 이 정본)"
  mkdir -p "제출/01. 과제 기획서" "제출/02. 결과물/2. 결과물 원본" "제출/02. 결과물/3. 설명서·매뉴얼" "제출/03. 데이터 세트" "제출/04. 제작 과정"
  bash tools/plan2pdf.sh "제출/01. 과제 기획서/과제기획서_앙그라쥬.pdf"                 "docs/과제기획서_rev2.html" | head -1
  bash tools/plan2pdf.sh "제출/02. 결과물/2. 결과물 원본/결과물원본_접속주소_앙그라쥬.pdf" "docs/결과물원본_rev2.html" | head -1
  bash tools/plan2pdf.sh "제출/02. 결과물/3. 설명서·매뉴얼/설명서_앙그라쥬.pdf"            "docs/설명서_rev2.html"     | head -1
  bash tools/plan2pdf.sh "제출/03. 데이터 세트/검증결과정리_앙그라쥬.pdf"                    "docs/검증결과정리_rev2.html" | head -1
  bash tools/plan2pdf.sh "제출/04. 제작 과정/문서안내_앙그라쥬.pdf"                          "docs/문서안내_rev2.html"     | head -1
  # 04 의 자동 생성·병합 md 는 내용은 그대로 두고 스타일만 입힌다 — ③ 이 방금 만든 md 를 다시 렌더
  [ -f "제출/04. 제작 과정/사용도구_앙그라쥬.md" ] && python3 tools/md2pdf.py --style sk "제출/04. 제작 과정/사용도구_앙그라쥬.md"
  if [ -f "제출/04. 제작 과정/회의록_앙그라쥬.md" ]; then
    # 회의록의 그림을 렌더 동안만 옆에 둔다 — 남겨 두면 submit_drive 가 png 를 Drive 에 올린다
    cp -r docs/회의록/img "제출/04. 제작 과정/img" 2>/dev/null || true
    python3 tools/md2pdf.py --style sk "제출/04. 제작 과정/회의록_앙그라쥬.md"
    rm -rf "제출/04. 제작 과정/img"
  fi
fi
echo "== 완료 — 이어서: bash tools/submit_drive.sh go [video]"
