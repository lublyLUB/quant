"""주문 실행 메뉴용 로컬 전용 서버.

키움 실거래 키는 이 서버(내 PC) 안에서만 사용되고 브라우저로는 절대 전달되지 않습니다.
배포된(GitHub Pages) index.html에서는 이 서버에 접근할 수 없으므로 주문 실행 메뉴는
이 서버를 직접 실행한 로컬 환경에서만 동작합니다.

사용법: python order_server.py  (5050 포트로 실행, index.html을 열어둔 채로 켜두면 됩니다)
"""

import json
import os
import subprocess
import sys
import threading
from datetime import datetime

import requests

from flask import Flask, jsonify, request
from flask_cors import CORS

import kiwoom_api

app = Flask(__name__)
CORS(app)  # 로컬 전용 서버이므로 file:// 출처(브라우저)도 허용

# 성과추적 데이터 (기준점/스냅샷) - 이 PC 안에만 저장, 깃허브에는 올리지 않음
PERFORMANCE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "performance_history.json")
BENCHMARK_CODES = {"kospi": "069500", "kosdaq": "229200"}  # KODEX 200 / KODEX 코스닥150 (지수 추종 ETF로 근사)


def _send_telegram(text: str):
    """텔레그램 봇으로 메시지 전송. config_local.py에 토큰/채팅ID 없으면 조용히 무시."""
    token = getattr(kiwoom_api, "_cfg", None) and None  # 아래에서 직접 import
    try:
        from config_local import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception:
        pass


DATA_JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.js")
ALERT_TOP_N = 50  # 이 순위 밖으로 밀리면 알림


def _load_strategy_ranks() -> dict:
    """data.js에서 전략별 종목 순위를 파싱. {종목명: {전략키: rank}} 반환."""
    import re
    if not os.path.exists(DATA_JS_PATH):
        return {}
    with open(DATA_JS_PATH, encoding="utf-8") as f:
        content = f.read()
    # 전략 블록 파싱: "key": [ {rank, name, ...}, ... ]
    strategy_pattern = re.compile(r'"([a-z_0-9]+)":\s*\[([^\]]*)\]', re.DOTALL)
    item_pattern = re.compile(r'"rank"\s*:\s*(\d+)[^}]*"name"\s*:\s*"([^"]+)"', re.DOTALL)
    result = {}
    for m in strategy_pattern.finditer(content):
        key, block = m.group(1), m.group(2)
        for item in item_pattern.finditer(block):
            rank, name = int(item.group(1)), item.group(2)
            result.setdefault(name, {})[key] = rank
    return result


def _check_portfolio_ranks() -> str:
    """포트폴리오 종목의 전략별 순위를 체크해 텔레그램 메시지 생성."""
    history = _load_performance()
    if not history.get("baselines"):
        return "📋 기준점 없음 - 포트폴리오를 먼저 설정하세요."

    portfolio = [s["name"] for s in history["baselines"][-1].get("stocks", [])]
    if not portfolio:
        return "📋 포트폴리오 종목이 없습니다."

    ranks = _load_strategy_ranks()
    strategy_count = len(set(k for v in ranks.values() for k in v))

    lines_warn, lines_ok = [], []
    for name in portfolio:
        stock_ranks = ranks.get(name, {})
        if not stock_ranks:
            lines_warn.append(f"⚠️ {name}: 전략 데이터 없음")
            continue
        # 보르다 평균 순위
        avg_rank = sum(stock_ranks.values()) / len(stock_ranks)
        matched = len(stock_ranks)
        if avg_rank > ALERT_TOP_N or matched <= 1:
            lines_warn.append(f"🔴 {name}: 평균순위 {avg_rank:.0f}위 ({matched}/{strategy_count}전략)")
        else:
            lines_ok.append(f"✅ {name}: 평균순위 {avg_rank:.0f}위 ({matched}전략)")

    now_str = datetime.now().strftime("%m/%d %H:%M")
    header = f"📊 <b>포트폴리오 순위 점검</b> ({now_str})  {len(portfolio)}종목\n"
    if lines_warn:
        return header + "\n".join(lines_warn)
    return header + "✅ 전 종목 TOP50 이내 이상 없음"


def _load_performance():
    if os.path.exists(PERFORMANCE_FILE):
        with open(PERFORMANCE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"baselines": []}


