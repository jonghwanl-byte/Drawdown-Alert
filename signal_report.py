"""종목별 N일 고점 대비 하락률 -> 텔레그램 발송

- 하락률 = (최근 종가 / 최근 N일 종가 최고점 - 1) * 100
- 보조: 최근 종가가 N일 이동평균선보다 몇 % 위/아래인지
- 환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

안전장치
- 야후 3경로(chart API / download / history) 중 가장 최신 날짜를 채택
- 장중 미완성 봉 제거
- 종목별로 '기대하는 마지막 거래일'보다 데이터가 오래되면 재조회, 끝까지 안 맞으면 경고와 함께 발송
- 상장 기간이 짧아 N일 데이터가 없으면 N/A 표시
- 실행 중 예외가 나면 텔레그램으로 오류 알림

실행 예)
  python signal_report.py --dry-run   # 발송하지 않고 화면에 출력
  python signal_report.py             # 실제 발송
"""
import argparse
import html
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

TICKERS = {
#    "XLE": "미국 에너지",
#    "SOXQ": "필라델피아 반도체",
    "069500.KS": "한국 주식",
    "283580.KS": "중국 주식",
    "241180.KS": "일본 주식",
    "453810.KS": "인도 주식",
    "385560.KS": "채권 30년",
}
WINDOWS = [20, 60, 200]
# 하락률이 이 값(%)보다 크면 경고 표시
ALERT_DD = {20: -5.0, 60: -8.0, 200: -12.0}

MIN_DAYS = min(WINDOWS)   # 이 일수 미만이면 조회 실패 처리
MAX_ROUNDS = 3            # 최신 데이터가 안 올라왔을 때 재조회 횟수
ROUND_WAIT_SEC = 90       # 재조회 간격

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")
# 시장별 '오늘 종가가 야후에 올라왔을 것으로 보는' 시각 (현지시간)
CUTOFF = {"US": (ET, (16, 30)), "KR": (KST, (16, 0))}


# ---------------------- 날짜 기준 ----------------------
def market_of(ticker: str) -> str:
    return "KR" if ticker.endswith(".KS") else "US"


def expected_session(ticker: str, now_utc: datetime):
    """지금 시점에 이미 확정됐어야 할 마지막 평일 세션 날짜. 공휴일은 모르므로 경고에만 사용."""
    tz, (h, m) = CUTOFF[market_of(ticker)]
    now = now_utc.astimezone(tz)
    d = now.date()
    if (now.hour, now.minute) < (h, m):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def trim_incomplete_bar(close: pd.Series, ticker: str, now_utc: datetime) -> pd.Series:
    """장 마감 전에 받아온 오늘자 미완성 봉 제거."""
    tz, (h, m) = CUTOFF[market_of(ticker)]
    now = now_utc.astimezone(tz)
    if len(close) and close.index[-1].date() == now.date() and (now.hour, now.minute) < (h, m):
        return close.iloc[:-1]
    return close


# ---------------------- 데이터 조회 (3경로) ----------------------
def _clean(s: pd.Series) -> pd.Series:
    s = s.dropna().copy()
    idx = pd.to_datetime(s.index)
    s.index = idx.tz_localize(None).normalize() if idx.tz is not None else idx.normalize()
    s = s[~s.index.duplicated(keep="last")]
    return s.sort_index().astype(float)


def _y_download(ticker: str) -> pd.Series:
    import yfinance as yf
    df = yf.download(ticker, period="2y", interval="1d", auto_adjust=True, progress=False)
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return _clean(close)


def _y_history(ticker: str) -> pd.Series:
    import yfinance as yf
    df = yf.Ticker(ticker).history(period="2y", interval="1d", auto_adjust=True)
    return _clean(df["Close"])


def _y_chart_api(ticker: str) -> pd.Series:
    """야후 chart API 직접 호출(라이브러리 우회). 배당조정 종가 사용."""
    import requests
    last = None
    for host in ("query1", "query2"):
        try:
            r = requests.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"range": "2y", "interval": "1d", "events": "div,splits"},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            quote = res["indicators"]["quote"][0]["close"]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or quote
            off = res["meta"].get("gmtoffset", 0)
            idx = pd.to_datetime(res["timestamp"], unit="s") + pd.to_timedelta(off, unit="s")
            return _clean(pd.Series(adj, index=idx))
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"chart API 실패: {last}")


def fetch_best(ticker: str, now_utc: datetime):
    """3경로를 모두 시도해 가장 최신 날짜의 데이터를 채택. 동률이면 download > history > chart."""
    cands = []
    for name, fn in (("download", _y_download), ("history", _y_history), ("chart", _y_chart_api)):
        try:
            s = trim_incomplete_bar(fn(ticker), ticker, now_utc)
            if s.empty:
                raise RuntimeError("빈 데이터")
            cands.append((name, s))
            print(f"[{ticker}] {name}: 마지막 {s.index[-1].date()} 종가 {s.iloc[-1]:.2f}")
        except Exception as e:  # noqa: BLE001
            print(f"[{ticker}] {name} 실패: {e}", file=sys.stderr)
    if not cands:
        raise RuntimeError(f"{ticker} 데이터를 가져오지 못했습니다")
    pri = {"download": 0, "history": 1, "chart": 2}
    cands.sort(key=lambda c: (-c[1].index[-1].value, pri[c[0]]))
    return cands[0][1]


