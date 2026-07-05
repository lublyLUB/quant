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
import logging
import sys
import threading
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WS] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("ws_debug.log", encoding="utf-8"),
    ],
)
_wslog = logging.getLogger("kiwoom_ws")

import requests
import websocket

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


def get_transaction_history(strt_dt: str, end_dt: str, tp: str = "0", stk_cd: str = "") -> list:
    """위탁종합거래내역요청 (kt00015).

    tp: "0"=전체, "1"=입출금, "3"=매매, "4"=매수, "5"=매도, "6"=입금, "7"=출금
    주요 반환 필드 (trst_ovrl_trde_prps_array):
      trde_dt, rmrk_nm, stk_nm, trde_amt, exct_amt,
      entra_remn, io_tp_nm, cmsn, trde_agri_tax, incm_resi_tax, tax_sum_cmsn
    연속조회(cont-yn=Y)로 전체 데이터를 가져옴.
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    body = {
        "strt_dt": strt_dt,
        "end_dt": end_dt,
        "tp": tp,
        "stk_cd": stk_cd,
        "crnc_cd": "",
        "gds_tp": "1",       # 국내주식
        "frgn_stex_code": "",
        "dmst_stex_tp": "%",
        "qry_sort_tp": "1",  # 최근거래순
    }
    results = []
    cont_yn = "N"
    next_key = ""
    while True:
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": "kt00015",
            "cont-yn": cont_yn,
            "next-key": next_key,
            "authorization": f"Bearer {token}",
        }
        resp = requests.post(url, headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("trst_ovrl_trde_prps_array") or []
        results.extend(rows)
        cont_yn = resp.headers.get("cont-yn", "N")
        next_key = resp.headers.get("next-key", "")
        if cont_yn != "Y":
            break
    return results


def get_net_deposit(strt_dt: str, end_dt: str) -> dict:
    """kt00015로 기간 내 순입금(입금-출금) 및 세금/수수료 합계 계산."""
    rows = get_transaction_history(strt_dt, end_dt, tp="1")  # 입출금
    in_amt  = sum(int(r.get("trde_amt") or 0) for r in rows if r.get("io_tp_nm", "").startswith("입"))
    out_amt = sum(int(r.get("trde_amt") or 0) for r in rows if r.get("io_tp_nm", "").startswith("출"))
    # 매매 수수료/세금
    trade_rows = get_transaction_history(strt_dt, end_dt, tp="3")
    tax_cmsn = sum(int(r.get("tax_sum_cmsn") or 0) for r in trade_rows)
    return {
        "in_amt":   in_amt,
        "out_amt":  out_amt,
        "net":      in_amt - out_amt,
        "tax_cmsn": tax_cmsn,
    }


def get_daily_asset_history(start_dt: str, end_dt: str) -> list:
    """kt00002 일별추정예탁자산현황. start_dt~end_dt 기간의 일별 자산 리스트 반환."""
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00002",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    resp = requests.post(url, headers=headers,
                         json={"start_dt": start_dt, "end_dt": end_dt}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("daly_prsm_dpst_aset_amt_prst") or []


def get_stock_list_flags():
    """ka10099로 KOSPI+KOSDAQ 전 종목의 플래그를 한 번에 조회.

    반환: {종목코드: {'is_admin': bool, 'is_admin_warning': bool, 'is_halt': bool, 'is_inv_warn': bool}}
    """
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/mrkcond"
    result = {}

    for mrkt_tp in ("0", "10"):  # 0: KOSPI, 10: KOSDAQ
        cont_yn = "N"
        next_key = ""
        while True:
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "api-id": "ka10099",
                "cont-yn": cont_yn,
                "next-key": next_key,
                "authorization": f"Bearer {token}",
            }
            resp = requests.post(url, headers=headers, json={"mrkt_tp": mrkt_tp}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            for s in data.get("list") or []:
                code = (s.get("code") or "").strip()
                if not code:
                    continue
                audit  = (s.get("auditInfo") or "").strip()
                state  = (s.get("state") or "").strip()
                warn   = str(s.get("orderWarning") or "0").strip()
                result[code] = {
                    "is_admin":         "관리" in audit and "우려" not in audit,
                    "is_admin_warning": "우려" in audit,
                    "is_halt":          "정지" in state or state == "2",
                    "is_inv_warn":      warn in ("3", "4", "5"),
                }
            cont_yn  = resp.headers.get("cont-yn", "N")
            next_key = resp.headers.get("next-key", "")
            if cont_yn != "Y":
                break

    return result


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


def get_realized_pl(stk_cd, strt_dt=""):
    """일자별종목별실현손익요청_일자 (ka10072) - 특정 종목의 실현손익·실제 수수료·세금 조회.

    stk_cd: 종목코드 6자리
    strt_dt: 시작일자 YYYYMMDD (빈값이면 당일)
    반환: tdy_sel_pl(당일매도손익), pl_rt(손익율), tdy_trde_cmsn(수수료), tdy_trde_tax(세금)
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    from datetime import datetime as _dt
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka10072",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "stk_cd": stk_cd,
        "strt_dt": strt_dt or _dt.now().strftime("%Y%m%d"),
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_daily_balance(qry_dt=""):
    """일별잔고수익률 (ka01690) - 특정 날짜의 포트폴리오 평가금액·수익률·종목별 잔고 조회.

    qry_dt: YYYYMMDD. 빈값이면 당일.
    tot_evlt_amt: 브로커 계산 총 평가금액 (직접 계산보다 정확)
    dbst_bal: 예수금
    day_bal_rt: 종목별 현재가·평가손익·수익률
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka01690",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    from datetime import datetime as _dt
    body = {"qry_dt": qry_dt or _dt.now().strftime("%Y%m%d")}
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_account_eval(qry_tp: str = "1"):
    """계좌평가현황요청 (kt00004).

    qry_tp: "0"=전체, "1"=상장폐지종목제외
    주요 반환 필드:
      tdy_lspft / tdy_lspft_rt  - 당일 투자손익 / 손익율
      lspft2    / lspft_ratio   - 당월 투자손익 / 손익율
      lspft     / lspft_rt      - 누적 투자손익 / 손익율
      tdy_lspft_amt / invt_bsamt / lspft_amt - 당일/당월/누적 투자원금
      stk_acnt_evlt_prst[].tdy_buyq/tdy_sellq - 금일 매수/매도 수량
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00004",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    resp = requests.post(url, headers=headers, json={"qry_tp": qry_tp, "dmst_stex_tp": "KRX"}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_deposit_detail(qry_tp: str = "2"):
    """예수금상세현황요청 (kt00001).

    qry_tp: "2"=일반조회, "3"=추정조회
    주요 반환 필드:
      entr          - 예수금
      pymn_alow_amt - 출금가능금액
      ord_alow_amt  - 주문가능금액
      d1_entra      - D+1 추정예수금
      d1_pymn_alow_amt - D+1 출금가능금액
      d2_entra      - D+2 추정예수금
      d2_pymn_alow_amt - D+2 출금가능금액
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00001",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    resp = requests.post(url, headers=headers, json={"qry_tp": qry_tp}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_settlement_balance():
    """체결잔고요청 (kt00005) - 주문가능현금(ord_alowa)과 종목별 결제잔고(setl_remn) 제공.

    ord_alowa: 실제 매수 주문에 사용 가능한 현금 (미결제·수수료 등 반영된 정확한 값)
    setl_remn: 종목별 결제 완료된 잔고 (미체결 매수 주문 미포함 → 실제 체결 수량)
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00005",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {"dmst_stex_tp": "KRX"}
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_order_possible_qty(stk_cd: str, price: int, trde_tp: str = "2"):
    """주문인출가능금액요청 (kt00010) - 특정 종목/가격 기준 주문가능수량 조회.

    trde_tp: "1"=매도, "2"=매수
    주요 반환:
      profa_100ord_alowq - 100% 증거금 기준 주문가능수량 (수수료 포함)
      ord_alowa          - 주문가능현금
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00010",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "io_amt": "",
        "stk_cd": f"A{stk_cd}" if not stk_cd.startswith("A") else stk_cd,
        "trde_tp": trde_tp,
        "trde_qty": "",
        "uv": str(price),
        "exp_buy_unp": "",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return {
        "max_qty":   int(data.get("profa_100ord_alowq") or 0),
        "ord_alowa": int(data.get("ord_alowa") or 0),
        "profa_20":  int(data.get("profa_20ord_alowq") or 0),
        "profa_30":  int(data.get("profa_30ord_alowq") or 0),
    }


def get_daily_contract_summary():
    """계좌별주문체결현황요청 (kt00009) - 당일 매도/매수/전체 약정금액 합계 조회."""
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00009",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "ord_dt": "",
        "stk_bond_tp": "1",   # 주식
        "mrkt_tp": "0",        # 전체
        "sell_tp": "0",        # 전체
        "qry_tp": "1",         # 체결
        "stk_cd": "",
        "fr_ord_no": "",
        "dmst_stex_tp": "%",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return {
        "sell_amt": int(data.get("sell_grntl_engg_amt") or 0),
        "buy_amt":  int(data.get("buy_engg_amt") or 0),
        "total":    int(data.get("engg_amt") or 0),
    }


def get_next_day_settlement():
    """계좌별익일결제예정내역요청 (kt00008).

    주요 반환 필드:
      sell_amt_sum / buy_amt_sum  - 매도/매수 정산 합계
      acnt_nxdy_setl_frcs_prps_array[]:
        stk_nm, sell_tp, qty, unp, exct_amt - 종목별 정산금액
        cmsn, trde_tax, incm_tax, rstx, resi_tax - 거래 비용
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00008",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    resp = requests.post(url, headers=headers, json={"strt_dcd_seq": ""}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_today_deposit():
    """계좌별당일현황요청 (kt00017) - 당일 입금액(ina_amt), 출금액(outa) 조회."""
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00017",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    resp = requests.post(url, headers=headers, json={}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_daily_realized_pl(qry_dt: str = "") -> dict:
    """일자별실현손익요청 (ka10074) - 특정일의 계좌 전체 실현손익 조회.

    qry_dt: YYYYMMDD. 빈값이면 당일.
    주요 반환 필드 (daly_rlzt_pl_prps_array[]):
      stk_nm, stk_cd, sel_qty, pur_uv, sel_uv, sel_pl, sel_pl_rt, cmsn, tax
    sum_sel_pl: 실현손익 합계, sum_cmsn: 수수료 합계
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    from datetime import datetime as _dt
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka10074",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    resp = requests.post(url, headers=headers, json={"qry_dt": qry_dt or _dt.now().strftime("%Y%m%d")}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_today_trade_journal() -> dict:
    """당일매매일지요청 (ka10170) - 오늘 매수/매도 전체 체결 내역 조회.

    주요 반환 필드 (tdy_trde_jrnl_array[]):
      cntr_tm(체결시간), stk_nm(종목명), sell_tp_nm(매수/매도),
      cntr_qty(체결수량), cntr_pric(체결가격), cntr_amt(체결금액),
      rlzt_pl(실현손익), pur_amt(매입금액)
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka10170",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    resp = requests.post(url, headers=headers, json={}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_vi_stocks() -> dict:
    """변동성완화장치발동종목요청 (ka10054) - 현재 VI 발동 중인 종목 목록 조회.

    반환: {"codes": [종목코드,...], "items": [{stk_cd, stk_nm, vi_gubun, vi_pric}, ...]}
    매수 주문 전 호출해서 VI 발동 종목을 사전 차단한다.
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET을 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/mrkcond"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka10054",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    resp = requests.post(url, headers=headers, json={}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    items = []
    for r in (data.get("vi_prcs_stk_prps_array") or data.get("list") or []):
        code = (r.get("stk_cd") or r.get("code") or "").lstrip("A").strip()
        if code:
            items.append({
                "stk_cd":   code,
                "stk_nm":   r.get("stk_nm") or r.get("name") or "",
                "vi_gubun": r.get("vi_gubun") or r.get("vi_type") or "",
                "vi_pric":  r.get("vi_pric") or r.get("vi_price") or "",
            })
    return {"codes": [it["stk_cd"] for it in items], "items": items}


def get_daily_stock_pl():
    """계좌수익률요청 (ka10085) - 당일 매도손익(tdy_sel_pl) 조회.

    보유 종목별 당일매도손익·수수료·세금 제공.
    kt00018의 (cur_prc - pred_close_pric) × rmnd_qty와 합산하면 순수 당일 주가 변동 손익.
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka10085",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    resp = requests.post(url, headers=headers, json={"stex_tp": "0"}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_account_summary():
    """계좌평가현황요청 (kt00004) - 누적/당일/당월 투자원금·손익·손익율 조회.

    lspft_amt: 누적투자원금, lspft: 누적투자손익, lspft_rt: 누적손익율
    tdy_lspft: 당일투자손익, tdy_lspft_rt: 당일손익율
    prsm_dpst_aset_amt: 추정예탁자산(총평가금액)
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00004",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    resp = requests.post(url, headers=headers, json={"qry_tp": "1", "dmst_stex_tp": "KRX"}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_period_eval(fr_dt, to_dt):
    """계좌기간별수익률현황요청 (kt00016) - 기간 내 총입금·총출금·평가손익·수익률 조회.

    fr_dt, to_dt: YYYYMMDD
    termin_tot_trns: 기간내총입금, termin_tot_pymn: 기간내총출금
    evltv_prft: 평가손익 (입출금 반영), prft_rt: 수익률
    invt_bsamt: 투자원금평잔
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00016",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {"fr_dt": fr_dt, "to_dt": to_dt}
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_order_status(access_token, account_no, is_mock=True, qry_tp="1"):
    """계좌별주문체결현황요청 (kt00009) - 당일 주문/체결 현황 조회.

    qry_tp: "0"-전체(미체결 포함), "1"-체결만.
    """
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
        "qry_tp": qry_tp,
        "stk_bond_tp": "0",
        "sell_tp": "0",
        "mrkt_tp": "0",
        "dmst_stex_tp": "KRX",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_order_status(qry_tp="1"):
    """당일 주문/체결 현황을 조회해서 반환. qry_tp="0"이면 미체결 주문도 포함."""
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    return fetch_order_status(token, KIWOOM_ACCOUNT_NO, KIWOOM_IS_MOCK, qry_tp)


