#!/usr/bin/env python3
"""처음 쓰는 사람을 위한 초기설정 마법사. 표준 라이브러리만 쓰므로 패키지를 설치하기 전에도 실행된다.

  python3 setup_wizard.py              대화형 설정 (NAS 또는 내 PC)
  python3 setup_wizard.py --mode nas   NAS용 안내로 바로 시작
  python3 setup_wizard.py --check      이미 만든 설정이 제대로 동작하는지만 점검

하는 일: 텔레그램 봇 연결 확인 → 한국투자증권 키·계좌 확인 → 종목·원금·분할 입력 → .env와 설정 파일 생성
        → 실행 방법 안내. 입력한 비밀값은 화면에 다시 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
STORAGE = ROOT / "storage"
SETTINGS_FILE = STORAGE / "trading_settings.json"
TOKEN_FILE = STORAGE / "token.dat"
KIS = "https://openapi.koreainvestment.com:9443"
TG = "https://api.telegram.org"
DEFAULT_PCT = {"TQQQ": 15.0, "SOXL": 20.0}


# ── 입출력 도우미 ────────────────────────────────────────────────

def say(text: str = "") -> None:
    print(text)


def title(text: str) -> None:
    say(f"\n{'=' * 60}\n{text}\n{'=' * 60}")


def ask(prompt: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = (getpass.getpass(f"{prompt}{suffix}: ") if secret else input(f"{prompt}{suffix}: ")).strip()
        if raw:
            return raw
        if default is not None:
            return default


def ask_yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{hint}]: ").strip().lower()
    return default if not raw else raw in ("y", "yes", "예", "네", "ㅇ")


def ask_choice(prompt: str, options: list[str], default: int = 1) -> int:
    for i, o in enumerate(options, 1):
        say(f"  {i}. {o}")
    while True:
        raw = ask(prompt, str(default))
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw)
        say("  번호를 입력해 주세요.")


def ask_number(prompt: str, default: float | None = None, minimum: float = 0.0) -> float:
    while True:
        raw = ask(prompt, None if default is None else f"{default:g}").replace(",", "").replace("$", "")
        try:
            v = float(raw)
            if v >= minimum:
                return v
        except ValueError:
            pass
        say(f"  {minimum:g} 이상의 숫자를 입력해 주세요.")


def http_json(method: str, url: str, headers: dict | None = None, body: dict | None = None,
              params: dict | None = None, timeout: int = 15) -> tuple[dict | None, str]:
    """(JSON 응답, 오류 메시지). 오류가 없으면 메시지는 빈 문자열."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}"), ""
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode()), f"HTTP {e.code}"
        except Exception:
            return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"연결 실패: {e}"


# ── 텔레그램 ────────────────────────────────────────────────────

def tg_call(token: str, method: str, params: dict | None = None, timeout: int = 15):
    return http_json("GET", f"{TG}/bot{token}/{method}", params=params, timeout=timeout)


