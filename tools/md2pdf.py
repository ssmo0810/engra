"""마크다운 제출 문서 → HTML → PDF (헤드리스 Chrome). 제출 폴더의 .md 옆에 같은 이름의 .pdf 를 둔다 —
심사위원이 마크다운 뷰어 없이도 열 수 있게. 형식은 자유(안내문)지만 열리지 않는 위험을 없앤다.
사용: python3 tools/md2pdf.py <file.md> [...]"""
import html, re, subprocess, sys
from pathlib import Path

def _find_chrome():
    """맥(팀장 환경)을 1순위로, 윈도(임도영 환경) 폴백. 못 찾으면 이유를 말하고 멈춘다."""
    import os, shutil
    for c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              shutil.which("google-chrome"), shutil.which("chromium")):
        if c and os.path.exists(c):
            return c
    raise SystemExit("크롬을 찾지 못했습니다 — md2pdf.py 의 _find_chrome 에 경로를 추가하세요.")

CHROME = _find_chrome()
CSS = ("body{font:12.5pt/1.65 -apple-system,'Apple SD Gothic Neo','Noto Sans KR',sans-serif;max-width:860px;margin:28px auto;padding:0 24px;color:#1b1b1b}"
       "h1{font-size:22pt;border-bottom:2px solid #333;padding-bottom:6px}h2{font-size:16pt;margin-top:28px;border-bottom:1px solid #ccc;padding-bottom:4px}h3{font-size:13.5pt;margin-top:20px}"
       "table{border-collapse:collapse;width:100%;font-size:11pt;margin:8px 0}td,th{border:1px solid #bbb;padding:4px 7px;text-align:left;vertical-align:top}th{background:#f2f2f2}"
       "pre{background:#f4f4f4;padding:8px 10px;font-size:10.5pt;overflow-x:auto;white-space:pre-wrap}code{background:#f0f0f0;padding:0 3px;font-size:11pt}blockquote{border-left:3px solid #ccc;margin:6px 0;padding:2px 12px;color:#444}li{margin:2px 0}")


CSS_SK = (
    "@page{size:210mm 297mm;margin:16mm 15mm 15mm}"
    "*{box-sizing:border-box}html{-webkit-print-color-adjust:exact;print-color-adjust:exact}"
    "body{margin:0;color:#2E3036;font:10.2pt/1.72 'Malgun Gothic','맑은 고딕',-apple-system,"
    "'Apple SD Gothic Neo','Noto Sans KR',sans-serif;word-break:keep-all}"
    "p{margin:0 0 .6em;text-align:justify}b,strong{color:#15161A}"
    "h1{font-size:16.5pt;font-weight:800;color:#15161A;letter-spacing:-.02em;"
    "margin:0 0 12px;padding-bottom:7px;border-bottom:2.5px solid #EA002C}"
    "h2{font-size:11.6pt;font-weight:700;color:#15161A;margin:18px 0 7px;"
    "padding-left:11px;border-left:3.5px solid #EA002C;page-break-after:avoid}"
    "h3{font-size:10.4pt;font-weight:700;color:#B00021;margin:14px 0 5px;page-break-after:avoid}"
    "table{border-collapse:collapse;width:100%;font-size:9pt;margin:8px 0 12px}"
    "thead{display:table-header-group}tr{page-break-inside:avoid}"
    "th{background:#FFF4F6;color:#B00021;font-weight:700;text-align:left;padding:5px 7px;"
    "border-bottom:1.6px solid #EA002C;border-top:1px solid #E3E4E8;line-height:1.45}"
    "td{padding:5px 7px;border-bottom:1px solid #F0F1F4;vertical-align:top;line-height:1.58}"
    "td:first-child{font-weight:600;color:#15161A}"
    "pre{background:#F7F7F9;border:1px solid #E3E4E8;border-left:3px solid #EA002C;"
    "padding:9px 12px;font:8.8pt/1.62 Consolas,'Courier New',monospace;white-space:pre-wrap;color:#15161A}"
    "code{font-family:Consolas,'Courier New',monospace;font-size:.9em;background:#F0F1F4;"
    "padding:.06em .32em;border-radius:2px;color:#B00021}"
    "blockquote{border-left:3px solid #EA002C;background:#FFF4F6;margin:9px 0 12px;"
    "padding:8px 12px;font-size:9.3pt;color:#2E3036}"
    "li{margin:2.5px 0}hr{border:0;height:1px;background:#E3E4E8;margin:14px 0}"
    "a{color:#B00021}img{max-width:100%;height:auto}"
)


