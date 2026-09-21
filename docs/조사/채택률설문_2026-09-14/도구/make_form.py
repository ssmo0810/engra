"""초안 JSON → 구글 설문을 만드는 Apps Script(.gs) 생성기.

설문은 초안 검토 화면(_view_pending)을 그대로 옮긴다:
  화면의 한 항목 카드 = 설문의 [구분 머리글 + 채택/제외 1문항 + 이유 1문항]
화면에서 최하단으로 내려가는 「이전에 제외한 것」은 설문에서도 마지막 구역에 둔다.
"""
import csv
import json
import os
import re
import sys

SEV_MARK = {"상": "●●●", "중": "●●○", "하": "●○○"}
HERE = os.path.dirname(os.path.abspath(__file__))
# engra 저장소 위치 — 이 파일은 docs/조사/<회차>/도구/ 에 있다. ENGRA_REPO 로 덮어쓸 수 있다.
REPO = os.environ.get("ENGRA_REPO") or os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
TAG_MASTER = os.path.join(REPO, "docs", "tag_master.csv")
TAG_RE = re.compile(r"\b([A-Z]{2,4}-\d{3})\b")


def load_tags():
    out = {}
    with open(TAG_MASTER, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            out[r["tag"]] = r
    return out


def glossary(items, tags):
    """초안에 실제로 나오는 태그만 추려 설명표를 만든다.

    설문 응답자는 우리 팀이 아니다 — TI-205 만 던져 놓고 판단하라고 하면
    태그를 아는 사람과 모르는 사람의 답이 갈린다. 그건 초안 품질이 아니라
    사전지식을 재는 것이 된다.
    """
    seen = []
    for it in items:
        blob = " ".join([
            it.get("title") or "", it.get("body") or "", it.get("evidence") or "",
            " ".join(it.get("related_tags_ai") or []),
        ])
        for t in TAG_RE.findall(blob):
            if t in tags and t not in seen:
                seen.append(t)
    seen.sort()

    # 알람 한계(LL/L/H/HH)는 싣지 않는다 — 임도영 결정 2026-09-14.
    # 한계값을 표로 주면 응답자가 "한계를 넘었나"만 보고 판단하게 된다. 알람 전 단계의
    # 이상을 넘길지 말지가 이 조사의 질문이므로, 한계는 필요한 안건 본문에만 나오게 둔다.
    lines = []
    for t in seen:
        r = tags[t]
        unit = (r.get("unit") or "").strip()
        lines.append(f"· {t}  {r['description']}" + (f" ({unit})" if unit else ""))
    return "\n".join(lines), len(seen)


ITEM_REF = re.compile(r"항목\s*(\d+)")
PREC_REF = re.compile(r"\[(\d+)\]")


def renumber(text):
    """AI 서술 안의 상호참조를 설문 번호에 맞춘다.

    AI 는 같은 초안의 다른 항목을 0-기반으로 가리킨다 — 첫 항목이 「항목 0」이다.
    화면에서도 설문에서도 사람은 1번부터 센다. 그대로 두면 「항목 3 참조」가
    한 칸 어긋난 안건을 가리켜, 응답자가 없는 연결을 찾게 된다.
    (엔진 쪽 문제라 팀에 따로 알린다 — 여기서는 설문이 읽히게만 고친다.)
    """
    text = ITEM_REF.sub(lambda m: f"안건 {int(m.group(1)) + 1}", text)
    return PREC_REF.sub(lambda m: f"사례 {int(m.group(1)) + 1}", text)


EMBED = "--embed" in sys.argv
# 이미 발행한 설문을 다시 지을 때 그 설문 ID. 빈 문자열이면 새로 만드는 길만 쓴다.
FORM_ID = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--form-id=")), "")
OVERVIEW_PNG = "engra_dcs_overview_tags.png"
# 설문에 심을 PNG 가 있는 곳. 기본은 이 회차 폴더의 그림/.
IMG_DIR = os.environ.get("ENGRA_SURVEY_IMG") or os.path.join(HERE, "..", "그림")
# 폼에서 720px 로 보이고 눌러서 확대한다. 확대해도 태그가 읽히는 최소선이 이 정도다.
OVERVIEW_W, TREND_W, COLORS = 1400, 900, 64


def embed_images(blocks):
    """그림을 줄여 base64 로 스크립트에 심는다 — 드라이브에 아무것도 안 올려도 되게.

    심지 않으면 응답자가 보는 그림이 통째로 빠지는데, 그건 「태그를 몰라 판단을 못 한다」는
    원래 문제로 되돌아가는 것이라 업로드 단계를 남겨 두느니 파일을 키우는 쪽을 택했다.
    """
    import base64
    import io
    import os

    from PIL import Image

    plan = [(OVERVIEW_PNG, OVERVIEW_W)] + [(b["trend_png"], TREND_W) for b in blocks]
    out, total = [], 0
    for name, w in plan:
        path = os.path.join(IMG_DIR, name)
        if not os.path.exists(path):
            print(f"  ⚠ {name} 없음 — 심지 않고 넘어감 (드라이브에서 찾게 됨)")
            continue
        im = Image.open(path).convert("RGB")
        im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.quantize(colors=COLORS, method=Image.MEDIANCUT,
                    dither=Image.FLOYDSTEINBERG).save(buf, "PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        total += len(b64)
        out.append(f'  "{name}":\n    "{b64}"')
        print(f"  심음 {name:30s} {w}px  {len(b64) / 1024:6.1f}KB")
    print(f"  그림 {len(out)}장 · base64 합계 {total / 1024:.0f}KB")
    return "{\n" + ",\n".join(out) + "\n}"


def block(it, n):
    """항목 하나를 설문 머리글 helpText 로. 화면에 보이는 순서 그대로."""
    L = []
    L.append(renumber(it["body"].strip()))

    if it.get("severity_reason"):
        line = "［중요도 판단］ " + renumber(it["severity_reason"].strip())
        rule = it.get("severity_rule")
        if rule and rule != it["severity"]:
            line += f"  (통계 기준 {rule} → AI 판정 {it['severity']})"
        if it.get("handover_worthy") not in (None, 1, True):
            line += "  ※ 전달 가치 낮음 — 정상 운전 범위로 판단"
        L.append(line)

    if it.get("related_tags_ai"):
        line = "［함께 봐야 할 항목］ " + ", ".join(it["related_tags_ai"])
        if it.get("related_note"):
            line += " — " + it["related_note"].strip()
        L.append(line)

    if it.get("evidence"):
        L.append("［근거］ " + it["evidence"].strip())

    if it.get("precedents"):
        head = f"［과거 조치］ 확정 일지 {len(it['precedents'])}건"
        if it.get("precedents_all", 0) > len(it["precedents"]):
            head += f" (같은 태그 사례 {it['precedents_all']}건 중 맞는 것)"
        if it.get("precedent_note"):
            head += " — " + renumber(it["precedent_note"].strip())
        L.append(head)
        for p in it["precedents"]:
            txt = " ".join(p["text"].split())
            if len(txt) > 300:
                txt = txt[:300] + "…"
            L.append(f"  · {p['shift_id']} {p['confirmed_at']} — {txt}")
    elif it.get("precedents_all"):
        line = f"［과거 조치 없음］ 같은 태그 확정 일지 {it['precedents_all']}건이 있었지만 이 현상에 맞지 않다고 판단"
        if it.get("precedent_note"):
            line += " — " + it["precedent_note"].strip()
        L.append(line)

    pe = it.get("prev_excluded")
    if pe:
        times = int(pe.get("times") or 1)
        line = (f"［이전 제외］ {pe.get('shift_id') or ''} "
                f"{str(pe.get('at') or '')[:10]} {pe.get('by') or '앞 근무자'} 님이 이 태그·종류를 제외했습니다")
        if times > 1:
            line += f" · {times}근무 연속"
        if pe.get("comment"):
            line += " — 남긴 코멘트: " + str(pe["comment"]).strip()
        L.append(line)
        L.append("※ 초안 화면에서도 이 항목은 기본 '제외' 상태로 최하단에 놓입니다.")

    text = "\n\n".join(x for x in L if x)
    # 구분 머리글의 설명문은 길면 잘린다. 자르는 쪽을 내가 정한다 — 뒤쪽(과거 조치 원문)이 먼저 잘리게.
    if len(text) > 3500:
        text = text[:3480] + "\n\n…(이하 생략)"
    return text


def main(json_path, gs_path):
    d = json.load(open(json_path, encoding="utf-8"))

    normal = [i for i in d["items"] if not i["demoted"]]
    demoted = [i for i in d["items"] if i["demoted"]]

    blocks = []
    n = 0
    for it in normal + demoted:
        n += 1
        m = TAG_RE.search(it["title"] or "")
        blocks.append({
            "n": n,
            "id": it["id"],
            "sev": it["severity"],
            "mark": SEV_MARK.get(it["severity"], "○○○"),
            "title": it["title"],
            "help": block(it, n),
            "demoted": it["demoted"],
            "default_on": it["default_on"],
            # 초안 화면의 추이 차트를 그대로 뜬 그림. 파일 이름이 계약이다 —
            # 스크립트가 드라이브에서 이 이름으로 찾는다.
            "trend_png": f"engra_trend_{n}.png",
            "trend_tag": m.group(1) if m else "",
        })

    qline = ""
    if d.get("quality"):
        qline = f"원본 데이터 품질 — {d['quality'][0]} · {d['quality'][1]}"

    kind = "주간 06:00~18:00" if d["shift_id"].endswith("-day") else "야간 18:00~06:00"
    date = d["shift_id"].rsplit("-", 1)[0]

    gloss, n_tags = glossary(d["items"], load_tags())

    payload = {
        "glossary": gloss,
        "n_tags": n_tags,
        "shift_id": d["shift_id"],
        "date": date,
        "kind": kind,
        "generated_at": d["generated_at"],
        "generator": d["generator"],
        "quality": qline,
        "n_normal": len(normal),
        "n_demoted": len(demoted),
        "blocks": blocks,
    }

    gs = SCRIPT.replace("__DATA__", json.dumps(payload, ensure_ascii=False, indent=2))
    gs = gs.replace("__IMAGES__", embed_images(blocks) if EMBED else "{}")
    gs = gs.replace("__FORM_ID__", FORM_ID)
    with open(gs_path, "w", encoding="utf-8") as f:
        f.write(gs)
    print(f"Apps Script 생성 → {gs_path}")
    print(f"  안건 {len(blocks)}개 (일반 {len(normal)} · 이전 제외 {len(demoted)})")
    print(f"  문항 수 = 안건당 2문항 × {len(blocks)} + 자유의견 1 = {len(blocks) * 2 + 1}")


SCRIPT = r'''/**
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

var DATA = __DATA__;

/** 공정도 PNG 파일 이름. */
var OVERVIEW_PNG = 'engra_dcs_overview_tags.png';

/**
 * 이미 만들어 둔 설문의 ID. 여기에 값이 있으면 rebuildEngraSurvey 로 그 설문의 내용만
 * 갈아끼울 수 있다 — 응답 링크가 바뀌지 않는다. 비워 두면 createEngraSurvey 로 새로 만든다.
 */
var FORM_ID = '__FORM_ID__';

/**
 * 설문에 들어갈 그림들 — 파일 이름 → PNG 를 base64 로 적어 둔 것.
 * 여기 들어 있으면 드라이브에 아무것도 안 올려도 된다. 길지만 읽을 필요는 없는 부분이다.
 * (비어 있으면 같은 이름의 파일을 내 드라이브에서 찾는다.)
 */
var IMAGES = __IMAGES__;

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
'''


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
