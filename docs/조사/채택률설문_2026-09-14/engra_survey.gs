/**
 * ENGRA 인수인계 초안 — 채택률 조사 설문 생성기
 *
 * 쓰는 법
 *   1) script.google.com → 새 프로젝트 → 이 파일 내용을 통째로 붙여넣기
 *   2) 위쪽 함수 목록에서 createEngraSurvey 선택 → 실행
 *   3) 첫 실행 때 권한 승인 (내 계정의 스프레드시트·설문 만들기)
 *   4) 실행 기록(Executions) 또는 로그에 찍히는 「응답 링크」를 동료들에게 전달
 *
 * 만들어지는 것
 *   - 구글 설문 1개 (내 드라이브 최상위)
 *   - 안건마다: 내용 전문 + 「최종 인수인계서로 넘긴다 / 넘기지 않는다」 + 이유(선택)
 *   - 맨 아래: ENGRA 자유 의견란
 *   - 완전 익명 (이메일 수집 안 함, 로그인 요구 안 함)
 */

var DATA = {
  "glossary": "· AI-707  LP O2 순도 (%)\n· AI-803  LOX 순도 O2 (%)\n· FI-204  토출 유량 (Nm3/h)\n· LI-701  HP 레벨 (%)\n· LI-806  LIN 레벨 (%)\n· PI-203  토출 압력 (bar)\n· SI-507  터빈 회전수 (rpm)\n· ZI-201  IGV 개도 (%)\n· ZI-805  LV-802 개도 (%)",
  "n_tags": 9,
  "shift_id": "2026-09-14-day",
  "date": "2026-09-14",
  "kind": "주간 06:00~18:00",
  "generated_at": "2026-09-14T11:25:25",
  "generator": "engine",
  "quality": "",
  "n_normal": 5,
  "n_demoted": 0,
  "blocks": [
    {
      "n": 1,
      "id": 22,
      "sev": "상",
      "mark": "●●●",
      "title": "AI-707(LP O2 순도) 하강 드리프트 — 근무 내내 진행, L 한계까지 13.1시간",
      "help": "AI-707(LP O2 순도) 628분 동안 한 방향으로 하강. 0.0244%/시간, 구간 변화 -0.293% 로 평소 오르내림의 12.5배. 실측 변화 -0.33% 중 외기로 설명되는 몫 0.0693% 을 제외한 값이라 외기만으로는 설명 안 됨. 알람은 아직 미발생이나 이 속도면 L 한계 99.0% 까지 약 13.1시간 — 다음 근무 구간 안에 도달함. AI-803 과 함께 움직임. 전 근무(9/13 야간)에도 같은 하강이 잡혀 있어 근무를 넘어 이어지는 드리프트로 의심됨. 원인 미확인, 추세 지속 여부 확인 필요.\n\n［중요도 판단］ 제품 순도 직결이고, 알람 전이지만 L 한계 99.0% 도달 예상 13.1시간이 다음 근무 구간 안에 들어와 인계 없이 넘기면 근무 중 품질 이탈로 감.\n\n［근거］ AI-707(LP O2 순도) 하강 추세 — 0.02443%/시간, 구간 변화 -0.293% (평소 오르내림의 12.5배) · 이 속도면 L 한계까지 약 13.1시간 · 실측 변화는 -0.33% 이나 외기로 설명되는 몫 0.06932% 을 제외한 값\n\n［과거 조치］ 확정 일지 1건 — 사례 1은 같은 AI-707 하강 드리프트로 증상은 맞으나 조치 내용이 남아 있지 않아 따를 조치는 없고, 전 근무부터 이어진 연속 드리프트라는 근거로만 씀.\n\n  · 2026-09-13-night 2026-09-14 — AI-707 LP O2 순도 — 드리프트 외 1개 태그 18:00~20:00 구간. 근무 내내 한 방향으로 서서히 움직였습니다. · AI-707(LP O2 순도) 하강 추세 — 0.1232%/시간, 구간 변화 -0.2454% (평소 산포의 7.1배) · 이 속도면 L 한계까지 약 4.6시간 · 실측 변화는 0.02% 이나 외기로 설명되는 몫 0.06957% 을 제외한 값 · AI-803(LOX 순도 O2) 하강 추세 — 0.06345%/시간, 구간 변화 -0.1264% (평소 산포의 7.0배) · 이 속도면 L 한계까지 약 6.1시…",
      "demoted": false,
      "default_on": true,
      "trend_png": "engra_trend_1.png",
      "trend_tag": "AI-707"
    },
    {
      "n": 2,
      "id": 23,
      "sev": "중",
      "mark": "●●○",
      "title": "SI-507(터빈 회전수) 순간 이탈 — 1.5분, 알람 미발생",
      "help": "SI-507(터빈 회전수) 18,577rpm 에서 17,678rpm 로 순간 이탈 (평소보다 최대 5% 낮아짐). 지속 1.5분으로 알람 지속시간 미달이라 알람은 안 울림. 교대 시점엔 복귀해 있어 화면·알람 이력만 보면 안 보임. 원인 미확인. 같은 태그의 과거 확정 일지 없음. 재발 여부 확인 필요.\n\n［중요도 판단］ 1회 발생 후 원복이라 즉시 위험은 아니지만, 터빈 회전수 평소보다 최대 5% 낮아짐 이탈은 반복되면 설비 손상·트립으로 갈 수 있고 교대 시점엔 정상이라 적지 않으면 다음 근무자가 모름.  (통계 기준 상 → AI 판정 중)\n\n［근거］ SI-507(터빈 회전수) 순간 이탈 — 18,577rpm 에서 17,678rpm 로 (평소보다 최대 5% 낮아짐), 알람 지속시간 미달로 미발생",
      "demoted": false,
      "default_on": true,
      "trend_png": "engra_trend_2.png",
      "trend_tag": "SI-507"
    },
    {
      "n": 3,
      "id": 24,
      "sev": "상",
      "mark": "●●●",
      "title": "LI-701(HP 레벨) 급상승 — 105분간 23.1%, 배출 밸브 개도 동반 상승",
      "help": "LI-701(HP 레벨) 11.6%/시간으로 상승, 105분 구간 변화 23.1% (평소 오르내림의 19.0배. H 한계는 40.0% 이나 도달 예상시간은 산출되지 않아 여유를 검출값으로 판단할 수 없음 — 실값 직접 확인 필요. 같은 길이의 구간에서 ZI-805(LV-802 개도)가 10.5%/시간으로 같이 올라감. 배출측 개도가 계속 열리는데도 레벨이 계속 오르는 형태라 밸브 통과불량·고착 의심됨(안건 4 참조). 원인 미확인.\n\n［중요도 판단］ 개도를 여는데도 레벨이 따라 안 내려가는 밸브 고착형 패턴이고, 레벨은 추세로만 판별되며 알람 전에 잡아야 컬럼 운전을 지킬 수 있음.\n\n［함께 봐야 할 항목］ ZI-805 — 태그 마스터 연결은 없으나 105분·101분으로 시간창이 거의 같고 레벨 상승과 배출 밸브 개도 상승이 같은 방향으로 붙어 있어, 액 배출 루프 한 사건의 두 얼굴로 봄.\n\n［근거］ LI-701(HP 레벨) 상승 추세 — 11.6%/시간, 구간 변화 23.1% (평소 오르내림의 19.0배)\n\n［과거 조치 없음］ 같은 태그 확정 일지 2건이 있었지만 이 현상에 맞지 않다고 판단 — [0]은 같은 LI-701 이지만 순간 이탈 5회로 이번 추세형과 증상이 다르고, [1]은 ZI-601 개도 건이 같은 일지에서 섞여 들어온 것이라 둘 다 기각.",
      "demoted": false,
      "default_on": true,
      "trend_png": "engra_trend_3.png",
      "trend_tag": "LI-701"
    },
    {
      "n": 4,
      "id": 25,
      "sev": "상",
      "mark": "●●●",
      "title": "ZI-805(LV-802 개도) 상승 — H 한계까지 0.9시간, 전 근무보다 빨라짐",
      "help": "ZI-805(LV-802 개도) 10.5%/시간 상승, 101분 구간 변화 41.8% (평소 오르내림의 5.6배. 이 속도면 H 한계 78.0% 까지 약 0.9시간 — 알람은 아직 안 울렸지만 교대 직후 도달함. 전 근무(9/13 야간)에도 같은 상승 드리프트가 있었고 그때는 4.10%/시간에 한계까지 9.7시간이었음 — 같은 드리프트가 빨라진 것으로 의심됨. LI-806 과 연동. LI-701(HP 레벨) 상승과 같은 사건으로 봄(안건 3). 원인 미확인.\n\n［중요도 판단］ 알람 전이지만 H 한계 78.0% 도달이 0.9시간으로 교대 직후에 걸리고, 개도가 상한까지 가면 제어 여력이 없어져 레벨을 잡을 수단이 남지 않음.\n\n［함께 봐야 할 항목］ LI-701 — 개도가 10.5%/시간으로 열리는 동안 LI-701 레벨이 11.6%/시간으로 같이 올라, 밸브를 열어도 레벨이 안 빠지는 한 사건으로 보임.\n\n［근거］ ZI-805(LV-802 개도) 상승 추세 — 10.5%/시간, 구간 변화 41.8% (평소 오르내림의 5.6배) · 이 속도면 H 한계까지 약 0.9시간\n\n［과거 조치］ 확정 일지 1건 (같은 태그 사례 2건 중 맞는 것) — 사례 1은 같은 ZI-805 상승 드리프트라 연속성 근거로 채택, 사례 2은 헌팅·단계 변화로 증상이 달라 기각 — 두 건 다 조치 내용이 없어 이번에 따를 조치는 없음.\n\n  · 2026-09-13-night 2026-09-14 — ZI-805 LV-802 개도 — 드리프트 외 1개 태그 19:30~06:00 구간. 근무 내내 한 방향으로 서서히 움직였습니다. · ZI-805(LV-802 개도) 상승 추세 — 4.10%/시간, 구간 변화 49.2% (평소 산포의 8.3배) · 이 속도면 H 한계까지 약 9.7시간 · 실측 변화는 -21.6% 이나 외기로 설명되는 몫 -66.1% 을 제외한 값 · LI-806(LIN 레벨) 하강 추세 — 3.54%/시간, 구간 변화 -42.5% (평소 산포의 7.4배) · 이 속도면 L 한계까지 약 16.3시간 · 실측 변화는 …",
      "demoted": false,
      "default_on": true,
      "trend_png": "engra_trend_4.png",
      "trend_tag": "ZI-805"
    },
    {
      "n": 5,
      "id": 26,
      "sev": "상",
      "mark": "●●●",
      "title": "ZI-201(IGV 개도)–FI-204(토출 유량) 연동 끊김 — 41분, 평소보다 최대 7% 낮아짐",
      "help": "ZI-201(IGV 개도)와 FI-204(토출 유량)의 평소 연동이 41분간 끊김, 평소보다 최대 7% 낮아짐. 단일 태그 화면으로는 보이지 않는 형태임. PI-203(토출압)이 같이 묶여 있음. 전 근무(9/13 야간)에도 같은 ZI-201–FI-204 연동 끊김이 FI-204 하강 드리프트와 함께 잡혔어 반복되는 중으로 의심됨. 원인 미확인 — 압축기 계통 공통 원인 배제 못 하므로 IGV·토출 유량·토출압을 같이 놓고 확인 필요.\n\n［중요도 판단］ 상관 붕괴는 단일 태그로는 드러나지 않는 고장 공통 신호이고 압축기 계통이라, 놓치면 서지 전조를 그냥 넘길 수 있음.\n\n［근거］ ZI-201(IGV 개도) 와 FI-204(토출 유량) 의 평소 연동이 끊김 — 평소보다 최대 7% 낮아짐. 단일 태그로는 보이지 않는 이상\n\n［과거 조치］ 확정 일지 1건 (같은 태그 사례 2건 중 맞는 것) — 사례 1은 같은 ZI-201–FI-204 연동 끊김이 기록돼 있어 채택, 사례 2은 하강 드리프트·순간 이탈로 이번 증상과 달라 기각 — 두 건 다 조치 내용은 남아 있지 않아 따를 조치는 없음.\n\n  · 2026-09-13-night 2026-09-14 — FI-204 토출 유량 — 드리프트 외 1개 태그 18:22~19:15 구간. 근무 내내 한 방향으로 서서히 움직였습니다. · FI-204(토출 유량) 하강 추세 — 1,548Nm3/h/시간, 구간 변화 -1,536Nm3/h (평소 산포의 5.2배) · 이 속도면 L 한계까지 약 1.1시간 · ZI-201(IGV 개도) 와 FI-204(토출 유량) 의 평소 연동이 끊김 — 잔차 6.6σ. 단일 태그로는 보이지 않는 이상 · PI-203(토출 압력) 상승 추세 — 0.1234bar/시간, 구간 변화 0.1224bar (평소 산포의 4…",
      "demoted": false,
      "default_on": true,
      "trend_png": "engra_trend_5.png",
      "trend_tag": "ZI-201"
    }
  ]
};

