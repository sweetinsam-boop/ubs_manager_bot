from __future__ import annotations

from pathlib import Path

# 모든 데이터 파일은 storage/ 디렉토리 하위에 위치한다.
# Docker WORKDIR=/app 이면 /app/storage/... (호스트 폴더를 /app 에 마운트하므로 재시작해도 유지)
BASE_DIR = Path(__file__).parent.parent
STORAGE_ROOT = BASE_DIR / "storage"

# 설정 파일 (사용자가 편집하는 값)
TRADING_SETTINGS_FILE = "trading_settings.json"
TOKEN_FILE = "token.dat"
CHAT_ID_FILE = "chat_id.json"

# 상태 관리 파일
CYCLE_STATE_FILE = "cycle_state.json"
TRADE_LOCK_FILE = "trade_lock.json"

# 기록 파일
CUMUL_PROFIT_FILE = "cumul_profit.json"
PERFORMANCE_LOG_FILE = "performance_daily.csv"  # 이름은 csv지만 내용은 JSON (기존 데이터 호환)
LAST_SETTLEMENT_DATE_FILE = "last_settlement_date.dat"


def get_path(filename: str) -> Path:
    """파일명에 따라 storage 루트 하위의 Path 객체 반환"""
    return STORAGE_ROOT / filename