def _save_performance(data):
    with open(PERFORMANCE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# 가격(KRX 시세) 갱신 작업 상태. DART는 캐시를 그대로 쓰므로 보통 수 분 내 끝남.
update_state = {"running": False, "log": "", "done": False, "success": None}

# VI 발동 종목 실시간 캐시 — 1h WebSocket으로 갱신
_vi_active: set = set()  # 현재 VI 발동 중인 종목코드 집합


def _run_update_data():
    update_state.update(running=True, log="", done=False, success=None)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "update_data.py", "--no-deploy"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        for line in proc.stdout:
            update_state["log"] += line
        proc.wait()
        update_state["success"] = (proc.returncode == 0)
    except Exception as e:
        update_state["log"] += f"\n오류: {e}"
        update_state["success"] = False
    finally:
        update_state["running"] = False
        update_state["done"] = True


@app.route("/api/holdings", methods=["GET"])
def holdings():
    try:
        return jsonify(kiwoom_api.get_holdings())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stock_warnings", methods=["GET"])
def stock_warnings():
    """ka10100 - 매수 대상 종목의 투자유의 여부 확인"""
    codes = [c for c in (request.args.get("codes") or "").split(",") if c]
    if not codes:
        return jsonify({"error": "codes 파라미터가 필요합니다."}), 400
    try:
        return jsonify(kiwoom_api.check_stock_warning(codes))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/realized_pl", methods=["GET"])
def realized_pl():
    """ka10072 - 종목별 실현손익·실제 수수료·세금 조회"""
    code = request.args.get("code", "")
    from_dt = request.args.get("from", "")
    if not code:
        return jsonify({"error": "code 파라미터가 필요합니다."}), 400
    try:
        return jsonify(kiwoom_api.get_realized_pl(code, from_dt))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/daily_balance", methods=["GET"])
def daily_balance():
    """ka01690 - 일별잔고수익률. date 파라미터 없으면 당일."""
    qry_dt = request.args.get("date", "")
    try:
        return jsonify(kiwoom_api.get_daily_balance(qry_dt))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/order_possible_qty", methods=["GET"])
def order_possible_qty():
    """kt00010 - 종목/가격 기준 주문가능수량 (수수료 포함 100% 증거금 기준)."""
    stk_cd = request.args.get("stk_cd", "")
    price  = int(request.args.get("price", "0") or 0)
    trde_tp = request.args.get("trde_tp", "2")
    if not stk_cd or not price:
        return jsonify({"error": "stk_cd, price 필수"}), 400
    try:
        return jsonify(kiwoom_api.get_order_possible_qty(stk_cd, price, trde_tp))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/daily_contract", methods=["GET"])
def daily_contract():
    """kt00009 - 당일 매도/매수/전체 약정금액 합계."""
    try:
        return jsonify(kiwoom_api.get_daily_contract_summary())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/transactions", methods=["GET"])
def transactions():
    """kt00015 - 위탁종합거래내역 (기간/구분 조회)."""
    strt_dt = request.args.get("strt_dt", "")
    end_dt  = request.args.get("end_dt", "")
    tp      = request.args.get("tp", "0")
    stk_cd  = request.args.get("stk_cd", "")
    if not strt_dt or not end_dt:
        return jsonify({"error": "strt_dt, end_dt 필수"}), 400
    try:
        rows = kiwoom_api.get_transaction_history(strt_dt, end_dt, tp, stk_cd)
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/net_deposit", methods=["GET"])
def net_deposit():
    """kt00015 기반 기간 순입금(입금-출금) + 세금/수수료 합계."""
    strt_dt = request.args.get("strt_dt", "")
    end_dt  = request.args.get("end_dt", "")
    if not strt_dt or not end_dt:
        return jsonify({"error": "strt_dt, end_dt 필수"}), 400
    try:
        return jsonify(kiwoom_api.get_net_deposit(strt_dt, end_dt))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/today_deposit", methods=["GET"])
def today_deposit():
    """kt00017 - 당일현황 (입금/출금/매도/매수/수수료/세금/배당금)."""
    try:
        return jsonify(kiwoom_api.get_today_deposit())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/next_day_settlement", methods=["GET"])
def next_day_settlement():
    """kt00008 - 익일 결제 예정 내역 (매도/매수 정산합, 종목별 정산금액/수수료/세금)."""
    try:
        return jsonify(kiwoom_api.get_next_day_settlement())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/account_eval", methods=["GET"])
def account_eval():
    """kt00004 - 계좌평가현황 (당일/당월/누적 손익 + 종목별 금일 매수/매도 수량)."""
    try:
        return jsonify(kiwoom_api.get_account_eval())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/deposit", methods=["GET"])
def deposit_detail():
    """kt00001 - 예수금상세현황 (예수금/출금가능/주문가능/D+1~D+2 추정예수금)."""
    qry_tp = request.args.get("qry_tp", "2")
    try:
        return jsonify(kiwoom_api.get_deposit_detail(qry_tp))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settlement_balance", methods=["GET"])
def settlement_balance():
    """kt00005 - 주문가능현금(ord_alowa) + 종목별 결제잔고(setl_remn) 조회"""
    try:
        return jsonify(kiwoom_api.get_settlement_balance())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/order_status", methods=["GET"])
def order_status():
    qry_tp = request.args.get("qry_tp", "1")
    try:
        return jsonify(kiwoom_api.get_order_status(qry_tp))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/open_orders", methods=["GET"])
def open_orders():
    """kt00007 - 미체결 주문만 직접 조회 (qry_tp=3)"""
    sell_tp = request.args.get("sell_tp", "0")
    try:
        return jsonify(kiwoom_api.get_open_orders(sell_tp))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/fills", methods=["GET"])
def fills():
    sell_tp = request.args.get("sell_tp", "0")
    try:
        return jsonify(kiwoom_api.get_fills(sell_tp))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/quotes", methods=["GET"])
def quotes():
    codes = [c for c in (request.args.get("codes") or "").split(",") if c]
    if not codes:
        return jsonify({"error": "codes 파라미터가 필요합니다."}), 400
    try:
        return jsonify({"prices": kiwoom_api.get_stock_quotes(codes)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vi_stocks", methods=["GET"])
def vi_stocks():
    """ka10054 - 현재 VI 발동 중인 종목 목록 + 실시간 캐시."""
    try:
        data = kiwoom_api.get_vi_stocks()
        # 실시간 캐시도 병합
        data["realtime_codes"] = list(_vi_active)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e), "realtime_codes": list(_vi_active)}), 500


@app.route("/api/daily_realized_pl", methods=["GET"])
def daily_realized_pl():
    """ka10074 - 일자별 실현손익 (당일 또는 date 파라미터 지정일)."""
    qry_dt = request.args.get("date", "")
    try:
        return jsonify(kiwoom_api.get_daily_realized_pl(qry_dt))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/today_journal", methods=["GET"])
def today_journal():
    """ka10170 - 당일매매일지 (오늘 전체 매수/매도 체결 내역)."""
    try:
        return jsonify(kiwoom_api.get_today_trade_journal())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/order", methods=["POST"])
