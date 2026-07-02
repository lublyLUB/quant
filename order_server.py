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


@app.route("/api/order", methods=["POST"])
def order():
    data = request.get_json(force=True) or {}
    stk_cd = data.get("stk_cd")
    qty = data.get("qty")
    side = data.get("side")
    price = data.get("price")
    if not stk_cd or not qty or side not in ("buy", "sell"):
        return jsonify({"error": "stk_cd, qty, side(buy/sell)는 필수입니다."}), 400
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


@app.route("/api/performance/history", methods=["GET"])
def performance_history():
    return jsonify(_load_performance())


def _build_daily_summary(label: str) -> str:
    """일별잔고(ka01690) + 성과추적 기준점으로 텔레그램 요약 메시지 생성."""
    try:
        today = kiwoom_api.get_daily_balance()
        total_asset = int(today.get("day_stk_asst") or 0)
        total_eval = int(today.get("tot_evlt_amt") or 0)
        accum_pl = int(today.get("tot_evltv_prft") or 0)
        accum_rt = float(today.get("tot_prft_rt") or 0)

        # 당일 손익: 어제 자산과 비교
        from datetime import timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        ydata = kiwoom_api.get_daily_balance(yesterday)
        yesterday_asset = int(ydata.get("day_stk_asst") or 0)
        day_pl = total_asset - yesterday_asset if yesterday_asset else 0
        day_rt = (day_pl / yesterday_asset * 100) if yesterday_asset else 0

        # 기준점 대비 손익
        history = _load_performance()
        baseline_pl_str = ""
        if history.get("baselines"):
            baseline = history["baselines"][-1]
            base_invest = baseline.get("totalInvestment") or 0
            if base_invest:
                base_pl = total_asset - base_invest
                base_rt = base_pl / base_invest * 100
                baseline_pl_str = f"\n📌 기준점 대비: <b>{base_pl:+,.0f}원</b> ({base_rt:+.2f}%)"

        # 기준점 대비 손익
        baseline_pl_str = ""
        if history.get("baselines"):
            baseline = history["baselines"][-1]
            base_invest = baseline.get("totalInvestment") or 0
            if base_invest:
                base_pl = total_asset - base_invest
                base_rt = base_pl / base_invest * 100
                baseline_pl_str = f"\n📌 기준점 대비: <b>{base_pl:+,.0f}원</b> ({base_rt:+.2f}%)"

        # 종목별 수익률 최고/최저
        stock_lines = ""
        holdings = [s for s in today.get("day_bal_rt", []) if s.get("prft_rt")]
        if holdings:
            best = max(holdings, key=lambda s: float(s["prft_rt"]))
            worst = min(holdings, key=lambda s: float(s["prft_rt"]))
            stock_lines = (
                f"\n📈 최고: {best['stk_nm']} ({float(best['prft_rt']):+.2f}%)"
                f"\n📉 최저: {worst['stk_nm']} ({float(worst['prft_rt']):+.2f}%)"
            )

        return (
            f"📈 <b>[{label}] 잔고 요약</b> ({datetime.now().strftime('%m/%d %H:%M')})\n"
            f"총평가금액: <b>{total_asset:,}원</b>\n"
            f"당일 손익: <b>{day_pl:+,.0f}원</b> ({day_rt:+.2f}%)\n"
            f"누적 손익: {accum_pl:+,.0f}원 ({accum_rt:+.2f}%)"
            f"{baseline_pl_str}"
            f"{stock_lines}"
        )
    except Exception as e:
        return f"⚠️ 잔고 요약 조회 실패: {e}"


def _daily_alert_scheduler():
    """평일 09:00 / 12:00 / 15:30 에 잔고 요약을 텔레그램으로 전송."""
    ALERT_TIMES = [("08:00", "개장 준비"), ("09:00", "장 개장"), ("12:00", "장 중간"), ("15:30", "장 마감")]
    sent_today = set()
    while True:
        now = datetime.now()
        # 주말 스킵
        if now.weekday() < 5:
            key = now.strftime("%Y%m%d")
            for t, label in ALERT_TIMES:
                slot = f"{key}_{t}"
                if slot not in sent_today and now.strftime("%H:%M") == t:
                    msg = _build_daily_summary(label)
                    _send_telegram(msg)
                    if t == "08:00":
                        _send_telegram(_check_portfolio_ranks())
                    sent_today.add(slot)
        # 자정 지나면 초기화
        if now.hour == 0 and now.minute == 0:
            sent_today.clear()
        threading.Event().wait(30)  # 30초마다 체크


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    threading.Thread(target=_daily_alert_scheduler, daemon=True).start()
    print(f"[시스템] 주문 서버 시작 (모의투자: {kiwoom_api.KIWOOM_IS_MOCK}) - http://localhost:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)
