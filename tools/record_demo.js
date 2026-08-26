// 시연 영상 녹화 — Playwright MCP browser_run_code_unsafe 에 그대로 붙여 넣는 함수 본문.
// 로컬 서버(8000)에 AI 시드 DB 가 올라온 상태에서 돈다. 실시간·무편집. 목표 2~3분.
// 순서: DCS → RTDB → 초안 검토(AI 필드를 천천히) → 중요도 변경·제외·코멘트 → 승인 → 확정(재검토 버튼) → 일지 조회
async (page) => {
  const ctx = await page.context().browser().newContext({
    viewport: { width: 1440, height: 900 },
    recordVideo: { dir: '/Users/gyeongmopark/.claude/jobs/a9c1dab5/tmp/video2', size: { width: 1440, height: 900 } },
    locale: 'ko-KR',
  });
  const p = await ctx.newPage(); const base = 'http://127.0.0.1:8000';
  const pause = (ms) => p.waitForTimeout(ms);
  const scroll = async (y, steps = 8, ms = 600) => { for (let i = 0; i < steps; i++) { await p.mouse.wheel(0, y / steps); await pause(ms); } };
  const log = []; const t0 = Date.now(); const mark = (s) => log.push(`${((Date.now() - t0) / 1000).toFixed(0)}s ${s}`);
  await p.goto(base + '/dcs'); await pause(2500); mark('DCS'); await scroll(600, 4, 700); await pause(2000);
  await p.goto(base + '/rtdb'); await pause(2500); mark('RTDB'); await scroll(500, 4, 700); await pause(2000);
  await p.goto(base + '/draft'); await pause(3000); mark('초안');
  await scroll(900, 10, 900); await pause(2500);
  const sel = await p.$('select.sevsel');
  if (sel) { await sel.scrollIntoViewIfNeeded(); await pause(800); await sel.selectOption('중'); await pause(1500); mark('중요도 변경'); }
  await scroll(1400, 10, 700); await pause(1500);
  const boxes = await p.$$('input[name="item"]');
  if (boxes.length > 3) { await boxes[boxes.length - 2].scrollIntoViewIfNeeded(); await pause(700); await boxes[boxes.length - 2].click(); await pause(900); await boxes[boxes.length - 1].click(); await pause(900); mark('2건 제외'); }
  const ta = await p.$('textarea[name^="comment_"]');
  if (ta) { await ta.scrollIntoViewIfNeeded(); await pause(600); await ta.click(); await ta.type('현장 확인 — 인터쿨러 냉각수 유량 정상, 추이 계속 감시 요청', { delay: 45 }); await pause(1200); mark('코멘트'); }
  const btn = await p.$('button[type="submit"]'); await btn.scrollIntoViewIfNeeded(); await pause(1200); await btn.click(); await pause(3500); mark('승인 → ' + p.url());
  await scroll(700, 6, 600); await pause(2500);
  await p.goto(base + '/'); await pause(2500); mark('일지 조회'); await scroll(400, 3, 600); await pause(2500);
  const video = p.video(); await ctx.close();
  return { path: await video.path(), log, total_s: ((Date.now() - t0) / 1000).toFixed(0) };
}
