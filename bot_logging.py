"""
앱 로그: 파일(30일 롤링) + 콘솔. /logs 명령용 최근 24시간 텍스트 추출.
"""
from __future__ import annotations

import datetime as dt
import logging
import logging.handlers
import re
from pathlib import Path
from typing import Optional

import pytz

from storage.paths import get_path

# 프로젝트 루트 기준 로그 폴더 (storage.paths.get_path와 함께 사용)
LOG_DIR = "logs"

LOG_FILE_NAME = "bot.log"
ROOT_LOGGER_NAME = "ubs"

_configured = False

_TS_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


class KSTFormatter(logging.Formatter):
    """로그 타임스탬프를 KST로 기록 (서버 TZ와 무관)."""

    def formatTime(self, record, datefmt=None):
        tz = pytz.timezone("Asia/Seoul")
        d = dt.datetime.fromtimestamp(record.created, tz=dt.timezone.utc).astimezone(tz)
        return d.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def setup_logging() -> None:
    """파일(TimedRotating 30일) + stderr. 중복 호출 무시."""
    global _configured
    if _configured:
        return
    _configured = True

    log_dir = get_path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILE_NAME

    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False

    fmt = KSTFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.handlers.TimedRotatingFileHandler(
        str(log_path),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)

    root.addHandler(fh)
    root.addHandler(sh)

    for noisy in (
        "httpx",
        "httpcore",
        "urllib3",
        "telegram",
        "telegram.ext",
        "apscheduler",
        "asyncio",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """이름은 ubs.<모듈> 로 통일 (예: ubs.scheduler)."""
    setup_logging()
    base = name.strip()
    if base.startswith(f"{ROOT_LOGGER_NAME}."):
        return logging.getLogger(base)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{base}")


def _parse_line_ts_kst(line: str) -> Optional[dt.datetime]:
    m = _TS_LINE.match(line)
    if not m:
        return None
    try:
        naive = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        kst = pytz.timezone("Asia/Seoul")
        return kst.localize(naive)
    except ValueError:
        return None


def _iter_log_blocks(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    block: list[str] = []
    block_ts: Optional[dt.datetime] = None
    for line in text.splitlines():
        ts = _parse_line_ts_kst(line)
        if ts is not None:
            if block and block_ts is not None:
                yield block_ts, "\n".join(block)
            block = [line]
            block_ts = ts
        else:
            if not block:
                block = [line]
                block_ts = None
            else:
                block.append(line)
    if block and block_ts is not None:
        yield block_ts, "\n".join(block)


def read_recent_log_text(
    hours: float = 24,
    max_total_chars: Optional[int] = None,
) -> tuple[str, bool]:
    """
    KST 기준 최근 hours 시간의 로그 블록을 합친 문자열.
    max_total_chars가 있으면 뒤에서 자르고 truncated=True.
    """
    setup_logging()
    kst = pytz.timezone("Asia/Seoul")
    now = dt.datetime.now(kst)
    cutoff = now - dt.timedelta(hours=hours)
    log_dir = get_path(LOG_DIR)
    if not log_dir.is_dir():
        return "(logs 디렉터리가 없습니다.)", False

    paths = list(log_dir.glob(f"{LOG_FILE_NAME}*"))
    if not paths:
        return "(로그 파일이 아직 없습니다. 봇을 조금 실행해 주세요.)", False

    blocks: list[tuple[dt.datetime, str]] = []
    for p in paths:
        try:
            for ts, blk in _iter_log_blocks(p):
                if ts >= cutoff:
                    blocks.append((ts, blk))
        except OSError:
            continue

    blocks.sort(key=lambda x: x[0])
    body = "\n\n".join(b[1] for b in blocks)
    truncated = False
    if max_total_chars is not None and len(body) > max_total_chars:
        truncated = True
        body = body[-max_total_chars:]
        body = "… (앞부분 생략) …\n\n" + body
    if not body.strip():
        return f"(최근 {hours:g}시간(KST) 동안 기록된 로그가 없습니다.)", False
    return body, truncated


