"""웹 화면.

표준 라이브러리만 쓴다. 팀원이 아무것도 설치하지 않고
`python3 app/cli.py serve` 한 줄로 띄울 수 있어야 하기 때문이다.

디자인은 `demo/index.html` 목업을 그대로 따른다. 목업과 프로토타입이 서로 다르게
생기면 결선에서 두 화면을 다 설명해야 하고, 화면을 두 번 만들게 된다.
토큰(색·간격·컴포넌트)을 목업에서 옮겨왔으므로 목업이 바뀌면 여기도 같이 바꾼다.
"""
import html
import json
import re
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import approve as approve_mod
import db
import numfmt   # 앱이 찍는 숫자 규칙 하나 — 엔진 detectors._fmt 와 같다
import jobs
import live       # 실시간 누적 — 재생 시작·정지·상태 (app/live.py 의 공개 함수만 부른다)
import pipeline   # quality_summary — 초안 화면 배너 문구
import llm
import ports
from config import DOCS_DIR

# demo/index.html 에서 옮겨온 디자인 토큰과 컴포넌트
STYLE = """
:root{--ink:#191b1f;--sub:#5f6672;--line:#e4e1dc;--bg:#f7f6f3;--card:#fff;--chip:#f2f0ec;
      --accent:#EA002C;                            /* SK 레드 — 워드마크·주 버튼·한계선에만 */
      --ok:#1a6b3c;--ok-soft:#e4f1e8;              /* 확정·완료 */
      --warn:#8a5300;--warn-soft:#fbeed3;          /* 초안(승인 대기) */
      --go:#0f6b63;--go-soft:#e1f2f0;              /* 진행중 — 다음 근무로 이어진다 */
      --bad:#b3261e;--bad-soft:#fde4e6;            /* AI 서술 실패 */
      --live:#0b62c4;--live-soft:#e5effb;          /* 쌓이는 중 */
      --obs:#4c5a6b;--obs-soft:#eef1f5;            /* 관찰 중·서술 중 — 아직 못 고른다 */
      --ready:#3a4aa0;--ready-soft:#ecedfa;        /* 선택 가능 */
      --chart:var(--accent);--band:#f0b323;         /* 그래프 선 = SK 레드(경모님 2026-09-15) · 감지 구간 음영 */
      --limit:#8b8f98;                             /* 알람 한계선 — 선이 빨강이 되어 회색 점선으로 물러났다 */
      --why-bg:#f6f5f2;--why-line:#d5d1ca;--sug-bg:#f1f6f2;--sug-line:#a9cdb6;
      --flash:#fdf2c9;                             /* 값이 바뀐 자리가 한 번 밝아진다 */
      --disc-bg:#fff8e6;--disc-line:#efe2bd;--off-bg:#fbfaf8}

/* 어두운 화면 — 토큰만 다시 정한다. 규칙은 하나고 색만 갈린다 */
@media (prefers-color-scheme: dark){
:root{--ink:#e8e6e3;--sub:#a6a29c;--line:#33312e;--bg:#131312;--card:#1c1b1a;--chip:#262421;
      --accent:#ff4d68;
      --ok:#61c48c;--ok-soft:#12301f;
      --warn:#e0a654;--warn-soft:#33260f;
      --go:#54bdb0;--go-soft:#10302c;
      --bad:#ff6b63;--bad-soft:#3a1a18;
      --live:#5aa2f5;--live-soft:#10243c;
      --obs:#9fb0c2;--obs-soft:#1e2530;
      --ready:#9aa6ee;--ready-soft:#1d2040;
      --chart:var(--accent);--band:#d9a03c;
      --limit:#9aa0aa;
      --why-bg:#232120;--why-line:#3d3a36;--sug-bg:#16251b;--sug-line:#2f5c3f;
      --flash:#4a3f1c;
      --disc-bg:#2a2413;--disc-line:#4a3f1f;--off-bg:#191817}
}
*{margin:0;padding:0;box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{font-family:"Pretendard","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;
     background:var(--bg);color:var(--ink);font-size:14px;line-height:1.65;
     font-variant-numeric:tabular-nums}
a{color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* 상단 얇은 바 — 워드마크·화면 이름은 왼쪽, 엔진·AI 상태는 오른쪽에 조용히.
   이동줄은 없다: DCS 와 일지 목록은 각자 주소로 연다 (경모님 2026-09-14) */
.top{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;padding:11px 20px;
     background:var(--card);border-bottom:1px solid var(--line)}
.top .brand{font-size:16px;font-weight:800;color:var(--accent);letter-spacing:-.02em}
.top .ttl{font-size:13px;color:var(--sub)}
.top .sys{margin-left:auto;font-size:13px;color:var(--sub)}
.wrap{max-width:1080px;margin:0 auto;padding:24px 20px 72px}

.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:18px 20px;margin-bottom:14px}
.card h2{font-size:16px;margin-bottom:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.card h3,.det h3{font-size:20px;margin-bottom:4px;letter-spacing:-.02em}
.det .sub{font-size:13px;color:var(--sub);margin-bottom:16px}
.note{font-size:13px;color:var(--sub);margin-bottom:10px}
.muted{color:var(--sub)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
.empty{color:var(--sub);text-align:center;padding:32px 16px;font-size:14px}
.back{display:inline-block;margin-bottom:14px;font-size:13px;font-weight:700;
      color:var(--accent);text-decoration:none}
.back:hover{text-decoration:underline}
.disc{font-size:13px;color:var(--sub);background:var(--disc-bg);border:1px solid var(--disc-line);
      padding:9px 13px;border-radius:8px;margin-bottom:14px}
.sc{border-collapse:collapse;width:100%;font-size:13px}
.sc th,.sc td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left}
.sc th{color:var(--sub);font-weight:600;font-size:13px}

/* 상태 — 초안 호박 · 확정 초록 · 관찰 중 흐린 회색. 빨강은 워드마크·LIVE·주 버튼에만 */
.pill{font-size:13px;font-weight:700;padding:2px 9px;border-radius:999px;
      background:var(--chip);color:var(--sub);white-space:nowrap}
.pill.on,.pill.done{background:var(--ok-soft);color:var(--ok)}          /* 확정 · 완료 */
.pill.amber{background:var(--warn-soft);color:var(--warn)}              /* 초안(승인 대기) */
.pill.going{background:var(--go-soft);color:var(--go)}                  /* 진행중 — 완료와 한눈에 갈리게 다른 색 */
.pill.red{background:var(--bad-soft);color:var(--bad)}
.pill.live{background:var(--live);color:#fff}                           /* 쌓이는 중 — 빨강은 워드마크·주 버튼에 남긴다 */

/* 한 화면 — 왼쪽 근무 목록(고정 폭) + 오른쪽 그 근무의 내용. 목록과 내용이 따로 열리면
   같은 근무를 두 번 찾아 들어가야 한다(경모님 2026-09-14) */
.split{display:flex;gap:18px;align-items:flex-start}
.side{width:280px;flex:none;background:var(--card);border:1px solid var(--line);
      border-radius:10px;overflow:hidden;position:sticky;top:14px;max-height:calc(100vh - 28px);
      overflow-y:auto}
.side h2{font-size:15px;padding:13px 14px 0}
.side .note{padding:3px 14px 11px;margin:0;font-size:12.5px}
.detail{flex:1;min-width:0}
/* 줄 = [날짜] [주간/야간] [상태]. 감지 요약·구간 시각은 뺀다 — 상태 뒤 채택 건수와 겹쳐 읽혔다 */
.nrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 14px;
      border-top:1px solid var(--line);text-decoration:none;color:inherit}
.nrow:hover{background:var(--bg)}
.nrow.on{background:var(--chip);box-shadow:inset 3px 0 0 var(--accent)}   /* 고른 줄 — 빨강은 선택 표시 하나 */
.nrow.live{background:var(--live-soft)}
.nrow.live .date{color:var(--live)}
.nrow .date{font-size:14px;font-weight:700;letter-spacing:-.02em}
.nrow .kind{font-size:13px;color:var(--sub)}
.nrow .pill{margin-left:auto;font-size:12px}
/* 모바일에서만 목록을 접는다. PC 에서는 접을 일이 없으니 체크박스를 아예 뺀다 —
   남겨 두면 첫 Tab 이 보이지도 않고 눌러도 아무 일도 안 나는 자리에 걸린다(반증 워커 실측 1440) */
.pickbox{display:none}
.pickbtn{display:none}
@media (max-width:760px){
  /* 접기가 실제로 도는 폭에서만 되살린다 — 안 보이되 초점은 받는다(JS 없이 펼치기) */
  .pickbox{position:absolute;display:block;width:1px;height:1px;opacity:0;pointer-events:none}
  .split{flex-direction:column;gap:12px}
  .side{width:100%;position:static;max-height:none;display:none}
  .pickbox:checked ~ .split .side{display:block}
  .pickbtn{display:inline-block;margin-bottom:12px;padding:8px 14px;background:var(--card);
           border:1px solid var(--line);border-radius:8px;font-size:13px;font-weight:700;cursor:pointer}
  .pickbox:focus-visible ~ .pickbtn{outline:2px solid var(--accent);outline-offset:2px}
}

/* 초안 항목 — 제목 → 본문 → 근거 순. 왼쪽 색 띠는 뺐다(경모님 2026-09-14) — 경계는 카드 테두리가 잡는다 */
.item{border:1px solid var(--line);border-radius:10px;
      padding:14px 16px;margin-bottom:10px;background:var(--card);transition:.15s}
.item.off{opacity:.55;background:var(--off-bg)}
/* 상태 색 — 「지금 무엇인가」(관찰 중 · 선택 가능 · 실패)를 나타낸다. 상태가 같으면 색도 같아서 항목끼리 비교되지 않는다 */
.item[data-state="ready"]{border-left:3px solid var(--ready)}
.item[data-state="observing"],.item[data-state="writing"]{border-left:3px solid var(--obs);background:var(--obs-soft)}
.item[data-state="ai_failed"]{border-left:3px solid var(--bad);background:var(--bad-soft)}
.item.obs .pill{background:var(--card);color:var(--obs);border:1px solid var(--obs)}
.item[data-state="ai_failed"] .pill{background:var(--bad-soft);color:var(--bad);border-color:var(--bad)}
.item .row1{display:flex;align-items:flex-start;gap:10px}
/* 값이 바뀐 관찰 카드만 한 번 깜빡인다 — 폴링이 새 마디를 끼울 때 class 가 붙어 애니메이션이 한 번 돈다 */
@keyframes flash{from{background:var(--flash)}to{background:var(--why-bg)}}
.item.flash .why{animation:flash 1.6s ease-out}
@keyframes pop{from{background:var(--flash)}to{background:var(--obs-soft)}}
.item.fresh{animation:pop .6s ease-out}
/* 한 줄 목록의 무리 머리 — AI 작성 중 · 관찰 중 · 초안. 개수는 폴링이 갈아 끼운다 */
.lane{font-size:12.5px;font-weight:700;color:var(--sub);letter-spacing:.02em;
      margin:16px 0 8px;padding-bottom:5px;border-bottom:1px solid var(--line)}
.lane b{color:var(--ink)}
.prog{height:3px;border-radius:2px;background:var(--chip);overflow:hidden;margin:2px 0 8px}
.prog i{display:block;height:100%;width:38%;background:var(--obs);animation:slide 1.2s ease-in-out infinite}
@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(320%)}}
@media (prefers-reduced-motion: reduce){.item.flash .why,.item.fresh,.prog i{animation:none}}
.det{position:relative}
.det .edit{position:absolute;top:14px;right:16px;background:none;border:0;color:var(--sub);
           font-size:13px;text-decoration:underline;cursor:pointer;padding:2px 4px;font-family:inherit}
.det .edit:hover{color:var(--ink)}
.item input[type=checkbox]{width:18px;height:18px;margin-top:2px;accent-color:var(--accent);cursor:pointer}
.item .ttl{font-weight:700;font-size:16px;flex:1;letter-spacing:-.01em}
.item .meta{font-size:13px;color:var(--sub);margin:4px 0 10px 28px}
.item .body{margin-left:28px}
.why{font-size:13px;background:var(--why-bg);border-left:3px solid var(--why-line);padding:8px 12px;
     border-radius:0 6px 6px 0;margin-bottom:10px}
.why b{color:var(--ink)}
.sug{font-size:13px;background:var(--sug-bg);border-left:3px solid var(--sug-line);padding:8px 12px;
     border-radius:0 6px 6px 0;margin-bottom:10px}
.sug .lb{font-size:13px;font-weight:700;color:var(--ok);display:block;margin-bottom:3px}
.sug button{font:inherit;font-size:13px;border:1px solid var(--sug-line);background:var(--card);
            color:var(--ok);border-radius:6px;padding:2px 9px;cursor:pointer;margin-left:6px}
textarea{width:100%;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit;
         font-size:14px;resize:vertical;min-height:38px;background:var(--card);color:inherit}

/* 승인 영역 — 화면 아래에 붙어 늘 보인다 */
.bar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;
     padding:14px 18px;background:var(--card);border:1px solid var(--line);border-radius:10px;
     position:sticky;bottom:12px}
.bar .cnt{font-size:14px}
.bar .cnt b{color:var(--accent);font-size:20px}
.bar .need{color:var(--accent);font-size:13px;font-weight:700}
.btn{font:inherit;font-weight:700;font-size:14px;padding:10px 22px;border-radius:8px;border:none;
     cursor:pointer;background:var(--accent);color:#fff;white-space:nowrap}
.btn:disabled{background:var(--line);color:var(--sub);cursor:not-allowed}
.btn.ghost{background:var(--card);color:var(--sub);border:1px solid var(--line)}

/* 완료 / 진행중 — 기본 선택 없음. 고르지 않으면 승인이 막힌다 */
.st{display:flex;gap:6px;align-items:center;flex-wrap:wrap;font-size:13px;margin:0 0 10px}
.st label{border:1px solid var(--line);border-radius:999px;padding:3px 12px;cursor:pointer;
          display:inline-flex;gap:6px;align-items:center}
.st input[type=radio]{accent-color:var(--accent);margin:0}
.st label:has(input[value="완료"]:checked){border-color:var(--ok);background:var(--ok);color:#fff}
.st label:has(input[value="진행중"]:checked){border-color:var(--go);background:var(--go);color:#fff}
.st .lb{font-size:13px;font-weight:700;color:var(--sub)}
.item.need{border-color:var(--accent);box-shadow:0 0 0 2px var(--bad-soft)}
.item.need .st .lb{color:var(--accent)}
.item.need .st .lb::after{content:" — 골라야 승인됩니다"}

/* 이월 항목 묶음 — 접힌 채로 두고 본문 항목과 섞지 않는다 */
.carry .ci{border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-top:10px;background:var(--card)}
.carry .ci .t{font-weight:700;font-size:14px}
.carry .ci .m{font-size:13px;color:var(--sub);margin:3px 0 7px}
.carry .ci .c{font-size:13px;background:var(--why-bg);padding:7px 11px;border-radius:6px;margin-bottom:10px}

/* 수동 추가 */
.item.man{border-style:dashed}
.item .tin{flex:1;font:inherit;font-weight:700;font-size:16px;border:none;
           border-bottom:1px solid var(--line);padding:2px;background:none;color:inherit}
.item .tin:focus{outline:none;border-bottom-color:var(--accent)}
.item .min{width:100%;font:inherit;font-size:13px;color:var(--sub);border:none;
           border-bottom:1px dashed var(--line);padding:2px;margin-bottom:10px;background:none}
.item .min:focus{outline:none;border-bottom-color:var(--accent)}
.del{font:inherit;font-size:13px;background:none;border:1px solid var(--line);color:var(--sub);
     border-radius:6px;padding:3px 10px;cursor:pointer}
.del:hover{border-color:var(--accent);color:var(--accent)}

/* 확정 일지 */
.ent{border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:12px;background:var(--card);position:relative}
.ent .t{font-weight:700;font-size:14px}
.ent .m{font-size:13px;color:var(--sub);margin-bottom:5px}
.ent .c{font-size:13px;background:var(--why-bg);padding:8px 12px;border-radius:6px}
.ex{border:1px solid var(--line);border-radius:10px;padding:10px 16px;margin-bottom:10px;opacity:.75;position:relative}
.ex .t{font-size:13px}

/* 확정 일지의 추이 — 기본은 접고 「<태그> 추이 보기」로 편다(JS 없이). 일지는 읽는 문서라 펴 두면 길어진다.
   체크박스는 보이지 않되 초점은 받는다 */
.trbox{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}
.trbtn{display:inline-block;margin:8px 0 2px;padding:3px 12px;border:1px solid var(--line);border-radius:999px;
       font-size:12.5px;font-weight:700;color:var(--sub);background:var(--card);cursor:pointer}
.trbtn::before{content:"▸ "}
.trbox:checked+.trbtn::before{content:"▾ "}
.trbox:focus-visible+.trbtn{outline:2px solid var(--accent);outline-offset:2px}
.trwrap{display:none;margin-top:8px}
.trbox:checked~.trwrap{display:block}
"""

