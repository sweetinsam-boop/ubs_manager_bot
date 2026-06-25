from __future__ import annotations

from pathlib import Path

# 모든 데이터 파일은 storage/ 디렉토리 하위에 위치하도록 설정
# Docker WORKDIR=/app 인 경우 /app/storage/... 가 됨
BASE_DIR = Path(__file__).parent.parent
STORAGE_ROOT = BASE_DIR / "storage"

# 설정 파일
TRADING_SETTINGS_FILE = "trading_settings.json"
TOKEN_FILE = "token.dat"
CHAT_ID_FILE = "chat_id.json"

# 상태 관리 파일
CYCLE_STATE_FILE = "cycle_state.json"
TRADE_LOCK_FILE = "trade_lock.json"
V3_STATE_FILE = "v3_state.json"
V4_STATE_FILE = "v4_state.json"
LAST_KNOWN_HOLD_AVGS_FILE = "last_known_hold_avgs.json"

# 기록/로그 파일
CUMUL_PROFIT_FILE = "cumul_profit.json"
PERFORMANCE_LOG_FILE = "performance_daily.csv"
LAST_SETTLEMENT_FILE = "last_settlement.json"
LAST_REPORT_FILE = "last_report.dat"
LAST_SETTLEMENT_DATE_FILE = "last_settlement_date.dat"

def get_path(filename: str) -> Path:
    """파일명에 따라 storage 루트 하위의 Path 객체 반환"""
    return STORAGE_ROOT / filename
