/* Apps Script 흉내 스텁 — engra_survey.gs 의 로직을 실제로 한 번 돌려 본다.
   구글에 올리기 전에 잡을 수 있는 것: 없는 변수, 깨진 루프, 문자열 조립 오류,
   글자수 제한 초과. 잡을 수 없는 것: 구글 쪽 권한·API 거동. */
const fs = require('fs');

const log = [];
const items = [];

function chain(kind) {
  const o = {_kind: kind, _t: '', _h: '', _c: []};
  o.setTitle = v => { o._t = v; return o; };
  o.setHelpText = v => { o._h = v; return o; };
  o.setChoiceValues = v => { o._c = v; return o; };
  o.setRequired = () => o;
  o.setImage = () => o;
  o.setWidth = () => o;
  items.push(o);
  return o;
}

const form = {
  setDescription: v => { form._d = v; return form; },
  setCollectEmail: () => form,
  setRequireLogin: () => form,
  setLimitOneResponsePerUser: () => form,
  setProgressBar: () => form,
  setAllowResponseEdits: () => form,
  setShowLinkToRespondAgain: () => form,
  setConfirmationMessage: () => form,
  setDestination: () => form,
  addSectionHeaderItem: () => chain('SECTION'),
  addPageBreakItem: () => chain('PAGE'),
  addMultipleChoiceItem: () => chain('CHOICE'),
  addTextItem: () => chain('TEXT'),
  addParagraphTextItem: () => chain('PARA'),
  addImageItem: () => chain('IMAGE'),
  setTitle: v => { form._title = v; return form; },
  getTitle: () => form._title,
  getResponses: () => [],
  getItems: () => { const old = items.slice(); return old; },
  deleteItem: it => { const i = items.indexOf(it); if (i >= 0) items.splice(i, 1); },
  getPublishedUrl: () => 'https://docs.google.com/forms/d/e/STUB/viewform',
  getEditUrl: () => 'https://docs.google.com/forms/d/STUB/edit',
};

global.FormApp = {
  create: t => { form._title = t; return form; },
  DestinationType: {SPREADSHEET: 'SPREADSHEET'},
  openById: id => { form._title = '(기존 설문)'; return form; },
};
global.SpreadsheetApp = {create: n => ({getId: () => 'SSID', getUrl: () => 'https://docs.google.com/spreadsheets/d/STUB'})};
// 공정도 PNG 가 드라이브에 "있는" 경우를 흉내 낸다
global.DriveApp = {
  getFilesByName: () => ({hasNext: () => true, next: () => ({getBlob: () => ({})})}),
};
global.Utilities={newBlob:(b,t,n)=>({_n:n}),base64Decode:(s)=>({length:1})};
global.Logger = {log: m => log.push(m)};

const src = fs.readFileSync(process.argv[2], 'utf8');
eval(src + '\ncreateEngraSurvey();');

// ── 점검 ──
let bad = 0;
const LIMITS = {title: 300, help: 4000};
console.log('설문 제목 :', form._title);
console.log('설명 길이 :', form._d.length, '자');
if (form._d.length > 4000) { console.log('  ✗ 설명이 4000자를 넘습니다'); bad++; }
console.log('항목 수   :', items.length);
console.log('');
const counts = {};
items.forEach(it => { counts[it._kind] = (counts[it._kind] || 0) + 1; });
console.log('구성      :', JSON.stringify(counts));
console.log('');
items.forEach((it, i) => {
  if (it._t.length > LIMITS.title) { console.log(`  ✗ [${i}] 제목 ${it._t.length}자 — 제한 초과`); bad++; }
  if (it._h.length > LIMITS.help) { console.log(`  ✗ [${i}] 설명 ${it._h.length}자 — 제한 초과`); bad++; }
  if (it._kind === 'CHOICE' && it._c.length !== 2) { console.log(`  ✗ [${i}] 선택지 ${it._c.length}개`); bad++; }
});

// 필수 문항이 안건 수와 맞는가
const choices = items.filter(it => it._kind === 'CHOICE');
const texts = items.filter(it => it._kind === 'TEXT');
console.log(`채택/제외 문항 ${choices.length}개 · 이유 문항 ${texts.length}개 · 자유의견 ${counts.PARA || 0}개`);
console.log(`가장 긴 설명 ${Math.max(...items.map(i => i._h.length))}자 · 가장 긴 제목 ${Math.max(...items.map(i => i._t.length))}자`);
console.log('');
console.log('--- 실행 로그 ---');
console.log(log.join('\n'));
console.log('');
console.log(bad === 0 ? '✓ 점검 통과 — 오류 없음' : `✗ ${bad}건 문제`);
process.exit(bad === 0 ? 0 : 1);
