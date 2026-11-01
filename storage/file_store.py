from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _atomic_write(path: Path, text: str) -> None:
    """임시 파일에 쓴 뒤 교체한다. 쓰는 도중 종료돼도 기존 파일이 깨지지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path: Path, default: Any = None) -> Any:
    """JSON 파일 로드. 파일이 없으면 default 반환."""
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"JSON 로드 실패 ({path.name}): {e}")
        return default if default is not None else {}


def save_json(path: Path, data: Any, **kwargs):
    """JSON 파일 저장. 부모 디렉토리 자동 생성."""
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("indent", 4)
    try:
        _atomic_write(path, json.dumps(data, **kwargs))
    except Exception as e:
        log.error(f"JSON 저장 실패 ({path.name}): {e}")


def load_text(path: Path) -> str:
    """텍스트 파일 로드. 파일이 없으면 빈 문자열 반환."""
    if not path.exists():
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        log.error(f"텍스트 로드 실패 ({path.name}): {e}")
        return ""


def save_text(path: Path, content: str) -> None:
    """텍스트 파일 저장. 부모 디렉토리 자동 생성."""
    try:
        _atomic_write(path, content)
    except Exception as e:
        log.error(f"텍스트 저장 실패 ({path.name}): {e}")
