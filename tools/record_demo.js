// 시연 영상 녹화 — Playwright MCP browser_run_code_unsafe 에 그대로 붙여 넣는 함수 본문.
// 로컬 서버(8000)에 AI 시드 DB 가 올라온 상태에서 돈다. 실시간·무편집. 목표 2~3분.
// 순서: DCS(전체 화면) → 근무 일지 한 화면(왼쪽 목록) → 승인 대기 초안 검토(AI 필드를 천천히) → 제외·완료/진행중·코멘트 → 승인 → 확정(재검토 버튼) → 왼쪽에서 다른 근무로
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
  await p.goto(base + '/dcs'); await pause(2500); mark('DCS'); await pause(4800);   // 전체 화면이라 스크롤이 없다
  await p.goto(base + '/draft'); await pause(2500); mark('근무 일지');
  // 먼저 확정 일지로 옮겨 「왼쪽에서 근무를 고른다」를 보인다 — /draft 가 초안을 이미 펴 놓아서
  // 초안 줄부터 누르면 제자리 클릭이 되어 장면이 사라진다(반증 워커).
  await p.click('a.nrow:not(.on)'); await pause(2600); mark('확정 일지'); await scroll(400, 4, 600); await pause(1500);
  await p.click('a.nrow:has(.pill.amber)'); await pause(3000); mark('초안');   // 왼쪽 목록의 「초안」 줄
  await scroll(900, 10, 900); await pause(2500);
  await scroll(1400, 10, 700); await pause(1500);
  // 항목은 위치가 아니라 제목의 태그로 고른다 — 화면 순서가 감지 시각순으로 바뀌자 「첫 칸에 코멘트 · 끝 두 칸 제외」 가 엉뚱한
  // 항목에 들어갔다(반증 워커 실측: 코멘트가 JI-206 대신 MI-102 로). 본문에 같은 태그가 적힌 다른 항목이 걸리지 않게 제목(.ttl) 기준,
  // 접힌 「이전에 제외한 것」 안의 보이지 않는 항목은 건너뛴다.
  const COMMENT_TAG = 'JI-206', EXCLUDE_TAGS = ['LI-806', 'TI-603'];
  const itemFor = async (tag) => {
    const i = await p.$$eval('.detail .item', (els, t) => els.findIndex((el) => {
      const h = el.querySelector('.ttl');
      return !!h && el.offsetParent !== null && h.textContent.trim().startsWith(t + ' ');
    }), tag);
    return i < 0 ? null : p.locator('.detail .item').nth(i);
  };
  const excluded = [];
  for (const tag of EXCLUDE_TAGS) {
    const it = await itemFor(tag);
    const box = it && it.locator('input[name="item"]');
    if (box && await box.isChecked()) { await box.scrollIntoViewIfNeeded(); await pause(700); await box.click(); await pause(900); excluded.push(tag); }
  }
  mark(excluded.length ? `${excluded.length}건 제외 ${excluded.join('·')}` : `제외 대상(${EXCLUDE_TAGS.join('·')}) 없음`);
  // 채택한 항목마다 완료/진행중을 골라야 승인된다(기본값 없음) — 고르지 않고 누르면 초안 화면에서 멈춘다(상태 스위치 반증 워커)
  const picks = await p.$$eval('input[name="item"]:checked', els => els.map(e => e.value));
  for (const id of picks) { const r = await p.$(`input[name="status_${id}"][value="완료"]`); if (r) { await r.scrollIntoViewIfNeeded(); await pause(350); await r.click(); } }
  if (picks.length) { await pause(900); mark('완료 ' + picks.length + '건'); }
  const target = await itemFor(COMMENT_TAG);
  if (target) {
    const ta = target.locator('textarea[name^="comment_"]');
    await ta.scrollIntoViewIfNeeded(); await pause(600); await ta.click();
    await ta.type('현장 확인 — 인터쿨러 냉각수 유량 정상, 추이 계속 감시 요청', { delay: 45 }); await pause(1200); mark('코멘트 → ' + COMMENT_TAG);
  } else { mark(`코멘트 대상 ${COMMENT_TAG} 없음`); }
  const btn = await p.$('.bar button[type="submit"]'); await btn.scrollIntoViewIfNeeded(); await pause(1200); await btn.click(); await pause(3500);
  mark('승인 → ' + p.url() + ((await p.$('.card.det')) ? ' · 확정 일지 열림' : ' · 확정 안 됨'));
  await scroll(700, 6, 600); await pause(2500);
  // 되돌아가기 줄은 없다 — 목록이 늘 왼쪽에 있다. 다른 근무를 눌러 한 화면에서 옮겨 가는 것을 보인다.
  const other = await p.$('a.nrow:not(.on)');
  if (other) { await other.click(); await pause(2500); mark('다른 근무'); await scroll(400, 3, 600); await pause(2500); }
  const video = p.video(); await ctx.close();
  return { path: await video.path(), log, total_s: ((Date.now() - t0) / 1000).toFixed(0) };
}