def get_open_orders(sell_tp="0"):
    """계좌별주문체결내역상세요청 (kt00007) - qry_tp=3(미체결만) 으로 조회.

    ord_remnq(주문잔량) 직접 제공, mdfy_cncl(정정취소) 필드로 취소 여부 판별 가능.
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00007",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "ord_dt": "",           # 당일
        "qry_tp": "3",          # 미체결만
        "stk_bond_tp": "0",     # 전체
        "sell_tp": sell_tp,
        "stk_cd": "",           # 전종목
        "fr_ord_no": "",
        "dmst_stex_tp": "%",    # 전체 거래소
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_fills(sell_tp="0"):
    """체결요청 (ka10076) - oso_qty(미체결수량)/ord_stt(주문상태) 포함, 미체결 판별에 더 정확.

    sell_tp: "0"-전체, "1"-매도, "2"-매수.
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka10076",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "stk_cd": "",
        "qry_tp": "0",    # 0:전체 종목
        "sell_tp": sell_tp,
        "ord_no": "",
        "stex_tp": "0",   # 0:통합
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def check_stock_warning(stk_cds):
    """종목정보 조회 (ka10100) - 투자유의종목 여부 확인.

    orderWarning: 0=정상, 2=정리매매, 3=단기과열, 4=투자위험, 5=투자경고
    매수 주문 전 호출해서 위험 종목을 사전 차단한다.
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET을 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/stkinfo"
    warnings = {}
    for code in stk_cds:
        try:
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "api-id": "ka10100",
                "cont-yn": "N",
                "next-key": "",
                "authorization": f"Bearer {token}",
            }
            resp = requests.post(url, headers=headers, json={"stk_cd": code}, timeout=15)
            data = resp.json()
            warnings[code] = {
                "orderWarning": data.get("orderWarning", "0"),
                "state": data.get("state", ""),
                "name": data.get("name", ""),
            }
        except Exception:
            warnings[code] = {"orderWarning": "0", "state": "", "name": ""}
    return warnings


def get_daily_trade_amount(stk_cd, n_days=20):
    """ka10086 일별주가로 최근 n_days일 평균 거래대금(원) 반환."""
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/stkpc"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka10086",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    resp = requests.post(url, headers=headers, json={"stk_cd": stk_cd}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    rows = (data.get("daly_stkpc") or [])[:n_days]
    if not rows:
        return 0
    total = sum(int(r.get("amt_mn") or 0) * 1_000_000 for r in rows)
    return total // len(rows)


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


def fetch_quote(access_token, stk_cd, api_id, is_mock=True):
    """호가/시세 조회 공통 함수 (/api/dostk/mrkcond 계열)."""
    url = f"{get_base_url(is_mock)}/api/dostk/mrkcond"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": api_id,
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {access_token}",
    }
    resp = requests.post(url, headers=headers, json={"stk_cd": stk_cd}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_stock_quotes(stk_cds):
    """여러 종목코드의 현재가를 조회해서 {코드: 가격} 형태로 반환.

    시간대별 최적 API 자동 선택:
    - 09:00~15:30 정규장: ka10004(주식호가) sel_fpr_bid(매도최우선호가) — 최유리지정가 매수 실제 체결가
    - 16:00~18:00 시간외단일가: ka10087(시간외단일가) ovt_sigpric_cur_prc
    - 그 외: ka10001(종목기본정보) cur_prc
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET을 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)

    t = datetime.now().time()
    t_0900 = datetime.strptime("09:00", "%H:%M").time()
    t_1530 = datetime.strptime("15:30", "%H:%M").time()
    t_1600 = datetime.strptime("16:00", "%H:%M").time()
    t_1800 = datetime.strptime("18:00", "%H:%M").time()

    prices = {}
    for code in stk_cds:
        try:
            # ka10001 단일 호출로 현재가·상한가·하한가·예상체결가 한 번에 조회
            # 시간외단일가(16:00~18:00)에는 ka10087로 정확한 시간외 현재가 추가 조회
            data = fetch_stock_quote(token, code, KIWOOM_IS_MOCK)
            price_raw = data.get("cur_prc") or data.get("stck_prpr")
            p = abs(int(price_raw)) if price_raw else None

            if t_1600 <= t < t_1800:
                try:
                    d2 = fetch_quote(token, code, "ka10087", KIWOOM_IS_MOCK)
                    ovt = d2.get("ovt_sigpric_cur_prc")
                    if ovt:
                        p = abs(int(ovt))
                except Exception:
                    pass  # 실패 시 ka10001 cur_prc 유지

            upl = data.get("upl_pric")
            lst = data.get("lst_pric")
            exp = data.get("exp_cntr_pric")
            flu = data.get("flu_rt")
            prices[code] = {
                "price": p,
                "upper_limit": abs(int(upl)) if upl else None,
                "lower_limit": abs(int(lst)) if lst else None,
                "exp_price": abs(int(exp)) if exp else None,
                "flu_rt": float(flu) if flu else None,
            } if p else None
        except Exception:
            prices[code] = None
    return prices


