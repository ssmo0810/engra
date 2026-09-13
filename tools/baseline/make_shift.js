#!/usr/bin/env node
/* 사람 기준치 측정용 근무 1개 생성 — 정답을 아무도 보지 않은 채로 만든다.

     node tools/baseline/make_shift.js                           # 2026-09-14 주간
     node tools/baseline/make_shift.js --date 2026-09-15 --kind night

   왜 브라우저 생성기(docs/asu_dcs_overview.html)를 그대로 안 쓰나 — 근무를 만들면 주입한
   시나리오 목록을 곧바로 화면에 그린다(renderShift). 시험 받을 사람이 누르면 그 순간 정답을
   본다. 그래서 같은 스크립트를 Node 에서 원본 그대로 돌리고, 화면에 그리는 함수만 비운다.
   데이터·정답지 로직은 한 줄도 옮겨 적지 않는다 — 생성기가 고쳐지면 그대로 따라간다.

   출력은 tools/out/ 아래라 커밋되지 않는다(main 은 Pages 로 공개된다 — .gitignore 참고).
     tools/out/baseline/<근무ID>/shift.csv               ← 시험 받는 사람에게 주는 파일
     tools/out/baseline/<근무ID>/tag_master.csv          ← 같이 준다 (docs/ 정본 복사 — 엔진과 같은 한계값)
     tools/out/baseline/<근무ID>/seal.txt                ← 위 두 파일과 정답지의 SHA-256
     tools/out/baseline/<근무ID>/sealed/answer_key.json  ← 채점 전까지 열지 않는다

   화면에는 주입 건수조차 찍지 않는다. 몇 건인지 알면 그만큼 찾고 멈추게 된다.
*/
'use strict';
// 생성기는 로컬 시각으로 06·18시 경계를 자른다. PC 시간대가 달라도 같은 근무가 나오게 고정한다.
process.env.TZ = 'Asia/Seoul';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const crypto = require('crypto');

const ROOT = path.resolve(__dirname, '..', '..');
const GENERATOR = path.join(ROOT, 'docs', 'asu_dcs_overview.html');

// 초기화와 근무 생성 중에 불리는 화면 함수들. 이 중 renderShift 가 정답을 그린다.
const MUTE = ['buildTable', 'buildScenList', 'renderHist', 'renderShift', 'rtdbSync', 'smsg', 'saveRun'];

function opt(name, def) {
  const i = process.argv.indexOf('--' + name);
  return i > 1 && i + 1 < process.argv.length ? process.argv[i + 1] : def;
}

function sha256(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

function loadGenerator() {
  const html = fs.readFileSync(GENERATOR, 'utf8');
  const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1])
    .filter((s) => s.includes('function nextShift('));
  if (blocks.length !== 1) throw new Error(`nextShift 가 든 <script> 를 하나로 특정하지 못했습니다 (${blocks.length}개)`);
  const src = blocks[0];
  const open = '(function(){';
  const head = src.indexOf(open);
  const tail = src.lastIndexOf('})();');
  const before = head < 0 ? 'x' : src.slice(0, head).replace(/\/\*[\s\S]*?\*\//g, '').trim();
  if (before !== '' || tail < 0 || src.slice(tail + 5).trim() !== '') {
    throw new Error('생성기 스크립트가 즉시 실행 함수 하나로 감싸인 형태가 아닙니다 — 구조가 바뀌었으면 이 파일을 고치세요');
  }
  // 함수 선언은 본문에 들어서는 순간 이미 만들어진다. 그래서 맨 앞에서 바꿔 끼우면
  // 뒤따르는 초기화 코드와 nextShift 가 모두 빈 함수를 부른다.
  const mute = MUTE.map((n) =>
    `if (typeof ${n} !== 'function') throw new Error('생성기에 ${n} 함수가 없습니다'); ${n} = function(){};`).join('\n');
  const expose = 'globalThis.__gen = { nextShift: nextShift, csvBlob: csvBlob, cache: function(){ return CACHE; } };\n';
  const at = head + open.length;
  return src.slice(0, at) + '\n' + mute + '\n' + src.slice(at, tail) + expose + src.slice(tail);
}

function element(value) {
  return {
    value: value === undefined ? '' : value, textContent: '', innerHTML: '', hidden: false, disabled: false,
    style: {}, classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, appendChild() {}, removeChild() {}, setAttribute() {},
    getAttribute() { return null; }, querySelector() { return null; }, querySelectorAll() { return []; },
    firstChild: null,
  };
}