/** 공정도 PNG 파일 이름. */
var OVERVIEW_PNG = 'engra_dcs_overview_tags.png';

/**
 * 이미 만들어 둔 설문의 ID. 여기에 값이 있으면 rebuildEngraSurvey 로 그 설문의 내용만
 * 갈아끼울 수 있다 — 응답 링크가 바뀌지 않는다. 비워 두면 createEngraSurvey 로 새로 만든다.
 */
var FORM_ID = '1vIJlm38wu0CufHQJHDC12pKhrAM5ylCuZNHwQ8T4z8U';

/**
 * 설문에 들어갈 그림들 — 파일 이름 → PNG 를 base64 로 적어 둔 것.
 * 여기 들어 있으면 드라이브에 아무것도 안 올려도 된다. 길지만 읽을 필요는 없는 부분이다.
 * (비어 있으면 같은 이름의 파일을 내 드라이브에서 찾는다.)
 */
var IMAGES = {};

var WARN = [];

/** 계정 종류에 따라 없을 수 있는 설정 — 실패해도 설문 생성은 계속한다. */
function soft(fn, what) {
  try {
    fn();
  } catch (e) {
    WARN.push('· ' + what + ' 실패 (' + e.message + ') — 설문은 그대로 만들어졌습니다');
  }
}