def order():
    data = request.get_json(force=True) or {}
    stk_cd = data.get("stk_cd")
    qty = data.get("qty")
    side = data.get("side")
    price = data.get("price")
    if not stk_cd or not qty or side not in ("buy", "sell"):
        return jsonify({"error": "stk_cd, qty, side(buy/sell)는 필수입니다."}), 400
    # VI 발동 종목 매수 차단
    if side == "buy" and stk_cd in _vi_active:
        return jsonify({"error": f"VI 발동 중인 종목({stk_cd})은 매수할 수 없습니다. VI 해제 후 재시도하세요."}), 400
    try:
        result = kiwoom_api.place_order(stk_cd, qty, side, price)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/modify_order", methods=["POST"])
def modify_order():
    data = request.get_json(force=True) or {}
    stk_cd = data.get("stk_cd")
    orig_ord_no = data.get("orig_ord_no")
    mdfy_qty = data.get("mdfy_qty")
    mdfy_uv = data.get("mdfy_uv")
    if not stk_cd or not orig_ord_no or not mdfy_qty or not mdfy_uv:
        return jsonify({"error": "stk_cd, orig_ord_no, mdfy_qty, mdfy_uv는 필수입니다."}), 400
    try:
        result = kiwoom_api.modify_order(stk_cd, orig_ord_no, mdfy_qty, mdfy_uv)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cancel_order", methods=["POST"])
def cancel_order():
    data = request.get_json(force=True) or {}
    stk_cd = data.get("stk_cd")
    orig_ord_no = data.get("orig_ord_no")
    cncl_qty = data.get("cncl_qty")
    if not stk_cd or not orig_ord_no or not cncl_qty:
        return jsonify({"error": "stk_cd, orig_ord_no, cncl_qty는 필수입니다."}), 400
    try:
        result = kiwoom_api.cancel_order(stk_cd, orig_ord_no, cncl_qty)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/update_data", methods=["POST"])
def update_data():
    if update_state["running"]:
        return jsonify({"error": "이미 갱신이 진행 중입니다."}), 409
    threading.Thread(target=_run_update_data, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/update_status", methods=["GET"])
def update_status():
    return jsonify(update_state)


@app.route("/api/performance/baseline", methods=["POST"])
def performance_baseline():
    """① 기준점 기록 - 매수/리밸런싱 시점의 종목별 매수가/수량/비중 + 벤치마크 ETF 가격을 저장."""
    data = request.get_json(force=True) or {}
    stocks = data.get("stocks") or []
    if not stocks:
        return jsonify({"error": "stocks가 비어있습니다."}), 400
    try:
        codes = [s["code"] for s in stocks] + list(BENCHMARK_CODES.values())
        quotes = kiwoom_api.get_stock_quotes(codes)

        history = _load_performance()
        prev_baseline = history["baselines"][-1] if history["baselines"] else None

        baseline_stocks = []
        for s in stocks:
            price = quotes.get(s["code"]) or s.get("price")
            baseline_stocks.append({
                "name": s["name"], "code": s["code"],
                "price": price, "qty": s["qty"], "weight": s.get("weight"),
                "strategyRanks": s.get("strategyRanks") or {},
            })

        new_baseline = {
            "id": datetime.now().strftime("%Y%m%d%H%M%S"),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "totalInvestment": data.get("totalInvestment"),
            "stocks": baseline_stocks,
            "benchmark": {k: quotes.get(v) for k, v in BENCHMARK_CODES.items()},
            "snapshots": [],
        }

        # ⑤ 직전 기준점과 비교 - 유지/제외된 종목과 그 시점까지의 수익률
        if prev_baseline:
            prev_codes = {s["code"]: s for s in prev_baseline["stocks"]}
            new_codes = {s["code"] for s in baseline_stocks}
            kept, removed = [], []
            for code, s in prev_codes.items():
                cur_price = quotes.get(code)
                ret = ((cur_price - s["price"]) / s["price"] * 100) if (cur_price and s.get("price")) else None
                entry = {"name": s["name"], "code": code, "entry_price": s["price"], "exit_price": cur_price, "return_pct": ret}
                (kept if code in new_codes else removed).append(entry)
            new_baseline["previousComparison"] = {
                "previousBaselineId": prev_baseline["id"],
                "kept": kept, "removed": removed,
                "added": [s for s in baseline_stocks if s["code"] not in prev_codes],
            }

        history["baselines"].append(new_baseline)
        _save_performance(history)
        return jsonify(new_baseline)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/performance/snapshot", methods=["POST"])
def performance_snapshot():
    """② 주기적 스냅샷 저장 - ka01690(일별잔고수익률)으로 브로커 기준 정확한 평가금액 사용."""
    history = _load_performance()
    if not history["baselines"]:
        return jsonify({"error": "기준점이 없습니다. 먼저 기준점을 기록해주세요."}), 400
    baseline = history["baselines"][-1]
    try:
        # ka01690으로 당일 브로커 기준 평가금액·종목별 현재가 조회
        daily = kiwoom_api.get_daily_balance()
        if not daily.get("error") and daily.get("tot_evlt_amt"):
            total_value = (int(daily.get("tot_evlt_amt") or 0) +
                           int(daily.get("dbst_bal") or 0))
            price_map = {
                (r.get("stk_cd") or "").replace("A", "").replace("*", ""): int(r.get("cur_prc") or 0)
                for r in (daily.get("day_bal_rt") or [])
            }
        else:
            # ka01690 실패 시 ka10001 폴백
            codes = [s["code"] for s in baseline["stocks"]]
            quotes_raw = kiwoom_api.get_stock_quotes(codes)
            price_map = {
                code: (v["price"] if isinstance(v, dict) else v)
                for code, v in quotes_raw.items() if v
            }
            total_value = sum(
                price_map.get(s["code"], 0) * s["qty"]
                for s in baseline["stocks"]
            )

        stock_values = []
        for s in baseline["stocks"]:
            price = price_map.get(s["code"])
            value = (price * s["qty"]) if price else None
            stock_values.append({"code": s["code"], "name": s["name"], "price": price, "value": value})

        # 벤치마크는 기존 방식 유지
        bench_quotes = kiwoom_api.get_stock_quotes(list(BENCHMARK_CODES.values()))
        bench_prices = {
            k: (v["price"] if isinstance(v, dict) else v)
            for k, v in bench_quotes.items() if v
        }

        snapshot = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "totalValue": total_value,
            "stocks": stock_values,
            "benchmark": {k: bench_prices.get(v) for k, v in BENCHMARK_CODES.items()},
        }
        baseline["snapshots"].append(snapshot)
        _save_performance(history)

        # 텔레그램 성과 알림
        base_value = baseline.get("totalInvestment") or total_value
        ret_pct = ((total_value - base_value) / base_value * 100) if base_value else 0
        stock_lines = "\n".join(
            f"  • {s['name']}: {s['price']:,}원" for s in stock_values if s.get("price")
        )
        _send_telegram(
            f"📊 <b>성과 스냅샷</b> ({snapshot['datetime']})\n"
            f"총평가금액: <b>{total_value:,}원</b>  ({ret_pct:+.2f}%)\n"
            f"KOSPI ETF: {bench_prices.get('kospi', '-'):,}원 / KOSDAQ ETF: {bench_prices.get('kosdaq', '-'):,}원\n\n"
            f"{stock_lines}"
        )
        return jsonify(snapshot)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/notify", methods=["POST"])
