# UBS Manager Trading Bot (무한매수법 V4.0 자동매매 봇)

라오어의 **무한매수법 V4.0**(일반모드 + 소진 후 리버스모드)을 한국투자증권(KIS) API로 자동 매매하는 텔레그램 봇입니다.
TQQQ와 SOXL을 대상으로 하며, 시놀로지 NAS(Docker)나 내 PC에서 돌릴 수 있습니다.

> 투자 손익의 책임은 본인에게 있습니다. 3배 레버리지 ETF는 변동성이 매우 크고, API 장애나 버그로 주문이 실패하거나 의도와 다르게 나갈 수 있습니다. 처음에는 작은 원금으로 며칠 지켜보세요.

## 할 수 있는 것

- 매일 미국 정규장 개장 직후(뉴욕 09:35)에 V4.0 규칙대로 LOC·지정가·MOC 주문을 자동 전송
- 장 마감 후 체결을 읽어 **T값과 잔금을 자동 갱신** (공식 규칙: 매수 +1/+0.5, 쿼터매도 ×0.75, 리버스 T 갱신)
- 소진 후 **리버스모드** 자동 전환과 복귀
- 텔레그램으로 현황, 오늘 계획, 예수금, 익절 기록 조회와 수동 주문
- 한 장짜리 **HTML 리포트**(T 진행도, 가격 사다리, 오늘 주문 계획, 계좌 추이, 체결, 실현수익) 매일 아침 자동 전송, `/report` 로 즉시 생성
- 등록한 본인 채팅 외의 모든 명령 차단

## 시작하기

### 준비물