def step_telegram() -> tuple[str, str]:
    title("1단계. 텔레그램 봇 연결")
    say("텔레그램에서 @BotFather 를 열고 /newbot 으로 새 봇을 만들면 토큰이 나옵니다.")
    say("(예: 123456789:ABCdef... 모양의 긴 문자열. 다른 사람에게 보여 주지 마세요.)\n")
    while True:
        token = ask("봇 토큰", secret=True)
        res, err = tg_call(token, "getMe")
        if res and res.get("ok"):
            username = res["result"].get("username", "")
            say(f"  확인됨: @{username}")
            break
        say(f"  토큰을 확인하지 못했습니다 ({err or (res or {}).get('description', '')}). 다시 입력해 주세요.")

    say(f"\n이제 텔레그램에서 @{username} 을 열고 /start 를 보내 주세요. 연결되면 자동으로 알아챕니다. (최대 3분)")
    say("봇이 다른 곳에서 이미 실행 중이면 먼저 멈춰야 합니다.")
    tg_call(token, "deleteWebhook")
    chat_id, name, offset, deadline = "", "", None, time.time() + 180
    while time.time() < deadline and not chat_id:
        params = {"timeout": 5, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            params["offset"] = offset
        res, err = tg_call(token, "getUpdates", params, timeout=20)
        if not res or not res.get("ok"):
            if res and "Conflict" in str(res.get("description", "")):
                say("  이 봇이 다른 프로그램에서 실행 중입니다. 그쪽을 멈춘 뒤 다시 시도해 주세요.")
                break
            time.sleep(2)
            continue
        for upd in res["result"]:
            offset = upd["update_id"] + 1
            chat = (upd.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private":
                chat_id, name = str(chat["id"]), chat.get("first_name", "")
                break
    if not chat_id:
        say("  메시지를 받지 못했습니다.")
        chat_id = ask("채팅 ID를 직접 입력하거나(@userinfobot 에게 물어보면 알려줍니다) 비워 두고 Enter", "")
        if not chat_id:
            say("  채팅 ID 없이 진행합니다. 이 경우 처음 /start 한 채팅이 주인이 됩니다.")
    else:
        say(f"  연결됨: {name or '사용자'} (채팅 ID {chat_id})")
        tg_call(token, "sendMessage", {"chat_id": chat_id, "text": "설정 마법사가 이 채팅을 봇의 주인으로 등록했습니다. 곧 봇이 시작됩니다."})
    return token, chat_id


# ── 한국투자증권 ─────────────────────────────────────────────────

def kis_token(app_key: str, app_secret: str) -> tuple[str, str, str]:
    res, err = http_json("POST", f"{KIS}/oauth2/tokenP",
                         body={"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret})
    if res and res.get("access_token"):
        return res["access_token"], res.get("access_token_token_expired", ""), ""
    return "", "", str((res or {}).get("error_description") or (res or {}).get("msg1") or err or "알 수 없는 오류")


def kis_get(path: str, tr_id: str, token: str, app_key: str, app_secret: str, params: dict):
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    return http_json("GET", f"{KIS}{path}", headers=headers, params=params)


def parse_account(raw: str) -> tuple[str, str] | None:
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return digits[:8], digits[8:]
    if len(digits) == 8:
        return digits, "01"
    return None


def step_kis() -> dict:
    title("2단계. 한국투자증권 연결")
    say("한국투자증권 개발자센터(apiportal.koreainvestment.com)에서 실전투자용 앱키와 시크릿을 발급받으세요.")
    say("해외주식 거래 신청과 API 서비스 신청이 되어 있어야 합니다. 모의투자는 지원하지 않습니다.\n")
    while True:
        app_key = ask("앱키 (App Key)", secret=True)
        app_secret = ask("앱 시크릿 (App Secret)", secret=True)
        acct = None
        while acct is None:
            acct = parse_account(ask("계좌번호 (예: 12345678-01)"))
            if acct is None:
                say("  숫자 8자리(+상품코드 2자리)로 입력해 주세요.")
        cano, prdt = acct

        say("  증권사에 접속하는 중입니다...")
        token, expire, err = kis_token(app_key, app_secret)
        if not token:
            say(f"  토큰 발급 실패: {err}")
            say("  (같은 키로 1분 안에 다시 발급하면 제한에 걸립니다. 1분 뒤 다시 입력해 주세요.)")
            if not ask_yes("다시 입력할까요?"):
                sys.exit(1)
            continue
        bal, err = kis_get("/uapi/overseas-stock/v1/trading/inquire-balance", "TTTS3012R", token, app_key, app_secret,
                           {"CANO": cano, "ACNT_PRDT_CD": prdt, "OVRS_EXCG_CD": "", "TR_CRCY_CD": "USD",
                            "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
        if not bal or bal.get("rt_cd") != "0":
            say(f"  계좌 조회 실패: {(bal or {}).get('msg1') or err}")
            say("  계좌번호나 해외주식 거래 신청 여부를 확인해 주세요.")
            if not ask_yes("다시 입력할까요?"):
                sys.exit(1)
            continue
        say("  연결됨: 계좌 조회에 성공했습니다.")
        held = [i.get("ovrs_pdno", "").strip() for i in bal.get("output1", []) if float(i.get("ovrs_cblc_qty", 0) or 0) > 0]
        if held:
            say(f"  현재 보유 종목: {', '.join(held)}")
            say("  이미 보유 중인 종목을 봇에 등록하면 T값을 보유 원가로 추정해 시작합니다. 시작 후 /set_t 로 맞출 수 있습니다.")

        cash = 0.0
        ps, _ = kis_get("/uapi/overseas-stock/v1/trading/inquire-psamount", "TTTS3007R", token, app_key, app_secret,
                        {"CANO": cano, "ACNT_PRDT_CD": prdt, "OVRS_EXCG_CD": "NASD", "OVRS_ORD_UNPR": "1", "ITEM_CD": "TQQQ"})
        if ps and ps.get("rt_cd") == "0":
            cash = float((ps.get("output") or {}).get("ord_psbl_frcr_amt", 0) or 0)
            say(f"  달러 주문가능금액: ${cash:,.2f}")
        else:
            say("  달러 주문가능금액은 확인하지 못했습니다. (계속 진행합니다)")

        # 봇이 같은 토큰을 재사용하도록 저장 (KIS는 분당 1회 발급 제한이 있다)
        if expire:
            STORAGE.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(json.dumps({"token": token, "expire": expire}), encoding="utf-8")
        return {"APP_KEY": app_key, "APP_SECRET": app_secret, "CANO": cano, "ACNT_PRDT_CD": prdt, "_cash": cash}


# ── 전략 설정 ───────────────────────────────────────────────────

def step_strategy(cash: float) -> dict:
    title("3단계. 어떤 종목을, 얼마로, 몇 번에 나눠 살까요")
    say("무한매수법 V4.0은 원금을 N등분해 매일 규칙대로 분할 매수하고 목표에서 분할 매도합니다.")
    say("공식 방법론은 TQQQ(처음이면 권장)와 SOXL만 다룹니다. 두 종목은 원금을 따로 둡니다.")
    say("분할 수는 20(공격적) 또는 40(안정적)을 권장합니다. 1회 매수금이 종목 1주 가격보다 작으면 첫 매수가 안 됩니다.\n")
    pick = ask_choice("종목 선택", ["TQQQ만 (처음이라면 권장)", "SOXL만", "TQQQ와 SOXL 모두", "다른 종목 (비권장, 공식 검증 대상 아님)"])
    tickers = {1: ["TQQQ"], 2: ["SOXL"], 3: ["TQQQ", "SOXL"]}.get(pick)
    if tickers is None:
        tickers = [ask("종목 코드 (영문 대문자)").upper()]

    settings, total = {}, 0.0
    for tk in tickers:
        say(f"\n[{tk}]")
        seed = ask_number(f"  {tk}에 배정할 원금 (달러)", 4000.0 if tk == "TQQQ" else 2000.0, minimum=100.0)
        n = int(ask_number("  분할 수 (20 또는 40 권장)", 20, minimum=5))
        pct = ask_number("  3/4 지정가 익절 목표 % (공식 기본값 그대로면 Enter)", DEFAULT_PCT.get(tk, 10.0), minimum=0.1)
        say(f"  → 1회 매수금 ${seed / n:,.0f}, 지정가 익절 +{pct:g}%")
        settings[tk] = {"seed": seed, "total_a": n, "target_profit": pct}
        total += seed
    if cash > 0 and total > cash:
        say(f"\n주의: 원금 합계 ${total:,.0f}가 현재 주문가능금액 ${cash:,.0f}보다 큽니다. 나중에 입금해도 되지만 부족하면 주문이 거절됩니다.")
    say("\n처음에는 작은 원금으로 며칠 지켜보다가 늘리는 것을 권합니다. 원금은 /edit_target 으로 언제든 바꿀 수 있습니다.")
    return {"POST_CLOSE_SYNC_NY": "16:40", "STRATEGY_SETTINGS": settings}


# ── 파일 쓰기 ───────────────────────────────────────────────────

def write_files(env: dict, settings: dict) -> None:
    title("설정 파일 저장")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if ENV_FILE.exists():
        backup = ENV_FILE.with_name(f".env.bak-{stamp}")
        ENV_FILE.rename(backup)
        say(f"  기존 .env 를 {backup.name} 로 백업했습니다.")
    lines = [f"{k}={v}" for k, v in env.items() if not k.startswith("_") and v != ""]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    STORAGE.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=4), encoding="utf-8")
    say(f"  {ENV_FILE.name} 저장 (키와 토큰이 들어 있으니 절대 공유하거나 깃에 올리지 마세요)")
    say(f"  {SETTINGS_FILE.relative_to(ROOT)} 저장")
    gi = ROOT / ".gitignore"
    if (ROOT / ".git").exists() and (not gi.exists() or ".env" not in gi.read_text(encoding="utf-8")):
        say("  경고: .gitignore 에 .env 가 없습니다. 깃에 올리기 전에 반드시 추가하세요.")


# ── 점검 ────────────────────────────────────────────────────────

def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def check() -> bool:
    title("설정 점검")
    env, ok = load_env(), True

    def report(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        say(f"  {'정상' if good else '문제'}  {name}{': ' + detail if detail else ''}")

    missing = [k for k in ("TELEGRAM_TOKEN", "APP_KEY", "APP_SECRET", "CANO", "ACNT_PRDT_CD") if not env.get(k)]
    report(".env 필수 항목", not missing, ("빠진 항목 " + ", ".join(missing)) if missing else "")
    if missing:
        return False
    res, err = tg_call(env["TELEGRAM_TOKEN"], "getMe")
    report("텔레그램 토큰", bool(res and res.get("ok")), "@" + res["result"]["username"] if res and res.get("ok") else err)
    report("텔레그램 채팅 ID", bool(env.get("TELEGRAM_CHAT_ID")),
           "" if env.get("TELEGRAM_CHAT_ID") else "없음. 처음 /start 한 채팅이 주인이 됩니다")
    token, _exp, err = kis_token(env["APP_KEY"], env["APP_SECRET"])
    report("증권사 토큰", bool(token), err)
    if token:
        bal, err = kis_get("/uapi/overseas-stock/v1/trading/inquire-balance", "TTTS3012R", token, env["APP_KEY"], env["APP_SECRET"],
                           {"CANO": env["CANO"], "ACNT_PRDT_CD": env["ACNT_PRDT_CD"], "OVRS_EXCG_CD": "", "TR_CRCY_CD": "USD",
                            "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
        report("계좌 조회", bool(bal and bal.get("rt_cd") == "0"), (bal or {}).get("msg1", "") or err)
    try:
        st = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("STRATEGY_SETTINGS", {})
        report("종목 설정", bool(st), ", ".join(f"{k} ${v['seed']:,.0f}/{v['total_a']}분할" for k, v in st.items()) or "비어 있음")
    except Exception as e:
        report("종목 설정", False, f"읽지 못함: {e}")
    say("\n" + ("모든 항목이 정상입니다." if ok else "문제가 있는 항목을 고친 뒤 다시 점검하세요."))
    return ok


# ── 실행 안내 ───────────────────────────────────────────────────

def guide_nas() -> None:
    title("4단계. NAS에서 실행하기 (시놀로지 기준)")
    say("""이 폴더 전체(.env 와 storage 포함)를 NAS의 /volume1/docker/ubs_manager_bot 으로 복사하세요.
  - File Station으로 드래그해서 올리거나, 맥/윈도우에서 SMB로 연결된 공유 폴더에 복사합니다.

NAS에 SSH로 접속해(DSM 제어판 > 터미널 및 SNMP > SSH 활성화) 아래를 실행합니다.

  cd /volume1/docker/ubs_manager_bot
  sudo docker-compose up -d --build        # 안 되면: sudo docker compose up -d --build

잘 시작됐는지 확인:
  sudo docker logs --tail 30 my_telegram_bot

화면 조작을 선호하면: Container Manager > 프로젝트 > 생성 > 경로에 위 폴더 선택 > docker-compose.yaml 사용.

업데이트할 때는 새 파일로 덮어쓴 뒤 같은 명령을 다시 실행합니다. 재시작은: sudo docker restart my_telegram_bot
설정(.env, storage)은 폴더에 남아 있으므로 업데이트해도 유지됩니다.""")


def guide_pc() -> None:
    title("4단계. 내 PC에서 실행하기")
    say("PC는 미국장이 열리는 한국 시간 밤(22:35 또는 23:35)과 장 마감 후까지 켜져 있어야 합니다.")
    say("절전·잠자기를 끄지 않으면 주문이 나가지 않습니다. 24시간 운용에는 NAS나 서버가 더 안전합니다.\n")
    if sys.version_info < (3, 10):
        say(f"파이썬 3.10 이상이 필요합니다 (현재 {platform.python_version()}). python.org 에서 설치한 뒤 다시 실행하세요.")
        return
    venv = ROOT / ".venv"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if ask_yes("가상환경을 만들고 필요한 패키지를 설치할까요? (인터넷 필요, 몇 분 걸립니다)"):
        if not py.exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(py), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], check=True)
        say("  설치 완료.")
    run_cmd = f'"{py}" main.py' if py.exists() else "python main.py"
    say(f"\n봇 시작 명령 (이 폴더에서):\n  {run_cmd}")
    say("터미널을 닫으면 봇도 멈춥니다. 계속 돌리려면 터미널을 열어 두거나 작업 스케줄러(윈도우)/launchd(맥)에 등록하세요.")
    if py.exists() and ask_yes("지금 봇을 시작할까요? (Ctrl+C로 중지)", default=False):
        os.chdir(ROOT)
        subprocess.run([str(py), "main.py"])


def finish() -> None:
    title("마지막으로")
    say("""1. 봇이 시작되면 텔레그램에서 /start 를 보내세요. 버튼 메뉴가 나타납니다.
2. /status 로 종목과 오늘 계획이 보이는지, /report 로 HTML 리포트가 오는지 확인하세요.
3. 매일 뉴욕 09:35(한국 22:35 또는 23:35)에 자동으로 주문이 나갑니다. 장 마감 후에는 체결을 정산합니다.
4. 문제가 생기면 /logs 로 최근 24시간 기록을 볼 수 있고, python3 setup_wizard.py --check 로 설정을 다시 점검할 수 있습니다.

투자 손익의 책임은 본인에게 있습니다. 3배 레버리지 ETF는 변동성이 매우 큽니다.""")


def main() -> None:
    ap = argparse.ArgumentParser(description="무한매수봇 초기설정 마법사")
    ap.add_argument("--mode", choices=["nas", "pc"], help="실행 환경 (생략하면 물어봅니다)")
    ap.add_argument("--check", action="store_true", help="설정만 점검")
    args = ap.parse_args()
    if args.check:
        sys.exit(0 if check() else 1)

    title("무한매수법 V4.0 봇 초기설정")
    say("몇 가지를 물어보고 설정 파일을 만들어 드립니다. 중간에 Ctrl+C로 멈춰도 기존 설정은 바뀌지 않습니다.")
    mode = args.mode or ["nas", "pc"][ask_choice("어디에서 봇을 돌릴 건가요", ["NAS (시놀로지 Docker, 24시간 운용 권장)", "내 PC (맥, 윈도우, 리눅스)"]) - 1]

    if ENV_FILE.exists():
        say("\n이미 .env 파일이 있습니다.")
        pick = ask_choice("무엇을 할까요", ["설정만 점검", "처음부터 다시 설정 (기존 .env는 백업)", "종료"], default=1)
        if pick == 1:
            sys.exit(0 if check() else 1)
        if pick == 3:
            return

    token, chat_id = step_telegram()
    kis = step_kis()
    settings = step_strategy(kis.pop("_cash", 0.0))
    write_files({"TELEGRAM_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id, **kis}, settings)
    check()
    (guide_nas if mode == "nas" else guide_pc)()
    finish()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\n중단했습니다. 다시 실행하면 처음부터 시작합니다.")
        sys.exit(130)
