"""설정값 한 곳.

경로와 근무 구간 규칙만 담는다. 알고리즘 파라미터는 여기 두지 않는다 —
그건 engine/ 담당이고, ports.py 를 통해 넘어온다.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
DOCS_DIR = ROOT / "docs"

DB_PATH = APP_DIR / "engra.db"
TAG_MASTER = DOCS_DIR / "tag_master.csv"

# 근무는 07:00~19:00(주간) / 19:00~07:00(야간).
# 분석 구간은 교대 1시간 전을 경계로 잡는다 → 주간조 초안은 06:00~18:00 을 다룬다.
# 이렇게 하면 시간축에 빈틈이 없고, 교대 시각에 초안이 이미 준비돼 있다.
SHIFT_WINDOW_START_HOUR = {"day": 6, "night": 18}
SHIFT_HOURS = 12
SAMPLE_INTERVAL_SEC = 2

# 원본 시계열은 짧게 돌리고 버린다. 요약·이벤트·일지는 영구 보존.
RAW_RETENTION_DAYS = 3

# 웹 서버
HOST = "127.0.0.1"
PORT = 8000