def ensure_market_hours(now=None):
    """09:00~18:00(정규장+시간외) 범위가 아니면 명확한 에러로 막는다.

    주문/취소/정정 모두 이 시간 밖에서는 키움 거래소 자체가 마감 상태라 거부되는데,
    그 경우 "취소 실패" 같은 모호한 메시지 대신 장마감 때문임을 바로 알 수 있게 한다.
    """
    now = now or datetime.now()
    t = now.time()
    if t < datetime.strptime("09:00", "%H:%M").time() or t >= datetime.strptime("18:00", "%H:%M").time():
        raise RuntimeError(f"장마감 시간({t.strftime('%H:%M')})이라 지금은 처리할 수 없습니다. 09:00~18:00 사이에 다시 시도해주세요.")
    return t


def resolve_trde_tp(price, now=None):
    """현재 시각(장중/시간외)에 맞는 거래구분(trde_tp) 코드를 정한다.

    09:00~15:20 정규장   : 최유리지정가(06) / 시장가(03)
    15:20~15:30 동시호가 : 지정가(00) — 반드시 price 필요 (예상체결가 기준)
    15:30~16:00 시간외   : 장마감후시간외 종가(81)
    16:00~18:00 시간외단일가: 지정가(62)
    """
    t = ensure_market_hours(now)
    t_1520 = datetime.strptime("15:20", "%H:%M").time()
    t_1530 = datetime.strptime("15:30", "%H:%M").time()
    t_1600 = datetime.strptime("16:00", "%H:%M").time()

    if t < t_1520:
        return "06" if price else "03"   # 최유리지정가 / 시장가
    if t < t_1530:
        if not price:
            raise RuntimeError("동시호가(15:20~15:30)는 반드시 가격을 지정해야 합니다.")
        return "00"                       # 동시호가 지정가
    if t < t_1600:
        if not price:
            raise RuntimeError("장마감후시간외(15:30~16:00)는 시장가 주문을 지원하지 않습니다.")
        return "81"                       # 장마감후시간외
    if not price:
        raise RuntimeError("시간외단일가(16:00~18:00)는 가격을 지정해야 합니다.")
    return "62"                           # 시간외단일가