def load_all():
    """전 종목 조회. 기대 거래일보다 뒤처진 종목만 골라 재조회.
    끝까지 안 맞아도 멈추지 않고 (데이터, 오류 목록, 최신 아님 목록)을 돌려준다."""
    data, fetch_err = {}, {}
    pending = list(TICKERS)
    for rnd in range(MAX_ROUNDS):
        now_utc = datetime.now(timezone.utc)
        for tk in pending:
            try:
                s = fetch_best(tk, now_utc)
                old = data.get(tk)
                if old is None or s.index[-1] >= old.index[-1]:
                    data[tk] = s
                fetch_err.pop(tk, None)
            except Exception as e:  # noqa: BLE001
                fetch_err[tk] = str(e)
                print(e, file=sys.stderr)
        pending = [tk for tk in TICKERS
                   if tk not in data or data[tk].index[-1].date() < expected_session(tk, now_utc)]
        if not pending:
            break
        if rnd < MAX_ROUNDS - 1:
            print(f"재조회 {rnd + 1}: 최신 아님/실패 {pending} -> {ROUND_WAIT_SEC}초 후 재시도")
            time.sleep(ROUND_WAIT_SEC)

    now_utc = datetime.now(timezone.utc)
    stale = []
    for tk, s in data.items():
        exp = expected_session(tk, now_utc)
        if s.index[-1].date() < exp:
            stale.append((tk, s.index[-1].date(), exp))
    return data, fetch_err, stale


# ---------------------- 계산 ----------------------
def analyze(close: pd.Series) -> dict:
    last = float(close.iloc[-1])
    out = {"date": close.index[-1].strftime("%m-%d"), "price": last, "days": len(close)}
    for n in WINDOWS:
        if len(close) < n:  # 상장 기간이 짧아 N일 데이터 없음
            out[f"dd{n}"] = None
            out[f"ma{n}"] = None
            continue
        high = float(close.iloc[-n:].max())
        ma = float(close.iloc[-n:].mean())
        out[f"dd{n}"] = (last / high - 1) * 100
        out[f"ma{n}"] = (last / ma - 1) * 100
    return out


# ---------------------- 메시지 ----------------------
def build_message(results: dict, errors: list, stale: list = None) -> str:
    lines = ["<b>📉 고점 대비 하락률 리포트</b>",
             "(N일 종가 최고점 대비 / 괄호는 N일 이평선 대비)", ""]
    if stale:
        items = ", ".join(f"{t} {d.strftime('%m-%d')}(기대 {e.strftime('%m-%d')})" for t, d, e in stale)
        lines.append(f"⚠️ 데이터 최신 아님: {items}")
        lines.append("휴장일이면 정상, 아니면 오늘 판단 보류 후 재실행하세요.")
        lines.append("")
    for tk, name in TICKERS.items():
        r = results.get(tk)
        if r is None:
            continue
        warn = any(r[f"dd{n}"] is not None and r[f"dd{n}"] <= ALERT_DD[n] for n in WINDOWS)
        mark = "⚠️" if warn else "✅"
        lines.append(f"{mark} <b>{html.escape(name)}</b> ({tk}) {r['date']} 기준")
        body = []
        for n in WINDOWS:
            if r[f"dd{n}"] is None:
                body.append(f"{n:>3}일     N/A  (상장 {r['days']}일)")
            else:
                body.append(f"{n:>3}일 {r[f'dd{n}']:6.1f}%  ({r[f'ma{n}']:+6.1f}%)")
        lines.append("<pre>" + "\n".join(body) + "</pre>")
    if errors:
        lines.append("❗ 조회 실패: " + ", ".join(errors))
    lines.append(f"⚠️ 표시 기준: 20일 {ALERT_DD[20]}% / 60일 {ALERT_DD[60]}% / 200일 {ALERT_DD[200]}% 이하")
    return "\n".join(lines)


# ---------------------- 텔레그램 ----------------------
def send_telegram(text: str, parse_html: bool = True) -> None:
    import requests
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 설정되지 않았습니다.")
    payload = {"chat_id": chat_id, "text": text[:4000]}
    if parse_html:
        payload["parse_mode"] = "HTML"
    last = None
    for attempt in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data=payload, timeout=20)
            if r.status_code == 200:
                return
            last = f"{r.status_code} {r.text}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"텔레그램 발송 실패: {last}")


# ---------------------- main ----------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="발송하지 않고 화면에만 출력")
    args = ap.parse_args()

    try:
        data, fetch_err, stale = load_all()
        results, errors = {}, list(fetch_err)
        for tk, s in data.items():
            if len(s) < MIN_DAYS:
                errors.append(f"{tk}(데이터 {len(s)}일)")
                continue
            results[tk] = analyze(s)
        if not results:
            raise RuntimeError("모든 종목 조회에 실패했습니다: " + ", ".join(errors))
        # 위에서 제외된 종목은 stale 목록에서도 뺀다
        stale = [x for x in stale if x[0] in results]
        msg = build_message(results, errors, stale)
        if args.dry_run:
            print(re_strip(msg))
        else:
            send_telegram(msg)
            print("텔레그램 발송 완료")
    except Exception as e:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if not args.dry_run:
            try:
                send_telegram(f"❌ 고점대비 리포트 실행 오류\n{type(e).__name__}: {e}", parse_html=False)
            except Exception:  # noqa: BLE001
                pass
        sys.exit(1)


def re_strip(msg: str) -> str:
    import re
    return html.unescape(re.sub(r"</?(b|pre)>", "", msg))


if __name__ == "__main__":
    main()