var MISSING = [];

/**
 * 그림 한 장을 얻는다. 아래 IMAGES 에 심어 둔 것을 먼저 쓰고, 없으면 내 드라이브에서
 * 같은 이름의 파일을 찾는다. 심어 둔 것이 있으면 드라이브에 아무것도 안 올려도 된다.
 */
function blobFor_(name) {
  var b64 = (typeof IMAGES !== 'undefined') ? IMAGES[name] : null;
  if (b64) return Utilities.newBlob(Utilities.base64Decode(b64), 'image/png', name);
  var it = DriveApp.getFilesByName(name);
  return it.hasNext() ? it.next().getBlob() : null;
}

/**
 * 그림을 설문에 넣는다. 없으면 그 그림만 빼고 계속한다 —
 * 그림 하나 때문에 설문 전체가 안 만들어지는 쪽이 더 나쁘다.
 */
function addImage_(form, name, title, help) {
  try {
    var blob = blobFor_(name);
    if (!blob) {
      MISSING.push(name);
      return false;
    }
    form.addImageItem()
      .setTitle(title)
      .setHelpText(help)
      .setImage(blob)
      .setWidth(720);
    return true;
  } catch (e) {
    WARN.push('· 「' + name + '」 삽입 실패 (' + e.message + ')');
    return false;
  }
}