# 트렌드 호버. 목업(demo/index.html)의 move()/out() 을 서버 차트 좌표계로 옮겼다.
# viewBox 720×150 을 width:100% 로 그리므로 x 축 배율만 계산하면 된다(세로 비율 유지).
TREND_JS = """
function upCheck(f){
  var fs=f.querySelector('input[type=file]').files, m=f.querySelector('.upmsg');
  if(!fs.length){m.textContent='파일을 고르세요'; return false;}
  upSend(f, fs, m); return false;   // 폼 제출 대신 XHR — 진행률을 보이고, 끝날 때까지 화면 이동을 막는다
}
function upLock(on){
  document.body.classList.toggle('uploading', on);
  document.querySelectorAll('button, input[type=file]').forEach(function(b){
    if(on){ if(!b.disabled){ b.disabled=true; b.dataset.uplock='1'; } }
    else if(b.dataset.uplock){ b.disabled=false; delete b.dataset.uplock; }   // 원래 꺼져 있던 버튼(실행 중 생성 등)은 그대로
  });
  window.onbeforeunload = on ? function(){return '업로드 중입니다';} : null;
  window.__uploading = on;   // 실행 중 5초 새로고침이 업로드를 끊지 않게
}
async function upSend(f, fs, m){
  var max=+f.dataset.max, mb=function(n){return (n/1048576).toFixed(1)+'MB';};
  upLock(true);
  try{
    var fd=new FormData(), tot=0, raw=0, gz=(typeof CompressionStream!=='undefined');
    for(var i=0;i<fs.length;i++){
      var file=fs[i]; raw+=file.size;
      if(gz && file.size>1048576){
        m.textContent='압축 중… '+file.name+' ('+mb(file.size)+')';
        var blob=await new Response(file.stream().pipeThrough(new CompressionStream('gzip'))).blob();
        fd.append('files', blob, file.name+'.gz'); tot+=blob.size;
      }else{ fd.append('files', file, file.name); tot+=file.size; }
    }
    if(tot>max){ m.textContent='압축해도 '+mb(tot)+' — 한 번에 '+mb(max)+' 까지. 근무 하나씩 올리세요'; upLock(false); return; }
    var xhr=new XMLHttpRequest(); xhr.open('POST','/pipeline/upload');
    xhr.upload.onprogress=function(e){ if(e.lengthComputable) m.textContent='올리는 중 '+Math.round(100*e.loaded/e.total)+'% ('+mb(e.loaded)+'/'+mb(e.total)+(gz?' · 원본 '+mb(raw):'')+')'; };
    xhr.upload.onload=function(){ m.textContent='서버에서 푸는 중… (원본 '+mb(raw)+')'; };
    xhr.onload=function(){
      if(xhr.status<400){ window.onbeforeunload=null; location.replace('/admin'); return; }
      var d=new DOMParser().parseFromString(xhr.responseText,'text/html'), t=d.querySelector('.empty');
      m.textContent='실패 '+xhr.status+' — '+(t?t.textContent:''); upLock(false);
    };
    xhr.onerror=function(){ m.textContent='연결이 끊겼습니다. 다시 올려 주세요'; upLock(false); };
    xhr.send(fd);
  }catch(e){ m.textContent='실패 — '+e; upLock(false); }
}
document.querySelectorAll('.trend[data-trend]').forEach(function(box){
  // 근무 구간 전체 그래프의 호버. 1분 평균은 원본이 빈 곳에서 간격이 벌어지므로, 마우스 가로 위치에서 가장 가까운
  // 실제 점을 찾아 그 점의 시각·값을 띄운다(없는 값을 만들지 않는다). 값은 서버가 화면 표기(쉼표·지수 없음)로 보낸다.
  var d; try{ d=JSON.parse(box.getAttribute('data-trend')); }catch(e){ return; }
  var svg=box.querySelector('svg'), cur=svg.querySelector('.cur'), dot=svg.querySelector('.dot'), tip=box.querySelector('.tip');
  var n=d.m.length, t0=Date.parse(d.t0);
  if(n<2) return;
  function hm(ms){ var x=new Date(ms); return ('0'+x.getHours()).slice(-2)+':'+('0'+x.getMinutes()).slice(-2); }
  function near(mm){
    var lo=0, hi=n-1;
    while(hi-lo>1){ var mid=(lo+hi)>>1; if(d.m[mid]<mm) lo=mid; else hi=mid; }
    return (Math.abs(d.m[hi]-mm)<Math.abs(mm-d.m[lo]))?hi:lo;
  }
  function move(ev){
    var r=svg.getBoundingClientRect(), sc=r.width/720;
    var x=((ev.touches?ev.touches[0].clientX:ev.clientX)-r.left)/sc;
    if(x<d.L-4||x>d.L+d.pw+4){ out(); return; }
    var i=near((x-d.L)/d.pw*d.span_m), px=d.L+d.m[i]/d.span_m*d.pw;
    cur.setAttribute('x1',px); cur.setAttribute('x2',px); cur.style.display='';
    dot.setAttribute('cx',px); dot.setAttribute('cy',d.y[i]); dot.style.display='';
    tip.textContent=hm(t0+d.m[i]*60000)+'  '+d.v[i]+(d.unit?' '+d.unit:'');
    tip.style.left=(px*sc)+'px'; tip.style.display='';
  }
  function out(){ cur.style.display='none'; dot.style.display='none'; tip.style.display='none'; }
  svg.addEventListener('mousemove',move); svg.addEventListener('mouseleave',out);
  svg.addEventListener('touchstart',move,{passive:true}); svg.addEventListener('touchmove',move,{passive:true}); svg.addEventListener('touchend',out);
});
"""

SCRIPT = """
function tg(cb){cb.closest('.item').classList.toggle('off',!cb.checked);cnt()}
function cnt(){
  var b=document.querySelectorAll('.item:not(.man) input[type=checkbox]');
  var n=0;b.forEach(function(c){if(c.checked)n++});
  var el=document.getElementById('n');if(el)el.textContent=n;
}
function use(btn,txt){
  var ta=btn.closest('.body').querySelector('textarea');
  ta.value=txt;ta.focus();
}
var mi=0;
function addMan(){
  mi++;
  var d=document.createElement('div');
  d.className='item man';
  d.innerHTML='<div class="row1"><input type="checkbox" checked disabled>'+
    '<input class="tin" name="manual_title" placeholder="제목 — 예: 3번 압축기 소음 증가" required>'+
    '<button type="button" class="del" onclick="this.closest(\\'.item\\').remove()">삭제</button></div>'+
    '<div class="body" style="margin-top:6px">'+
    // 직접 추가도 상태가 있어야 한다. 라디오 이름은 항목마다 다르고, 서버에는 제목·내용과 같은 순서로 나란히 가는 숨은 칸으로 보낸다
    '<div class="st"><span class="lb">상태</span>'+
    '<label><input type="radio" name="mst_'+mi+'" value="완료" onchange="mst(this)">완료</label>'+
    '<label><input type="radio" name="mst_'+mi+'" value="진행중" onchange="mst(this)">진행중</label>'+
    '<input type="hidden" name="manual_status" value=""></div>'+
    '<textarea name="manual_body" '+
    'placeholder="내용 — 언제부터, 무엇을, 다음 근무가 무엇을 봐야 하는지"></textarea></div>';
  document.getElementById('mans').appendChild(d);
  d.querySelector('.tin').focus();
}
function mst(r){ r.closest('.st').querySelector('[name=manual_status]').value=r.value; r.closest('.item').classList.remove('need'); }
// 채택했는데 완료/진행중이 빈 항목이 하나라도 있으면 보내지 않고 그 항목을 짚는다.
// 서버도 같은 검사를 한다(approve.decide) — 여기는 왕복을 줄이는 것뿐이다.
function chk(f){
  var bad=[];
  f.querySelectorAll('.item:not(.man)').forEach(function(it){
    var cb=it.querySelector('input[type=checkbox][name=item]'); if(!cb||!cb.checked){it.classList.remove('need');return;}
    var on=it.querySelector('input[type=radio][name=status_'+cb.value+']:checked');
    it.classList.toggle('need',!on); if(!on)bad.push(it);
  });
  f.querySelectorAll('.item.man').forEach(function(it){
    var h=it.querySelector('[name=manual_status]'); var ok=h&&h.value;
    it.classList.toggle('need',!ok); if(!ok)bad.push(it);
  });
  var m=document.getElementById('need');
  if(bad.length){ if(m)m.textContent='완료/진행중이 빈 채택 항목 '+bad.length+'건'; bad[0].scrollIntoView({behavior:'smooth',block:'center'}); return false; }
  if(m)m.textContent=''; return true;
}
document.addEventListener('DOMContentLoaded',cnt);
"""


# 옛 화면 주소 → 새 화면. 공개 소개 페이지(docs/intro.html 「파이프라인 열기」)·발표 PC 북마크가 아직 옛 주소를 써서
# 404 가 됐다(조각 1 반증). /pipeline 의 기능은 관리로 갔지만 공개 링크로 관리가 새면 안 되므로 초안으로 보낸다.
OLD_PATHS = {"/": "/draft", "/pipeline": "/draft", "/rtdb": "/dcs"}


