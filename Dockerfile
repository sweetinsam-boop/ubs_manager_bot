# Python 3.11 슬림 이미지 사용
FROM python:3.11-slim

# 작업 디렉토리 설정 (엔트리포인트 및 storage.paths 기준 디렉터리)
WORKDIR /app

# 시스템 의존성 (필요 시 추가)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 복사 후 설치 (캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 소스 (.dockerignore 로 상태 JSON/토큰 등 제외)
COPY . .

# 실행 (런타임에 볼륨으로 trading_settings.json·.env 등을 덮어써도 됨)
CMD ["python", "main.py"]
