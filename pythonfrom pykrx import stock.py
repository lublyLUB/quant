pythonfrom pykrx import stock
import pandas as pd

# 가장 최근 거래일 직접 테스트
date = "20260613"  # 지난 금요일
print("티커 목록 가져오는 중...")
tickers = stock.get_market_ticker_list(date, market="KOSPI")
print(f"티커 수: {len(tickers)}")

print("펀더멘털 데이터 가져오는 중...")
df = stock.get_market_fundamental_by_ticker(date=date, market="KOSPI")
print(df.head())