def get_closing_auction_price(stk_cd: str) -> int:
    """동시호가 주문용 가격 계산 (ka10001 예상체결가 × 1.005, 상한가 이하로 제한).

    15:20 시점 예상체결가에 0.5% 버퍼를 더해 동시호가 내 체결 확률을 높인다.
    예상체결가가 없으면 현재가를 사용한다.
    """
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    data  = fetch_stock_quote(token, stk_cd, KIWOOM_IS_MOCK)
    exp   = data.get("exp_cntr_pric")
    cur   = data.get("cur_prc")
    upl   = data.get("upl_pric")
    base  = abs(int(exp)) if exp else abs(int(cur)) if cur else 0
    upper = abs(int(upl)) if upl else base
    price = int(base * 1.005)
    return min(price, upper)


def place_order(stk_cd, qty, side, price=None, is_mock=KIWOOM_IS_MOCK):
    """주식 매수/매도 주문 (kt10000/kt10001).

    side: "buy" 또는 "sell". 현재 시각에 따라 정규장/동시호가/시간외 거래구분을 자동으로 선택한다.
    동시호가(15:20~15:30) 매수 시 price=None이면 예상체결가×1.005를 자동 계산한다.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side는 'buy' 또는 'sell'이어야 합니다.")
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")

    now  = datetime.now()
    t    = now.time()
    t_1520 = datetime.strptime("15:20", "%H:%M").time()
    t_1530 = datetime.strptime("15:30", "%H:%M").time()
    # 동시호가 매수 시 price 미지정이면 자동 계산
    if side == "buy" and t_1520 <= t < t_1530 and not price:
        price = get_closing_auction_price(stk_cd)

    trde_tp = resolve_trde_tp(price, now)
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
    # 최유리지정가(06)/시장가(03)는 가격을 거래소가 정하므로 ord_uv를 비워야 한다.
    needs_price = trde_tp in ("00", "81", "62")
    body = {
        "dmst_stex_tp": "KRX",
        "stk_cd": stk_cd,
        "ord_qty": str(int(qty)),
        "trde_tp": trde_tp,
        "ord_uv": str(int(price)) if (needs_price and price) else "",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def modify_order(stk_cd, orig_ord_no, mdfy_qty, mdfy_uv, is_mock=KIWOOM_IS_MOCK):
    """주식 정정주문 (kt10002) - 미체결 주문의 잔량/가격을 새 값으로 정정한다."""
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    ensure_market_hours()
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, is_mock)
    url = f"{get_base_url(is_mock)}/api/dostk/ordr"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt10002",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "dmst_stex_tp": "KRX",
        "orig_ord_no": orig_ord_no,
        "stk_cd": stk_cd,
        "mdfy_qty": str(int(mdfy_qty)),
        "mdfy_uv": str(int(mdfy_uv)),
        "mdfy_cond_uv": "",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def cancel_order(stk_cd, orig_ord_no, cncl_qty, is_mock=KIWOOM_IS_MOCK):
    """주식 취소주문 (kt10003) - 미체결 주문을 취소한다."""
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    ensure_market_hours()
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, is_mock)
    url = f"{get_base_url(is_mock)}/api/dostk/ordr"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt10003",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "dmst_stex_tp": "KRX",
        "orig_ord_no": orig_ord_no,
        "stk_cd": stk_cd,
        "cncl_qty": str(int(cncl_qty)),
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── 실시간 주문체결 WebSocket ──────────────────────────────────────────────────

PROD_WS_URL = "wss://api.kiwoom.com/api/dostk/websocket"
MOCK_WS_URL = "wss://mockapi.kiwoom.com/api/dostk/websocket"


# 실시간 잔고 캐시 — 04 이벤트로 업데이트, {종목코드: {필드코드: 값}}
realtime_balance: dict = {}


def _parse_values(raw):
    """values 필드(List<Map> or dict)를 {str키: 값} dict로 변환."""
    if isinstance(raw, list):
        vals = {}
        for item in raw:
            vals.update(item)
        return vals
    return {str(k): v for k, v in (raw or {}).items()}


def start_order_realtime(on_fill, on_balance=None, on_stock_info=None, on_error=None,
                         on_vi=None, on_market_open=None):
    """주문체결(00) + 잔고(04) + 종목정보(0g) + VI(1h) + 장시작(0s) 실시간 WebSocket 시작.

    on_fill(values)        : 체결 이벤트.
    on_balance(values)     : 잔고 업데이트 (optional).
    on_stock_info(code, v) : 종목정보 (optional).
    on_vi(code, vals)      : VI 발동/해제 실시간 (optional). vals["215"]="1"발동,"2"해제.
    on_market_open(vals)   : 장운영 이벤트(장전/개장/마감 등) (optional).
    """
    ws_url = MOCK_WS_URL if KIWOOM_IS_MOCK else PROD_WS_URL

    # 보유 종목 코드 목록 (0g/1h 등록용) — 외부에서 갱신 가능
    _ws_ref = [None]

    def _register_stock_info(ws, codes):
        """보유 종목 코드를 0g(종목정보) + 1h(VI발동/해제)로 등록."""
        if not codes:
            return
        _wslog.info("REG 0g+1h 종목 %d개: %s", len(codes), codes)
        ws.send(json.dumps({
            "trnm": "REG",
            "grp_no": "2",
            "refresh": "0",
            "data": [{"item": code, "type": "0g"} for code in codes],
        }))
        ws.send(json.dumps({
            "trnm": "REG",
            "grp_no": "3",
            "refresh": "0",
            "data": [{"item": code, "type": "1h"} for code in codes],
        }))

    def on_open(ws):
        _ws_ref[0] = ws
        _wslog.info("WebSocket 연결됨 (%s)", ws_url)
        # 주문체결(00) + 잔고(04) + 장운영(0s)
        ws.send(json.dumps({
            "trnm": "REG",
            "grp_no": "1",
            "refresh": "1",
            "data": [
                {"item": "", "type": "00"},
                {"item": "", "type": "04"},
                {"item": "", "type": "0s"},
            ],
        }))
        _wslog.info("REG 완료: 00(체결) 04(잔고) 0s(장운영)")
        # 현재 보유 종목 즉시 등록
        try:
            h = get_holdings()
            codes = [(r.get("stk_cd") or "").lstrip("A")
                     for r in h.get("acnt_evlt_remn_indv_tot", []) if r.get("stk_cd")]
            if codes:
                _register_stock_info(ws, codes)
            else:
                _wslog.info("보유 종목 없음 — 0g/1h 등록 스킵")
        except Exception as e:
            _wslog.warning("보유종목 조회 실패 (0g/1h 미등록): %s", e)

    # 보유 종목 변경 시 외부에서 호출
    def update_watched_codes(codes):
        ws = _ws_ref[0]
        if ws and codes:
            _register_stock_info(ws, codes)

    def on_message(ws, message):
        try:
            msg = json.loads(message)
        except Exception:
            return
        trnm = msg.get("trnm")
        # REG 응답(ACK) 로그
        if trnm == "PING":
            return
        if trnm != "REAL":
            _wslog.info("수신 trnm=%s: %s", trnm, json.dumps(msg, ensure_ascii=False)[:200])
            return
        for entry in msg.get("data", []):
            evt_type = entry.get("type")
            vals = _parse_values(entry.get("values"))
            _wslog.info("REAL type=%s item=%s vals_keys=%s",
                        evt_type, entry.get("item", ""), list(vals.keys())[:10])
            if evt_type == "00" and vals.get("913") == "체결":
                on_fill(vals)
            elif evt_type == "04":
                code = (vals.get("9001") or "").lstrip("A")
                if code:
                    realtime_balance[code] = vals
                if on_balance:
                    on_balance(vals)
            elif evt_type == "0g":
                _wslog.info("0g 종목정보 code=%s vals=%s",
                            entry.get("item", ""), json.dumps(vals, ensure_ascii=False)[:300])
                if on_stock_info:
                    on_stock_info(entry.get("item", ""), vals)
            elif evt_type == "1h":
                code = (entry.get("item") or vals.get("9001") or "").lstrip("A")
                _wslog.info("1h VI code=%s vals=%s",
                            code, json.dumps(vals, ensure_ascii=False)[:300])
                if on_vi and code:
                    on_vi(code, vals)
            elif evt_type == "0s":
                _wslog.info("0s 장운영 vals=%s", json.dumps(vals, ensure_ascii=False)[:200])
                if on_market_open:
                    on_market_open(vals)

    def on_ws_error(ws, error):
        _wslog.error("WebSocket 오류: %s", error)
        if on_error:
            on_error(error)

    def on_ws_close(ws, close_status_code, close_msg):
        _wslog.warning("WebSocket 끊김 (code=%s msg=%s) — 10초 후 재연결", close_status_code, close_msg)

    def run():
        while True:
            now = datetime.now()
            is_weekend = now.weekday() >= 5
            in_market = now.replace(hour=8, minute=0, second=0, microsecond=0) <= now <= now.replace(hour=16, minute=10, second=0, microsecond=0)
            if is_weekend or not in_market:
                wait = 600
                _wslog.info("장외/주말 — WebSocket 연결 생략, %d초 후 재확인", wait)
                threading.Event().wait(wait)
                continue
            try:
                fresh_token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
                _wslog.info("토큰 발급 완료, WebSocket 연결 시도...")
                ws = websocket.WebSocketApp(
                    ws_url,
                    header={"authorization": f"Bearer {fresh_token}"},
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_ws_error,
                    on_close=on_ws_close,
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                _wslog.error("WebSocket run_forever 예외: %s", e)
            threading.Event().wait(10)

    threading.Thread(target=run, daemon=True).start()
    return update_watched_codes


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"[시스템] 모의투자 서버 사용: {KIWOOM_IS_MOCK}")
    try:
        result = get_holdings()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"❌ 조회 실패: {e}")