def page(title, body):
    """화면 한 장. 위에는 얇은 바(워드마크 · 화면 이름 · 엔진·AI 상태)만 두고 이동줄은 없다 —
    DCS 와 일지 목록은 각자 주소로 연다(경모님 2026-09-14). 돌아갈 길은 화면 안의 「‹ 일지 목록」이다."""
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · ENGRA</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%23EA002C'/><text x='16' y='23' font-size='19' font-family='sans-serif' font-weight='700' fill='white' text-anchor='middle'>E</text></svg>">
<style>{STYLE}</style>
<header class="top"><span class="brand">ENGRA</span><span class="ttl">{html.escape(title)}</span>
<span class="sys">엔진 {ports.engine_source()} · {llm.status()} · <span id="wallclock" class="mono"></span></span></header>
<script>(function(){{var e=document.getElementById('wallclock');
function z(n){{return (n<10?'0':'')+n}}
function t(){{var d=new Date();e.textContent=d.getFullYear()+'.'+z(d.getMonth()+1)+'.'+z(d.getDate())+' '+z(d.getHours())+':'+z(d.getMinutes())+':'+z(d.getSeconds())}}
t();setInterval(t,1000);}})();</script>
<main class="wrap">
{body}
</main><script>{SCRIPT}</script><script>{TREND_JS}</script></html>"""


def esc(v):
    return html.escape(str(v)) if v is not None else ""


def _span(ts):
    """2026-08-24T06:00:00 -> 08-24 06:00"""
    return (ts or "")[5:16].replace("T", " ")


_OLD_SEV = re.compile(r"^(\s*\d+\. )\[(?:상|중|하|-)\] ")


def _old_body(body):
    """이전 확정본 본문. 옛 기록의 「1. [상] 제목」 같은 중요도 표시만 뗀다(중요도는 화면에서 뺐다). 문장은 고쳐 쓰지 않는다."""
    return "\n".join(_OLD_SEV.sub(r"\1", ln) for ln in (body or "").split("\n"))


# --- 화면 -------------------------------------------------------------

ASU_FILE = DOCS_DIR / "asu_dcs_overview.html"

# 임도영님 원본을 그대로 서비스한다. 복사하지 않는 이유 — 그분이 고칠 때마다
# 사본이 뒤처지고, 태그 체계가 어긋나면 심사에서 화면과 데이터가 다르게 보인다.
# 해시(#ovw/#data/#scen)로 원하는 탭을 열도록 클릭 한 줄만 덧붙인다.
_ASU_TAB_JS = """
<script>
(function(){
  function open(){
    var h=(location.hash||'').replace('#','');
    var id={ovw:'tabOvw',data:'tabData',scen:'tabScen',rtdb:'tabRtdb'}[h];
    if(!id) return;
    var el=document.getElementById(id);
    if(el && el.click) el.click();
  }
  if(document.readyState==='complete') open();
  else window.addEventListener('load', open);
  window.addEventListener('hashchange', open);
})();
</script>"""


def view_asu():
    """설비 개요·태그 상세 원본 (docs/asu_dcs_overview.html)."""
    if not ASU_FILE.exists():
        return page("설비 화면 없음",
                    '<div class="card"><div class="empty">'
                    'docs/asu_dcs_overview.html 이 없습니다.</div></div>')
    return ASU_FILE.read_text(encoding="utf-8") + _ASU_TAB_JS


# /dcs — 개요 한 화면만 창 가득. ENGRA 상단바·이동줄·카드·설명문을 두르면 결선 심사에서 DCS 가 묻혔다.
# 원본은 고치지 않고 _ASU_TAB_JS 처럼 서빙 때 CSS 만 덧붙인다 — 원본 스크립트가 쓰는 id·클래스(hmi, hminav, viewOverview …)에만 기댄다.
# 원본이 #rtdb 해시로 탭을 직접 바꾸므로 개요 밖 화면은 !important 로 눌러 둔다. 세로 화면(폰)은 폭에 맞춰 위에 붙인다.
# 원본의 「모의 화면 · 가상 데이터」 안내(.disc)만은 가리지 않는다 — 공개 주소라 심사위원이 보는 화면에서 가상 데이터 표시를 빼지 않는다.
# 흐름도가 비어 있는 오른쪽 아래(범례 줄 위)에 작게 띄운다.
# 모든 규칙은 원본에 이 구조(.hmi 안 #viewOverview · .wrap 바로 아래 .disc)가 있을 때만 켠다($ON). 원본이 바뀌어 구조가 어긋나면
# 빈 화면이나 안내 누락이 조용히 나는 대신 원본이 그대로 보인다(조각 1 반증). 구조 자체는 test_dcs_source_has_the_structure_the_css_expects 가 본다.
_DCS_FULL_CSS = """
<style>
html$ON{background:#c8c9c4;height:100%;overflow:hidden}
body$ON{height:100%;overflow:hidden;visibility:hidden}
body$ON .hmi{visibility:visible;position:fixed;inset:0;display:flex;flex-direction:column;border:0;border-radius:0}
body$ON .hminav,body$ON #viewData,body$ON #viewScen,body$ON #viewRtdb{display:none!important}
body$ON #viewOverview{display:flex!important;flex-direction:column;flex:1;min-height:0}
body$ON #viewOverview svg.mimic{flex:1;min-height:0;height:0}
body$ON #sModal{visibility:visible}
body$ON .wrap>.disc{visibility:visible;position:fixed;right:12px;bottom:34px;z-index:5;margin:0;max-width:340px;padding:4px 9px;font-size:10.5px;line-height:1.45;opacity:.92}
@media (orientation:portrait){body$ON .hmi{bottom:auto}body$ON #viewOverview{flex:none}body$ON #viewOverview svg.mimic{flex:none;height:auto}body$ON .wrap>.disc{left:12px;max-width:none;bottom:12px}}
</style>""".replace("$ON", ":has(.hmi #viewOverview):has(.wrap>.disc)")


# 재생 중이면 흐름도 숫자를 근무 시각의 값으로 바꾼다. 원본(docs/asu_dcs_overview.html)은 고치지 않고 서빙 때 덧붙인다 —
# 원본이 태그마다 만드는 g[data-tag] 안의 값 글자(text.tv)만 건드린다. 재생이 없으면 값이 비어 원본 숫자가 그대로 남는다.
_DCS_LIVE_JS = """<script>(function(){
function set(){fetch('/api/live/values').then(function(r){return r.json()}).then(function(j){
 var v=j.values||{};
 Object.keys(v).forEach(function(t){
  var el=document.querySelector('g[data-tag="'+t+'"] text.tv'); if(el) el.textContent=v[t];
 });
 setTimeout(set,2000);
}).catch(function(){setTimeout(set,5000);});}
setTimeout(set,1500);})();</script>"""


def view_dcs():
    """DCS 개요 — 원본 파일 그대로 + 전체 화면 CSS + 재생 중 숫자 갱신."""
    if not ASU_FILE.exists():
        return view_asu()
    return ASU_FILE.read_text(encoding="utf-8") + _DCS_FULL_CSS + _DCS_LIVE_JS + _DCS_CLOCK


# DCS 화면은 원본 파일 그대로라 머리말이 없다 — 현재 시각과 재생 시각을 오른쪽 위에 얹는다.
# 「서버에서 실제로 돌고 있다」가 이 화면에서도 보여야 한다(경모님 2026-09-15).
_DCS_CLOCK = """<div id="clockbox" style="position:fixed;top:10px;right:14px;z-index:99;text-align:right;
 font:600 12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:#5f6672;background:rgba(255,255,255,.88);
 border:1px solid #e4e1dc;border-radius:8px;padding:5px 9px">
<div id="wallclock"></div><div class="rclock" style="color:#0b62c4"></div></div>
<script>(function(){
function z(n){return (n<10?'0':'')+n}
function stamp(ms){var d=new Date(ms);
 return d.getFullYear()+'.'+z(d.getMonth()+1)+'.'+z(d.getDate())+' '+z(d.getHours())+':'+z(d.getMinutes())+':'+z(d.getSeconds())}
var w=document.getElementById('wallclock'), r=document.querySelector('#clockbox .rclock');
var rc={ms:null,speed:1,run:false,at:0};
setInterval(function(){
 w.textContent=stamp(Date.now());
 r.textContent=(rc.ms===null)?'':('재생 '+stamp(rc.ms+(rc.run?(Date.now()-rc.at)*rc.speed:0))+(rc.run?'':' (멈춤)'));
},250);
function pull(){fetch('/api/live').then(function(x){return x.json()}).then(function(j){
 var s=j.status||{};
 if(s.clock){rc.ms=Date.parse(s.clock);rc.speed=s.speed||1;rc.run=(s.phase==='running');rc.at=Date.now()}
 else {rc.ms=null}
 setTimeout(pull,2000);
}).catch(function(){setTimeout(pull,5000)})}
pull();})();</script>"""


def _score_html():
    """정답지 대조 — 정답지 파일이 여러 개라도 표는 하나. 경모님 지적(2026-08-27): "왜 기준/sim 이 나눠져 있고,
    검출이벤트·탐지·주입·오탐이 무슨 뜻인지 모르겠고 합이 안 맞는다". 합이 맞게 보이는 두 축으로 나눈다:
      정답 쪽: 심은 이상 = 잡음 + 놓침 / 엔진 쪽: 낸 이벤트 = 정답 맞춤 + 오탐.
    한 이상을 여러 이벤트가 잡을 수 있어 '잡음'(이상 수) 과 '정답 맞춤'(이벤트 수) 은 다른 숫자다."""
    b = jobs.scoreboard()
    rows, errs = [], []
    T = {"inj": 0, "hit": 0, "ev": 0, "fp": 0}
    cov_inj, cov_hit, missed = set(), set(), []
    if b is None:
        b = {"per_shift": [], "cov_inj": [], "cov_hit": [], "missed": [], "per_shift_trk": {}}
    if b.get("error"):
        errs.append('<div class="note">채점 실패 — ' + esc(b["error"]) + '</div>'); b = {"per_shift": [], "cov_inj": [], "cov_hit": [], "missed": [], "per_shift_trk": {}}
    cov_inj.update(b["cov_inj"]); cov_hit.update(b["cov_hit"]); missed.extend(b["missed"])
    # 사후 대조 — 정답지 파일이 그 근무의 초안 생성 시각보다 뒤에 올라왔으면 "검출·초안이 정답지를 볼 수 없었다" 는 근거가 된다
    from datetime import datetime
    gen_at = {}
    with db.connect() as conn:
        for sid in jobs.KEYS:
            d0 = db.load_draft(conn, sid)
            if d0 and d0.get("generated_at"):
                try:
                    gen_at[sid] = datetime.fromisoformat(d0["generated_at"]).timestamp()
                except ValueError:
                    pass
    def when(sid):
        k = jobs.KEYS.get(sid, {})
        if sid not in gen_at or not k.get("uploaded_at"):
            return '<td class="muted">—</td>'
        return ('<td style="color:var(--ok, #2a7)">사후 ✓</td>' if k["uploaded_at"] > gen_at[sid]
                else '<td class="muted" title="초안 전에 올라온 정답지 — 검출은 정답지를 읽지 않지만, 순서로는 증명되지 않는다">사전</td>')
    for sid, ev, inj, hit, fp in b["per_shift"]:
        link = '<a href="/answer/' + esc(sid) + '">' + esc(sid) + '</a>'
        src = esc(jobs.KEYS.get(sid, {}).get("key_file", ""))
        if ev is None:
            rows.append((sid, '<tr><td class="mono">' + link + '</td><td class="muted mono" style="font-size:11px">' + src + '</td>' + when(sid) + '<td>' + str(inj) + '</td>'
                         '<td class="muted" colspan="6">아직 안 돌림 — 위 카드에서 「검출 + AI 초안」을 돌리면 채점</td></tr>'))
            continue
        T["inj"] += inj; T["hit"] += hit; T["ev"] += ev; T["fp"] += fp
        miss = inj - hit
        trk = (b.get("per_shift_trk") or {}).get(sid, 0); T["trk"] = T.get("trk", 0) + trk
        rows.append((sid, '<tr><td class="mono">' + link + '</td><td class="muted mono" style="font-size:11px">' + src + '</td>' + when(sid) +
                     '<td>' + str(inj) + '</td><td><b>' + str(hit) + '</b></td><td' + (' style="color:var(--bad)"' if miss else '') + '>' + str(miss) + '</td><td class="muted">' + (str(trk) if trk else '') + '</td>'
                     '<td>' + str(ev) + '</td><td>' + str(ev - fp) + '</td><td' + (' style="color:var(--bad)"' if fp else '') + '>' + str(fp) + '</td></tr>'))
    rows.sort(key=lambda r: r[0])
    if T["inj"]:
        pct = 100 * T["hit"] / T["inj"]
        head = ('<b>주입한 이상 ' + str(T["inj"]) + '건 중 ' + str(T["hit"]) + '건 탐지 성공 (' + f'{pct:.0f}' + '%) · 시나리오 '
                + str(len(cov_hit)) + '/' + str(len(cov_inj)) + '종 · 잘못 잡음(오탐) ' + str(T["fp"]) + '건</b>')
        total = ('<tr style="border-top:2px solid var(--line);font-weight:700"><td>합계</td><td></td><td></td><td>' + str(T["inj"]) + '</td><td>' + str(T["hit"]) + '</td><td>'
                 + str(T["inj"] - T["hit"]) + '</td><td class="muted">' + (str(T.get("trk", 0)) or '') + '</td><td>' + str(T["ev"]) + '</td><td>' + str(T["ev"] - T["fp"]) + '</td><td>' + str(T["fp"]) + '</td></tr>')
    else:
        head = '<span class="muted">' + ('아직 올린 정답지가 없습니다 — 초안을 만든 뒤 그 근무의 asu_answer_*.json 을 올리면 여기서 대조합니다' if not rows else '아직 돌린 근무가 없습니다 — 「검출 + AI 초안」을 돌리면 여기서 바로 채점됩니다') + '</span>'; total = ''
    legend = ('<details style="margin:10px 0 0"><summary style="cursor:pointer;font-size:13px;color:var(--sub)">읽는 법</summary>'
              '<p class="note" style="margin:8px 0 0">'
              '<b>읽는 법</b> — <b>주입한 이상</b>: 정답지가 이 근무 데이터에 넣어 둔 이상 상황 수. <b>탐지 성공</b>: 그중 검출 이벤트가 같은 태그·같은 시간대에 하나라도 있는 것. '
              '<b>놓침</b> = 주입한 이상 − 탐지 성공. <b>추적 중</b>: 근무 끝까지 이어지는 이상을 이 근무에서 못 잡은 것 — 다음 근무(이어받은 항목)에서 판정하므로 놓침도 주입 수에도 넣지 않는다. <b>총 검출 수</b>: 엔진이 "이상이다" 하고 낸 이벤트 수 = <b>맞게 잡음</b>(정답지에 있는 이상을 가리킨 이벤트) + <b>잘못 잡음</b>(정답지에 없는데 이상이라고 한 이벤트 = 오탐). '
              '한 이상을 여러 이벤트가 잡을 수 있어 탐지 성공(이상 수)과 맞게 잡음(이벤트 수)은 다른 숫자다. <b>대조 시점</b>: 정답지 파일이 초안 생성 뒤에 올라왔으면 「사후 ✓」 — 검출·초안이 정답지를 볼 수 없었다는 순서 근거. 근무를 누르면 이상별로 무엇을 잡고 놓쳤는지 보인다.</p></details>')
    missed_html = ''
    if missed:
        missed_html = ('<p class="note" style="margin-top:8px"><b>놓친 이상</b></p><ul style="margin:4px 0 0 18px;font-size:12.5px">'
                       + "".join('<li>' + esc(sid) + ' #' + str(no) + ' ' + esc(name) + ' <span class="mono">' + esc(tag) + '</span> ' + esc(s_) + '~' + esc(e_) + '</li>'
                                 for sid, no, name, tag, s_, e_ in sorted(missed)) + '</ul>')
    return ('<div class="card">'
            '<h2 style="font-size:15px">정답지 대조 <span class="muted" style="font-weight:400;font-size:12px"><b>검출·초안 생성은 정답지를 읽지 않습니다</b> — 정답지는 여기 채점에만 쓰입니다</span></h2>'
            '<p class="note" style="margin:4px 0 8px">' + head + '</p>'
            '<div style="overflow-x:auto"><table class="sc"><tr><th rowspan="2">근무</th><th rowspan="2">정답지</th><th rowspan="2" title="정답지가 초안 생성 뒤에 올라왔으면 사후">대조 시점</th><th colspan="4" style="text-align:center">정답지가 주입한 이상</th><th colspan="3" style="text-align:center">엔진이 검출한 이벤트</th></tr>'
            '<tr><th>주입한 이상</th><th>탐지 성공</th><th>놓침</th><th title="근무 끝까지 이어지는 이상 — 다음 근무에서 판정">추적 중</th><th>총 검출 수</th><th>맞게 잡음</th><th>잘못 잡음(오탐)</th></tr>'
            + "".join(r for _, r in rows) + total + '</table></div>' + legend + missed_html + "".join(errs) + '</div>')


def _reset_bar():
    return ('<form method="post" action="/reset" style="display:flex;gap:8px;align-items:center;margin:0 0 12px">'
            '<span class="muted" style="font-size:12px">처음부터 —</span>'
            '<button class="btn" name="empty" value="1" style="padding:6px 12px;font-size:12px;background:var(--sub)" '
            "onclick=\"if(!window.confirm('완전 빈 상태로 되돌립니다. 근무·초안·확정 이력이 전부 지워집니다.')){return false;} this.form.sure.value='1'\">완전 빈 상태로</button>"
            '<input type="hidden" name="sure" value="0">'
            '<span class="muted" style="font-size:11.5px">· 매시 정각에 자동으로 비워집니다(최근 2시간 안에 눌렀으면 건너뜀)</span></form>')


def _upload_result_html():
    up = jobs.last_upload()
    if not up["files"]:
        return ""
    return ('<div class="note" style="margin:0 0 10px;font-size:12.5px"><b>업로드 결과</b> — 받은 파일: ' + esc(", ".join(up["files"]))
            + (' · 정답지 근무: <b>' + esc(", ".join(up["added"])) + '</b>' if up["added"] else '')
            + "".join('<br><span class="muted">' + esc(w) + '</span>' for w in up.get("info", []))
            + "".join('<br><span style="color:var(--accent)">⚠ ' + esc(w) + '</span>' for w in up["warn"]) + '</div>')


_ai_check = (None, "")   # 마지막 「AI 연결 점검」 결과 (성공 여부, 설명)


def _admin_key():
    """심사 기간에 공개 URL 의 관리 동작(리셋·다시 만들기·정답지 업로드 등)을 아무나 누르지 못하게 하는 열쇠.
    환경 변수 ENGRA_ADMIN_KEY 가 비어 있으면 잠금 없음(QA 기간). 09-02 심사 세팅 때 켠다 (경모님 2026-08-28)."""
    import os
    return (os.environ.get("ENGRA_ADMIN_KEY") or "").strip()


def _admin_ok(handler):
    key = _admin_key()
    if not key:
        return True
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "engra_admin" and v == key:
            return True
    return False


def _admin_gate_html(locked):
    if not _admin_key():
        return ""
    if locked:
        return ('<div class="card" style="padding:12px 18px;border-left:3px solid var(--accent)"><b>관리 동작 잠김</b> — 심사 기간에는 열쇠를 넣어야 리셋·다시 만들기·정답지 업로드가 됩니다(보기는 자유). '
                '<form method="post" action="/admin/unlock" style="display:inline-flex;gap:6px;margin-left:8px"><input type="password" name="key" placeholder="열쇠" style="font-size:12.5px;padding:4px 8px">'
                '<button class="btn" style="padding:5px 10px;font-size:12.5px">열기</button></form></div>')
    return '<div class="note muted" style="font-size:12px">관리 동작 열쇠 확인됨 — 이 브라우저에서만 유효</div>'


def view_admin(handler=None):
    """관리 — 검증·리셋·시연용 컨트롤을 한곳에. 경모님(2026-08-27): "관리 버튼이 여기저기 있으면 기존 로직인지
    관리·시연용인지 구분이 안 된다. 운영 페이지엔 실제 동작만." 그래서 운영 화면에서 이쪽으로 옮겼다.
    운영 화면(DCS · 초안)은 여기를 잇지 않는다 — 주소를 아는 사람만 들어온다."""
    locked = not _admin_ok(handler) if handler is not None else False
    body = (_admin_gate_html(locked) + _pipeline_card(locked) + _live_card(locked) + _score_html()
            + '<div class="card"><h2 style="font-size:15px">AI 계층 — 무엇을 하고, 무엇을 기준으로</h2>'
            '<p class="note" style="margin:0 0 6px">검출·묶음·수치는 전부 통계 엔진(<span class="mono">engine/</span>)이 한다. AI(<span class="mono">app/llm.py</span>)는 그 결과 위에서 네 가지만 한다 — 숫자를 만들지 않고, <b>조치를 지어내지 않는다</b>.</p>'
            '<details style="margin:0 0 8px"><summary style="cursor:pointer;font-size:13px;color:var(--sub)">네 가지 — 펼쳐 보기</summary>'
            '<ol style="margin:8px 0 0 18px">'
            '<li><b>서술</b> — 엔진의 근거 수치(정상범위 폭 대비 크기, 기울기, 한계 대비, 지속 시간)를 근무자 말투의 문장으로. 근거에 있는 숫자만 쓴다.</li>'
            '<li><b>중요도 재판정</b> — 통계 크기가 매긴 상/중/하를 "놓치면 무엇이 일어나는가"(품질·안전·설비 직결 / 손실·비효율 / 후속 영향 작음)로 다시 매기고 이유를 쓴다. 판정은 기록으로 남기지만 근무 화면에는 보이지 않는다 — 무엇을 넘길지는 근무자가 고른다.</li>'
            '<li><b>전달 가치</b> — 외기(TI-101·MI-102) 하루 주기와 그에 따라 함께 움직인 완만한 변화는 정상 운전으로 보고 「전달 가치 낮음」. 한계 접근·다른 이상과 겹침이면 전달. 갈리면 전달(놓치는 쪽이 비싸다).</li>'
            '<li><b>사례 적합성 · 연관</b> — 같은 태그의 확정 일지 중 이번 현상에 맞는 것만 남기고(기각 사유 기록), 태그 마스터 연결이 놓친 인과(예: 순도 하강 ↔ Cold end 온도)를 「함께 봐야 할 항목」으로 잇는다. 근거 없으면 잇지 않는다.</li></ol></details>'
            '<form method="post" action="/admin/ai_check" style="margin:6px 0 10px"><button class="btn" style="background:var(--sub);padding:6px 12px;font-size:12.5px">AI 연결 점검 — 실제로 한 번 호출(수 초)</button>'
            + (('<span class="pill' + (' on' if _ai_check[0] else ' red') + '" style="margin-left:8px">' + esc(_ai_check[1]) + '</span>') if _ai_check[1] else '') + '</form>'
            '<p class="note muted" style="margin:0;font-size:12px">AI 가 아는 것 = 공기분리장치 공정 일반 지식 + 태그 마스터 설명 + 이번 근무의 근거 수치 + 과거 확정 일지. 이 공장의 절차·이력은 모른다 — 그래서 조치는 확정 일지에서만 오고, 원본 결측이 있으면 그 사실을 받아 신뢰도를 낮게 적는다. 기준 원문: <span class="mono">app/llm.py SYSTEM</span>.</p></div>'
            + '<div class="card"><h2 style="font-size:15px">처음부터</h2>'
            '<p class="note" style="margin:0 0 8px">근무·초안·확정 이력·정답지 대조가 전부 지워진다. 빈 상태에서 한 근무를 돌리면 과거 조치가 없고, 두 번째 근무부터 앞 근무의 확정 코멘트가 회수되는 것을 볼 수 있다. '
            '매시 정각에도 자동으로 비워진다(최근 2시간 안에 화면에서 실행·리셋을 눌렀으면 건너뜀).</p>' + _reset_bar() + '</div>')
    return page("관리", body)


def view_answer(shift_id):
    """정답지 뷰 — 이 근무에 무엇을 심었고, 검출기가 무엇을 잡았고 무엇을 놓쳤고 무엇이 오탐인지.

    경모님 지적(2026-08-27): "정답지에 어떤 게 들어있는지 보여줘야 하고, 어떤 케이스가 어떤 문제였는지
    날짜별·CSV별로 조회가 다 돼야 한다." 대조표의 숫자 한 줄을 여기서 펼친다.
    """
    sh = jobs.KEYS.get(shift_id)
    if not sh:
        return page("없음", '<div class="card"><div class="empty">그 근무의 정답지가 없습니다 — 관리에서 생성기의 asu_answer_*.json 을 올리면 여기서 볼 수 있습니다.</div></div>')
    r = jobs.score_mod.score_shifts([sh])
    sh = next(x for x in r["shift_list"] if x["shift_id"] == shift_id)
    d = r["detail"].get(shift_id)
    kind = "주간" if sh.get("kind") == "day" else "야간"
    head = (f'<a class="back" href="/admin">‹ 관리로</a>'
            f'<div class="card"><h2>정답지 — {esc(shift_id)} ({kind})</h2>'
            f'<p class="note" style="margin:4px 0">파일 <span class="mono">{esc((jobs.csv_for(shift_id) or Path("—")).name)}</span> · 정답지 <span class="mono">{esc(sh.get("key_file", ""))}</span> · {esc(sh["from"][11:16])} ~ {esc(sh["to"][11:16])} · '
            f'태그 {sh.get("tag_count","?")}점 · {sh.get("rows",0):,}행 · 주입 <b>{len(sh["injected"])}건</b>'
            + (f' · 동시 발생 {len(sh.get("overlaps") or [])}건' if sh.get("overlaps") else "") + '</p>')
    if not d:
        head += ('<p class="note"><b>아직 이 근무를 돌리지 않았습니다.</b> 아래는 심어둔 시나리오만 보입니다. '
                 '관리에서 이 근무를 돌리면 검출 결과와 대조가 붙습니다.</p>')
    else:
        hit = sum(1 for i in d["injected"] if i["hit"]); tot = len(d["injected"])
        head += (f'<p class="note"><b>대조 결과 — 주입 {tot}건 중 {hit}건 잡음 · 오탐 {len(d["fp"])}건</b> '
                 f'<span class="muted">(잡음 = 주입 구간과 시간이 겹치는 이벤트가 영향 태그 중 하나에서 나옴)</span></p>')
    head += '</div>'
    rows = []
    items = d["injected"] if d else [{**i, "hit": None, "matched": []} for i in sh["injected"]]
    for inj in items:
        st = ('<span class="pill on">잡음</span>' if inj["hit"] else
              ('<span class="pill amber" title="근무 끝까지 이어지는 이상 — 다음 근무에서 판정">추적 중 — 다음 근무로</span>' if inj.get("status") == "tracking" else
               ('<span class="pill red">놓침</span>' if inj["hit"] is False else '<span class="pill">미실행</span>')))
        alarm = esc(str(inj.get("dcs_alarm", "")))
        flags = []
        if inj.get("carried_in"): flags.append("앞 근무에서 이어짐")
        if inj.get("continues_next"): flags.append("다음 근무로 이어짐")
        matched = "".join(f'<li><span class="mono">{esc(m["tag"])}</span> {esc(m["kind"])} {esc(m["start"])}~{esc(m["end"])} <span class="muted">— {esc(m["evidence"] or "")[:90]}</span></li>'
                          for m in inj.get("matched", []))
        flags_html = ('<span class="muted" style="font-size:12px">· ' + " · ".join(flags) + '</span>') if flags else ""
        matched_html = ('<div style="margin-top:6px;font-size:12.5px"><b>잡은 이벤트</b><ul style="margin:4px 0 0 18px">' + matched + '</ul></div>') if matched else ""
        rows.append(f'''<div class="card">
<div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap"><b>#{inj["scenario_id"]} {esc(inj["name"])}</b> {st}
<span class="mono muted" style="font-size:12px">{esc(inj["trigger_tag"])} · {esc(inj["start"][11:16])}~{esc(inj["end"][11:16])} · {inj.get("minutes","?")}분</span>
<span class="muted" style="font-size:12px">알람 {alarm}</span>{flags_html}</div>
<p class="note" style="margin:6px 0">{esc(inj.get("description",""))}</p>
<div class="muted" style="font-size:12px">영향 태그: <span class="mono">{esc(", ".join(inj.get("affected_tags", [])))}</span></div>
{matched_html}
</div>''')
    fp_html = ""
    if d and d["fp"]:
        fp_html = ('<div class="card"><h2 style="font-size:15px">오탐 — 어느 주입과도 겹치지 않는 검출 ' + str(len(d["fp"])) + '건</h2>'
                   '<ul style="margin:6px 0 0 18px;font-size:12.5px">'
                   + "".join(f'<li><span class="mono">{esc(f["tag"])}</span> {esc(f["kind"])} {esc(f["start"])}~{esc(f["end"])} <span class="muted">— {esc(f["evidence"] or "")[:90]}</span></li>' for f in d["fp"])
                   + '</ul><p class="note" style="margin-top:8px">오탐이 전부 12시간 전구간 드리프트면 외기 일주기 오인이다 (#13). 단일 태그 전구간 드리프트가 정상인지는 정답지가 아니라 현장 판단이다.</p></div>')
    ov_html = ""
    if sh.get("overlaps"):
        ov_html = ('<div class="card"><h2 style="font-size:15px">동시 발생</h2><ul style="margin:6px 0 0 18px;font-size:12.5px">'
                   + "".join(f'<li>#{o["a"]} × #{o["b"]} — <span class="mono">{esc(", ".join(o.get("tags", [])))}</span> {esc(o["from"][11:16])}~{esc(o["to"][11:16])} ({o.get("minutes","?")}분)</li>' for o in sh["overlaps"])
                   + '</ul><p class="note" style="margin-top:6px">겹친 구간에서는 한 시나리오의 알람이 다른 시나리오 때문일 수 있다. 어느 쪽에 붙어도 탐지로 인정한다.</p></div>')
    return page(f"정답지 {shift_id}", head + "".join(rows) + fp_html + ov_html)


def _pipeline_card(locked):
    """근무 올리기 → 검출 + AI 초안 → 진행을 한 카드에. 「확정돼 있어도 다시 만들기」는 실행의 선택지다.

    /pipeline 화면(CSV 업로드·실행·진행)과 관리의 정답지 업로드·다시 만들기가 같은 폼을 두 번 보였다.
    결선에서 /pipeline 을 없애며 관리 한 카드로 합쳤다. 정답지 JSON·다시 만들기의 열쇠는 서버가 따로 본다."""
    st = jobs.state()
    log = "\n".join(esc(x) for x in st["lines"][-40:])
    running = st["running"]
    ingested = (st.get("result") or {}).get("ingested") if isinstance(st.get("result"), dict) else None
    cur = st.get("shift_id") or (ingested[-1] if ingested else None)   # 새로고침마다 1번으로 돌아가던 것 (경모님 QA)
    opts = "".join('<option value="' + esc(s["shift_id"]) + '"' + (' selected' if s["shift_id"] == cur else '') + '>' + esc(s["shift_id"]) + ' · '
                   + ("확정" if s["status"] == "confirmed" else ("초안 있음" if s["status"] == "pending" else "적재됨")) + '</option>' for s in jobs.sources())
    links = "".join('<a href="/answer/' + esc(sid) + '" class="pill" style="text-decoration:none;font-size:11.5px">' + esc(sid) + '</a> ' for sid in sorted(jobs.KEYS))
    skip_html = "".join(
        '<form method="post" action="/pipeline/ingest_skip" style="display:inline-block;margin:4px 8px 8px 0"><input type="hidden" name="file" value="' + esc(fn) + '">'
        '<button class="btn" style="background:var(--sub);padding:6px 12px;font-size:12.5px" onclick="return window.confirm(\'깨진 행을 건너뛰고 적재합니다. 건너뛴 사실은 근무 품질 기록으로 남아 초안과 AI 판단에 들어갑니다.\')">'
        + esc(fn) + ' — 깨진 행을 건너뛰고 적재</button></form>' for fn in (st.get("can_skip") or []) if not st["running"])
    adv_html = ('<div class="note" style="margin:6px 0;border-left:3px solid var(--accent);padding-left:10px">' + esc(st["advice"]) + '</div>') if st.get("advice") and not st["running"] else ""
    err = ('<pre class="mono" style="color:var(--accent);white-space:pre-wrap;font-size:11.5px">' + esc(st["error"]) + '</pre>' + adv_html + skip_html) if st.get("error") else ""
    status = ("실행 중 · " + esc(st["step"] or "")) if running else "대기"
    link = ""
    lr = st.get("last_run") or {}
    if not running and lr.get("shift_id"):   # 뒤이어 적재가 돌았어도 마지막 실행의 링크는 남는다
        link = ('<p class="note"><a href="/shift/' + esc(lr["shift_id"]) + '"><b>→ 초안 검토로 (' + esc(lr["shift_id"]) + ')</b></a>'
                ' — 채택·제외·상태·코멘트 후 승인하면 아래 대조표가 갱신됩니다.</p>')
    hint = ('<span class="muted" style="font-size:12px">진행은 자동 갱신 · <span id="jobelapsed"></span></span>'
            '<a id="jobdone" href="/admin" class="pill" style="display:none">완료 — 결과 보기</a>') if running else ""
    # 새로고침이 아니라 /api/job 폴링으로 로그·경과만 바꾼다 — 새로고침은 파일 선택을 지우고 업로드를 끊었다 (경모님 QA)
    reload_js = ('<script>(function(){function tick(){fetch("/api/job").then(function(r){return r.json()}).then(function(j){'
                 'var pre=document.getElementById("joblog");if(pre)pre.textContent=(j.lines||[]).slice(-40).join("\\n")||"여기에 진행이 표시됩니다.";'
                 'var el=document.getElementById("jobelapsed");if(el&&j.elapsed!=null){var s=Math.floor(j.elapsed);el.textContent="경과 "+Math.floor(s/60)+"분 "+(s%60)+"초";}'
                 'if(!j.running){var f=document.querySelector("input[type=file]");'
                 'if(!window.__uploading&&!(f&&f.files.length)){location.reload();return;}'
                 'var p=document.getElementById("jobpill");if(p){p.textContent="완료";p.classList.remove("on");}var d=document.getElementById("jobdone");if(d)d.style.display="inline";return;}'
                 'setTimeout(tick,3000);}).catch(function(){setTimeout(tick,5000);});}setTimeout(tick,3000);})();</script>') if running else ""
    lock = ' disabled title="열쇠 필요"' if locked else ''
    return ('<div class="card"><h2>관리 — 근무 올리기 · 검출 + AI 초안 <span class="muted" style="font-weight:400;font-size:12px">시연·QA 용. 운영 화면(DCS · 초안)에는 없는 기능만 모았다</span></h2>'
            '<p class="note" style="margin:0 0 12px">생성기(<a href="/asu#rtdb">RTDB 탭</a>)에서 받은 근무 CSV 를 올리고 「검출 + AI 초안」을 누르면 초안 화면에서 승인·확정한다. '
            '정답지(asu_answer_*.json)를 함께 올리면 아래 대조표에서 채점한다 — 초안을 먼저 만든 뒤 올리면 「사후 ✓」.</p>'
            '<div class="muted" style="font-size:12px;margin:6px 0 2px"><b>파일 올리기</b></div>'
            f'<form method="post" action="/pipeline/upload" enctype="multipart/form-data" onsubmit="return upCheck(this)" data-max="{Handler.MAX_UPLOAD}" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">'
            '<input type="file" name="files" multiple accept=".csv,.json" style="font-size:12.5px">'
            '<button class="btn" style="background:var(--sub);padding:6px 12px;font-size:12.5px">업로드</button>'
            '<span class="muted" style="font-size:11.5px">근무 CSV · 정답지 JSON, 여러 개 가능. CSV 는 올리면 바로 적재된다' + (' · 정답지는 열쇠가 필요' if locked else '') + '</span>'
            '<span class="upmsg" style="font-size:12px;color:var(--bad)"></span></form>' + _upload_result_html()
            + (('<div style="margin:0 0 12px;font-size:12px;color:var(--sub)">정답지 보기 — 무엇을 심었고 무엇을 잡았나: ' + links + '</div>') if jobs.KEYS else '')
            + '<div class="muted" style="font-size:12px;margin:12px 0 2px"><b>검출 + AI 초안</b>' + ('' if opts else ' <span style="color:var(--bad)">— 먼저 파일을 올리세요</span>') + '</div>'
            '<form method="post" action="/pipeline/run" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px">'
            '<select name="shift_id" class="pill" style="font-size:13px;padding:6px 10px;min-width:300px">' + opts + '</select>'
            '<label style="font-size:12.5px;display:inline-flex;gap:5px;align-items:center"><input type="checkbox" name="redo" value="1"' + lock + '>확정돼 있어도 다시 만들기</label>'
            '<button class="btn"' + (' disabled' if (running or not opts) else '') + ' onclick="var f=this.form, o=f.shift_id.selectedOptions[0].text;'
            ' if(f.redo.checked) return window.confirm(\'이 근무의 확정 일지와 초안을 지우고 다시 만듭니다. 계속할까요?\');'
            ' if(o.indexOf(\'초안 있음\')>=0) return window.confirm(\'이 근무는 검토 중인 초안이 있습니다. 지금 초안을 지우고 새로 만듭니다. 계속할까요?\');">적재 + 검출 + AI 초안 생성</button></form>'
            '<p class="note" style="margin:0 0 10px;font-size:12px">「확정돼 있어도 다시 만들기」 — 확정 일지와 초안을 지우고 처음부터 다시 검출·초안(확정 일지는 이력에 남지 않는다). 원본이 정리됐으면 올린 CSV 에서 다시 적재한다.</p>'
            '<div style="display:flex;gap:10px;align-items:center;margin:0 0 4px">'
            '<span id="jobpill" class="pill' + (' on' if running else '') + '">' + status + '</span>'
            '<span class="muted" style="font-size:12px">' + esc(st["shift_id"] or "") + '</span>' + hint + '</div>'
            '<pre id="joblog" class="mono" style="background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px;min-height:60px;max-height:280px;overflow:auto;font-size:11.5px;white-space:pre-wrap">'
            + (log or "여기에 진행이 표시됩니다.") + '</pre>' + err + link + '</div>'
            + reload_js)


LIVE_MAX_SPEED = 300     # 이 기계에서 12시간 창 틱 한 번이 1초 남짓 — 그보다 높이면 재생이 밀린다(지휘자 실측: 600배속 최대 지연 27초)
LIVE_LATE_SEC = 5        # 이만큼 밀리면 상태 줄에 드러낸다
_LIVE_PHASE = {"idle": "대기", "preparing": "준비 중", "running": "재생 중", "ended": "재생 끝", "stopped": "정지됨", "failed": "실패"}


def _clock_text(iso):
    """재생 중인 근무 시각 — 경모님이 정한 형식 2026.09.21 19:43:34. 초가 없으면 00 으로 채운다."""
    t = str(iso or "")
    if len(t) < 16:
        return ""
    return f"{t[0:4]}.{t[5:7]}.{t[8:10]} {t[11:16]}:{t[17:19] or '00'}"


def _live_line(st):
    """재생 상태 한 줄 — 관리 화면과 /api/live 가 같은 글자를 쓴다."""
    parts = [_LIVE_PHASE.get(st["phase"], st["phase"])]
    if st.get("shift_id"):
        c = st.get("counts") or {}
        parts.append(st["shift_id"])
        if st.get("clock"):
            parts.append("근무 시계 " + _clock_text(st["clock"]))
        parts.append(f"관찰 중 {c.get('observing', 0)} · 서술 중 {c.get('writing', 0)} · 선택 가능 {c.get('ready', 0)}"
                     + (f" · AI 서술 실패 {c['ai_failed']}" if c.get("ai_failed") else ""))
    late = (st.get("tick") or {}).get("late_sec") or 0
    if late >= LIVE_LATE_SEC:      # 틱 한 번이 1초 남짓이라 배속을 높이면 밀린다 — 밀리는 중임을 드러낸다
        parts.append(f"따라잡는 중(늦음 {late:.0f}초)")
    parts += [str(st[k]) for k in ("window_note", "phase_note", "error") if st.get(k)]
    return " · ".join(parts)


_LIVE_POLL_JS = ("<script>(function(){var el=document.getElementById('livestate');if(!el)return;"
                 "function tick(){fetch('/api/live').then(function(r){return r.json()}).then(function(j){el.textContent=j.line;setTimeout(tick,2000);})"
                 ".catch(function(){setTimeout(tick,5000);});}setTimeout(tick,2000);})();</script>")


def _live_failed_html(locked):
    """AI 서술이 끝내 실패한 항목 — 조용히 넘기지 않는다. 초안에 넣지 않고 여기에 드러내며, 남아 있는 동안 승인이 잠긴다.
    다시 시도는 그 항목 하나만 다시 쓴다(app/live.py retry)."""
    failed = [v for v in (live.items() or []) if v.get("state") == "ai_failed"]
    if not failed:
        return ""
    lock = ' disabled title="열쇠 필요"' if locked else ""
    rows = "".join(
        '<form method="post" action="/live/retry" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0 0">'
        f'<input type="hidden" name="key" value="{esc(v["key"])}">'
        f'<span class="mono" style="font-size:12px">{esc(v.get("tag") or "")} · {esc(v.get("title") or "")}</span>'
        f'<span class="muted" style="font-size:12px">{esc(v.get("error") or "")}</span>'
        f'<button class="btn" style="background:var(--sub);padding:5px 10px;font-size:12px"{lock}>다시 시도</button></form>'
        for v in failed)
    return ('<div class="note" style="margin:0 0 8px;border-left:3px solid var(--accent);padding-left:10px">'
            f'<b>AI 서술 실패 {len(failed)}건</b> — 초안에 들어가지 않았고 그동안 승인이 잠긴다.{rows}</div>')


def _live_card(locked):
    """실시간 재생 — 올린 근무 CSV 를 근무 시계로 흘려 초안을 쌓는다(app/live.py). 시작·정지만 두고 배속은 100 이다.

    재생은 현장과 같은 코드(적재·검출·추적·AI·초안 저장)를 타고, 바뀌는 것은 데이터 입구 하나다. 재생 중에는 작업 락을 쥐어
    일괄 실행·리셋·업로드 적재가 기존 거부 문구로 막힌다."""
    st = live.status()
    up = Path(jobs.UPLOAD_DIR)
    sid_of = {str(x.get("source") or "")[4:]: x["shift_id"] for x in jobs.sources() if str(x.get("source") or "").startswith("csv:")}
    # 근무 시각순으로 세운다 — 여러 개를 고르면 그 순서가 곧 이어 재생할 순서다(이어짐은 live 가 다시 검사한다)
    names = sorted((x.name for x in up.glob("*.csv")), key=lambda n: (sid_of.get(n, "~"), n)) if up.is_dir() else []
    running = st["phase"] in ("preparing", "running")

    # 아무것도 고르지 않고 「재생 시작」만 눌러도 되게 미리 골라 둔다 — 가장 이른 근무가 앞 근무(기준선),
    # 나머지 전부가 시각순 이어 재생이다(경모님이 직접 누르신다).
    first = names[0] if names else None
    rest = names[1:]

    def opts(blank=None, pick=()):
        head = f'<option value="">{esc(blank)}</option>' if blank else ""
        return head + "".join(f'<option value="{esc(n)}"{" selected" if n in pick else ""}>{esc(n)}'
                              + (f" · {esc(sid_of[n])}" if n in sid_of else "") + "</option>" for n in names)

    def button(label, off, extra=""):
        why = ' title="열쇠 필요"' if locked else ""
        return f'<button class="btn"{extra}{why}{" disabled" if (locked or off) else ""}>{label}</button>'

    sel = 'class="pill" style="font-size:13px;padding:6px 10px;min-width:240px"'
    return ('<div class="card"><h2>실시간 재생 <span class="muted" style="font-weight:400;font-size:12px">올린 근무 CSV 를 근무 시계로 흘려 초안을 쌓는다 · 100배속</span></h2>'
            '<p class="note" style="margin:0 0 10px">앞 근무 CSV 를 고르면 먼저 적재해 최근 12시간 창으로 검출한다. 쌓이는 구간은 근무 일지에 LIVE 로 보인다.</p>'
            '<form method="post" action="/live/start" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">'
            f'<select name="prev" {sel}>{opts("앞 근무 CSV 없음", pick=([first] if first else []))}</select>'
            f'<select name="csv" {sel} multiple size="8" title="여러 근무를 고르면 시각순으로 이어서 재생한다">{opts(pick=rest)}</select>'
            f'<input type="number" name="speed" value="100" min="1" max="{LIVE_MAX_SPEED}" step="10" class="pill" '
            'style="font-size:13px;padding:6px 10px;width:92px" title="배속">'
            '<label style="font-size:12.5px;display:inline-flex;gap:5px;align-items:center"><input type="checkbox" name="replace" value="1">확정 안 된 초안 교체</label>'
            + button("재생 시작", running or not names)
            + '<input type="hidden" name="sure" value="0">'
            + button("처음부터 다시 돌리기", not names,
                     ' formaction="/live/restart" style="background:var(--sub)"'
                     " onclick=\"if(!window.confirm('데모를 기준선 8근무로 되돌리고 고른 근무를 처음부터 다시 재생합니다.')){return false;} this.form.sure.value='1'\"")
            + '</form>'
            '<p class="note muted" style="margin:0 0 8px;font-size:12px">여러 근무를 고르면 정지할 때까지 시각순으로 이어서 재생한다. '
            f'배속은 100~200 을 권한다 — 틱 한 번이 1초 남짓이라 그보다 높이면 밀린다(상한 {LIVE_MAX_SPEED}).</p>'
            '<form method="post" action="/live/stop" style="margin:0 0 8px">' + button("정지", not running, ' style="background:var(--sub)"') + '</form>'
            + _live_failed_html(locked)
            + f'<div id="livestate" class="mono" style="font-size:12px;color:var(--sub)">{esc(_live_line(st))}</div>'
            + _LIVE_POLL_JS + '</div>')


def view_draft():
    """근무 일지 — 왼쪽 목록에서 고른 근무를 오른쪽에 편다. 주소 없이 들어오면 하나 골라 준다."""
    return _one_page(None)


def _nav_html(sel):
    """왼쪽 근무 목록 — 첫 그림과 폴링이 이 함수 하나를 쓴다. (목록 HTML, 고른 근무, 적재만 된 근무 수)

    전역 깃발로 _one_page 의 반환을 바꿔치기했더니, 화면 요청과 폴링이 동시에 들어오는 순간
    전체 화면 자리에 목록 조각만 나갔다(ThreadingHTTPServer · codex 반증 2026-09-15). 상태를 나눠 쓰지 않는다.
    """
    with db.connect() as conn:
        rows = db.list_shifts(conn)

    # 'live' = 실시간으로 쌓이는 중(조각 2 의 재생이 만든다). 맨 위에 따로 세운다.
    only_ingested = [r["id"] for r in rows if r["draft_status"] not in ("pending", "confirmed", "live")]
    live_rows = [r for r in rows if r["draft_status"] == "live"]   # 이름이 live 면 실시간 모듈(import live)을 가린다
    rest = [r for r in rows if r["draft_status"] in ("pending", "confirmed")]   # 경모님: "미생성은 있을 필요 없다"
    running_sid = (live.status() or {}).get("shift_id")
    live_rows.sort(key=lambda r: (r["id"] != running_sid, r["id"]))     # 지금 쌓는 구간이 맨 위
    listed = live_rows + rest
    if sel is None and listed:
        sel = listed[0]["id"]      # 쌓이는 중이면 그것, 아니면 가장 최근 근무 (빈 화면을 만들지 않는다)

    def row(r, state, cls=""):
        date, kind = r["id"].rsplit("-", 1)
        return (f'<a class="nrow{cls}{" on" if r["id"] == sel else ""}" data-shift="{esc(r["id"])}" href="/shift/{esc(r["id"])}">'
                f'<span class="date">{esc(date)}</span>'
                f'<span class="kind">{"주간" if kind == "day" else "야간"}</span>{state}</a>')

    st = live.status() if live_rows else {}

    def live_state(r):
        """LIVE 줄 — 근무 시계와 지금 개수. 폴링이 같은 자리를 글자만 바꾼다."""
        mine = st.get("shift_id") == r["id"]
        c = (st.get("counts") or {}) if mine else {}
        txt = f"관찰 중 {c.get('observing', 0)} · 초안 {c.get('ready', 0)}"
        # 재생 시계는 화면이 초 단위로 이어 돌린다 — 폴링은 2초에 한 번뿐이라 서버 값만 쓰면 뚝뚝 끊긴다
        clk = (f'<span class="rclock mono" style="font-size:11px;color:var(--live)">{esc(_clock_text(st.get("clock")))}</span>'
               if mine and st.get("clock") else "")
        return ('<span class="pill live">LIVE · 쌓이는 중</span>' + clk
                + f'<span class="livecnt muted" style="font-size:11.5px">{esc(txt)}</span>')

    nav = [row(r, live_state(r), " live") for r in live_rows]
    nav += [row(r, f'<span class="pill on">확정 · 채택 {r["adopted_count"]}건</span>'
                if r["draft_status"] == "confirmed" else '<span class="pill amber">초안</span>') for r in rest]
    return "".join(nav), sel, len(only_ingested)


def _one_page(sel):
    """왼쪽 근무 목록 + 오른쪽 그 근무의 내용, 한 화면.

    목록과 상세가 따로 열려 같은 근무를 두 번 찾아 들어가야 했다(경모님 2026-09-14).
    주소는 그대로 /shift/<근무> 다 — 줄은 링크고, 승인 뒤 돌아오는 자리와 북마크가 계속 동작한다.
    """
    nav, sel, only_ingested = _nav_html(sel)
    if sel:
        right = _shift_body(sel)
    else:
        why = (f'적재된 근무 {only_ingested}개가 있지만 아직 초안이 없습니다.' if only_ingested else '아직 근무가 없습니다.')
        right = '<div class="card"><div class="empty">' + why + '<br>초안이 만들어지면 여기에 쌓입니다.</div></div>'

    # 고를 것이 없으면 왼쪽과 「근무 고르기」를 아예 그리지 않는다 — 제목만 남은 껍데기가
    # 모바일에서 빈 상자로 열렸다(반증 워커). 빈 상태 안내는 오른쪽에만 둔다.
    left = (f'<input type="checkbox" id="pick" class="pickbox">'
            f'<label for="pick" class="pickbtn">근무 고르기</label>'
            f'<div class="split"><nav class="side"><h2>근무 일지</h2>'
            f'<div id="shiftnav">{nav}</div></nav>' if nav else '<div class="split">')
    return page("근무 일지", f'{left}<section class="detail">{right}</section></div>{_POLL_JS}')


def _rounds_html(shift_id):
    """이전 확정 이력. 없으면 빈 문자열, 있으면 'n차 확정' 과 이전 본문 접기."""
    with db.connect() as conn:
        rounds = db.handover_rounds(conn, shift_id)
    if not rounds:
        return ""
    cur = len(rounds) + 1
    parts = [f'<div class="note" style="margin-top:8px"><b>{cur}차 확정</b> — 이전 확정 {len(rounds)}회']
    for r in rounds:
        why = f' · 사유: {esc(r["reopened_reason"])}' if r.get("reopened_reason") else ""
        parts.append(f'<details style="margin-top:4px"><summary>{r["round"]}차 · {esc((r["confirmed_at"] or "")[:16].replace("T"," "))} · '
                     f'채택 {r["adopted_count"]}/제외 {r["excluded_count"]} · 되돌림 {esc((r["reopened_at"] or "")[:16].replace("T"," "))}{why}</summary>'
                     f'<pre class="mono" style="white-space:pre-wrap;font-size:11.5px;margin:6px 0 0">{esc(_old_body(r["body"]))}</pre></details>')
    parts.append("</div>")
    return "".join(parts)


def _view_confirmed(shift_id, draft, handover):
    own = _by_time([i for i in draft["items"] if i["origin"] != "carried"], _starts(shift_id))      # 이월 판단 행은 위 묶음에서 보인다
    adopted = [i for i in own if i["adopted"] == 1]
    excluded = [i for i in own if i["adopted"] == 0]
    with db.connect() as conn:
        opened = db.open_items(conn, shift_id)
        choices = db.carried_choices(conn, draft["id"])

    waves = _waves(shift_id)
    curves = {}

    def trend(it):
        """접힌 추이 — 저장된 곡선, 없으면 남은 원본. 둘 다 없으면 버튼도 그리지 않는다(빈 그래프·오류 금지)."""
        full = it.get("curve")
        if not full and it.get("tag"):
            if it["tag"] not in curves:
                curves[it["tag"]] = _shift_curve(shift_id, it["tag"])
            full = curves[it["tag"]]
        wf, wm = waves.get(it.get("event_id")) or (None, {})
        graph = _spark(wf, wm, unit=(wm or {}).get("unit", ""), full=full)
        if not graph:
            return ""
        key = f"tr{it['id']}"
        return (f'<input type="checkbox" id="{key}" class="trbox">'
                f'<label for="{key}" class="trbtn">{esc(it["tag"])} 추이 보기</label>'
                f'<div class="trwrap">{graph}</div>')

    ents = []
    for it in adopted:
        man = {"manual": ' <span class="pill">직접 추가</span>', "quality": ' <span class="pill amber">원본 품질</span>'}.get(it["origin"], "")
        comment = (f'<div class="c">{esc(it["comment"])} '
                   f'<span class="muted">— {esc(handover["confirmed_by"])}</span></div>'
                   if it.get("comment")
                   else '<div class="c muted">코멘트 없음</div>')
        ents.append(
            f'<div class="ent"><div class="t">{esc(it["title"])} {_status_pill(it.get("status"))}{man}</div>'
            f'<div class="m">{esc(it["evidence"] or it["body"])}</div>{comment}{trend(it)}</div>'
        )
    if not ents:
        ents.append('<div class="empty">채택된 항목이 없습니다.</div>')

    ex = ""
    if excluded:
        rows = "".join(
            f'<div class="ex"><div class="t">{esc(i["title"])}</div>'
            f'<div class="m">{esc(i["evidence"] or i["body"])} · <b>제외</b></div>{trend(i)}</div>'
            for i in excluded
        )
        ex = (f'<div style="border-top:1px solid var(--line);margin:18px 0 12px"></div>'
              f'<p class="note" style="margin-bottom:8px"><b style="color:var(--ink)">제외된 항목</b></p>{rows}')

    date, kind = shift_id.rsplit("-", 1)
    # 「‹ 일지 목록」 줄은 없다 — 목록이 늘 왼쪽에 있다(모바일은 「근무 고르기」).
    return f"""{_carry_box(opened, choices, editable=False)}
<div class="card det">
<h3>{esc(date)} {"주간조" if kind == "day" else "야간조"} 인수인계서</h3>
<div class="sub">분석 구간 {esc(_span(draft.get("window_start")))} · 작성 ENGRA
{esc((draft["generated_at"] or "").replace("T", " "))} · 승인 {esc(handover["confirmed_by"])}
{esc((handover["confirmed_at"] or "").replace("T", " "))} · 감지 {len(own)}건 중
<b>{handover["adopted_count"]}건 채택 / {handover["excluded_count"]}건 제외</b></div>
{_rounds_html(shift_id)}
<form method="post" action="/reopen" style="margin:0"><input type="hidden" name="shift_id" value="{esc(shift_id)}">
<button class="edit">수정</button></form>
{"".join(ents)}{ex}
</div>"""


def _status_pill(status):
    # 기능 도입 전에 확정된 기록은 상태가 NULL 이다. 「상태 없음」 알약을 붙이면 데모 사이트의 확정 근무가
    # 결함처럼 보여서, 완료/진행중이 있는 항목에만 알약을 붙인다(팀장 결정 2026-09-14).
    cls = {"완료": "pill done", "진행중": "pill going"}.get(status)
    return f'<span class="{cls}">{esc(status)}</span>' if cls else ""


def _status_radios(name, cur=None, labels=("완료", "진행중")):
    """완료 / 진행중. 기본 선택이 없다 — 안 읽고 승인하는 것을 막는 자리다 (심사평 08).
    labels 는 이월 묶음에서 「완료로 닫기 / 계속 진행중」 처럼 문구만 바꿀 때 쓴다(값은 같다)."""
    opts = "".join(
        f'<label><input type="radio" name="{name}" value="{v}"{" checked" if cur == v else ""} '
        f'onchange="this.closest(\'.item,.ci\').classList.remove(\'need\')">{esc(lb)}</label>'
        for v, lb in zip(db.ITEM_STATUSES, labels))
    return f'<div class="st"><span class="lb">상태</span>{opts}</div>'


def _carry_box(opened, choices, editable):
    """「이월 항목 N건」 접힌 묶음. 지난 근무들에서 '진행중' 으로 남긴 것을 본문 항목과 섞지 않고 따로 보인다.

    editable=True(대기 초안): 항목마다 「완료로 닫기 / 계속 진행중」 을 고른다. 고르지 않으면 그대로 열린 채 다음 근무로 간다.
    editable=False(확정 일지): 이 근무가 어떻게 했는지만 보인다. 열린 것이 없으면 아무것도 그리지 않는다.
    """
    if not opened:
        return ""
    rows = []
    for o in opened:
        ch = choices.get(o["id"]) or {}
        ago = f'{o["shifts_ago"]}근무 전' if o["shifts_ago"] else "직전 근무"
        last = f' · 마지막 {esc(o["last_shift_id"])}' if o["last_shift_id"] != o["shift_id"] else ""
        cmt = (f'<div class="c">{esc(o["comment"])} <span class="muted">— 마지막 코멘트</span></div>'
               if o.get("comment") else '<div class="c muted">코멘트 없음</div>')
        if editable:
            act = (_status_radios(f'carry_{o["id"]}', ch.get("status"), labels=("완료로 닫기", "계속 진행중"))
                   + f'<textarea name="carry_comment_{o["id"]}" placeholder="이번 근무에서 한 일 · 다음 근무가 볼 것 (선택)">{esc(ch.get("comment") or "")}</textarea>')
        else:
            verb = {"완료": "이번 근무에서 완료로 닫음", "진행중": "계속 진행중 — 다음 근무로"}.get(ch.get("status"), "이번 근무에서 판단하지 않음 — 그대로 다음 근무로")
            act = f'<div class="m"><b style="color:var(--ink)">{esc(verb)}</b>' + (f' · {esc(ch["comment"])}' if ch.get("comment") else "") + '</div>'
        rows.append(
            f'<div class="ci"><div class="t">{esc(o["title"])} <span class="pill">{esc(o["tag"] or "-")}</span></div>'
            f'<div class="m">원 근무 {esc(o["shift_id"])} · {ago}{last} · 남긴 사람 {esc(o["confirmed_by"] or "-")}</div>'
            f'<div class="m">{esc(o["body"] or "")}</div>{cmt}{act}</div>')
    n = len(opened)
    hint = ('여기서 완료로 닫으면 다음 근무 초안에서 빠집니다. 고르지 않으면 열린 채로 넘어갑니다.'
            if editable else '지난 근무에서 「진행중」 으로 남긴 것. 완료로 닫힐 때까지 매 근무 초안에 이어집니다.')
    return (f'<details class="card carry" style="padding:10px 16px">'
            f'<summary style="cursor:pointer;font-weight:600">이월 항목 {n}건'
            f'<span class="muted" style="font-weight:400;font-size:12px"> · 앞 근무에서 진행중으로 남긴 것</span></summary>'
            f'<p class="note" style="margin:8px 0 4px">{hint}</p>{"".join(rows)}</details>')


def _frac(t0, t1, t):
    """t0~t1 안에서 t 의 가로 위치(0~1)."""
    from datetime import datetime as _d
    try:
        a, b, x = _d.fromisoformat(t0), _d.fromisoformat(t1), _d.fromisoformat(t)
    except (TypeError, ValueError):
        return 0.0
    span = (b - a).total_seconds() or 1
    return max(0.0, min(1.0, (x - a).total_seconds() / span))


def _curve(pts, t0, t1, *, height=150, band=None, limit=None, unit="", ticks=5, gap=0.0, note="", minutes=None, span_m=None):
    """(가로 위치 0~1, 값) 점들 → 인라인 SVG 한 장. 외부 라이브러리 없음.

    band=(f0,f1) 는 감지 구간 음영, gap 보다 벌어진 자리는 선을 끊는다(원본이 빈 구간).
    minutes(구간 시작부터 분)를 주면 호버 데이터를 붙인다 — TREND_JS 가 .trend[data-trend] 에서 가장 가까운 실제 점을 띄운다.
    숫자는 전부 numfmt.fmt 로 낸다 — 눈금을 `:.4g` 로 찍어 값이 큰 태그에서 `1.77e+04` 가 나왔다(경모님 지적 2026-09-14).
    """
    import math
    if len(pts) < 2:
        return ""
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    ylo, yhi = lo, hi
    if isinstance(limit, (int, float)) and abs(limit - (lo + hi) / 2) < (hi - lo or 1) * 6:
        ylo, yhi = min(ylo, limit), max(yhi, limit)
    pad = ((yhi - ylo) or abs(yhi) * 0.01 or 1.0) * 0.08
    ylo -= pad
    yhi += pad
    span = (yhi - ylo) or 1.0
    if not (math.isfinite(ylo) and math.isfinite(yhi) and math.isfinite(span)):
        # 값이 부동소수 최대치 근처면 여백을 더한 범위가 무한대가 된다 — 자리수 계산이 거기서 멈췄다(탐침 재현).
        # 그릴 수 없는 그래프는 빼고 나머지 화면은 그대로 간다(원본이 없을 때와 같다).
        return ""
    W, H = 720, height
    L, R, T, B = 64, 12, 10, 26            # 축 여백 — 쉼표 붙은 큰 수(40,131)가 들어갈 만큼
    pw, ph = W - L - R, H - T - B
    # 눈금 자리수 = 값 크기의 표시 자리수와, 눈금 간격이 구별되는 자리수 중 큰 쪽
    step = span / 3
    d = numfmt.decimals(sorted(abs(v) for v in vals)[len(vals) // 2])
    if step > 0:
        d = min(12, max(d, math.ceil(-math.log10(step))))

    def X(f):
        return L + f * pw

    def Y(v):
        return T + ph - (v - ylo) / span * ph

    segs, cur, prev = [], [], None
    for f, v in pts:
        if prev is not None and gap and (f - prev) > gap:
            segs.append(cur)
            cur = []
        cur.append(f"{X(f):.1f},{Y(v):.1f}")
        prev = f
    segs.append(cur)
    poly = "".join(f'<polyline points="{" ".join(s)}" fill="none" stroke="var(--chart)" stroke-width="1.6" stroke-linejoin="round"/>'
                   for s in segs if len(s) > 1)
    yt = "".join(
        f'<line x1="{L}" y1="{Y(ylo + span * k / 3):.1f}" x2="{W - R}" y2="{Y(ylo + span * k / 3):.1f}" stroke="var(--line)" stroke-dasharray="2 3"/>'
        f'<text x="{L - 6}" y="{Y(ylo + span * k / 3) + 3.5:.1f}" font-size="10" text-anchor="end" fill="var(--sub)">{numfmt.fmt(ylo + span * k / 3, d=d)}</text>'
        for k in range(4))
    xt = ""
    try:
        from datetime import datetime as _d
        a, b = _d.fromisoformat(t0), _d.fromisoformat(t1)
        xt = "".join(
            f'<line x1="{X(k / (ticks - 1)):.1f}" y1="{T + ph}" x2="{X(k / (ticks - 1)):.1f}" y2="{T + ph + 4}" stroke="var(--line)"/>'
            f'<text x="{X(k / (ticks - 1)):.1f}" y="{H - 8}" font-size="10" text-anchor="middle" fill="var(--sub)">'
            f'{(a + (b - a) * k / (ticks - 1)).strftime("%H:%M")}</text>' for k in range(ticks))
    except (TypeError, ValueError):
        pass
    shade = ""
    if band:
        f0, f1 = max(0.0, band[0]), min(1.0, band[1])
        shade = f'<rect x="{X(f0):.1f}" y="{T}" width="{max(3.0, (f1 - f0) * pw):.1f}" height="{ph}" fill="var(--band)" opacity=".22"/>'
    lim = ""
    if isinstance(limit, (int, float)) and ylo <= limit <= yhi:
        # 선이 SK 레드가 되어 한계선은 회색 점선으로 물러났다 — 둘 다 빨강이면 구분이 안 된다(경모님 2026-09-15)
        lim = (f'<line x1="{L}" y1="{Y(limit):.1f}" x2="{W - R}" y2="{Y(limit):.1f}" stroke="var(--limit)" stroke-width="1.2" stroke-dasharray="6 4"/>'
               f'<text x="{W - R}" y="{Y(limit) - 4:.1f}" font-size="10" text-anchor="end" fill="var(--limit)">한계 {numfmt.fmt(limit)}{esc(unit)}</text>')
    end = f'<circle cx="{X(pts[-1][0]):.1f}" cy="{Y(pts[-1][1]):.1f}" r="3" fill="var(--chart)"/>'
    data = ""
    if minutes and len(minutes) == len(pts):
        # 정수 분 · 소수 한 자리 화면 좌표 · 화면 표기 문자열만 보낸다 — 실수를 그대로 JSON 에 넣으면 3.2e-05 가 나온다
        data = " data-trend='" + esc(json.dumps({
            "t0": t0, "span_m": span_m or 1, "L": L, "pw": pw, "unit": unit,
            "m": [int(m) for m in minutes], "y": [round(Y(v), 1) for v in vals], "v": [numfmt.fmt(v, d=d) for v in vals]},
            ensure_ascii=False, separators=(",", ":"))) + "'"
    return (f'<div class="trend"{data} style="position:relative;margin-bottom:10px">'
            f'<svg viewBox="0 0 {W} {H}" style="display:block;width:100%;height:auto;background:var(--card);'
            f'border:1px solid var(--line);border-radius:8px">{yt}{shade}{poly}{lim}{end}{xt}'
            f'<line x1="{L}" y1="{T}" x2="{L}" y2="{T + ph}" stroke="var(--line)"/>'
            f'<line x1="{L}" y1="{T + ph}" x2="{W - R}" y2="{T + ph}" stroke="var(--line)"/>'
            f'<line class="cur" x1="0" y1="{T}" x2="0" y2="{T + ph}" stroke="var(--chart)" stroke-width="1" style="display:none"/>'
            f'<circle class="dot" cx="0" cy="0" r="4" fill="var(--chart)" style="display:none"/></svg>'
            + (f'<div class="muted" style="font-size:13px;margin-top:3px">{note}</div>' if note else "")
            + '<div class="tip mono" style="display:none;position:absolute;top:6px;transform:translateX(-50%);background:var(--ink);color:#fff;'
              'font-size:11.5px;padding:3px 8px;border-radius:4px;white-space:nowrap;pointer-events:none"></div></div>')


def _shift_curve(shift_id, tag):
    """근무 구간 전체의 태그 곡선을 남은 원본에서 읽는다(1분 평균, db.raw_curve). 원본이 회전돼 없으면 None.

    항목에 저장된 곡선(draft_item.curve_json)이 없을 때만 부른다 — 저장은 db.save_curves 가 초안·확정 때 한다.
    """
    if not tag:
        return None
    with db.connect() as conn:
        return db.raw_curve(conn, shift_id, tag)


def _spark(w, metrics=None, unit="", full=None, height=150, ticks=5, legend=True):
    """항목 그래프 — 근무 구간 전체 하나. 감지 구간은 빨간 음영, 마우스를 올리면 가장 가까운 점의 시각·값.

    경모님 지적(2026-08-27) "trend 가 보기 불편하다" → 눈금·한계선·음영이 있는 차트로, (2026-09-14) "근무 어디쯤이었는지
    알 수 없다" → 근무 구간 전체를 그리고, 같은 날 "트렌드 두 개 말고 전체 근무 기준 하나만" → 확대 그래프(±30분)를 없앴다.
    확대 쪽에만 있던 최저·최고(감지 구간 ±30분 파형)는 그래프 아래 설명줄로 옮겼다.
    full = 저장된 곡선 또는 남은 원본에서 읽은 곡선({"t0","t1","m","v"}). 없으면 그리지 않는다(빈 그래프 금지).
    """
    from datetime import datetime as _d
    if not full or len(full.get("m") or []) < 2:
        return ""
    try:
        span_m = max(1, int((_d.fromisoformat(full["t1"]) - _d.fromisoformat(full["t0"])).total_seconds() // 60))
    except (TypeError, ValueError, KeyError):
        return ""
    limit = (metrics or {}).get("limit")
    band = None
    if w and w.get("mark"):
        band = (_frac(full["t0"], full["t1"], w["mark"][0]), _frac(full["t0"], full["t1"], w["mark"][1]))
    note = ""
    if legend:
        note = "근무 구간 전체 · 음영 = 감지 구간" + (" · 빨간 점선 = 알람 한계" if isinstance(limit, (int, float)) else "")
        if w and w.get("v"):
            note += (f" · 감지 구간 ±30분 최저 {numfmt.fmt(min(w['v']))} · 최고 {numfmt.fmt(max(w['v']))}"
                     + (f" {esc(unit)}" if unit else ""))
    pts = [(m / span_m, v) for m, v in zip(full["m"], full["v"])]
    return _curve(pts, full["t0"], full["t1"], band=band, limit=limit, unit=unit, gap=0.01, note=note,
                  minutes=full["m"], span_m=span_m, height=height, ticks=ticks)


def _waves(shift_id):
    """근무의 이벤트 파형·지표를 event_id → (파형, metrics) 로."""
    out = {}
    with db.connect() as conn:
        for e in db.load_events(conn, shift_id):
            raw = e["waveform_json"] if "waveform_json" in e.keys() else None
            if raw:
                try:
                    m = json.loads(e["metrics_json"] or "{}") if "metrics_json" in e.keys() else {}
                    out[e["id"]] = (json.loads(raw), m)
                except ValueError:
                    pass
    return out


def _starts(shift_id):
    """근무의 이벤트 id → 처음 감지된 시각. 순서 규칙은 db.by_time 에 있다 — 확정 일지 본문도 같은 것을 쓴다."""
    with db.connect() as conn:
        return db.item_starts(conn, shift_id)


_by_time = db.by_time


def _prev_ex_note(pe):
    """지난 근무에서 누가·언제 제외했는지. 판단이 아니라 자료로 보인다."""
    who = str(pe.get("by") or "앞 근무자")
    sid = str(pe.get("shift_id") or "")
    when = str(pe.get("at") or "")[:10]
    times = int(pe.get("times") or 1)
    rep = f' · <b>{times}근무 연속</b>' if times > 1 else ""
    cmt = pe.get("comment")
    tail = f' <span class="muted">— 남긴 코멘트: {esc(str(cmt))}</span>' if cmt else ""
    return ('<div class="note" style="margin:6px 0 4px"><b>이전 제외</b> — '
            + esc(sid) + ' ' + esc(when) + ' ' + esc(who) + ' 님이 이 태그·종류를 제외했습니다'
            + rep + tail + '</div>')


def _item_on(it):
    """이 항목이 기본으로 채택인가 — 근무자가 정한 것이 우선이고, 미결정이면 앞 근무자의 제외 결정을 이어받아 꺼 둔다."""
    pe = it.get("prev_excluded") if hasattr(it, "keys") else None
    return it.get("adopted") == 1 or (it.get("adopted") is None and not pe)


def _item_card(it, wf, wm, full, key=None):
    """항목 카드 한 장 — 초안 화면과 실시간 폴링 조각이 같은 것을 그린다.

    data-* 표식(항목 id · 태그 · 이벤트 id)은 폴링과 시연 녹화가 **위치가 아니라 항목으로** 고르게 한다.
    녹화가 첫 칸·끝 칸으로 고르다가 화면 순서가 바뀌자 엉뚱한 항목에 코멘트가 들어갔다(반증 워커).
    """
    judged = ""
    if it.get("handover_worthy") in (0, False):
        judged = '<div class="note" style="margin:6px 0 4px"><span class="pill">전달 가치 낮음 — 정상 운전 범위로 판단</span></div>'
    rel = it.get("related_tags_ai") if hasattr(it, "keys") else None
    if rel:
        judged += (f'<div class="note" style="margin:4px 0"><b>함께 봐야 할 항목</b> — '
                   f'{", ".join(esc(t) for t in rel)}'
                   + (f' <span class="muted">— {esc(it.get("related_note") or "")}</span>' if it.get("related_note") else "")
                   + '</div>')
    sug = ""
    pre = it.get("precedents") or []
    pall = it.get("precedents_all") or []
    pnote = it.get("precedent_note") if hasattr(it, "keys") else None
    if pre:
        # 과거 조치 = 같은 태그의 확정 일지에서 AI 가 이번 현상에 맞다고 판정한 것. 원문은 접어 둔다 —
        # 앞 근무자의 문장은 판단이 아니라 자료라, AI 판정을 먼저 읽게 한다(QA 6차).
        def _prec_li(pp):
            txt = str(pp.get("text") or "")
            head = txt.strip().replace("\n", " ")[:60]
            more = "…" if len(txt.strip()) > 60 else ""
            btn = (f'<button type="button" onclick="use(this,'
                   f'{html.escape(json.dumps(txt, ensure_ascii=False), quote=True)})">코멘트로 사용</button>')
            return ('<li>' + esc(str(pp.get("shift_id") or "")) + ' '
                    + esc(str(pp.get("confirmed_at") or "")[:10]) + ' — ' + esc(head + more)
                    + ' ' + btn
                    + '<details style="margin-top:3px"><summary class="muted" style="cursor:pointer;font-size:11.5px">'
                    + '원문 보기 — 앞 근무자가 직접 쓴 문장</summary>'
                    + f'<pre class="mono" style="white-space:pre-wrap;font-size:11.5px;margin:4px 0 0">{esc(txt)}</pre>'
                    + '</details></li>')
        rows_ = "".join(_prec_li(pp) for pp in pre[:3])
        sug = (f'<div class="sug"><span class="lb">과거 조치 — 확정 일지 {len(pre)}건'
               + (f' <span class="muted">(같은 태그 사례 {len(pall)}건 중 맞는 것)</span>' if len(pall) > len(pre) else '')
               + (f' <span class="muted">— {esc(pnote)}</span>' if pnote else '')
               + f'</span><ul style="margin:4px 0 0 16px;font-size:12px">{rows_}</ul></div>')
    elif pall:
        sug = (f'<div class="sug muted" style="font-size:12px"><span class="lb">과거 조치 없음</span>같은 태그 확정 일지 {len(pall)}건이 '
               '있었지만 이 현상에 맞지 않다고 판단' + (f' — {esc(pnote)}' if pnote else '') + '</div>')
    pe = it.get("prev_excluded") if hasattr(it, "keys") else None
    if pe:
        judged += _prev_ex_note(pe)
    on = _item_on(it)
    # 설명은 사람이 읽는 문장만, 수치는 「감지 근거」 칸에만 — 엔진 body 가 문장 + 태그별 근거 줄이라 두 자리에 같은 숫자가 나왔다.
    lines = [ln.strip() for ln in (it["body"] or "").split("\n") if ln.strip()]
    say = " ".join(ln for ln in lines if not ln.startswith("·"))
    ev = (it["evidence"] or "").strip()
    nums = [x for x in (ln.lstrip("·").strip() for ln in lines if ln.startswith("·")) if x and x != ev]
    why = ('<div class="why"><b>감지 근거</b> — ' + esc(ev)
           + "".join(f'<div style="margin-top:5px">{esc(x)}</div>' for x in nums) + '</div>')
    mark = f' data-key="{esc(key)}"' if key else ""
    return f"""<div class="item{"" if on else " off"}" data-state="ready" data-item="{it['id']}" data-tag="{esc(it['tag'] or '')}" data-event="{it.get('event_id') or ''}"{mark}>
<div class="row1"><input type="checkbox" name="item" value="{it['id']}"{" checked" if on else ""} onchange="tg(this)">
<div class="ttl">{esc(it['title'])}</div></div>
<div class="meta">{esc(it['tag'])}{(" · " + esc(say)) if say else ""}</div>
<div class="body">
{_spark(wf, wm, unit=(wm or {}).get("unit", ""), full=full)}{why}
{judged}{sug}
{_status_radios(f"status_{it['id']}", it.get("status"))}<textarea name="comment_{it['id']}" placeholder="코멘트 (선택)">{esc(it.get("comment") or "")}</textarea>
</div></div>"""


_LIVE_STATE = {"observing": "관찰 중", "writing": "AI 서술 중", "ai_failed": "AI 서술 실패"}


def _held(first_seen, clock):
    """처음 잡힌 뒤 얼마나 됐나 — 재생 시계 기준. 지속 시간은 관찰 카드가 살아 있다는 표시라 매 폴링 달라진다."""
    from datetime import datetime as _d
    try:
        m = int((_d.fromisoformat(clock) - _d.fromisoformat(first_seen)).total_seconds() // 60)
    except (TypeError, ValueError):
        return ""
    if m < 0:
        return ""
    return f"{m}분째" if m < 90 else f"{m / 60:.1f}시간째"


def _short_evidence(text):
    """관찰 카드 한 줄 — 무엇이 얼마나 빨리 움직이나와 한계까지 남은 시간만.
    긴 근거 문장(정상범위 대비·평소 대비·실측 보정)은 AI 가 쓴 초안 카드에 그대로 있다(경모님 2026-09-15)."""
    base = (text or "").split("\n")[0]
    head = base
    for sep in (",", " ("):
        i = head.find(sep)
        if i > 0:
            head = head[:i]
    m = re.search(r"이 속도면[^·]*", base)
    return head.strip() + (" · " + m.group(0).strip() if m else "")


def _live_gray_card(v, full=None, clock=None, value=None):
    """아직 고를 수 없는 카드 — 관찰 중·서술 중·AI 서술 실패. 입력 요소를 두지 않는다(못 건드린다, 경모님 결정 §0).

    폴링이 이 카드를 매 번 다시 그린다. 경모님 지적(2026-09-15) "40초를 봐도 카드 글자가 하나도 안 바뀐다" →
    지금 값·지속 시간을 맨 앞에 세우고 작은 추이를 붙였다. 추이는 근무 구간 전체 위에 그려 틱마다 오른쪽으로 자란다.
    """
    when = (v.get("first_seen") or "")[11:16]
    err = (f'<div class="note" style="margin:6px 0 0;color:var(--accent)">{esc(v.get("error") or "")}</div>'
           if v.get("state") == "ai_failed" else "")
    now = [x for x in (f"지금 {numfmt.fmt(value)}" if isinstance(value, (int, float)) else "",
                       _held(v.get("first_seen"), clock)) if x]
    head = ('<b>' + ' · '.join(esc(x) for x in now) + '</b> — ') if now else ''
    prog = '<div class="prog"><i></i></div>' if v["state"] == "writing" else ''
    return (f'<div class="item obs off" data-state="{esc(v["state"])}" data-key="{esc(v["key"])}" data-tag="{esc(v.get("tag") or "")}">'
            f'<div class="row1"><span class="pill">{esc(_LIVE_STATE.get(v["state"], v["state"]))}</span>'
            f'<div class="ttl">{esc(v.get("title") or "")}</div></div>'
            f'<div class="meta">{esc(v.get("tag") or "")}{f" · {when} 부터" if when else ""}</div>'
            f'<div class="body">{prog}{_spark(None, None, full=full, height=72, ticks=3, legend=False)}'
            f'<div class="why">{head}{esc(_short_evidence(v.get("evidence")))}</div>{err}</div></div>')


# 한 줄 목록의 무리 차례 — AI 작성 중이 맨 위, 그 아래 관찰 중(최근 것 위), 맨 아래 초안.
# 카드 하나가 올라갔다 내려오는 것으로 관측 → 추적 → AI → 초안이 한 화면에서 보인다(경모님 2026-09-15).
_LANES = ("ai_failed", "writing", "observing", "ready")
_LANE_NAME = {"ai_failed": "AI 서술 실패", "writing": "AI 작성 중", "observing": "관찰 중", "ready": "초안"}


# 빈 안내에도 표식을 단다. 표식이 없으면 폴링이 지우지 못해, 0건일 때 연 화면은 카드가 쌓여도
# 「감지된 항목이 없습니다」가 그대로 남았다(경모님 지적 2026-09-15).
_EMPTY_CARD = '<div class="card" data-key="__empty"><div class="empty">감지된 항목이 없습니다.</div></div>'


def _lane_head(state, n):
    return f'<div class="lane" data-state="head" data-key="__h_{state}">{esc(_LANE_NAME[state])} <b>{n}</b></div>'


def _live_rows(shift_id, draft, views, same, heads=True):
    """쌓이는 중 화면의 줄 차례 — 첫 그림과 폴링이 이 함수 하나를 쓴다(모양이 갈라지지 않는다).

    지난 근무에서 제외한 항목은 여기 넣지 않는다 — 그것은 최하단 접힌 칸의 몫이고,
    넣으면 폴링이 같은 카드를 목록에 한 장 더 끼운다.
    """
    keyed = {v["draft_item_id"]: v for v in views if v.get("draft_item_id")}
    vals = (live.values() or {}) if same else {}
    now = vals.get("clock")
    waves = _waves(shift_id)
    curves = {}

    def curve(tag):
        if tag not in curves:
            curves[tag] = _shift_curve(shift_id, tag)
        return curves[tag]

    lanes = {k: [] for k in _LANES}
    for it in _by_time([x for x in draft["items"] if x["origin"] != "carried"], _starts(shift_id)):
        if it.get("prev_excluded"):
            continue
        key = (keyed.get(it["id"]) or {}).get("key") or f"i{it['id']}"
        wf, wm = waves.get(it.get("event_id")) or (None, {})
        lanes["ready"].append({"key": key, "state": "ready",
                               "html": _item_card(it, wf, wm, it.get("curve") or curve(it["tag"]), key=key)})
    for v in sorted((x for x in views if x["state"] in _LIVE_STATE),
                    key=lambda x: x.get("first_seen") or "", reverse=True):
        cur = ((vals.get("values") or {}).get(v.get("tag")) or [None, None])[1]
        lanes[v["state"]].append({"key": v["key"], "state": v["state"],
                                  "html": _live_gray_card(v, full=curve(v.get("tag")), clock=now, value=cur)})
    rows = []
    for st in _LANES:
        if lanes[st]:
            if heads:
                rows.append({"key": f"__h_{st}", "state": "head", "html": _lane_head(st, len(lanes[st]))})
            rows += lanes[st]
    # 빈 안내는 화면이 보여 주는 것과 같은 규칙으로 — 최하단 「이전에 제외한 것」 칸에 뭔가 있으면 빈 화면이 아니다
    # (_view_pending 의 조건과 같다). 초안에 그런 항목만 있을 때 빈 안내를 안 내보내면 목록만 비고 까닭이 없다(codex 반증).
    own = [x for x in draft["items"] if x["origin"] != "carried"]
    if not rows and not [x for x in own if x.get("prev_excluded")]:
        rows.append({"key": "__empty", "state": "empty", "html": _EMPTY_CARD})
    return rows


def _live_summary(rows):
    """오른쪽 요약 — 지금 화면에 있는 것을 그대로 센다."""
    n = {st: sum(1 for r in rows if r["state"] == st) for st in _LANES}
    return (f'관찰 중 <b style="color:var(--ink)">{n["observing"]}</b> · '
            f'초안 <b style="color:var(--ink)">{n["ready"]}</b>')


def live_cards(shift_id):
    """폴링용 — 화면에 보이는 것 전부를 지금 값으로 다시 준다(카드 · 왼쪽 목록 · 요약 · 채택 분모 · 잠금).

    화면은 이것을 그대로 갈아 끼우고, 근무자가 지금 입력 중인 카드만 건너뛴다(_POLL_JS).
    분모(채택 N / M 의 M)를 매 번 다시 보내는 까닭: 첫 그림 때 값으로 두었더니 AI 가 항목을 다 쓸 때마다
    카드는 느는데 분모는 그대로라 「채택 10 / 8건」 이 됐다(경모님 지적 2026-09-15).
    """
    with db.connect() as conn:
        draft = db.load_draft(conn, shift_id)
    st = live.status()
    same = st.get("shift_id") == shift_id            # 지금 쌓는 근무인가 — 아니면 개수·시계는 이 줄의 것이 아니다
    out = {"draft": (draft or {}).get("status"), "lock": live.approve_lock(shift_id),
           "counts": (st.get("counts") or {}) if same else {}, "clock": st.get("clock") if same else None,
           "phase": st.get("phase"), "speed": st.get("speed"),
           "nav": _nav_html(shift_id)[0], "cards": [], "order": [], "total": 0, "summary": ""}
    if draft is None or draft.get("status") == "confirmed":
        return out
    # 마감된 초안(pending)도 카드를 준다. 마감 직전에 AI 가 쓴 항목이 화면에 못 올라오면 근무자가 보지 못한 채
    # 승인돼 그 항목이 제외로 남는다(codex 반증 3차). 무리 머리는 쌓이는 중에만 세운다.
    on = draft.get("status") == "live"
    rows = _live_rows(shift_id, draft, live.items(shift_id) if on else [], same, heads=on)
    n = len([x for x in draft["items"] if x["origin"] != "carried"])
    out["cards"] = rows
    out["order"] = [r["key"] for r in rows]
    out["total"] = n
    out["summary"] = _live_summary(rows) if on else f'감지 <b style="color:var(--ink)">{n}</b>건'
    return out


# 화면 갱신 장치 하나 — 모든 근무 일지 화면이 2초마다 지금 상태를 받아 다시 그린다(경모님 2026-09-15).
# 다시 그리지 않는 유일한 예외는 근무자가 지금 손대고 있는 카드다. 그 밖의 카드는 갈아 끼우되
# 이미 넣어 둔 체크·완료/진행중·코멘트·펼침을 그대로 되돌려 놓는다 — 새로고침이 그것을 지웠다.
_POLL_JS = """<script>(function(){
var box=document.getElementById('items'); var sid=(box&&box.dataset.shift)||'';
function busy(el){return el.contains(document.activeElement)&&document.activeElement!==document.body}
/* 자리 옮기기를 막는 것은 「지금 글을 쓰고 있는 칸」 하나뿐이다.
   초점이 목록 안이기만 하면 막았더니, 카드를 한 번 누른 뒤로 순서가 영영 안 맞았다(경모님 지적 2026-09-15) —
   체크 상자를 누르면 초점이 거기 남고, 사람이 다른 곳을 눌러도 초점은 빠지지 않는다. */
var lastKey=0;                                   /* 마지막 타자 시각 — 초점만 남은 칸은 붙잡지 않는다 */
if(box){box.addEventListener('keydown',function(){lastKey=Date.now()},true);
        box.addEventListener('input',function(){lastKey=Date.now()},true);}
function writing(){
 var a=document.activeElement; if(!a||!box||!box.contains(a)) return null;
 var t=(a.tagName||'').toUpperCase();
 if(!(t==='TEXTAREA'||(t==='INPUT'&&/^(text|search|number|email|url|tel|password)$/i.test(a.type||'text')))) return null;
 /* 글 쓰던 손이 멈추면(3초) 그 카드도 다시 차례를 맞춘다 — 글자·초점·커서 자리는 되돌려 놓는다.
    초점이 남아 있다는 이유만으로 붙잡았더니 그 카드 하나가 끝내 제자리를 못 찾았다(codex 반증). */
 return (Date.now()-lastKey < 3000) ? a.closest('[data-key]') : null;
}
var still=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;   /* 즉시 이동 */
function keep(el){          /* 근무자가 넣은 것 — 갈아 끼운 뒤 그대로 되돌린다(초점까지) */
 var a=document.activeElement;
 var o={cb:[],rd:null,ta:{},dt:[],
        fc:(a&&a.name&&el.contains(a))?{n:a.name,v:a.value,s:a.selectionStart,e:a.selectionEnd}:null};
 el.querySelectorAll('input[type=checkbox]').forEach(function(x){o.cb.push([x.value,x.checked])});
 var r=el.querySelector('input[type=radio]:checked'); if(r) o.rd=r.value;
 el.querySelectorAll('textarea').forEach(function(x){o.ta[x.name]=x.value});
 el.querySelectorAll('details').forEach(function(x){o.dt.push(x.open)});
 return o;
}
function put(el,o){
 el.querySelectorAll('input[type=checkbox]').forEach(function(x){
  o.cb.forEach(function(p){if(p[0]===x.value) x.checked=p[1]});
  var it=x.closest('.item'); if(it) it.classList.toggle('off',!x.checked);
 });
 if(o.rd) el.querySelectorAll('input[type=radio]').forEach(function(x){x.checked=(x.value===o.rd)});
 el.querySelectorAll('textarea').forEach(function(x){if(o.ta[x.name]!==undefined) x.value=o.ta[x.name]});
 el.querySelectorAll('details').forEach(function(x,i){if(o.dt[i]!==undefined) x.open=o.dt[i]});
 if(o.fc){        /* 누르던 칸에 초점을 돌려 놓는다 — 이름이 같은 라디오는 값으로 가른다 */
  var f=el.querySelector('[name="'+o.fc.n+'"][value="'+o.fc.v+'"]')||el.querySelector('[name="'+o.fc.n+'"]');
  if(f&&f.focus){f.focus({preventScroll:true});
   if(o.fc.s!=null&&f.setSelectionRange){try{f.setSelectionRange(o.fc.s,o.fc.e)}catch(_){}}}
 }
}
/* 재생 시계 — 폴링은 2초에 한 번이라, 그 사이는 배속만큼 이어 돌리고 새 값이 오면 맞춘다.
   멈춘 재생은 시계도 멈춘다(그 사실이 화면에 그대로 드러나야 한다). */
var rc={ms:null,speed:1,run:false,at:0};
function z(n){return (n<10?'0':'')+n}
function stamp(ms){var d=new Date(ms);
 return d.getFullYear()+'.'+z(d.getMonth()+1)+'.'+z(d.getDate())+' '+z(d.getHours())+':'+z(d.getMinutes())+':'+z(d.getSeconds())}
function paintClock(){
 if(rc.ms===null) return;
 var t=rc.ms+(rc.run?(Date.now()-rc.at)*rc.speed:0), s=stamp(t);
 Array.prototype.forEach.call(document.querySelectorAll('.rclock'),function(e){e.textContent=s});
}
setInterval(paintClock,250);
function tick(){
 fetch('/api/live/cards?shift='+encodeURIComponent(sid)).then(function(r){return r.json()}).then(function(j){
  if(j.clock){rc.ms=Date.parse(j.clock);rc.speed=j.speed||1;rc.run=(j.phase==='running');rc.at=Date.now();paintClock()}
  else {rc.ms=null}
  var nav=document.getElementById('shiftnav');
  if(nav&&j.nav&&!busy(nav)) nav.innerHTML=j.nav;
  /* 목록을 서버가 실제로 그려 준 경우에만 카드를 건드린다 — 빈 응답과 「빈 목록」은 다르다(codex 반증 4차).
     확정본·없는 근무면 응답에 목록이 없으니 화면을 그대로 둔다. */
  var listed=(j.draft==='live'||j.draft==='pending');
  if(box&&box.dataset.live&&listed){
   var hold=writing();                            /* 지금 글을 쓰고 있는 카드 하나 — 이것만 그대로 둔다 */
   /* 누르고 있던 칸 — 카드를 갈아 끼우거나 자리를 옮기면 초점이 떨어진다(마디를 옮기면 브라우저가 blur 시킨다).
      다 그린 뒤 제자리에 돌려 놓는다. 그 사이 근무자가 다른 곳을 눌렀으면 건드리지 않는다. */
   var af=document.activeElement;
   var back=(af&&af.name&&box.contains(af))?{n:af.name,v:af.value}:null;
   /* 쓰고 있는 칸은 화면에서 제자리에 둔다 — 위쪽 카드가 늘거나 줄면 커서 밑에서 칸이 밀린다(codex 반증) */
   var foc=(document.activeElement&&document.activeElement!==document.body&&box.contains(document.activeElement))
           ?document.activeElement.closest('[data-key]'):null;
   var focTop=foc?foc.getBoundingClientRect().top:null;
   /* 옮겨가는 것이 보이게 — 옮기기 전 자리를 재 둔다. 문서 기준으로 재야 아래 scrollBy 보정에 흔들리지 않는다
      (화면 기준으로 쟀더니 보정한 만큼 카드가 튄 자리에서 전환이 시작됐다 — codex 반증) */
   var was={};
   if(!still) Array.prototype.forEach.call(box.querySelectorAll('[data-key]'),function(e){
    was[e.getAttribute('data-key')]=e.getBoundingClientRect().top+window.scrollY;
   });
   var seen={};
   (j.cards||[]).forEach(function(c){ seen[c.key]=1;
    var el=box.querySelector('[data-key="'+c.key+'"]');
    if(el&&el===hold) return;                     /* 글을 쓰는 중인 카드만 건드리지 않는다 */
    var was=el?((el.querySelector('.why')||{}).textContent||''):'';
    var st=el?keep(el):null;
    if(el){el.outerHTML=c.html;} else {box.insertAdjacentHTML('beforeend',c.html);}
    var now=box.querySelector('[data-key="'+c.key+'"]');
    if(!now) return;
    if(st) put(now,st);
    if(!el) now.classList.add('fresh');           /* 새로 들어온 카드 */
    else if(was&&((now.querySelector('.why')||{}).textContent||'')!==was) now.classList.add('flash');
   });
   Array.prototype.forEach.call(box.querySelectorAll('[data-key]'),function(e){
    if(!seen[e.getAttribute('data-key')]&&e!==hold) e.remove();   /* 사라진 카드 · 마감돼 걷힌 무리 머리 */
   });
   /* 차례 맞추기 — 쓰고 있는 카드 하나만 제자리에 두고 나머지는 매 번 맞춘다.
      한 번 막히면 영영 안 맞던 구조를 없앴다: 쓰기를 멈추면 바로 다음 폴링이 따라잡는다. */
   if((j.order||[]).length){
    var prev=null;
    j.order.forEach(function(k){
     var e=box.querySelector('[data-key="'+k+'"]'); if(!e||e===hold) return;
     var want=prev?prev.nextElementSibling:box.firstElementChild;
     while(want===hold) want=want.nextElementSibling;   /* 쓰는 중인 카드의 자리는 건너뛴다 */
     if(e!==want) box.insertBefore(e,want);
     prev=e;
    });
   }
   if(foc&&foc.isConnected){                     /* 쓰던 칸이 있던 자리에 그대로 보이게 화면을 먼저 맞춘다 */
    var dy=foc.getBoundingClientRect().top-focTop;
    if(Math.abs(dy)>1) window.scrollBy(0,dy);
   }
   if(!still) Array.prototype.forEach.call(box.querySelectorAll('[data-key]'),function(e){
    if(e===hold) return;                         /* 쓰고 있는 칸은 미끄러뜨리지 않는다 — 커서 아래에서 움직인다 */
    var b=was[e.getAttribute('data-key')]; if(b===undefined) return;
    var d=b-(e.getBoundingClientRect().top+window.scrollY);
    if(Math.abs(d)<2) return;                    /* 안 움직였으면 그대로 */
    e.style.transition='none'; e.style.transform='translateY('+d+'px)';
    requestAnimationFrame(function(){e.style.transition='transform .25s ease';e.style.transform=''});
   });
   if(back&&document.activeElement===document.body){
    var f=box.querySelector('[name="'+back.n+'"][value="'+back.v+'"]')||box.querySelector('[name="'+back.n+'"]');
    if(f&&f.focus) f.focus({preventScroll:true});
   }
  }
  var sm=document.getElementById('summary'); if(sm&&j.summary) sm.innerHTML=j.summary;
  var tot=document.getElementById('tot'); if(tot&&j.total) tot.textContent=j.total;
  var lk=document.getElementById('approvelock'), btn=document.getElementById('approve');
  if(lk) lk.textContent=j.lock||'';
  if(btn) btn.disabled=!!j.lock;
  if(window.cnt) window.cnt();      /* 채택 수를 다시 센다 */
  setTimeout(tick,2000);
 }).catch(function(){setTimeout(tick,5000);});
}
setTimeout(tick,2000);})();</script>"""


def _view_pending(shift_id, draft):
    waves = _waves(shift_id)
    curves = {}
    live_mode = draft.get("status") == "live"
    lives = live.items(shift_id) if live_mode else []      # 관찰 중·서술 중 회색 카드 — 초안 표에는 아직 없다(메모리)

    def curve(tag):
        """근무 구간 전체 곡선 — 같은 태그를 여러 항목이 쓰면 한 번만 읽는다."""
        if tag not in curves:
            curves[tag] = _shift_curve(shift_id, tag)
        return curves[tag]

    items = []
    rows = []           # 쌓이는 중이면 한 줄 목록의 차례(AI 작성 중 · 관찰 중 · 초안)
    low = []            # 지난 근무에서 제외한 것 — 지우지 않고 최하단으로 내린다 (#30)
    on_n = 0
    quality = None
    with db.connect() as conn:
        quality = db.load_quality(conn, shift_id)
        opened = db.open_items(conn, shift_id)              # 앞 근무에서 진행중으로 남긴 것 — 맨 위 접힌 묶음
        choices = db.carried_choices(conn, draft["id"])     # 재검토로 돌아왔으면 앞서 고른 것을 되살린다
    qbanner = ""
    qs = pipeline.quality_summary(quality)
    if qs:
        qbanner = ('<div class="note" style="margin:0 0 10px;border-left:3px solid var(--accent);padding-left:10px"><b>원본 데이터 품질</b> — ' + esc(qs[0]) + ' · ' + esc(qs[1]) + '</div>')
    own = _by_time([it for it in draft["items"] if it["origin"] != "carried"], _starts(shift_id))   # 이월 판단 행은 승인 때 다시 쓴다
    keys = {v["draft_item_id"]: v["key"] for v in lives if v.get("draft_item_id")}
    for it in own:
        pe = it.get("prev_excluded") if hasattr(it, "keys") else None
        # 지난 근무에서 제외한 것과 같은 (태그·종류) 는 최하단 칸으로 내린다. 숨기지 않는다 — 한 번의 제외가 영구 삭제가 되면 안 된다 (#30).
        if _item_on(it):
            on_n += 1
        wf, wm = waves.get(it.get("event_id")) or (None, {})
        # 표식은 재생이 떠난 뒤에도 있어야 한다 — 없으면 폴링이 같은 카드를 못 알아보고 한 장 더 넣는다(실제 재생에서 봤다)
        (low if pe else items).append(
            _item_card(it, wf, wm, it.get("curve") or curve(it["tag"]), key=keys.get(it["id"]) or f"i{it['id']}"))
    if live_mode:
        # 쌓이는 중에는 폴링과 똑같은 함수로 그린다 — 첫 그림과 2초 뒤 그림이 갈라지면 카드가 뛴다
        same = live.status().get("shift_id") == shift_id
        rows = _live_rows(shift_id, draft, lives, same)
        items = [r["html"] for r in rows]

    if not items and not low:
        items.append(_EMPTY_CARD)

    disc = ""
    if ports.engine_source() == "stub":
        disc = ('<div class="disc">임시 엔진으로 돌고 있습니다. 알람 한계를 넘은 것만 잡히고, '
                '헌팅·드리프트·상관 붕괴 같은 신호는 감지되지 않습니다. '
                '검출 엔진이 연결되면 이 목록이 달라집니다.</div>')

    # 최하단 「이전에 제외한 것」 — 접혀 있지만 건수는 항상 보인다.
    # 접힘은 읽기 부담을 줄이려는 것이지 감추려는 것이 아니다.
    lowbox = ""
    if low:
        lowbox = (
            '<details class="card" style="padding:10px 16px">'
            '<summary style="cursor:pointer;font-weight:600">이전에 제외한 것 — '
            + str(len(low)) + '건</summary>'
            + "".join(low) + '</details>')

    date, kind = shift_id.rsplit("-", 1)
    n = len(own)

    # 마감된 초안 화면도 폴링이 갱신한다(data-live 는 「이 목록은 서버가 관리한다」는 표식이다).
    # 안 하면 화면을 열어 둔 사이 cli.py run 이 초안을 다시 만들어도 옛 목록이 남고, 승인하는 순간
    # 폼에 없던 새 항목이 전부 제외로 닫힌다(approve.decide 의 adopted IS NULL → 0 — codex 홀리스틱 반증).
    # 쌓이는 중(status='live')이어도 AI 가 다 쓴 항목은 고를 수 있다 — 관찰 중·서술 중은 회색이라 입력이 없고,
    # 잠그는 것은 승인 버튼뿐이다(경모님 결정 §0). 폼을 우회한 제출은 approve.decide 가 막는다.
    # 잠금 문구는 live.approve_lock 이 정한다(쌓이는 중 · AI 서술 실패 남음 · 앞 근무가 아직 승인 대기 등).
    lock = live.approve_lock(shift_id)
    lst = live.status()
    live_pill = ('<span class="pill live">LIVE · 쌓이는 중</span>'
                 + (f'<span class="rclock mono" style="margin-left:8px;font-size:13px;color:var(--live)">'
                    f'{esc(_clock_text(lst.get("clock")))}</span>' if lst.get("shift_id") == shift_id and lst.get("clock") else "")
                 ) if live_mode else ""
    # 쌓이는 중에는 「감지 N건」이 초안 표에 든 수라 화면의 회색 카드와 어긋난다(지휘자 실측: 회색 2장인데 「감지 0건」).
    # 지금 보이는 것을 그대로 센다. 「전체 표시」는 마감 뒤 초안에서만 뜻이 있다.
    summary = _live_summary(rows) if live_mode else f'감지 <b style="color:var(--ink)">{n}</b>건'

    head = ('<form method="post" action="/approve" onsubmit="return chk(this)">'
            f'<input type="hidden" name="shift_id" value="{esc(shift_id)}">')
    tail = ('<div class="card" style="padding:12px 16px;display:flex;justify-content:flex-end">'
            '<button type="button" class="btn ghost" onclick="addMan()">+ 항목 직접 추가</button></div>'
            f'<div class="bar"><div class="cnt">채택 <b id="n">{on_n}</b> / <span id="tot">{n}</span>건 '
            '<span id="need" class="need"></span>'
            f'<span id="approvelock" class="need">{esc(lock or "")}</span></div>'
            f'<button type="submit" id="approve" class="btn"{" disabled" if lock else ""}>승인하고 확정</button></div></form>')
    # 설명 문구는 화면에 두지 않는다 — 상태 이름과 잠금 사유만 남긴다(경모님 지시 2026-09-15).
    # 「‹ 일지 목록」 줄은 없다 — 목록이 늘 왼쪽에 있다(모바일은 「근무 고르기」).
    return f"""{disc}
{qbanner}
<div class="card" style="display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;
     align-items:flex-start">
<div><h2 style="margin-bottom:4px">{esc(date)} {"주간조" if kind == "day" else "야간조"}
인수인계 초안 {live_pill}</h2></div>
<div class="muted" style="font-size:13px" id="summary">{summary}
{f'<br><span style="font-size:11.5px">이전에 제외한 것 {len(low)}건은 최하단</span>' if low else ""}
</div>
</div>
{head}
{_carry_box(opened, choices, editable=True)}
<div id="items" data-shift="{esc(shift_id)}" data-live="1">
{"".join(items)}
</div>
{lowbox}
<div id="mans"></div>
{tail}"""



def view_shift(shift_id):
    """그 근무를 고른 채로 같은 한 화면을 그린다 — 주소는 유지된다."""
    return _one_page(shift_id)


def _shift_body(shift_id):
    """한 화면의 오른쪽 — 초안 검토 또는 확정 일지."""
    with db.connect() as conn:
        draft = db.load_draft(conn, shift_id)
        handover = db.load_handover(conn, shift_id)
        row = conn.execute(
            "SELECT window_start FROM shift WHERE id = ?", (shift_id,)
        ).fetchone()

    if draft is None:
        return (f'<div class="card"><div class="empty">{esc(shift_id)} 의 초안이 '
                f'없습니다.<br><code class="mono">python3 app/cli.py run '
                f'{esc(shift_id)}</code></div></div>')

    draft["window_start"] = row["window_start"] if row else None
    if handover:
        return _view_confirmed(shift_id, draft, handover)
    return _view_pending(shift_id, draft)


# --- 서버 -------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ENGRA"

    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path}")

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in OLD_PATHS:
                self.send_response(303); self.send_header("Location", OLD_PATHS[path]); self.end_headers()
            elif path == "/draft":
                self._send(200, view_draft())
            elif path == "/admin":
                self._send(200, view_admin(self))
            elif path.startswith("/answer/"):
                self._send(200, view_answer(path.rstrip("/").split("/")[-1]))   # /answer/<근무> (옛 /answer/<정답지>/<근무> 도 마지막 조각)
            elif path == "/api/job":
                self._json(jobs.state())
            elif path == "/api/live":
                st = live.status()
                self._json({"status": st, "line": _live_line(st)})
            elif path == "/api/live/values":
                v = live.values()      # {태그: [시각, 값]} → 앱 숫자 규칙으로 찍은 글자만 내보낸다(지수 표기 금지)
                self._json({"clock": v.get("clock"),
                            "values": {t: numfmt.fmt(x[1]) for t, x in (v.get("values") or {}).items()}})
            elif path == "/api/live/cards":
                q = parse_qs(urlparse(self.path).query)
                self._json(live_cards(q.get("shift", [""])[0]))
            elif path == "/dcs":
                self._send(200, view_dcs())
            elif path == "/asu":
                self._send(200, view_asu())
            elif path.startswith("/shift/"):
                self._send(200, view_shift(path[len("/shift/"):]))
            elif path == "/api/shifts":
                with db.connect() as conn:
                    self._json([dict(r) for r in db.list_shifts(conn)])
            elif path.startswith("/api/draft/"):
                with db.connect() as conn:
                    self._json(db.load_draft(conn, path[len("/api/draft/"):]))
            else:
                self._send(404, page("없음", '<div class="card"><div class="empty">없는 주소입니다.'
                                             '<br><a class="back" style="margin-top:12px" href="/draft">일지 목록으로</a>'
                                             '</div></div>'))
        except Exception as exc:  # 화면에 그대로 드러낸다 — 조용히 넘기지 않는다
            self._send(500, page("오류", f'<div class="card"><h2>오류</h2>'
                                        f'<pre class="mono">{esc(exc)}</pre>'
                                        f'<a class="back" style="margin-top:12px" href="/draft">일지 목록으로</a></div>'))

    MAX_BODY = 256 * 1024           # 승인·리셋 폼. 코멘트 수십 개라도 수십 KB
    MAX_UPLOAD = 48 * 1024 * 1024   # 생성기 12h CSV 35.8MB + 정답지 = 근무 하나. 라이브 실측: 37.5MB 업로드에 MemoryPeak 420MB(email 파서 ~11배) — MemoryMax 700MB 라 96MB 면 OOM (Codex 반증 2026-08-27)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        ctype = self.headers.get("Content-Type", "")
        is_upload = ctype.startswith("multipart/form-data")
        cap = self.MAX_UPLOAD if is_upload else self.MAX_BODY
        if length < 0 or length > cap:
            self._send(413, page("요청이 너무 큽니다", f'<div class="card"><div class="empty">'
                                                  f'본문 {length:,}B — 허용 {cap:,}B</div></div>'))
            return
        if is_upload:
            # 한 번에 하나만 — 본문을 읽기 전에 줄을 서므로 두 번째 업로드는 메모리를 안 먹고 기다린다.
            # (409 로 거절하면 브라우저가 본문을 다 보내기 전에 끊겨 "연결 재설정"만 본다 — 실측)
            with jobs.upload_lock:
                raw = self._read_body(length)
                if raw is None:
                    return
                self._handle_upload(ctype, raw)
            return
        self._handle_form(self.rfile.read(length))

    UPLOAD_STALL_S = 30      # recv 한 번이 이만큼 멎으면 끊는다
    UPLOAD_TOTAL_S = 300     # 96MB 를 이 안에 못 보내면 끊는다 (1MB/s 면 96s)

    def _read_body(self, length):
        """업로드 본문을 조각으로 읽는다. 멎은 클라이언트가 upload_lock 을 무기한 잡는 것을 막는다
        (Codex 반증 2026-08-27). 시간 초과면 408 을 보내고 None."""
        import time
        deadline = time.monotonic() + self.UPLOAD_TOTAL_S
        buf = bytearray()
        try:
            while len(buf) < length:
                # read1 = recv 한 번. read(n) 은 n 바이트 찰 때까지 recv 를 반복해 조금씩 흘리는
                # 클라이언트가 deadline 검사를 영원히 피한다 (Codex 3차 반증).
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(min(self.UPLOAD_STALL_S, left))
                chunk = self.rfile.read1(min(1 << 20, length - len(buf)))
                if not chunk:
                    raise TimeoutError
                buf += chunk
        except (TimeoutError, OSError):
            print(f"  UPLOAD 시간 초과 {len(buf):,}/{length:,}B")
            self.connection.settimeout(self.UPLOAD_STALL_S)   # 마지막 반복의 극소 timeout 이 응답 전송까지 남지 않게 (Codex 4차)
            try:
                self._send(408, page("업로드 시간 초과", '<div class="card"><div class="empty">전송이 멎어 끊었습니다. 다시 올려 주세요.</div></div>'))
            except OSError:
                pass
            return None
        self.connection.settimeout(self.UPLOAD_STALL_S)
        return bytes(buf)

    MAX_RAW = 64 * 1024 * 1024   # .gz 를 풀었을 때 파일 하나 상한 — 압축 폭탄이 메모리를 채우지 않게

    @classmethod
    def _gunzip(cls, data):
        """스트리밍으로 풀며 상한을 넘으면 즉시 멈춘다. gzip.decompress 는 다 풀고 나서야 크기를 알 수 있다."""
        import zlib
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = bytearray()
        chunk = data
        while True:
            out += d.decompress(chunk, cls.MAX_RAW + 1 - len(out))
            if len(out) > cls.MAX_RAW:
                raise ValueError(f"압축을 풀면 {cls.MAX_RAW // 1048576}MB 를 넘습니다 — 근무 하나씩 올리세요")
            chunk = d.unconsumed_tail
            if not chunk:
                break
        out += d.flush()
        if not d.eof:
            raise ValueError("gzip 스트림이 끝까지 오지 않았습니다 — 업로드가 중간에 끊긴 파일입니다")
        if d.unused_data:
            raise ValueError("gzip 뒤에 다른 데이터가 붙어 있습니다 — 파일 하나만 눌러 보내세요")
        if len(out) > cls.MAX_RAW:
            raise ValueError(f"압축을 풀면 {cls.MAX_RAW // 1048576}MB 를 넘습니다 — 근무 하나씩 올리세요")
        return bytes(out)

    def _upload_file(self, part):
        """multipart 한 조각 → (저장할 이름, 내용). 이름은 jobs.upload_name 으로만 정리한다 — 검사와 저장이 같은 이름을
        봐야 「x.json/」 같은 이름이 열쇠 검사를 빠져나가지 않는다(조각 1 2차 재반증).
        브라우저가 CompressionStream 으로 눌러 보낸 .gz 는 여기서 푼다 — 37MB CSV 가 수 MB 로 줄어 업로드가 수 초.
        .gz 를 먼저 떼고 이름을 한 번만 검사한다 — 압축 때문에 이름이 3바이트 길어져, 같은 파일이 크기에 따라 되다 안 되다 했다(조각 1 3차 재반증)."""
        name, data = part.get_filename(), part.get_payload(decode=True)
        if jobs.name_key(name).endswith(".gz"):
            data, name = self._gunzip(data), name[:-3]
        return jobs.upload_name(name), data

    def _check_upload_file(self, part):
        """저장 전 검사 → 저장할 이름. 거부 사유는 예외로 올린다. 정답지인지는 저장할 이름으로 판정한다(jobs.is_key_name)."""
        name, data = self._upload_file(part)
        if jobs.is_key_name(name):
            if not _admin_ok(self):
                raise PermissionError("정답지 JSON 은 열쇠가 필요합니다 — CSV 만 따로 올리거나 관리에서 열쇠를 넣으세요")
            jobs.key_shifts(data)
        return name

    def _handle_upload(self, ctype, raw):
        # 생성기 CSV + 정답지 JSON. 표준 라이브러리 email 파서로 multipart 를 푼다.
        import email.parser, email.policy
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
        # 저장하기 전에 올린 파일을 전부 검사하고, 하나라도 거부되면 아무것도 저장하지 않는다. 파일마다 저장하며 검사했더니
        # 열쇠 잠금에서 CSV·정답지 JSON 을 함께 올리면 앞의 CSV 는 uploads/ 에 남고 적재는 안 되는 부분 상태가 생겼다(조각 1 반증).
        # 검사에서 푼 내용은 들고 있지 않고 저장할 때 다시 푼다 — 전부 풀어 두면 압축 폭탄 여러 개(파일당 MAX_RAW)로 메모리가 찬다.
        parts = [part for part in msg.iter_parts() if part.get_filename()]
        rejected, names = [], {}
        for part in parts:
            shown = str(part.get_filename()).replace("\x00", "\\x00")
            try:
                name = self._check_upload_file(part)
            except Exception as exc:
                rejected.append(f"{shown}: {exc}")
                continue
            names.setdefault(jobs.name_key(name), (name, []))[1].append(shown)
        # 정리·정규화하면 같은 이름이 되는 파일이 한 요청에 둘 — 알림 없이 뒤 파일이 앞 파일을 덮었다(조각 1 2·3차 재반증)
        rejected += [f"같은 이름으로 저장될 파일이 둘: {name} ← {' , '.join(src)}" for name, src in names.values() if len(src) > 1]
        if rejected:
            self._send(400, page("업로드 실패", '<div class="card"><div class="empty">아무 파일도 저장하지 않았습니다 — '
                                             + esc(" · ".join(rejected)) + '</div></div>'))
            return
        try:
            paths, key_sids = jobs.save_uploads(self._upload_file(part) for part in parts)
        except Exception as exc:   # 검사를 통과한 뒤의 실패 = 서버 쪽 문제. 경로 같은 자세한 내용은 로그에만 — 응답에 서버 절대 경로가 나갔다(조각 1 2차 재반증)
            print(f"  !! UPLOAD 저장 실패: {type(exc).__name__}: {exc}")
            self._send(500, page("업로드 실패", '<div class="card"><div class="empty">업로드를 끝내지 못했습니다 — 서버 오류('
                                             + esc(type(exc).__name__) + '). 자세한 내용은 서버 로그에 있습니다.</div></div>'))
            return
        saved = [p.name for p in paths]
        print("  UPLOAD " + str(saved))
        csvs = [p for p in paths if p.suffix == ".csv"]
        queued = (not jobs.ingest_async(csvs)) if csvs else False   # 올린 CSV 는 바로 적재 → 그 안의 근무가 전부 목록에
        jobs.note_upload(saved, key_sids, queued)
        self.send_response(303); self.send_header("Location", "/admin"); self.end_headers()
        return

    def _handle_form(self, raw):
        form = parse_qs(raw.decode("utf-8", "replace"))
        try:
            path = urlparse(self.path).path
            if path == "/admin/unlock":
                key = form.get("key", [""])[0]
                self.send_response(303); self.send_header("Location", "/admin")
                if _admin_key() and key == _admin_key():
                    secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""   # Caddy 뒤에서만 — 로컬 http 시험은 그대로
                    self.send_header("Set-Cookie", f"engra_admin={key}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200{secure}")
                self.end_headers(); return
            if path in ("/admin/ai_check", "/pipeline/ingest_skip", "/reset", "/live/start", "/live/stop", "/live/retry", "/live/restart") or (path == "/pipeline/run" and form.get("redo", ["0"])[0] == "1"):
                if not _admin_ok(self):
                    self._send(403, page("잠김", '<div class="card"><div class="empty">심사 기간에는 관리 동작에 열쇠가 필요합니다 — <a href="/admin">관리</a>에서 열쇠를 넣으세요.</div></div>')); return
            if path == "/admin/ai_check":
                global _ai_check
                import time as _t
                ok, msg = llm.check()
                if "이미 진행 중" not in msg:   # 진행 중 안내는 앞선 결과를 덮지 않는다
                    _ai_check = (ok, _t.strftime("%H:%M:%S ") + msg)
                self.send_response(303); self.send_header("Location", "/admin"); self.end_headers(); return
            if path == "/pipeline/ingest_skip":
                try:
                    started = jobs.ingest_skip(form.get("file", [""])[0])
                except FileNotFoundError as exc:
                    self._send(400, page("없음", '<div class="card"><div class="empty">' + esc(exc) + '</div></div>')); return
                self.send_response(303); self.send_header("Location", "/admin"); self.end_headers(); return
            if path == "/pipeline/run":
                sid = form["shift_id"][0]
                redo = form.get("redo", ["0"])[0] == "1"
                if not any(x["shift_id"] == sid for x in jobs.sources()):
                    self._send(400, page("없음", '<div class="card"><div class="empty">적재된 근무가 아닙니다. 먼저 CSV 를 올리세요.</div></div>')); return
                with db.connect() as conn:
                    row = conn.execute("SELECT ingested_at FROM shift WHERE id = ?", (sid,)).fetchone()
                    confirmed = db.load_handover(conn, sid) is not None
                    stale = not db.raw_complete(conn, sid)   # 3일 회전이 원본을 지웠거나 앞부분을 잘랐다 → 파일에서 다시 적재
                csv = jobs.csv_for(sid)
                newer = False
                if csv is not None:
                    from datetime import datetime
                    try:
                        # 같은 근무 ID 로 새 파일을 올렸으면(파일이 적재 시각보다 새로움) 그 파일로 다시 적재. ingested_at 은 초 단위 → 1초 여유 (Codex)
                        newer = csv.stat().st_mtime > datetime.fromisoformat(row["ingested_at"]).timestamp() + 1
                    except (OSError, TypeError, ValueError):
                        newer = False
                if confirmed and not redo:   # 확정 근무는 제품 흐름에서 다시 돌리지 않는다 — 비동기 실패 대신 바로 안내
                    self._send(400, page("확정된 근무", '<div class="card"><div class="empty">' + esc(sid) + ' 는 확정된 근무입니다. 다시 만들려면 <a href="/admin">관리</a>에서 「확정돼 있어도 다시 만들기」를 켜고 실행하세요.</div></div>')); return
                if (newer or stale) and csv is None:
                    self._send(400, page("원본 없음", '<div class="card"><div class="empty">' + esc(sid) + ' 의 원본이 보관 기간(3일)이 지나 정리됐고 올린 파일도 없습니다. 그 근무의 CSV 를 다시 올리세요.</div></div>')); return
                if (newer or stale) and confirmed and not redo:   # 재적재 뒤 비동기로 "확정된 근무" 실패하지 않게 먼저 막는다 (Codex)
                    self._send(400, page("확정된 근무", '<div class="card"><div class="empty">' + esc(sid) + ' 는 확정된 근무인데 원본을 다시 적재해야 합니다. 새로 만들려면 「확정돼 있어도 다시 만들기」를 켜세요.</div></div>')); return
                try:
                    jobs.run_async(sid, csv_path=str(csv) if (newer or stale) else None, redo=redo,
                                   reingest=(newer or stale), why=("새 파일" if newer else "보관 기간(3일)이 지나 원본이 정리됨") if (newer or stale) else None)
                except RuntimeError as exc:
                    self._send(409, page("실행 중", '<div class="card"><div class="empty">' + esc(exc) + '</div></div>')); return
                self.send_response(303); self.send_header("Location", "/admin"); self.end_headers()
                return
            if path in ("/live/start", "/live/stop", "/live/retry", "/live/restart"):
                try:
                    csvs = [x.strip() for x in form.get("csv", []) if x.strip()]      # 고른 순서 = 이어 재생 순서
                    prev_name = (form.get("prev", [""])[0] or "").strip() or None
                    raw = (form.get("speed", ["100"])[0] or "100").strip()
                    try:
                        speed = float(raw)
                    except ValueError:
                        raise ValueError(f"배속은 숫자여야 합니다. 받은 것: {raw!r}") from None
                    if speed > LIVE_MAX_SPEED:
                        raise ValueError(f"배속은 {LIVE_MAX_SPEED} 이하로 주세요 — 틱 한 번이 1초 남짓이라 그보다 높이면 재생이 밀립니다.")
                    if path == "/live/restart":
                        # 경모님이 누르는 단추는 둘뿐이다 — 「재생 시작」, 막히면 「처음부터 다시 돌리기」.
                        # 지금까지 사람이 서버에서 세 단계로 하던 것(정지 → 기준선 복원 → 재생)을 한 번으로 묶는다.
                        if form.get("sure", ["0"])[0] != "1":
                            self._send(400, page("확인 필요", '<div class="card"><div class="empty">화면의 확인 대화상자를 거쳐야 합니다.</div></div>')); return
                        # 고른 파일이 실제로 있는지 먼저 본다 — 뒤에서 재생이 거절되면 되돌리기만 되고 재생은 안 된 상태가 남는다
                        up_dir = Path(jobs.UPLOAD_DIR)
                        have = {x.name for x in up_dir.glob("*.csv")} if up_dir.is_dir() else set()
                        unknown = [c for c in csvs + ([prev_name] if prev_name else []) if c not in have]
                        if unknown:
                            raise ValueError(f"업로드 폴더에 없는 파일입니다: {unknown[0]!r}")
                        try:
                            live.stop()
                        except ValueError:
                            pass                      # 안 돌고 있으면 그대로 다음 단계로
                        held = jobs.hold()
                        if held is None:
                            self._send(409, page("실행 중", '<div class="card"><div class="empty">적재·검출 작업이 돌고 있습니다. 끝난 뒤 다시 누르세요.</div></div>')); return
                        try:
                            jobs.mark_qa_active()
                            db.reset(empty=False)     # 기준선이 없으면 FileNotFoundError — 조용히 빈 상태로 가지 않는다
                        except (FileNotFoundError, ValueError) as exc:
                            self._send(400, page("기준선 없음", '<div class="card"><div class="empty">' + esc(exc) + '</div></div>')); return
                        finally:
                            jobs.release_hold(held)
                    if path in ("/live/start", "/live/restart"):
                        live.start(csvs, speed=speed, prev_csv_name=prev_name,
                                   replace_unconfirmed=form.get("replace", ["0"])[0] == "1")
                    elif path == "/live/stop":
                        live.stop()
                    else:
                        live.retry((form.get("key", [""])[0] or "").strip())
                except ValueError as exc:      # 사용자 잘못(이름·순서·재생 중 아님 등) — live 의 문장을 그대로 보인다
                    hint = ('<br><span class="muted">관리 화면에서 <b>처음부터 다시 돌리기</b> 를 누르면 기준선으로 되돌리고 바로 재생합니다.</span>'
                            if "초안" in str(exc) else "")
                    self._send(400, page("재생", '<div class="card"><div class="empty">' + esc(exc) + hint + '</div></div>')); return
                self.send_response(303); self.send_header("Location", "/admin"); self.end_headers(); return
            if path == "/reopen":
                # 확정 후 잘못 적은 것을 고친다. 이전 확정본은 이력에 남는다.
                sid = form["shift_id"][0]
                reason = ((form.get("reason", [""])[0] or "").strip() or None)
                if reason:
                    reason = reason[:200]
                with db.connect() as conn:
                    rnd = db.reopen_handover(conn, sid, reason)
                print(f"  REOPEN {sid} (이전 {rnd}차 확정 → 이력)")
                self.send_response(303)
                self.send_header("Location", f"/shift/{sid}")
                self.end_headers()
                return
            if path == "/reset":
                # QA 용. 확정을 눌러도 되돌릴 수 있어야 마음 놓고 눌러본다.
                empty = form.get("empty", ["0"])[0] == "1"
                if empty and form.get("sure", ["0"])[0] != "1":
                    self._send(400, page("확인 필요", '<div class="card"><div class="empty">빈 상태 리셋은 확정 이력까지 지웁니다. '
                                                    '화면의 확인 대화상자를 거쳐야 합니다.</div></div>'))
                    return
                held = jobs.hold()   # 실행 스레드가 DB 파일을 잡고 있는데 파일을 갈아끼우면 그 스레드는 지워진 inode 에 쓴다 — 락을 잡은 채 교체 (Codex TOCTOU)
                if held is None:
                    self._send(409, page("실행 중", '<div class="card"><div class="empty">적재·검출 작업이 돌고 있습니다. 끝난 뒤 리셋하세요.</div></div>')); return
                try:
                    jobs.mark_qa_active()
                    what = db.reset(empty=empty)
                    if empty:
                        jobs.clear_runtime(wipe_uploads=True)   # 정답지·올린 파일·마지막 실행·대기 큐까지 — 진짜 빈 상태
                finally:
                    jobs.release_hold(held)
                self.send_response(303)
                self.send_header("Location", "/admin?reset=" + ("empty" if empty else "seed"))
                self.end_headers()
                print(f"  RESET → {what}")
                return
            if path != "/approve":
                self._send(404, page("없음", '<div class="card"><div class="empty">없는 주소입니다.'
                                             '<br><a class="back" style="margin-top:12px" href="/draft">일지 목록으로</a>'
                                             '</div></div>'))
                return

            shift_id = form["shift_id"][0]

            def _back(msg, items=()):
                """상태가 빈 채택 항목 — 어느 항목인지 짚어 주고 초안으로 되돌려 보낸다. 저장된 것은 없다."""
                lis = "".join(f'<li>#{i} {esc(t)}</li>' for i, t in items)
                self._send(400, page("승인 안 됨", f'<div class="card"><h2>승인되지 않았습니다</h2>'
                                                  f'<p class="note">{esc(msg)}</p>'
                                                  + (f'<ul style="margin:0 0 10px 18px;font-size:13px">{lis}</ul>' if lis else "")
                                                  + f'<a class="back" href="/shift/{esc(shift_id)}">‹ 초안으로 돌아가 상태를 고른다</a></div>'))

            try:
                chosen = {int(v) for v in form.get("item", [])}
            except ValueError:      # 위조된 항목 번호 — 요청 잘못이라 400 (500 이 났다)
                _back(f"항목 번호가 아닙니다: {', '.join(form.get('item', []))}"); return

            # 직접 추가는 여러 건이 올 수 있다. 제목·내용·상태가 같은 이름으로 반복 전송되고 순서로 짝짓는다.
            # parse_qs 기본값은 빈 값을 버려 짝이 어긋난다(앞 건 내용이 비면 뒤 건 내용이 당겨졌다 — 실측). 빈 값을 살려 읽는다.
            # 넣는 것은 decide 가 승인과 같은 트랜잭션에서 한다 — 승인이 거부되면 함께 되돌아간다.
            keep = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
            titles = keep.get("manual_title", [])
            bodies = keep.get("manual_body", [])
            mstat = keep.get("manual_status", [])
            manual = [{"title": t.strip(),
                       "body": bodies[i].strip() if i < len(bodies) else "",
                       "status": (mstat[i].strip() if i < len(mstat) else "") or None}
                      for i, t in enumerate(titles) if t.strip()]

            with db.connect() as conn:
                draft = db.load_draft(conn, shift_id)
            if draft is None:       # 초안 없는 근무 — 요청 잘못이라 400 (draft["items"] 에서 500 이 났다)
                _back(f"{shift_id} 의 초안이 없습니다."); return
            decisions = {
                it["id"]: {
                    "adopted": it["id"] in chosen or it["origin"] == "manual",
                    "comment": (form.get(f"comment_{it['id']}", [""])[0] or "").strip() or None,
                    "status": (form.get(f"status_{it['id']}", [""])[0] or "").strip() or None,
                }
                for it in draft["items"] if it["origin"] != "carried"
            }
            # 이월 항목 판단: carry_<원 항목 id> = 완료 | 진행중. 고르지 않은 것은 그대로 열린 채 넘어간다.
            carried = {}
            for k, v in form.items():
                if k.startswith("carry_") and not k.startswith("carry_comment_"):
                    st = (v[0] or "").strip()
                    if st:
                        try:
                            rid = int(k[len("carry_"):])
                        except ValueError:      # 위조된 키 — 요청 잘못이라 400 (500 이 났다)
                            _back(f"이월 항목 번호가 아닙니다: {k}"); return
                        carried[rid] = {"status": st, "comment": (form.get(f"carry_comment_{rid}", [""])[0] or "").strip() or None}
            try:
                approve_mod.decide(shift_id, decisions, carried=carried, manual=manual)
            except approve_mod.StatusMissing as exc:
                _back("채택한 항목마다 완료 / 진행중을 골라야 승인됩니다. 아래 항목이 비어 있습니다.", exc.items); return
            except ValueError as exc:      # 위조된 상태 값·없는 항목·열려 있지 않은 이월 항목 — 요청 잘못이라 400
                _back(str(exc)); return

            self.send_response(303)
            self.send_header("Location", f"/shift/{shift_id}")
            self.end_headers()
        except Exception as exc:
            self._send(500, page("오류", f'<div class="card"><h2>오류</h2>'
                                        f'<pre class="mono">{esc(exc)}</pre>'
                                        f'<a class="back" style="margin-top:12px" href="/draft">일지 목록으로</a></div>'))


def serve(host, port):
    db.init()
    print(f"ENGRA 화면: http://{host}:{port}  (엔진: {ports.engine_source()})")
    print("멈추려면 Ctrl+C")
    try:
        jobs.reconcile_uploads()   # 재시작 뒤 적재 안 된 업로드 CSV 를 살린다
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
