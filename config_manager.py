"""
앱/증권 설정 로딩 및 저장.
설정 파일 경로와 JSON I/O는 storage 모듈을 사용합니다.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

from storage.paths import get_path, TRADING_SETTINGS_FILE
from storage.file_store import load_json, save_json
from bot_logging import get_logger

load_dotenv()

log = get_logger(__name__)


def load_config():
    """더 이상 JSON 설정 파일 대신 환경변수를 사용합니다."""
    return {
        "TELEGRAM_TOKEN": os.environ.get("TELEGRAM_TOKEN"),
        "APP_KEY": os.environ.get("APP_KEY"),
        "APP_SECRET": os.environ.get("APP_SECRET"),
        "CANO": os.environ.get("CANO", ""),
        "ACNT_PRDT_CD": os.environ.get("ACNT_PRDT_CD", "")
    }

trading_data = load_json(get_path(TRADING_SETTINGS_FILE), default={})

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
APP_KEY = os.environ.get("APP_KEY")
APP_SECRET = os.environ.get("APP_SECRET")
CANO = os.environ.get("CANO", "")
ACNT_PRDT_CD = os.environ.get("ACNT_PRDT_CD", "")

ENABLE_PREMARKET = trading_data.get("ENABLE_PREMARKET", False)
STRATEGY_SETTINGS = trading_data.get("STRATEGY_SETTINGS", {})

def get_post_close_sync_ny_time() -> tuple[int, int]:
    """
    trading_settings.json 의 POST_CLOSE_SYNC_NY (예: \"16:40\") → (시, 분). 미설정 시 (16, 40).
    장 마감 후 자동 /sync 스케줄에 사용 (미국 동부).
    """
    td = load_json(get_path(TRADING_SETTINGS_FILE), default={})
    raw = str(td.get("POST_CLOSE_SYNC_NY", "16:40")).strip()
    try:
        if ":" in raw:
            a, b = raw.split(":", 1)
            return max(0, min(23, int(a.strip()))), max(0, min(59, int(b.strip())))
        parts = raw.split()
        if len(parts) >= 2:
            return max(0, min(23, int(parts[0]))), max(0, min(59, int(parts[1])))
    except (TypeError, ValueError):
        pass
    return 16, 40


def save_config(updates):
    """투자 설정 파일(trading_settings.json)만 업데이트하고 저장합니다."""
    try:
        current_settings = load_json(get_path(TRADING_SETTINGS_FILE), default={})
        current_settings.update(updates)
        save_json(get_path(TRADING_SETTINGS_FILE), current_settings, ensure_ascii=False, indent=4)
        
        # 전역 변수 업데이트
        if "ENABLE_PREMARKET" in updates:
            global ENABLE_PREMARKET
            ENABLE_PREMARKET = updates["ENABLE_PREMARKET"]
        if "STRATEGY_SETTINGS" in updates:
            global STRATEGY_SETTINGS
            STRATEGY_SETTINGS = updates["STRATEGY_SETTINGS"]
        return True
    except Exception as e:
        log.error("설정 저장 실패: %s", e)
        return False
