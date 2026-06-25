from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

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
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 기본 옵션: ensure_ascii=False, indent=4
        kwargs.setdefault("ensure_ascii", False)
        kwargs.setdefault("indent", 4)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, **kwargs)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        log.error(f"텍스트 저장 실패 ({path.name}): {e}")
