"""키움증권 REST API 연동 - 1단계: 보유 내역(계좌평가잔고내역) 조회.

사용법:
    1. config_local.py에 KIWOOM_APP_KEY / KIWOOM_APP_SECRET / KIWOOM_ACCOUNT_NO 입력
    2. KIWOOM_IS_MOCK = True (모의투자)로 먼저 테스트
    3. python kiwoom_api.py 실행

참고: 키움 REST API는 정식 문서가 동적 페이지라 직접 크롤링이 어려워, 공개된
커뮤니티 라이브러리(kiwoom-rest-api)의 실제 구현을 참고해 엔드포인트/헤더 형식을
확인했습니다. kt00018 요청 바디의 정확한 필드명은 발급받은 키로 실제 호출해보면서
오류 메시지를 참고해 조정이 필요할 수 있습니다.
"""

import json
import sys

import requests

try:
    from config_local import (
        KIWOOM_APP_KEY,
        KIWOOM_APP_SECRET,
        KIWOOM_ACCOUNT_NO,
        KIWOOM_IS_MOCK,
    )
except ImportError:
    KIWOOM_APP_KEY = ""
    KIWOOM_APP_SECRET = ""
    KIWOOM_ACCOUNT_NO = ""
    KIWOOM_IS_MOCK = True

PROD_URL = "https://api.kiwoom.com"
MOCK_URL = "https://mockapi.kiwoom.com"


def get_base_url(is_mock=True):
    return MOCK_URL if is_mock else PROD_URL


def issue_access_token(app_key, app_secret, is_mock=True):
    """접근토큰발급 (OAuth2). 성공 시 토큰 문자열을 반환."""
    url = f"{get_base_url(is_mock)}/oauth2/token"
    resp = requests.post(
        url,
        json={
            "grant_type": "client_credentials",
            "appkey": app_key,
            "secretkey": app_secret,
        },
        headers={"Content-Type": "application/json;charset=UTF-8"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token") or data.get("access_token")
    if not token:
        raise RuntimeError(f"토큰 발급 실패: {data}")
    return token


def fetch_account_balance(access_token, account_no, is_mock=True):
    """계좌평가잔고내역요청 (kt00018) - 보유 종목 내역 조회."""
    url = f"{get_base_url(is_mock)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00018",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {access_token}",
    }
    body = {
        "acnt_no": account_no,
        "qry_tp": "1",  # 조회구분
        "dmst_stex_tp": "KRX",  # 국내거래소구분
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_holdings():
    """보유 내역을 조회해서 반환 (종목코드/종목명/수량/평가금액 등 원본 응답 그대로)."""
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    return fetch_account_balance(token, KIWOOM_ACCOUNT_NO, KIWOOM_IS_MOCK)


def fetch_order_status(access_token, account_no, is_mock=True):
    """계좌별주문체결현황요청 (kt00009) - 당일 주문/체결 현황 조회."""
    url = f"{get_base_url(is_mock)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00009",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {access_token}",
    }
    body = {
        "acnt_no": account_no,
        "qry_tp": "1",
        "stk_bond_tp": "0",
        "sell_tp": "0",
        "mrkt_tp": "0",
        "dmst_stex_tp": "KRX",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_order_status():
    """당일 주문/체결 현황을 조회해서 반환."""
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    return fetch_order_status(token, KIWOOM_ACCOUNT_NO, KIWOOM_IS_MOCK)


def fetch_stock_quote(access_token, stk_cd, is_mock=True):
    """종목기본정보요청 (ka10001) - 현재가 등 기본 시세 조회 (성과추적용 가격 스냅샷에 사용)."""
    url = f"{get_base_url(is_mock)}/api/dostk/stkinfo"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka10001",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {access_token}",
    }
    body = {"stk_cd": stk_cd}
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_stock_quotes(stk_cds):
    """여러 종목코드의 현재가를 한 번에 조회해서 {코드: 가격} 형태로 반환."""
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET을 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    prices = {}
    for code in stk_cds:
        try:
            data = fetch_stock_quote(token, code, KIWOOM_IS_MOCK)
            price = data.get("cur_prc") or data.get("stck_prpr")
            prices[code] = abs(int(price)) if price else None
        except Exception:
            prices[code] = None
    return prices


def place_order(stk_cd, qty, side, price=None, is_mock=KIWOOM_IS_MOCK):
    """주식 매수/매도 주문 (kt10000/kt10001).

    side: "buy" 또는 "sell". price를 주면 보통(한도가) 주문, 없으면 시장가 주문.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side는 'buy' 또는 'sell'이어야 합니다.")
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, is_mock)
    url = f"{get_base_url(is_mock)}/api/dostk/ordr"
    api_id = "kt10000" if side == "buy" else "kt10001"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": api_id,
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "dmst_stex_tp": "KRX",
        "stk_cd": stk_cd,
        "ord_qty": str(int(qty)),
        "trde_tp": "00" if price else "03",  # 00: 보통(한도가), 03: 시장가
        "ord_uv": str(int(price)) if price else "",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"[시스템] 모의투자 서버 사용: {KIWOOM_IS_MOCK}")
    try:
        result = get_holdings()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"❌ 조회 실패: {e}")
