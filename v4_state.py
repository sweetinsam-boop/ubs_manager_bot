"""V4.0 종목별 상태: T, 잔금(pool), 리버스 여부, 아직 정산하지 않은 주문 기록.

trading_settings.json(사용자 설정)과 별도 파일이며, 필드는 다음과 같다.
  t                  현재 T (None이면 아직 시작 전 → 보유 원가로 추정)
  pool               잔금: 원금 - 매수누적 + 매도회수 (달러)
  reverse            소진 후 리버스모드 여부
  reverse_started_ymd  리버스 진입일 (YYYY-MM-DD, 표시용)
  reverse_day1_done  리버스 첫날 MOC 매도를 이미 냈는지
  orders             정산 대기 주문 [{ymd, odno, kind, side, qty, price, half, settled}]
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any

from storage.paths import get_path
from storage.file_store import load_json, save_json

_V4_STATE_JSON = "v4_strategy_state.json"
_MAX_ORDERS = 80
_lock = threading.RLock()


@contextmanager
def locked():
    """읽기-수정-쓰기 구간을 직렬화한다 (스케줄러 스레드와 텔레그램 명령 동시 접근 방어)."""
    with _lock:
        yield


def _default_record() -> dict[str, Any]:
    return {
        "t": None,
        "pool": None,
        "reverse": False,
        "reverse_started_ymd": None,
        "reverse_day1_done": False,
        "orders": [],
    }


def load_v4_state() -> dict[str, Any]:
    return load_json(get_path(_V4_STATE_JSON), default={})


def save_v4_state(data: dict[str, Any]) -> None:
    save_json(get_path(_V4_STATE_JSON), data, ensure_ascii=False, indent=2)


def _merge(raw: Any) -> dict[str, Any]:
    rec = _default_record()
    if isinstance(raw, dict):
        # 구버전 필드(pending_moc, frozen_rem, reverse_sell_proceeds)는 버린다.
        rec.update({k: v for k, v in raw.items() if k in rec})
    if not isinstance(rec["orders"], list):
        rec["orders"] = []
    return rec


def get_record(ticker: str) -> dict[str, Any]:
    return _merge(load_v4_state().get(ticker))


def put_record(ticker: str, **kwargs: Any) -> None:
    with _lock:
        st = load_v4_state()
        cur = _merge(st.get(ticker))
        cur.update(kwargs)
        st[ticker] = cur
        save_v4_state(st)


def add_order(ticker: str, order: dict[str, Any]) -> None:
    with _lock:
        rec = get_record(ticker)
        put_record(ticker, orders=(rec["orders"] + [order])[-_MAX_ORDERS:])


def reset_record(ticker: str) -> None:
    """사이클 종료(전량 매도) 후 초기화."""
    with _lock:
        st = load_v4_state()
        st[ticker] = _default_record()
        save_v4_state(st)


def delete_record(ticker: str) -> None:
    with _lock:
        st = load_v4_state()
        if ticker in st:
            del st[ticker]
            save_v4_state(st)