CSS_BREAK_H1 = "h1{page-break-before:always}h1:first-of-type{page-break-before:auto}"

def inline(t):
    t = html.escape(t)
    t = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t)
    t = re.sub(r'`([^`]+)`', r'<code>\1</code>', t)
    t = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', r'<a href="\2">\1</a>', t)
    return re.sub(r'(?<!["=])(https?://[^\s)<]+)', r'<a href="\1">\1</a>', t)


def md2html(md, title, css=None):
    out, in_code, in_table, in_quote = [], False, False, False
    lines = md.split('\n')
    for i, l in enumerate(lines):
        if l.startswith('```'):
            in_code = not in_code; out.append('<pre>' if in_code else '</pre>'); continue
        if in_code:
            out.append(html.escape(l)); continue
        if l.startswith('> '):
            if not in_quote: out.append('<blockquote>'); in_quote = True
            out.append(inline(l[2:]) + '<br>'); continue
        if in_quote and not l.startswith('>'):
            out.append('</blockquote>'); in_quote = False
        if l.strip() == '>':
            out.append('<br>'); continue
        if l.startswith('|'):
            cells = [c.strip() for c in l.strip().strip('|').split('|')]
            if all(re.fullmatch(r':?-{3,}:?', c) for c in cells): continue
            if not in_table: out.append('<table>'); in_table = True
            hdr = i + 1 < len(lines) and lines[i + 1].startswith('|') and set(lines[i + 1].replace('|', '').strip()) <= set('-: ')
            tag = 'th' if hdr else 'td'
            out.append('<tr>' + ''.join(f'<{tag}>{inline(c)}</{tag}>' for c in cells) + '</tr>'); continue
        if in_table:
            out.append('</table>'); in_table = False
        m = re.match(r'^(#{1,4}) (.*)', l)
        if m: out.append(f'<h{len(m.group(1))}>{inline(m.group(2))}</h{len(m.group(1))}>')
        elif re.match(r'^\s*[-*] \[[ x]\] ', l): out.append(f'<li>{inline(re.sub(r"^\s*[-*] ", "", l))}</li>')
        elif re.match(r'^\s*[-*] ', l): out.append(f'<li>{inline(re.sub(r"^\s*[-*] ", "", l))}</li>')
        elif re.match(r'^\s*\d+\. ', l): out.append(f'<li>{inline(re.sub(r"^\s*\d+\. ", "", l))}</li>')
        elif re.match(r'^\s*<img [^>]+>\s*$', l):
            # 그림 한 줄짜리 태그는 그대로 통과 — 이스케이프하면 태그가 글자로 찍힌다 (회의록 실측)
            out.append('<p style="text-align:center;margin:10px 0">' + l.strip().replace('width="893"','style="max-width:100%"').replace('width="506"','style="max-width:70%"') + '</p>')
        elif l.strip() == '---': out.append('<hr>')
        elif l.strip(): out.append(f'<p>{inline(l)}</p>')
    if in_table: out.append('</table>')
    if in_quote: out.append('</blockquote>')
    return f'<!doctype html><html lang="ko"><meta charset="utf-8"><title>{html.escape(title)}</title><style>{css or CSS}</style><body>' + '\n'.join(out) + '</body></html>'


def main(paths):
    css = None; extra = ''
    while paths and paths[0].startswith('--'):
        if paths[:2] == ['--style', 'sk']:
            css, paths = CSS_SK, paths[2:]
        elif paths[0] == '--break-h1':      # 회의록 병합본용 — h1(회의)마다 새 면
            extra, paths = CSS_BREAK_H1, paths[1:]
        else:
            raise SystemExit('모르는 옵션: ' + paths[0])
    if extra:
        css = (css or CSS) + extra
    for p in map(Path, paths):
        h = p.with_suffix('.html'); pdf = p.with_suffix('.pdf')
        h.write_text(md2html(p.read_text(encoding='utf-8'), p.stem, css), encoding='utf-8')
        # 경로는 절대화한다 — 상대 경로면 크롬이 아무 말 없이 빈손으로 끝난다 (윈도 실측)
        subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--no-pdf-header-footer',
                        f'--print-to-pdf={pdf.resolve()}', h.resolve().as_uri()],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        h.unlink(missing_ok=True)
        print(f"  {pdf.name}: {pdf.stat().st_size // 1024 if pdf.exists() else 0} KB")


if __name__ == "__main__":
    main(sys.argv[1:])