def notify():
    """브라우저에서 직접 텔레그램 메시지 전송 (주문 완료, 성과 알림 등)."""
    data = request.get_json(force=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "text가 비어있습니다."}), 400
    try:
        _send_telegram(text)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/realtime_balance", methods=["GET"])
def realtime_balance():
    """04 실시간 잔고 캐시 반환 — {종목코드: {필드코드: 값}}"""
    return jsonify(kiwoom_api.realtime_balance)


@app.route("/api/performance/history", methods=["GET"])
def performance_history():
    return jsonify(_load_performance())


@app.route("/api/performance/summary", methods=["GET"])
def performance_summary():
    """총평가금액 + 누계순입금 + 수익금액/수익률 JSON 반환."""
    try:
        today_str = datetime.now().strftime("%Y%m%d")
        history = _load_performance()
        base_dt = history.get("tracking_start_date", "")
        if not base_dt and history.get("baselines"):
            base_dt = history["baselines"][-1].get("date", "").replace("-", "")[:8]

        total_asset = 0
        net_in = 0
        evltv_prft = 0
        prft_rt = 0.0
        if base_dt:
            perf = kiwoom_api.get_period_eval(fr_dt=base_dt, to_dt=today_str)
            total_asset  = int(perf.get("tot_amt_to") or 0)
            base_asset   = int(perf.get("tot_amt_fr") or 0)   # 기준일 순자산
            invt_bsamt   = int(perf.get("invt_bsamt") or 0)   # 투자원금평잔
            tern_rt      = float(perf.get("tern_rt") or 0)    # 회전율
            evltv_prft   = int(perf.get("evltv_prft") or 0)
            prft_rt      = float(perf.get("prft_rt") or 0)
            net_in = int(perf.get("termin_tot_trns") or 0) - int(perf.get("termin_tot_pymn") or 0)
            try:
                today_dep = kiwoom_api.get_today_deposit()
                net_in += int(today_dep.get("ina_amt") or 0)
                net_in -= int(today_dep.get("outa") or 0)
            except Exception:
                pass

        return jsonify({
            "total_asset": total_asset,
            "base_asset":  base_asset,
            "invt_bsamt":  invt_bsamt,
            "tern_rt":     tern_rt,
            "net_in": net_in,
            "profit": evltv_prft,
            "profit_pct": prft_rt,
            "base_dt": base_dt,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _build_daily_summary(label: str) -> str:
    """kt00016(기간수익률) + ka01690(종목별 수익률)으로 텔레그램 요약 메시지 생성."""
    try:
        from datetime import timedelta
        today_str = datetime.now().strftime("%Y%m%d")

        # 총평가금액 (kt00018)
        holdings_data = kiwoom_api.get_holdings()
        total_asset = int(holdings_data.get("prsm_dpst_aset_amt") or 0)

        # 당일/당월/누적 손익 (kt00004)
        day_pl = day_rt = 0
        month_pl = month_rt = cum_pl = cum_rt = 0
        try:
            acnt = kiwoom_api.get_account_eval()
            day_pl   = int(acnt.get("tdy_lspft") or 0)
            day_rt   = float(acnt.get("tdy_lspft_rt") or 0)
            month_pl = int(acnt.get("lspft2") or 0)
            month_rt = float(acnt.get("lspft_ratio") or 0)
            cum_pl   = int(acnt.get("lspft") or 0)
            cum_rt   = float(acnt.get("lspft_rt") or 0)
        except Exception:
            # fallback: 수동 계산
            for s in holdings_data.get("acnt_evlt_remn_indv_tot", []):
                cur = int(s.get("cur_prc") or 0)
                pred = int(s.get("pred_close_pric") or 0)
                qty = int(s.get("rmnd_qty") or 0)
                day_pl += (cur - pred) * qty
            total_eval = int(holdings_data.get("tot_evlt_amt") or 0)
            day_rt = (day_pl / (total_eval - day_pl) * 100) if (total_eval - day_pl) > 0 else 0

        # 누계 수익률 (kt00016, 기준일~오늘)
        cum_str = ""
        history = _load_performance()
        base_dt = history.get("tracking_start_date", "")
        if not base_dt and history.get("baselines"):
            base_dt = history["baselines"][-1].get("date", "").replace("-", "")[:8]
        if base_dt:
            try:
                perf = kiwoom_api.get_period_eval(fr_dt=base_dt, to_dt=today_str)
                evltv_prft = int(perf.get("evltv_prft") or 0)
                prft_rt = float(perf.get("prft_rt") or 0)
                net_in = int(perf.get("termin_tot_trns") or 0) - int(perf.get("termin_tot_pymn") or 0)
                # 당일 입금은 kt00016에 아직 미반영 → kt00017로 보완
                tdy_sell = tdy_buy = tdy_cmsn = tdy_tax = tdy_dvida = 0
                try:
                    today_dep = kiwoom_api.get_today_deposit()
                    net_in += int(today_dep.get("ina_amt") or 0)
                    net_in -= int(today_dep.get("outa") or 0)
                    tdy_sell  = int(today_dep.get("sell_amt") or 0)
                    tdy_buy   = int(today_dep.get("buy_amt") or 0)
                    tdy_cmsn  = int(today_dep.get("cmsn") or 0)
                    tdy_tax   = int(today_dep.get("tax") or 0)
                    tdy_dvida = int(today_dep.get("dvida_amt") or 0)
                except Exception:
                    pass
                tdy_trade_str = ""
                if tdy_sell or tdy_buy:
                    tdy_trade_str = (
                        f"\n당일 매도: <b>{tdy_sell:,}원</b>  매수: <b>{tdy_buy:,}원</b>"
                        f"\n수수료: <b>{tdy_cmsn:,}원</b>  세금: <b>{tdy_tax:,}원</b>"
                    )
                dvida_str = f"\n💰 배당금: <b>{tdy_dvida:,}원</b>" if tdy_dvida > 0 else ""
                cum_str = (
                    f"\n누계 순입금: <b>{net_in:,}원</b>\n"
                    f"누계 수익률: <b>{evltv_prft:+,.0f}원</b> ({prft_rt:+.2f}%)"
                    f"{tdy_trade_str}"
                    f"{dvida_str}"
                )
            except Exception:
                pass

        # 종목별 수익률 최고/최저 (kt00018 prft_rt 재사용 — 별도 API 호출 불필요)
        stock_lines = ""
        indv = [s for s in holdings_data.get("acnt_evlt_remn_indv_tot", []) if s.get("prft_rt")]
        if indv:
            best  = max(indv, key=lambda s: float(s["prft_rt"]))
            worst = min(indv, key=lambda s: float(s["prft_rt"]))
            stock_lines = (
                f"\n📈 최고: {best['stk_nm']} ({float(best['prft_rt']):+.2f}%)"
                f"\n📉 최저: {worst['stk_nm']} ({float(worst['prft_rt']):+.2f}%)"
            )

        # 당일 약정금액 (kt00009)
        contract_str = ""
        try:
            ct = kiwoom_api.get_daily_contract_summary()
            if ct["total"] > 0:
                contract_str = (
                    f"\n당일 약정금액: <b>{ct['total']:,}원</b>"
                    f" (매도 {ct['sell_amt']:,} / 매수 {ct['buy_amt']:,})"
                )
        except Exception:
            pass

        # 익일 결제 예정 (kt00008)
        setl_str = ""
        try:
            setl = kiwoom_api.get_next_day_settlement()
            sell_sum = int(setl.get("sell_amt_sum") or 0)
            buy_sum  = int(setl.get("buy_amt_sum") or 0)
            rows = setl.get("acnt_nxdy_setl_frcs_prps_array") or []
            tot_cmsn = sum(int(r.get("cmsn") or 0) + int(r.get("trde_tax") or 0)
                           + int(r.get("incm_tax") or 0) + int(r.get("rstx") or 0)
                           + int(r.get("resi_tax") or 0) for r in rows)
            if sell_sum or buy_sum:
                setl_str = (
                    f"\n📅 익일 결제 예정"
                    f"\n매도 정산: <b>{sell_sum:,}원</b>  매수 정산: <b>{buy_sum:,}원</b>"
                    f"\n거래비용 합계: <b>{tot_cmsn:,}원</b>"
                )
                for r in rows:
                    nm   = r.get("stk_nm", "")
                    tp   = "매도" if r.get("sell_tp", "").startswith("1") else "매수"
                    exct = int(r.get("exct_amt") or 0)
                    setl_str += f"\n  {nm} {tp} {exct:+,}원"
        except Exception:
            pass

        # 예수금 상세 (kt00001)
        dep_str = ""
        try:
            dep = kiwoom_api.get_deposit_detail()
            entr       = int(dep.get("entr") or 0)
            pymn_alow  = int(dep.get("pymn_alow_amt") or 0)
            ord_alow   = int(dep.get("ord_alow_amt") or 0)
            d1_entr    = int(dep.get("d1_entra") or 0)
            d2_entr    = int(dep.get("d2_entra") or 0)
            dep_str = (
                f"\n예수금: <b>{entr:,}원</b>  출금가능: <b>{pymn_alow:,}원</b>"
                f"\n주문가능: <b>{ord_alow:,}원</b>"
                f"\nD+1 추정예수금: <b>{d1_entr:,}원</b>  D+2: <b>{d2_entr:,}원</b>"
            )
        except Exception:
            pass

        month_cum_str = ""
        if month_pl or cum_pl:
            month_cum_str = (
                f"\n당월 손익: <b>{month_pl:+,}원</b> ({month_rt:+.2f}%)"
                f"\n누적 손익: <b>{cum_pl:+,}원</b> ({cum_rt:+.2f}%)"
            )

        # 실현손익 (ka10074) - 15:30 장마감 시에만 포함
        rlzt_str = ""
        if label == "장 마감":
            try:
                rlzt = kiwoom_api.get_daily_realized_pl()
                rlzt_rows = rlzt.get("daly_rlzt_pl_prps_array") or []
                rlzt_sum = int(rlzt.get("sum_sel_pl") or 0)
                rlzt_cmsn = int(rlzt.get("sum_cmsn") or 0)
                if rlzt_rows or rlzt_sum:
                    rlzt_lines = "\n".join(
                        f"  {r.get('stk_nm','')}: {int(r.get('sel_pl') or 0):+,}원 ({float(r.get('sel_pl_rt') or 0):+.2f}%)"
                        for r in rlzt_rows[:5]
                    )
                    rlzt_str = (
                        f"\n💹 실현손익 합계: <b>{rlzt_sum:+,}원</b>  수수료: {rlzt_cmsn:,}원"
                        + (f"\n{rlzt_lines}" if rlzt_lines else "")
                    )
            except Exception:
                pass

        return (
            f"📈 <b>[{label}] 잔고 요약</b> ({datetime.now().strftime('%m/%d %H:%M')})\n"
            f"총평가금액: <b>{total_asset:,}원</b>\n"
            f"당일 손익: <b>{day_pl:+,.0f}원</b> ({day_rt:+.2f}%)"
            f"{month_cum_str}"
            f"{cum_str}"
            f"{contract_str}"
            f"{setl_str}"
            f"{rlzt_str}"
            f"{dep_str}"
            f"{stock_lines}"
        )
    except Exception as e:
        return f"⚠️ 잔고 요약 조회 실패: {e}"


def _build_performance_summary() -> str:
    """텔레그램 봇 응답용: 총평가금액 + 누계 수익률."""
    try:
        today_str = datetime.now().strftime("%Y%m%d")
        history = _load_performance()
        base_dt = history.get("tracking_start_date", "")
        if not base_dt and history.get("baselines"):
            base_dt = history["baselines"][-1].get("date", "").replace("-", "")[:8]
        if not base_dt:
            return "⚠️ tracking_start_date 미설정"
        perf = kiwoom_api.get_period_eval(fr_dt=base_dt, to_dt=today_str)
        total_asset = int(perf.get("tot_amt_to") or 0)
        base_asset  = int(perf.get("tot_amt_fr") or 0)
        invt_bsamt  = int(perf.get("invt_bsamt") or 0)
        tern_rt     = float(perf.get("tern_rt") or 0)
        evltv_prft  = int(perf.get("evltv_prft") or 0)
        prft_rt     = float(perf.get("prft_rt") or 0)
        net_in = int(perf.get("termin_tot_trns") or 0) - int(perf.get("termin_tot_pymn") or 0)
        try:
            today_dep = kiwoom_api.get_today_deposit()
            net_in += int(today_dep.get("ina_amt") or 0)
            net_in -= int(today_dep.get("outa") or 0)
        except Exception:
            pass
        base_label = base_dt[:4] + "-" + base_dt[4:6] + "-" + base_dt[6:]
        dep_str = ""
        try:
            dep = kiwoom_api.get_deposit_detail()
            ord_alow  = int(dep.get("ord_alow_amt") or 0)
            d1_entr   = int(dep.get("d1_entra") or 0)
            dep_str = f"\n주문가능: <b>{ord_alow:,}원</b>  D+1 예수금: <b>{d1_entr:,}원</b>"
        except Exception:
            pass
        return (
            f"💰 <b>성과 요약</b> ({datetime.now().strftime('%m/%d %H:%M')})\n"
            f"기준일 자산: <b>{base_asset:,}원</b> ({base_label})\n"
            f"현재 총평가: <b>{total_asset:,}원</b>\n"
            f"총순입금: <b>{net_in:,}원</b>\n"
            f"수익금액: <b>{evltv_prft:+,}원</b> ({prft_rt:+.2f}%)\n"
            f"투자원금평잔: <b>{invt_bsamt:,}원</b>  회전율: <b>{tern_rt:.2f}%</b>"
            f"{dep_str}"
        )
    except Exception as e:
        return f"⚠️ 성과 조회 실패: {e}"


def _build_holdings_summary() -> str:
    """텔레그램 봇 응답용: 보유 종목 목록."""
    try:
        h = kiwoom_api.get_holdings()
        stocks = h.get("acnt_evlt_remn_indv_tot", [])
        if not stocks:
            return "보유 종목이 없습니다."
        lines = [f"📋 <b>보유 종목</b> ({datetime.now().strftime('%m/%d %H:%M')})"]
        for s in stocks:
            name = s.get("stk_nm", "")
            qty = int(s.get("rmnd_qty") or 0)
            cur = int(s.get("cur_prc") or 0)
            pred = int(s.get("pred_close_pric") or 0)
            day_rt = ((cur - pred) / pred * 100) if pred > 0 else 0
            sign = "+" if day_rt >= 0 else ""
            lines.append(f"  {name}: {qty}주 {cur:,}원 ({sign}{day_rt:.2f}%)")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ 보유종목 조회 실패: {e}"


def _handle_telegram_command(text: str) -> str | None:
    """봇 수신 메시지를 파싱해 응답 문자열 반환. 인식 못하면 None."""
    from datetime import timedelta
    t = text.strip()
    if t in ("잔고", "수익률", "성과", "요약"):
        return _build_performance_summary()
    if t in ("보유", "보유종목", "종목"):
        return _build_holdings_summary()
    if t in ("거래내역", "입출금", "거래"):
        try:
            today = datetime.now()
            strt  = (today - timedelta(days=30)).strftime("%Y%m%d")
            end   = today.strftime("%Y%m%d")
            nd = kiwoom_api.get_net_deposit(strt, end)
            return (
                f"💳 <b>최근 30일 거래 요약</b>\n"
                f"입금: <b>{nd['in_amt']:,}원</b>\n"
                f"출금: <b>{nd['out_amt']:,}원</b>\n"
                f"순입금: <b>{nd['net']:+,}원</b>\n"
                f"수수료/세금: <b>{nd['tax_cmsn']:,}원</b>"
            )
        except Exception as e:
            return f"⚠️ 거래내역 조회 실패: {e}"
    if t in ("실현손익", "매도손익"):
        try:
            data = kiwoom_api.get_daily_realized_pl()
            rows = data.get("daly_rlzt_pl_prps_array") or []
            total_pl = int(data.get("sum_sel_pl") or 0)
            total_cmsn = int(data.get("sum_cmsn") or 0)
            if not rows and total_pl == 0:
                return "📊 오늘 매도 실현손익이 없습니다."
            lines = [f"💹 <b>당일 실현손익</b> ({datetime.now().strftime('%m/%d')})\n합계: <b>{total_pl:+,}원</b>  수수료: {total_cmsn:,}원"]
            for r in rows[:10]:
                nm  = r.get("stk_nm", "")
                pl  = int(r.get("sel_pl") or 0)
                prt = float(r.get("sel_pl_rt") or 0)
                lines.append(f"  {nm}: {pl:+,}원 ({prt:+.2f}%)")
            return "\n".join(lines)
        except Exception as e:
            return f"⚠️ 실현손익 조회 실패: {e}"
    if t in ("매매일지", "일지", "오늘거래"):
        try:
            data = kiwoom_api.get_today_trade_journal()
            rows = data.get("tdy_trde_jrnl_array") or []
            if not rows:
                return "📋 오늘 매매 내역이 없습니다."
            lines = [f"📋 <b>당일매매일지</b> ({datetime.now().strftime('%m/%d')})  {len(rows)}건"]
            for r in rows[:15]:
                tm  = r.get("cntr_tm", "")
                nm  = r.get("stk_nm", "")
                tp  = r.get("sell_tp_nm", "")
                qty = r.get("cntr_qty", "")
                prc = int(r.get("cntr_pric") or 0)
                pl  = int(r.get("rlzt_pl") or 0)
                pl_str = f" | 손익: {pl:+,}" if pl else ""
                t_fmt = f"{tm[:2]}:{tm[2:4]}" if len(tm) >= 4 else tm
                lines.append(f"  {t_fmt} {tp} {nm} {qty}주@{prc:,}{pl_str}")
            return "\n".join(lines)
        except Exception as e:
            return f"⚠️ 매매일지 조회 실패: {e}"
    if t in ("VI", "vi발동"):
        try:
            data = kiwoom_api.get_vi_stocks()
            items = data.get("items", [])
            rt = list(_vi_active)
            if not items and not rt:
                return "✅ 현재 VI 발동 종목 없음"
            lines = [f"⚡ <b>VI 발동 종목</b> ({datetime.now().strftime('%H:%M')})"]
            for it in items:
                lines.append(f"  {it['stk_nm']}({it['stk_cd']}) {it['vi_gubun']} @{it['vi_pric']}")
            if rt:
                lines.append(f"실시간 발동 코드: {', '.join(rt)}")
            return "\n".join(lines)
        except Exception as e:
            return f"⚠️ VI 조회 실패: {e}"
    if t in ("도움말", "help", "?"):
        return (
            "📌 <b>사용 가능한 명령어</b>\n"
            "잔고 / 수익률 / 성과 → 성과 요약\n"
            "보유 / 보유종목 → 보유 종목 현황\n"
            "거래내역 / 입출금 → 최근 30일 입출금 및 수수료\n"
            "실현손익 / 매도손익 → 당일 실현손익\n"
            "매매일지 / 일지 → 당일 전체 체결 내역\n"
            "VI / vi발동 → VI 발동 종목 조회\n"
            "도움말 → 이 안내"
        )
    return None


def _telegram_polling():
    """텔레그램 getUpdates 롱폴링으로 봇 메시지 수신 및 응답."""
    try:
        from config_local import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    except ImportError:
        return
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            resp = requests.get(f"{base_url}/getUpdates", params=params, timeout=40)
            updates = resp.json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                # 등록된 chat_id에서 온 메시지만 처리
                if str(msg.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
                    continue
                text = msg.get("text", "")
                reply = _handle_telegram_command(text)
                if reply:
                    requests.post(f"{base_url}/sendMessage", json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": reply,
                        "parse_mode": "HTML",
                    }, timeout=10)
        except Exception:
            threading.Event().wait(5)


def _build_period_asset_summary(label: str, start_dt: str, end_dt: str) -> str:
    """kt00002로 기간 자산 변화 요약 메시지 생성."""
    try:
        rows = kiwoom_api.get_daily_asset_history(start_dt, end_dt)
        if not rows:
            return f"⚠️ [{label}] 기간 자산 데이터 없음"
        rows_sorted = sorted(rows, key=lambda r: r.get("dt", ""))
        first = rows_sorted[0]
        last  = rows_sorted[-1]
        start_amt = int(first.get("prsm_dpst_aset_amt") or 0)
        end_amt   = int(last.get("prsm_dpst_aset_amt") or 0)
        change    = end_amt - start_amt
        change_rt = (change / start_amt * 100) if start_amt > 0 else 0
        color_sign = "📈" if change >= 0 else "📉"
        # 기간 내 최고/최저
        amts = [int(r.get("prsm_dpst_aset_amt") or 0) for r in rows_sorted if r.get("prsm_dpst_aset_amt")]
        high_amt = max(amts) if amts else 0
        low_amt  = min(amts) if amts else 0
        # 기간 순입금/수수료 (kt00015)
        nd_str = ""
        try:
            nd = kiwoom_api.get_net_deposit(start_dt, end_dt)
            nd_str = (
                f"\n순입금: <b>{nd['net']:+,}원</b>"
                f" (입금 {nd['in_amt']:,} / 출금 {nd['out_amt']:,})"
                f"\n수수료/세금: <b>{nd['tax_cmsn']:,}원</b>"
            )
        except Exception:
            pass

        return (
            f"{color_sign} <b>[{label} 요약]</b> ({first['dt'][:4]}-{first['dt'][4:6]}-{first['dt'][6:]} ~ "
            f"{last['dt'][:4]}-{last['dt'][4:6]}-{last['dt'][6:]})\n"
            f"시작 자산: <b>{start_amt:,}원</b>\n"
            f"종료 자산: <b>{end_amt:,}원</b>\n"
            f"변화: <b>{change:+,}원</b> ({change_rt:+.2f}%)\n"
            f"기간 최고: {high_amt:,}원 / 최저: {low_amt:,}원"
            f"{nd_str}"
        )
    except Exception as e:
        return f"⚠️ [{label}] 자산 요약 실패: {e}"


def _daily_alert_scheduler():
    """평일 08:00/09:00/12:00/15:30 일별 요약, 월요일 주간 요약, 매월 1일 월간 요약 전송."""
    from datetime import timedelta
    ALERT_TIMES = [("08:00", "개장 준비"), ("09:00", "장 개장"), ("12:00", "장 중간"), ("15:30", "장 마감")]
    sent_today = set()
    while True:
        now = datetime.now()
        key = now.strftime("%Y%m%d")

        if now.weekday() < 5:  # 평일
            for t, label in ALERT_TIMES:
                slot = f"{key}_{t}"
                if slot not in sent_today and now.strftime("%H:%M") == t:
                    msg = _build_daily_summary(label)
                    _send_telegram(msg)
                    if t == "08:00":
                        _send_telegram(_check_portfolio_ranks())
                        # 배당금 수령 여부 확인 (kt00017 dvida_amt)
                        try:
                            dep = kiwoom_api.get_today_deposit()
                            dvida = int(dep.get("dvida_amt") or 0)
                            if dvida > 0:
                                _send_telegram(f"💰 <b>배당금 수령</b>\n금액: <b>{dvida:,}원</b>")
                        except Exception:
                            pass
                        # 월요일 08:00 → 주간 요약 (직전 월~금)
                        if now.weekday() == 0:
                            prev_mon = (now - timedelta(days=7)).strftime("%Y%m%d")
                            prev_fri = (now - timedelta(days=3)).strftime("%Y%m%d")
                            _send_telegram(_build_period_asset_summary("주간", prev_mon, prev_fri))
                        # 매월 1일 08:00 → 월간 요약 (전월 1일~말일)
                        if now.day == 1:
                            first_this = now.replace(day=1)
                            last_prev  = first_this - timedelta(days=1)
                            first_prev = last_prev.replace(day=1)
                            _send_telegram(_build_period_asset_summary(
                                "월간",
                                first_prev.strftime("%Y%m%d"),
                                last_prev.strftime("%Y%m%d"),
                            ))
                    sent_today.add(slot)

        if now.hour == 0 and now.minute == 0:
            sent_today.clear()
        threading.Event().wait(30)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    threading.Thread(target=_daily_alert_scheduler, daemon=True).start()
    threading.Thread(target=_telegram_polling, daemon=True).start()

    def _on_fill(vals):
        side  = "매수" if vals.get("907") == "2" else "매도"
        name  = vals.get("302", "")
        qty   = vals.get("911", "")
        price = vals.get("910", "")
        total = vals.get("903", "")
        t     = vals.get("908", "")
        # 잔고(04) 캐시에서 당일실현손익 보완
        code = (vals.get("9001") or "").lstrip("A")
        bal  = kiwoom_api.realtime_balance.get(code, {})
        day_pl = bal.get("990", "")
        try:
            t_fmt     = f"{t[:2]}:{t[2:4]}:{t[4:]}" if len(t) >= 6 else t
            price_fmt = f"{int(price):,}" if price else price
            total_fmt = f"{int(total):,}" if total else total
            pl_line   = f"\n당일실현손익: <b>{int(day_pl):+,}원</b>" if day_pl else ""
        except Exception:
            t_fmt, price_fmt, total_fmt, pl_line = t, price, total, ""
        _send_telegram(
            f"✅ <b>주문 체결</b> ({t_fmt})\n"
            f"{side} | {name}\n"
            f"체결가: <b>{price_fmt}원</b> × {qty}주\n"
            f"누계금액: {total_fmt}원"
            f"{pl_line}"
        )

    def _on_stock_info(code, vals):
        info = vals.get("370", "")
        if not info:
            return
        _send_telegram(
            f"⚠️ <b>종목 조치 알림</b>\n"
            f"종목코드: {code}\n"
            f"내용: {info}"
        )

    def _on_vi(code, vals):
        """1h: VI 발동/해제 실시간 이벤트 — 보유 종목만 텔레그램 알림."""
        vi_type = str(vals.get("215") or "")
        stk_nm  = vals.get("302") or code
        if vi_type == "1":
            _vi_active.add(code)
            _send_telegram(
                f"⚡ <b>VI 발동</b>\n"
                f"종목: {stk_nm} ({code})\n"
                f"⛔ 해당 종목 매수가 일시 차단됩니다."
            )
        elif vi_type == "2":
            _vi_active.discard(code)
            _send_telegram(f"✅ <b>VI 해제</b>\n종목: {stk_nm} ({code})")

    # 장운영구분 코드: 2=정규장 개시, 3=정규장 마감, 4=시간외단일가, 9=전체종료
    _MARKET_LABELS = {"2": "🔔 정규장 개장", "3": "🔔 정규장 마감", "4": "🔔 시간외단일가 시작", "9": "🔔 전체 시장 종료"}

    def _on_market_open(vals):
        """0s: 장운영 이벤트 — 개장/마감 텔레그램 알림."""
        op_tp = str(vals.get("215") or "")
        tm    = vals.get("20", "")
        label = _MARKET_LABELS.get(op_tp)
        if label:
            t_fmt = f"{tm[:2]}:{tm[2:4]}:{tm[4:]}" if len(tm) >= 6 else tm
            _send_telegram(f"{label} ({t_fmt})")

    _update_watched = kiwoom_api.start_order_realtime(
        on_fill=_on_fill,
        on_stock_info=_on_stock_info,
        on_vi=_on_vi,
        on_market_open=_on_market_open,
    )
    print(f"[시스템] 주문 서버 시작 (모의투자: {kiwoom_api.KIWOOM_IS_MOCK}) - http://localhost:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)