function addOverviewImage_(form) {
  addImage_(form, OVERVIEW_PNG, 'ASU 공정 개요 (DCS Overview)',
            '태그 번호가 어느 설비에 붙어 있는지 보여 주는 그림입니다. 크게 보려면 이미지를 누르세요.');
}

/**
 * 설문 본문을 짓는다. 한 안건 = 【굵은 제목】 → 【추이 그래프】 → 【넘길까요?】 세 덩어리.
 *
 * 글을 길게 붙이지 않는다 — 안건마다 900자씩 깔았더니 「수학문제 같다」는 말을 들었다
 * (임도영 2026-09-14). 판단에 필요한 것은 제목 한 줄과 그래프 모양이고,
 * 수치 근거·과거 조치는 읽히지 않으면 없는 것과 같다.
 */
function buildBody_(form) {
  form.addSectionHeaderItem()
    .setTitle('시작하기 전에')
    .setHelpText('안건에 나오는 AI-707, LI-701 같은 것은 계기 태그 번호입니다.\n' +
                 '아래 그림에서 어느 설비인지 확인하실 수 있습니다.');

  addOverviewImage_(form);

  for (var i = 0; i < DATA.blocks.length; i++) {
    var b = DATA.blocks[i];

    form.addSectionHeaderItem()
      .setTitle('안건 ' + b.n + '.  ' + b.title)
      .setHelpText('중요도 ' + b.sev + (b.demoted ? '  ·  앞 근무자가 제외했던 항목' : ''));

    addImage_(form, b.trend_png,
              b.trend_tag ? b.trend_tag + ' 추이' : '추이',
              '분홍색 구간이 ENGRA 가 이상으로 잡은 구간입니다.');

    form.addMultipleChoiceItem()
      .setTitle('안건 ' + b.n + ' — 최종 인수인계서에 채택하시겠습니까?')
      .setChoiceValues(['채택한다', '채택 안한다'])
      .setRequired(true);

    form.addTextItem()
      .setTitle('이유 (선택)')
      .setRequired(false);
  }

  form.addPageBreakItem().setTitle('마지막으로');

  form.addParagraphTextItem()
    .setTitle('ENGRA 를 써 보니 어떠셨습니까?')
    .setHelpText('초안이 현장 감각에 맞던가요? 교대 때 실제로 쓰시겠습니까?\n' +
                 '쓴소리일수록 도움이 됩니다.')
    .setRequired(false);
}

