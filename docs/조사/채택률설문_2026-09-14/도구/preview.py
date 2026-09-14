"""생성된 .gs 안의 DATA 를 읽어 설문이 어떻게 보일지 글로 미리 출력한다.

폼을 만들기 전에 문장을 눈으로 읽고 고칠 수 있어야 한다 — 만들고 나서 고치는 것보다 싸다.
"""
import json
import re
import sys

gs = open(sys.argv[1], encoding="utf-8").read()
d = json.loads(re.search(r"var DATA = (\{.*?\n\});", gs, re.S).group(1))

out = []
W = 78


def rule(ch="─"):
    out.append(ch * W)


title = f"ENGRA 인수인계 초안 검토 — {d['date']} {'주간' if '주간' in d['kind'] else '야간'}"
rule("═")
out.append(f"[설문 제목]  {title}")
rule("═")
out.append("")
out.append("[설문 설명]")
out.append(f"ENGRA 가 {d['date']} {d['kind']} 근무의 운전 데이터를 읽고 자동으로 만든 인수인계 초안입니다.")
out.append("")
out.append(f"안건 {len(d['blocks'])}건 중 다음 근무조에게 넘길 것을 골라 주세요. 정답은 없습니다.")
out.append("완전 익명이고, 5분이면 됩니다.")
out.append("")
rule()
out.append("[구분]  시작하기 전에")
rule()
out.append("안건에 나오는 AI-707, LI-701 같은 것은 계기 태그 번호입니다.")
out.append("아래 그림에서 어느 설비인지 확인하실 수 있습니다.")
out.append("")
out.append("[이미지]  ASU 공정 개요 (DCS Overview)   ← engra_dcs_overview_tags.png")
out.append("")

for i, b in enumerate(d["blocks"]):
    rule()
    out.append(f"[구분 · 굵게]  안건 {b['n']}.  {b['title']}")
    out.append(f"               중요도 {b['sev']}" + ("  ·  앞 근무자가 제외했던 항목" if b["demoted"] else ""))
    rule()
    tag = f"{b['trend_tag']} 추이" if b.get("trend_tag") else "추이"
    out.append(f"[이미지]  {tag}   ← {b['trend_png']}")
    out.append("           분홍색 구간이 ENGRA 가 이상으로 잡은 구간입니다.")
    out.append("")
    out.append(f"[객관식·필수]  안건 {b['n']} — 최종 인수인계서에 채택하시겠습니까?")
    out.append("                ( ) 채택한다")
    out.append("                ( ) 채택 안한다")
    out.append("")
    out.append("[단답·선택]    이유 (선택)")
    out.append("                ______________________________________________")
    out.append("")

rule("━")
out.append("[페이지]  마지막으로")
rule("━")
out.append("ENGRA 라는 프로그램 자체에 대한 의견을 듣고 싶습니다.")
out.append("")
out.append("[장문·선택]  ENGRA 를 써 보니 어떠셨습니까? 자유롭게 적어 주세요.")
out.append("  · 초안 내용이 현장 감각에 맞던가요, 엉뚱하던가요?")
out.append("  · 이런 게 교대 때 실제로 있으면 쓰시겠습니까? 안 쓰신다면 왜?")
out.append("  · 빠져 있어서 아쉬운 것, 반대로 불필요하게 많은 것")
out.append("  · 문장·용어·화면 구성에 대한 지적")
out.append("  ______________________________________________________________")
out.append("")
rule("═")
out.append(f"안건 {len(d['blocks'])}건 · 문항 {len(d['blocks']) * 2 + 1}개 (필수 {len(d['blocks'])}개)")
rule("═")

text = "\n".join(out)
open(sys.argv[2], "w", encoding="utf-8").write(text)
print(text)