function makeContext(inputs) {
  const byId = new Map(Object.entries(inputs));
  const document = {
    getElementById(id) { if (!byId.has(id)) byId.set(id, element()); return byId.get(id); },
    createElement() { return element(); },
    createElementNS() { return element(); },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    body: element(),
  };
  return vm.createContext({
    document,
    window: { addEventListener() {} },
    location: { hash: '' },
    // 빈 저장소 — 브라우저에서 고쳐 둔 태그 마스터나 이전 진행 상태가 섞이지 않고, 늘 기본값·새 seed 로 시작한다.
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    confirm() { return false; },
    // 생성은 setTimeout(…, 30) 안에서 돈다(화면이 「생성 중」을 먼저 그리도록). 여기선 바로 불러 예외가 그대로 올라오게 한다.
    setTimeout(fn) { fn(); return 0; },
    Blob,
    URL,
    console,
  });
}

async function main() {
  const date = opt('date', '2026-09-14');
  const kind = opt('kind', 'day');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !['day', 'night'].includes(kind)) {
    console.error('사용법: node tools/baseline/make_shift.js [--date YYYY-MM-DD] [--kind day|night]');
    return 2;
  }
  const shiftId = `${date}-${kind}`;
  const outDir = path.join(ROOT, 'tools', 'out', 'baseline', shiftId);
  if (fs.existsSync(outDir)) {
    console.error(`${path.relative(ROOT, outDir)} 가 이미 있습니다. 앞선 시험의 정답지를 덮지 않도록 멈춥니다 — 지우거나 --date 를 바꾸세요.`);
    return 1;
  }

  const ctx = makeContext({
    sDate: element(date),
    sKind: element(kind),
    sMode: element('rand'),   // 생성기의 「랜덤 6~10건」. 사람이 고르면 고른 사람이 정답을 안다.
  });
  vm.runInContext(loadGenerator(), ctx, { filename: 'docs/asu_dcs_overview.html' });
  ctx.__gen.nextShift();

  const cache = ctx.__gen.cache();
  if (cache.length !== 1) throw new Error(`근무가 만들어지지 않았습니다 (CACHE ${cache.length}건)`);
  const c = cache[0];
  const entry = c.entry;
  if (entry.shift_id !== shiftId) throw new Error(`근무 ID 가 다릅니다: ${entry.shift_id} ≠ ${shiftId}`);
  if (!entry.injected.length) throw new Error('주입이 0건인 근무가 나왔습니다 — 시험으로 쓸 수 없으니 다시 실행하세요');

  const r = ctx.__gen.csvBlob(c, '', 0);
  const csv = Buffer.from(await r.blob.arrayBuffer());
  let lines = 0;
  for (const b of csv) if (b === 10) lines++;
  if (lines !== r.rows + 1) throw new Error(`CSV 행 수가 맞지 않습니다: ${lines} ≠ ${r.rows + 1}`);
  const key = Buffer.from(JSON.stringify(entry, null, 2), 'utf8');   // 생성기 「답안지」 버튼과 같은 모양

  fs.mkdirSync(path.join(outDir, 'sealed'), { recursive: true });
  fs.writeFileSync(path.join(outDir, 'shift.csv'), csv);
  fs.writeFileSync(path.join(outDir, 'sealed', 'answer_key.json'), key);
  // 한계선은 엔진(engine/core.py)과 같은 정본을 준다 — 사람만 다른 기준을 보면 비교가 기운다.
  // 봉인에도 넣어, 채점 때 사람이 연 파일·엔진 정본과 같은지 compare.py 가 다시 본다.
  const master = fs.readFileSync(path.join(ROOT, 'docs', 'tag_master.csv'));
  fs.writeFileSync(path.join(outDir, 'tag_master.csv'), master);
  const seal = [
    `근무 ${shiftId} · 생성 ${new Date().toISOString()}`,
    `shift.csv               SHA-256 ${sha256(csv)}`,
    `tag_master.csv          SHA-256 ${sha256(master)}`,
    `sealed/answer_key.json  SHA-256 ${sha256(key)}`,
  ].join('\n');
  fs.writeFileSync(path.join(outDir, 'seal.txt'), seal + '\n');

  console.log(`생성 완료 — ${c.ids.length}태그 × ${c.D[c.ids[0]].length.toLocaleString()}스텝 = ${r.rows.toLocaleString()}행`);
  console.log(`  ${path.relative(ROOT, outDir)}/shift.csv, tag_master.csv  ← 시험 받는 사람에게 주는 두 파일 (뷰어에서 함께 연다)`);
  console.log(`  ${path.relative(ROOT, outDir)}/sealed/     ← 채점 전까지 열지 않는다`);
  console.log('\n아래 네 줄을 시험 시작 전에 시각이 남는 곳(이슈·메신저)에 적어 두세요. 채점할 때 같은 값인지 확인합니다.');
  console.log(seal);
  return 0;
}

main().then((code) => process.exit(code), (err) => { console.error(err.stack || err); process.exit(1); });