/** 설문 제목·설명·익명 설정. 만들 때와 다시 지을 때 똑같이 쓴다. */
function applySettings_(form) {
  form.setDescription(
    'ENGRA 가 ' + DATA.date + ' ' + DATA.kind + ' 근무의 운전 데이터를 읽고 자동으로 만든 인수인계 초안입니다.\n' +
    '\n' +
    '안건 ' + DATA.blocks.length + '건 중 다음 근무조에게 넘길 것을 골라 주세요. 정답은 없습니다.\n' +
    '완전 익명이고, 5분이면 됩니다.'
  );

  // 계정 종류(개인 Gmail / Workspace)에 따라 없는 설정이 있다. 하나 실패했다고 설문 생성 전체가
  // 죽으면 안 되므로 개별로 감싼다 — 익명 설정은 실패해도 로그로 알린다.
  soft(function () { form.setCollectEmail(false); }, 'setCollectEmail');
  soft(function () { form.setRequireLogin(false); }, 'setRequireLogin(익명)');
  soft(function () { form.setLimitOneResponsePerUser(false); }, 'setLimitOneResponsePerUser');
  soft(function () { form.setProgressBar(true); }, 'setProgressBar');
  soft(function () { form.setAllowResponseEdits(false); }, 'setAllowResponseEdits');
  soft(function () { form.setShowLinkToRespondAgain(false); }, 'setShowLinkToRespondAgain');
  soft(function () {
    form.setConfirmationMessage('응답해 주셔서 감사합니다. 결과는 ENGRA 초안 채택률 집계에만 쓰입니다.');
  }, 'setConfirmationMessage');
}

/**
 * 이미 만들어 둔 설문의 내용만 갈아끼운다 — 링크가 그대로 유지된다.
 * 이미 공유한 링크를 살리려고 이 길을 둔다. 응답이 하나라도 있으면 손대지 않는다.
 */