1. **한국투자증권 API 키**: [개발자센터](https://apiportal.koreainvestment.com/)에서 실전투자용 앱키·시크릿 발급 (해외주식 거래 신청 필수). 모의투자는 지원하지 않습니다.
2. **텔레그램 봇**: 텔레그램에서 [@BotFather](https://t.me/botfather)에게 `/newbot` 을 보내 봇을 만들고 토큰을 받습니다.
3. **실행할 곳**: 24시간 켜 둘 수 있는 NAS(권장) 또는 PC

### 1. 설정 마법사 실행

```bash
git clone https://github.com/sweetinsam-boop/ubs_manager_bot.git
cd ubs_manager_bot
python3 setup_wizard.py
```

마법사가 텔레그램 연결, 증권사 키와 계좌 확인, 종목·원금·분할 수 입력을 차례로 안내하고 `.env` 와 `storage/trading_settings.json` 을 만듭니다. 입력한 키가 맞는지 실제로 접속해 확인하고, 나중에 `python3 setup_wizard.py --check` 로 다시 점검할 수 있습니다. 표준 라이브러리만 쓰므로 패키지 설치 전에도 실행됩니다.

Python이 없는 NAS에서는 Docker로 마법사를 실행합니다.

```bash
sudo docker run -it --rm -v "$PWD":/app -w /app python:3.11-slim python setup_wizard.py --mode nas
```

### 2-A. NAS에서 실행 (시놀로지)

폴더 전체(`.env`, `storage` 포함)를 `/volume1/docker/ubs_manager_bot` 같은 곳에 두고 SSH로 접속해 실행합니다.

```bash
cd /volume1/docker/ubs_manager_bot
sudo docker-compose up -d --build     # 안 되면 sudo docker compose up -d --build
sudo docker logs --tail 30 my_telegram_bot
```

Container Manager의 프로젝트 기능으로 `docker-compose.yaml` 을 불러와도 됩니다. 코드를 바꾼 뒤에는 `sudo docker restart my_telegram_bot` 으로 재시작합니다(파이썬 파일은 마운트된 폴더에서 읽습니다. `requirements.txt` 가 바뀌면 `--build` 가 필요합니다).

### 2-B. PC에서 실행

PC는 미국장이 열리는 한국 시간 밤(22:35 또는 23:35)부터 마감 후까지 켜져 있어야 하고 절전을 꺼야 합니다.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # 윈도우: .venv\Scripts\pip
.venv/bin/python main.py                       # 윈도우: .venv\Scripts\python main.py
```

마법사를 `--mode pc` 로 돌리면 가상환경 생성과 설치까지 도와줍니다.

### 3. 확인

텔레그램에서 봇에게 `/start` → `/status` → `/report` 를 차례로 보내 보세요.

### 수동 설정 (마법사 없이)

`.env.example` 을 `.env` 로, `trading_settings.example.json` 을 `storage/trading_settings.json` 으로 복사해 값을 채웁니다.

## 하루 일정

| 시각 | 하는 일 |
|---|---|
| 뉴욕 09:35 (한국 22:35 또는 23:35) | 동기화, 지난 체결 정산, 종목별 오늘 주문 전송 |
| 뉴욕 16:40 | 계좌 동기화, 체결 정산(T·잔금 갱신), 매도 수익 기록, 장 마감 리포트 |
| 뉴욕 16:50 | 계좌 자산 기록 (추이 차트용) |
| 한국 08:00 (화~토) | 전일 매매 반영 후 HTML 리포트 전송 |

휴장일에는 건너뜁니다. 봇이 꺼져 있었다면 다음 실행 때 지난 체결을 먼저 정산하고 시작합니다.

## 명령어

| 명령어 | 설명 |
|---|---|
| `/status` | 현황, T 진행도, 별지점과 목표가, 잔금, 오늘 주문 상태. 종목 버튼으로 상세 지시서 |
| `/report` | HTML 리포트 생성과 전송 |
| `/cash` | 달러·원화 예수금과 보유 종목 |
| `/profit_history` | 실현 수익 요약과 전체 기록 |
| `/sync` | 계좌 동기화와 체결 정산 |
| `/manual_order` | 오늘 주문 계획을 보고 확인 후 수동 전송 (중복 주문 방지 확인 포함) |
| `/set_t 종목 [T값]` | 진행도 T 보정. 값을 생략하면 보유 원가로 다시 추정 |
| `/add_target 종목 원금 분할수 [수익%]` | 종목 추가. 예: `/add_target TQQQ 8000 20` |
| `/edit_target 종목 원금 분할수 [수익%]` | 종목 수정 |
| `/del_target [종목]` | 감시 중단 (주식은 팔지 않음) |
| `/liquidate [종목]` | 전량 매도 후 종료 |
| `/profit_reset` | 누적 수익 기록 초기화 (확인 후) |
| `/logs` | 최근 24시간 서버 로그 |

## V4.0 규칙 요약

기준은 라오어 카페의 V4.0 방법론 글이며, 코드(`v4_rules.py`)의 수치는 글의 예시 표와 같은지 테스트로 검증합니다.

- 별% = 기준% − (2×기준%÷분할수)×T, 기준%는 TQQQ 15, SOXL 20. 별지점 = 평단×(1+별%)
- 1회 매수금 = 잔금 ÷ (분할수 − T). 매수는 모두 LOC, 매수점은 별지점 −0.01
- 전반전(T < N/2)은 별지점·평단에 반반, 후반전은 별지점에 전액, 하락 대비 줍줍 LOC 추가
- 매도: 보유의 1/4은 별지점 LOC, 나머지는 +15%(TQQQ) / +20%(SOXL) 지정가
- T > N−1이면 리버스모드: 첫날 MOC 분할매도, 이후 5일 종가 평균 별지점으로 LOC 매도와 쿼터매수, 종가가 평단 −15% / −20% 위로 회복되면 일반모드 복귀

공식 규칙과 다른 점 하나: 현재가가 이미 지정가 목표 이상이면 봇은 **전량 MOC 매도**로 익절하고 그날 추가 매수를 하지 않습니다.

## 안전장치

- **접근 제어**: `.env` 의 `TELEGRAM_CHAT_ID`(또는 처음 `/start` 한 채팅) 외의 모든 메시지와 버튼을 무시합니다.
- **중복 주문 방지**: 종목별 하루 한 번 잠금, 수동 주문 전 미체결 확인, 주문 전 증권사 실잔고 대조
- **조회 실패 보호**: 증권사 응답이 비정상이면 보유 수량을 0으로 덮어쓰지 않고 기존 상태 유지, 체결 조회가 실패하면 정산을 다음으로 미룸
- **상태 파일 원자적 저장**: 쓰는 도중 꺼져도 T값 파일이 깨지지 않음
- **T와 잔금은 봇 기록 기준**: 증권사 앱·카페 엑셀과 어긋나면 `/set_t` 로 맞춥니다. `/status` 는 저장 T와 보유 원가 추정 T가 크게 다르면 경고합니다.

## 구버전에서 옮겨 온 경우

- V2.2 / V3.0 은 제거되었습니다. 설정 파일의 `method` 항목은 무시됩니다.
- 예전 버전은 일반모드에서 T값을 갱신하지 않았습니다. 업데이트 후 `/set_t 종목` 으로 현재 보유 원가 기준으로 다시 맞추고, 카페 엑셀 등 기준 값이 있으면 `/set_t 종목 값` 으로 지정하세요.
- 성과 로그의 예전 기록에는 예수금이 빠져 있을 수 있습니다.

## 파일 구조

| 파일 | 역할 |
|---|---|
| `main.py` | 진입점, 핸들러 등록, 접근 제어 |
| `v4_rules.py` | V4.0 순수 규칙 (계획 계산, T·잔금 정산). 단위 테스트 대상 |
| `strategy.py` | 시세·계좌로 계획 만들기, 주문 전송, 체결 정산 |
| `v4_state.py` | T, 잔금, 리버스 상태와 정산 대기 주문 저장 |
| `trade_service.py` | 일일 처리, 현황 요약, 매도 수익 반영 |
| `scheduler.py` | 스케줄 작업 |
| `telegram_bot.py`, `ui_formatter.py` | 명령어, 버튼, 메시지 형식 |
| `report_html.py` | HTML 리포트 생성 |
| `kis_client.py` | 한국투자증권 API 래퍼 |
| `setup_wizard.py` | 초기설정 마법사 |
| `storage/` | 설정, 상태, 기록, 로그 (커밋하지 않음) |

## 개발

```bash
python3 -m unittest discover tests
```

`tests/test_v4_rules.py` 는 카페 글의 예시 수치와 일치하는지, `tests/test_report_html.py` 는 리포트 생성을 확인합니다. 증권사 API 코드는 거래소 코드 매핑(TQQQ=NASD, SOXL=AMEX 등)을 통일하면 주문이 깨지므로 `tests/test_exchange_mappings.py` 가 지킵니다.

**Theory based on**: 라오어의 무한매수법 (Laoe's Infinite Purchase Method)
