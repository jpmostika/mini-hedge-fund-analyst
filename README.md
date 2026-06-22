# Meridian Capital Partners
### AI-Powered Long/Short Equity Hedge Fund System

A fully automated quantitative hedge fund system built in Python. Scores the entire S&P 500 daily, runs AI qualitative analysis on top candidates via Claude, constructs a market-neutral portfolio, monitors risk in real time, and serves everything through a dark-theme dashboard with a built-in AI analyst named JARVIS.

---

## Architecture — 7 Layers

```
Layer 1  data/          Data ingestion — prices, fundamentals, SEC filings, short interest
Layer 2  factors/       Scoring engine — 8 factors, 27 sub-factors, sector-relative ranking
Layer 3  analysis/      Claude AI analysis — earnings calls, filings, insider activity
Layer 4  portfolio/     Portfolio construction — MVO + conviction-tilt optimizer
Layer 5  risk/          Risk management — Barra model, pre-trade veto, circuit breakers
Layer 6  execution/     Trade execution — Alpaca (coming soon)
Layer 7  dashboard/     JARVIS dashboard — 6-page Streamlit UI + reporting engine
```

---

## Quickstart

### 1. Prerequisites

- **Python 3.10+** — [python.org](https://python.org/downloads) → check "Add Python to PATH" during install
- **Git**

### 2. Clone and install

```bash
git clone git@github.com:jpmostika/mini-hedge-fund-analyst.git
cd mini-hedge-fund-analyst
pip install -r requirements.txt
```

### 3. Configure API keys

```bash
cp .env.example .env
```

Open `.env` and fill in your keys:

| Key | Required? | Where to get it | Cost |
|---|---|---|---|
| `SEC_USER_AGENT` | **Yes** | Your name + email — e.g. `John Smith john@email.com` | Free |
| `ANTHROPIC_API_KEY` | For AI features | [console.anthropic.com](https://console.anthropic.com) → API Keys | ~$2–5/run |
| `POLYGON_API_KEY` | No | [polygon.io](https://polygon.io) | Paid (yfinance fallback is free) |
| `FMP_API_KEY` | No | [financialmodelingprep.com](https://financialmodelingprep.com) | Paid (earnings transcripts only) |
| `FRED_API_KEY` | No | [fred.stlouisfed.org/docs/api](https://fred.stlouisfed.org/docs/api/api_key.html) | Free |

> **SEC_USER_AGENT is the only required key.** The SEC requires a name and email in the request header. All other keys are optional — the system falls back to free data sources automatically.

### 4. Run in order

```bash
# Layer 1 — Pull market data (~5–8 min on first run, ~2 min incremental)
python run_data.py --no-filings --no-13f

# Layer 2 — Score all 503 S&P 500 stocks
python run_scoring.py --no-market-fetch

# Layer 3 — Claude AI analysis on top candidates (requires ANTHROPIC_API_KEY)
python run_analysis.py --estimate-cost   # preview cost before running
python run_analysis.py                   # full run: ~$2–5

# Layer 4 — Build the portfolio (preview mode)
python run_portfolio.py --whatif

# Layer 5 — Full risk check
python run_risk_check.py

# Layer 7 — Launch the JARVIS dashboard
python run_dashboard.py
# Open http://localhost:8502
```

---

## What Each Layer Does

### Layer 1 — Data Infrastructure
Pulls from 5 sources into a local SQLite database (`cache/meridian.db`):
- **Prices** — 3-year daily OHLCV for all S&P 500 stocks + benchmarks via yfinance (or Polygon)
- **Fundamentals** — 24 derived ratios (ROE, FCF yield, accruals, Altman Z, Piotroski) via yfinance
- **SEC Filings** — Form 4 insider transactions, 10-K/10-Q/8-K via EDGAR (rate-limited to 8 req/s)
- **Institutional** — 13-F holdings for 9 hedge funds (Citadel, Point72, Bridgewater, Berkshire, etc.)
- **Alternative** — Short interest, analyst estimates, earnings calendar, earnings transcripts

### Layer 2 — Scoring Engine
Scores all 503 stocks 0–100, **ranked within their GICS sector** so every score is peer-relative:

| Factor | Sub-factors | Weight |
|---|---|---|
| Momentum | 12-1m, 6m, 3m returns, acceleration, 52w proximity, sector-relative strength | 20% |
| Quality | ROE stability, gross margin, Piotroski F-Score, Altman Z-Score, accruals | 20% |
| Value | Fwd earnings yield, book-to-price, FCF yield, EV/EBITDA, shareholder yield | 15% |
| Estimate Revisions | 30/60/90-day EPS consensus changes | 15% |
| Insider Activity | CEO/CFO buys (3x weight), cluster buying flag, net dollar flow | 10% |
| Growth | Revenue YoY, earnings YoY, acceleration, R&D intensity, FCF growth | 10% |
| Short Interest | Short % float, days-to-cover, change vs prior | 5% |
| Institutional Flow | Fund count, net change in holdings, simultaneous new positions | 5% |

Regime-conditional weights: shifts toward quality/value at VIX > 25, momentum at VIX < 15.

### Layer 3 — Claude AI Analysis
Runs 4 qualitative analyzers on top LONG/SHORT candidates via the Anthropic API:
- **Earnings Call** — scores management on confidence, guidance, margins, competitive position (1–10 each)
- **Filing Quality** — forensic accounting review; flags Sloan accruals, CFO/NI divergence, AR inflation
- **Risk Factors** — separates boilerplate from material 10-K risks; flags new risks vs prior year
- **Insider Activity** — interprets Form 4 patterns; distinguishes routine sales from meaningful buying

Combined score = 60% quantitative (Layer 2) + 40% Claude fundamental. Falls back to 100% quant gracefully.

### Layer 4 — Portfolio Construction
- **MVO Optimizer** — Markowitz via scipy SLSQP with factor covariance, net-of-cost expected returns, beta constraint, sector limits
- **Conviction-Tilt** — Equal-weight base with score tilts (top 5% → 1.5x), liquidity caps, earnings adjustments, always converges
- Target: 20 longs + 20 shorts, 75% gross each side, net exposure 0–10%, |net beta| ≤ 0.15
- Transaction cost model: spread + market impact

### Layer 5 — Risk Management
- **Barra Factor Risk Model** — cross-sectional regression, factor covariance matrix, MCTR per position
- **Pre-Trade Veto** — 8 absolute checks (halt lock, earnings blackout, liquidity, position size, sector, gross/net, beta, pairwise correlation). No override.
- **Circuit Breakers** — daily 1.5%/2.5% loss, weekly 4%, 8% drawdown → KILL_SWITCH
- **Tail Risk** — VIX ≥ 25 → reduce gross 20%; VIX ≥ 35 → reduce gross 50%
- **Stress Tests** — 6 scenarios: 2008 crisis, 2020 COVID, 2022 rate hikes, sector shock, momentum reversal, short squeeze

### Layer 7 — JARVIS Dashboard
6-page Streamlit dashboard at `http://localhost:8502`:

| Page | Contents |
|---|---|
| I · Portfolio | JARVIS AI chat, 10-metric status strip, VIX regime badge |
| II · Research | Factor heatmap (top 30 + bottom 30), candidate cards, approve/reject workflow |
| III · Risk | Circuit breaker gauges, risk decomposition donut, MCTR table, stress test results |
| IV · Performance | Equity curve vs SPY, P&L attribution bars, sector-relative alpha, monthly returns grid |
| V · Execution | Order queue, trade history, short availability (Layer 6 slots in here) |
| VI · Letter | Daily LP letter authored by JARVIS, letterhead + compliance footer, download |

Auto-refreshes every 5 minutes during market hours (9:30–16:00 ET).

---

## Do I Need a Claude/Anthropic Account?

**Only for the AI features.** Everything else is completely free.

| Feature | Needs Anthropic key? |
|---|---|
| Data ingestion (Layer 1) | No |
| Quantitative scoring (Layer 2) | No |
| AI qualitative analysis (Layer 3) | **Yes** |
| Portfolio construction (Layer 4) | No |
| Risk management (Layer 5) | No |
| Dashboard — most pages | No |
| Dashboard — JARVIS chat | **Yes** |
| Dashboard — LP Letter | **Yes** |

**To get an Anthropic API key:**
1. Sign up at [console.anthropic.com](https://console.anthropic.com)
2. Add a payment method (pay-as-you-go, no subscription)
3. Go to **API Keys** → **Create Key**
4. Paste into `.env` as `ANTHROPIC_API_KEY=sk-ant-...`

Cost: ~$2–5 for a full 20-long + 20-short analysis run using Claude Sonnet. Results are cached for 30 days — re-running on the same filings is free.

---

## Daily Automation

Set up automatic daily runs at 17:15 on weekdays (Windows):

```powershell
# Run once as Administrator
.\automation\setup_task_scheduler.ps1
```

This registers a Task Scheduler job that refreshes prices, short interest, estimates, and rescores all factors (~10 min).

---

## Command Reference

```bash
# Data
python run_data.py                           # full run (prices + filings + 13F)
python run_data.py --no-filings --no-13f     # fast daily run (~2 min)

# Scoring
python run_scoring.py                         # full score + market metrics fetch
python run_scoring.py --no-market-fetch       # use cached metrics (faster)
python run_scoring.py --ticker AAPL           # single-stock deep-dive

# AI Analysis
python run_analysis.py --estimate-cost        # preview cost, no API calls
python run_analysis.py                        # analyze top 20 long + short
python run_analysis.py --ticker AAPL          # single-stock analysis
python run_analysis.py --sector Technology    # sector-wide analysis

# Portfolio
python run_portfolio.py --whatif              # preview rebalance (no commit)
python run_portfolio.py --rebalance           # queue trades for approval
python run_portfolio.py --current             # show current positions
python run_portfolio.py --optimize-method mvo # use Markowitz optimizer

# Risk
python run_risk_check.py                      # full daily risk check
python run_risk_check.py --stress             # include all 6 stress scenarios
python run_risk_check.py --tail-only          # VIX + credit spread only (fast)
python run_risk_check.py --clear-halt         # lift kill-switch after drawdown

# Dashboard
python run_dashboard.py                       # launch at http://localhost:8502
```

---

## Project Structure

```
mini-hedge-fund-analyst/
├── data/               Layer 1 — data ingestion modules
├── factors/            Layer 2 — scoring engine (8 factors)
├── analysis/           Layer 3 — Claude AI analyzers
├── portfolio/          Layer 4 — portfolio construction
├── risk/               Layer 5 — risk management
├── reporting/          Layer 7 — P&L attribution, commentary, LP letter
├── dashboard/          Layer 7 — Streamlit app (6 pages)
│   └── pages/          Individual page modules
├── automation/         Windows Task Scheduler scripts
├── cache/              SQLite database + cached results (gitignored)
├── output/             CSVs, logs, reports (gitignored)
├── config.yaml         All parameters — edit this to customize
├── .env                API keys (gitignored — copy from .env.example)
├── run_data.py         Layer 1 entry point
├── run_scoring.py      Layer 2 entry point
├── run_analysis.py     Layer 3 entry point
├── run_portfolio.py    Layer 4 entry point
├── run_risk_check.py   Layer 5 entry point
└── run_dashboard.py    Layer 7 entry point
```

---

## Key Configuration

All parameters live in `config.yaml`. Key settings to know:

```yaml
portfolio:
  nav: 1_000_000           # Paper NAV in USD
  num_longs: 20            # Target long positions
  num_shorts: 20           # Target short positions
  max_position_pct: 0.05   # 5% max per position

analysis:
  model: claude-sonnet-4-6      # Claude model (use haiku for lower cost)
  cost_ceiling_per_run: 25.00   # Abort if run exceeds $25

risk:
  drawdown_kill_switch: 0.08    # 8% drawdown triggers halt
  max_net_beta: 0.20            # Net portfolio beta limit
```

---

## License

MIT — free to use, modify, and distribute.

---

*Built with Python, SQLite, Anthropic Claude, yfinance, Streamlit, and Plotly.*
*Layers 1–5 and 7 complete. Layer 6 (Alpaca execution) in progress.*