function rebuildEngraSurvey() {
  if (!FORM_ID) throw new Error('FORM_ID 가 비어 있습니다. 새로 만들려면 createEngraSurvey 를 쓰세요.');
  var form = FormApp.openById(FORM_ID);

  var n = form.getResponses().length;
  if (n > 0) {
    var msg = '이 설문에 이미 응답이 ' + n + '건 있습니다. 문항을 지우면 그 응답을 읽을 수 없게 되므로 중단합니다.\n' +
              '정말 다시 지으려면 createEngraSurvey 로 새 설문을 만드세요.';
    Logger.log(msg);
    return msg;
  }

  var items = form.getItems();
  for (var i = items.length - 1; i >= 0; i--) form.deleteItem(items[i]);

  form.setTitle('ENGRA 인수인계 초안 검토 — ' + DATA.date + ' ' + (DATA.kind.indexOf('주간') === 0 ? '주간' : '야간'));
  applySettings_(form);
  buildBody_(form);

  var out = report_('다시 지었습니다 (링크 그대로)', form, '(그대로)');
  Logger.log(out);
  return out;
}

/**
 * 설문 접수를 닫는다. 링크는 살아 있고, 열면 「더 이상 응답을 받지 않습니다」가 뜬다.
 * 이미 받은 응답과 시트는 그대로다. 다시 열려면 reopenEngraSurvey.
 */
function closeEngraSurvey() {
  var form = FormApp.openById(FORM_ID);
  form.setAcceptingResponses(false);
  soft(function () {
    form.setCustomClosedFormMessage(
      '설문이 마감되었습니다. 응답해 주셔서 감사합니다.\n' +
      '결과는 ENGRA 초안 채택률 집계에 쓰였습니다.');
  }, 'setCustomClosedFormMessage');
  var msg = '설문 접수를 닫았습니다 — 응답 ' + form.getResponses().length + '건으로 마감.\n' + form.getPublishedUrl();
  Logger.log(msg);
  return msg;
}

function reopenEngraSurvey() {
  var form = FormApp.openById(FORM_ID);
  form.setAcceptingResponses(true);
  var msg = '설문 접수를 다시 열었습니다.\n' + form.getPublishedUrl();
  Logger.log(msg);
  return msg;
}

function createEngraSurvey() {
  var title = 'ENGRA 인수인계 초안 검토 — ' + DATA.date + ' ' + (DATA.kind.indexOf('주간') === 0 ? '주간' : '야간');
  var form = FormApp.create(title);

  applySettings_(form);
  buildBody_(form);

  // 응답을 스프레드시트로 바로 받게 연결 — 집계가 훨씬 편하다
  var ssUrl = '(연결 실패)';
  try {
    var ss = SpreadsheetApp.create('ENGRA 초안 채택률 응답 — ' + DATA.shift_id);
    form.setDestination(FormApp.DestinationType.SPREADSHEET, ss.getId());
    ssUrl = ss.getUrl();
  } catch (e) {
    WARN.push('· 응답 스프레드시트 연결 실패 (' + e.message + ') — 설문 「응답」 탭에서 직접 연결하시면 됩니다');
  }

  var out = report_('만들어졌습니다', form, ssUrl);
  Logger.log(out);
  return out;
}

/** 실행 로그에 찍을 결과 — 링크와 경고를 한 덩어리로. */
function report_(head, form, ssUrl) {
  if (MISSING.length) {
    WARN.push('· 드라이브에서 못 찾은 그림 ' + MISSING.length + '개: ' + MISSING.join(', '));
    WARN.push('  → 이 파일들을 내 드라이브에 같은 이름으로 올린 뒤 다시 실행하면 그림까지 들어갑니다.');
    WARN.push('  → 지금 설문은 그 자리에 그림만 없고 나머지는 정상입니다.');
  }

  return [
    '',
    '===== ' + head + ' =====',
    '설문 제목 : ' + form.getTitle(),
    '안건 수   : ' + DATA.blocks.length + '건',
    '구성      : 안건마다 [제목] → [추이 그래프] → [넘길까요?] → [이유(선택)]',
    '',
    '▶ 동료들에게 보낼 응답 링크:',
    form.getPublishedUrl(),
    '',
    '▶ 내가 고칠 때 쓰는 편집 링크:',
    form.getEditUrl(),
    '',
    '▶ 응답이 쌓이는 스프레드시트:',
    ssUrl,
    ''
  ].concat(WARN.length ? ['[알림]'].concat(WARN) : ['(경고 없음)'])
   .concat(['==========================']).join('\n');
}
