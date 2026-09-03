#!/usr/bin/env python3
"""소스 스냅샷 zip 에서 비ASCII 파일명을 걷어낸다 — 어느 환경에서든 전부 풀리게.

**왜.** git archive 가 만든 zip 은 한글 이름에 UTF-8 플래그를 제대로 단다. 그런데 macOS 에
기본 탑재된 Info-ZIP `unzip` 6.0(2009)은 그 플래그를 읽지 않아 경로 생성에 실패하고,
한 번 실패하면 **그 뒤 항목까지 통째로 건너뛴다.** 2026-09-03 실측 — 같은 zip 을
python/ditto 로 풀면 91개, `unzip` 으로 풀면 34개였다. 운영 사무국이 마감 당일
"열리지 않는 파일이 있는지 확인하라"고 공지한 바로 그 사고다.

빠지는 것은 전부 문서(md·html·png·pptx)이고 코드는 하나도 없다 — 그 문서들은 01~04 폴더에
PDF 로 따로 제출되므로 정보 손실이 없다. 코드가 걸리면 중단한다(그건 다른 문제다).
"""
import sys, zipfile, shutil
from pathlib import Path

# 실행되는 것만 코드로 본다. docs/*.html 은 제출 문서의 원본이지 앱이 아니다
# (앱 화면은 app/server.py 가 문자열로 만들고, demo/·sample/ 은 이름이 ASCII 라 애초에 안 걸린다).
CODE_SUFFIXES = {".py", ".sh", ".js"}
NOTE = """ENGRA — source snapshot
=======================
This archive holds the source code of the ENGRA prototype (team Angrajyu).

Files whose names are not ASCII were left out on purpose: the `unzip` shipped with
macOS ignores the UTF-8 name flag and silently stops extracting when it meets one.
Every omitted file is a document, not code, and each is submitted as a PDF in
folders 01-04 of the same Drive package.

To run it:  python3 app/cli.py serve      (Python 3.10+, no dependencies)
Live app:   https://engra.64-176-227-85.sslip.io
"""

def main(path):
    src = Path(path)
    tmp = src.with_suffix(".tmp.zip")
    dropped, kept = [], 0
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            if any(ord(c) > 0x7F for c in info.filename):
                if Path(info.filename).suffix.lower() in CODE_SUFFIXES:
                    print(f"   !! 코드 파일이 비ASCII 이름이다 — 손대지 않고 멈춘다: {info.filename}")
                    tmp.unlink(missing_ok=True)
                    return 1
                dropped.append(info.filename)
                continue
            zout.writestr(info, zin.read(info.filename))
            kept += 1
        zout.writestr("README_snapshot.txt", NOTE)
    shutil.move(str(tmp), str(src))
    print(f"   비ASCII 이름 {len(dropped)}개 제외 · {kept + 1}개 수록 (코드 전량 포함)")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
