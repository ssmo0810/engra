"""마크다운 제출 문서 → HTML → PDF (헤드리스 Chrome). 제출 폴더의 .md 옆에 같은 이름의 .pdf 를 둔다 —
심사위원이 마크다운 뷰어 없이도 열 수 있게. 형식은 자유(안내문)지만 열리지 않는 위험을 없앤다.
사용: python3 tools/md2pdf.py <file.md> [...]"""
import html, re, subprocess, sys
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CSS = ("body{font:12.5pt/1.65 -apple-system,'Apple SD Gothic Neo','Noto Sans KR',sans-serif;max-width:860px;margin:28px auto;padding:0 24px;color:#1b1b1b}"
       "h1{font-size:22pt;border-bottom:2px solid #333;padding-bottom:6px}h2{font-size:16pt;margin-top:28px;border-bottom:1px solid #ccc;padding-bottom:4px}h3{font-size:13.5pt;margin-top:20px}"
       "table{border-collapse:collapse;width:100%;font-size:11pt;margin:8px 0}td,th{border:1px solid #bbb;padding:4px 7px;text-align:left;vertical-align:top}th{background:#f2f2f2}"
       "pre{background:#f4f4f4;padding:8px 10px;font-size:10.5pt;overflow-x:auto;white-space:pre-wrap}code{background:#f0f0f0;padding:0 3px;font-size:11pt}blockquote{border-left:3px solid #ccc;margin:6px 0;padding:2px 12px;color:#444}li{margin:2px 0}")


def inline(t):
    t = html.escape(t)
    t = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t)
    t = re.sub(r'`([^`]+)`', r'<code>\1</code>', t)
    t = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', r'<a href="\2">\1</a>', t)
    return re.sub(r'(?<!["=])(https?://[^\s)<]+)', r'<a href="\1">\1</a>', t)


def md2html(md, title):
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
        elif l.strip() == '---': out.append('<hr>')
        elif l.strip(): out.append(f'<p>{inline(l)}</p>')
    if in_table: out.append('</table>')
    if in_quote: out.append('</blockquote>')
    return f'<!doctype html><html lang="ko"><meta charset="utf-8"><title>{html.escape(title)}</title><style>{CSS}</style><body>' + '\n'.join(out) + '</body></html>'


def main(paths):
    for p in map(Path, paths):
        h = p.with_suffix('.html'); pdf = p.with_suffix('.pdf')
        h.write_text(md2html(p.read_text(encoding='utf-8'), p.stem), encoding='utf-8')
        subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--no-pdf-header-footer', f'--print-to-pdf={pdf}', f'file://{h.resolve()}'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        h.unlink(missing_ok=True)
        print(f"  {pdf.name}: {pdf.stat().st_size // 1024 if pdf.exists() else 0} KB")


if __name__ == "__main__":
    main(sys.argv[1:])
