"""
Provider abstraction layer — routes data requests to best available source.
Priority: Polygon > yfinance (prices), FMP > yfinance (fundamentals/transcripts),
          FRED > computed (macro), SEC EDGAR (filings, always).
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


class DataProviders:
    def __init__(self):
        self.polygon_key = os.getenv("POLYGON_API_KEY", "").strip()
        self.fmp_key = os.getenv("FMP_API_KEY", "").strip()
        self.fred_key = os.getenv("FRED_API_KEY", "").strip()
        self.sec_agent = os.getenv("SEC_USER_AGENT", "Meridian Capital Partners admin@example.com")

        self.use_polygon = bool(self.polygon_key)
        self.use_fmp = bool(self.fmp_key)
        self.use_fred = bool(self.fred_key)

        self._log_active_providers()

    def _log_active_providers(self):
        if self.use_polygon:
            logger.info("Using Polygon for prices (licensed exchange data)")
        else:
            logger.info("Falling back to yfinance for prices")

        if self.use_fmp:
            logger.info("Using FMP for transcripts + structured financials")
        else:
            logger.info("Falling back to yfinance for fundamentals")

        if self.use_fred:
            logger.info("Using FRED for yield curve, credit spread, fed funds rate")
        else:
            logger.info("FRED not configured — macro data will be sourced from yfinance proxies")

        logger.info("SEC EDGAR active for filings (always on)")

    # ------------------------------------------------------------------ #
    # Price data                                                           #
    # ------------------------------------------------------------------ #

    def get_prices(self, tickers: list, start: str, end: str) -> "pd.DataFrame":
        if self.use_polygon:
            return self._prices_polygon(tickers, start, end)
        return self._prices_yfinance(tickers, start, end)

    def _prices_polygon(self, tickers: list, start: str, end: str) -> "pd.DataFrame":
        import requests
        import pandas as pd

        frames = []
        base = "https://api.polygon.io/v2/aggs/ticker"
        for ticker in tickers:
            url = f"{base}/{ticker}/range/1/day/{start}/{end}"
            resp = requests.get(url, params={"apiKey": self.polygon_key, "limit": 50000})
            if resp.status_code != 200:
                logger.warning(f"Polygon failed for {ticker}: {resp.status_code}, falling back to yfinance")
                df = self._prices_yfinance([ticker], start, end)
                frames.append(df)
                continue
            data = resp.json().get("results", [])
            if not data:
                continue
            df = pd.DataFrame(data)
            df["ticker"] = ticker
            df["date"] = pd.to_datetime(df["t"], unit="ms").dt.date
            df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
            frames.append(df[["ticker", "date", "open", "high", "low", "close", "volume"]])

        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _prices_yfinance(self, tickers: list, start: str, end: str) -> "pd.DataFrame":
        import yfinance as yf
        import pandas as pd

        raw = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=False)
        if raw.empty:
            return pd.DataFrame()

        if isinstance(raw.columns, pd.MultiIndex):
            frames = []
            for ticker in tickers:
                try:
                    sub = raw.xs(ticker, axis=1, level=1).copy()
                    sub["ticker"] = ticker
                    sub.index.name = "date"
                    sub = sub.reset_index()
                    sub["date"] = sub["date"].dt.date
                    sub.columns = [c.lower() for c in sub.columns]
                    frames.append(sub[["ticker", "date", "open", "high", "low", "close", "volume"]])
                except KeyError:
                    continue
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        else:
            raw["ticker"] = tickers[0]
            raw.index.name = "date"
            raw = raw.reset_index()
            raw["date"] = raw["date"].dt.date
            raw.columns = [c.lower() for c in raw.columns]
            return raw[["ticker", "date", "open", "high", "low", "close", "volume"]]

    # ------------------------------------------------------------------ #
    # Fundamentals                                                        #
    # ------------------------------------------------------------------ #

    def get_fundamentals(self, ticker: str) -> dict:
        """Returns dict with 'income', 'balance', 'cashflow' DataFrames."""
        if self.use_fmp:
            result = self._fundamentals_fmp(ticker)
            if result:
                return result
        return self._fundamentals_yfinance(ticker)

    def _fundamentals_fmp(self, ticker: str) -> dict:
        import requests
        import pandas as pd

        base = "https://financialmodelingprep.com/api/v3"
        headers = {}
        try:
            income_q = requests.get(f"{base}/income-statement/{ticker}",
                                    params={"apikey": self.fmp_key, "period": "quarter", "limit": 20}).json()
            income_a = requests.get(f"{base}/income-statement/{ticker}",
                                    params={"apikey": self.fmp_key, "limit": 5}).json()
            balance_q = requests.get(f"{base}/balance-sheet-statement/{ticker}",
                                     params={"apikey": self.fmp_key, "period": "quarter", "limit": 20}).json()
            balance_a = requests.get(f"{base}/balance-sheet-statement/{ticker}",
                                     params={"apikey": self.fmp_key, "limit": 5}).json()
            cf_q = requests.get(f"{base}/cash-flow-statement/{ticker}",
                                params={"apikey": self.fmp_key, "period": "quarter", "limit": 20}).json()
            cf_a = requests.get(f"{base}/cash-flow-statement/{ticker}",
                                params={"apikey": self.fmp_key, "limit": 5}).json()
            return {
                "income_q": pd.DataFrame(income_q),
                "income_a": pd.DataFrame(income_a),
                "balance_q": pd.DataFrame(balance_q),
                "balance_a": pd.DataFrame(balance_a),
                "cashflow_q": pd.DataFrame(cf_q),
                "cashflow_a": pd.DataFrame(cf_a),
                "source": "fmp",
            }
        except Exception as e:
            logger.warning(f"FMP fundamentals failed for {ticker}: {e}")
            return {}

    def _fundamentals_yfinance(self, ticker: str) -> dict:
        import yfinance as yf

        t = yf.Ticker(ticker)
        return {
            "income_q": t.quarterly_income_stmt.T if t.quarterly_income_stmt is not None else None,
            "income_a": t.income_stmt.T if t.income_stmt is not None else None,
            "balance_q": t.quarterly_balance_sheet.T if t.quarterly_balance_sheet is not None else None,
            "balance_a": t.balance_sheet.T if t.balance_sheet is not None else None,
            "cashflow_q": t.quarterly_cashflow.T if t.quarterly_cashflow is not None else None,
            "cashflow_a": t.cashflow.T if t.cashflow is not None else None,
            "info": t.info,
            "source": "yfinance",
        }

    # ------------------------------------------------------------------ #
    # Macro (FRED)                                                        #
    # ------------------------------------------------------------------ #

    def get_macro_series(self, series_id: str, start: str, end: str) -> "pd.DataFrame":
        if self.use_fred:
            return self._macro_fred(series_id, start, end)
        logger.warning(f"FRED not configured, cannot fetch {series_id}")
        return None

    def _macro_fred(self, series_id: str, start: str, end: str) -> "pd.DataFrame":
        import requests
        import pandas as pd

        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "observation_start": start,
            "observation_end": end,
            "api_key": self.fred_key,
            "file_type": "json",
        }
        resp = requests.get(url, params=params)
        if resp.status_code != 200:
            logger.error(f"FRED error for {series_id}: {resp.status_code}")
            return None
        observations = resp.json().get("observations", [])
        df = pd.DataFrame(observations)[["date", "value"]]
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df["series_id"] = series_id
        return df

    # ------------------------------------------------------------------ #
    # SEC EDGAR                                                           #
    # ------------------------------------------------------------------ #

    def sec_headers(self) -> dict:
        return {"User-Agent": self.sec_agent}


# Module-level singleton
_providers: DataProviders = None


def get_providers() -> DataProviders:
    global _providers
    if _providers is None:
        _providers = DataProviders()
    return _providers
