# Meridian Capital Partners
### AI-Powered Long/Short Equity Hedge Fund System

This program acts as an automated hedge fund analyst. Every day it pulls data on all 500+ S&P 500 companies, scores them using a quantitative model, sends the top candidates to Claude AI for a deeper review, builds a market-neutral portfolio, monitors risk, and displays everything in a live dashboard with a built-in AI analyst named **JARVIS** that you can chat with.

---

## What Does It Actually Do?

Think of it as a six-step pipeline that runs automatically:

1. **Collect data** — Downloads stock prices, financial statements, SEC filings, insider trades, and short interest data for every S&P 500 company
2. **Score everything** — Ranks each stock 0–100 on 8 factors (momentum, value, quality, growth, etc.) compared to its sector peers
3. **AI deep-dive** — Sends the top-ranked stocks to Claude AI, which reads their earnings call transcripts, 10-K filings, and insider activity to generate a qualitative assessment
4. **Build a portfolio** — Constructs a list of 20 stocks to buy (longs) and 20 to sell short, optimized for low risk and high expected return
5. **Monitor risk** — Runs continuous checks: circuit breakers, stress tests against 2008/2020/2022 scenarios, factor crowding warnings
6. **Show a dashboard** — A live web interface where you can see everything, chat with JARVIS, and read a daily investor letter

---

## Before You Start — What You Need

### Required software (free)

**Python** — the programming language this runs on
- Download from [python.org/downloads](https://python.org/downloads)
- During installation, **check the box that says "Add Python to PATH"** — this is important
- If asked which version, choose Python 3.10 or newer

**Git** — used to download this project
- Download from [git-scm.com](https://git-scm.com/downloads)
- Install with all default settings

### Required account (free)

**SEC EDGAR** — you just need to provide your name and email. The U.S. Securities and Exchange Commission requires this to identify who is downloading their public data. No registration needed — you just type it into a configuration file.

### Optional account (needed only for AI features)

**Anthropic** — the company that makes Claude AI. You only need this if you want the AI analysis features (JARVIS chat, stock analysis reports, investor letter). Everything else works without it.
- Sign up at [console.anthropic.com](https://console.anthropic.com)
- Cost: roughly $2–5 per full analysis run. Results are saved for 30 days, so re-running the same analysis is free.

---

## Setup — Step by Step

### Step 1 — Download the project

Open a terminal (on Windows: search for "Command Prompt" or "PowerShell" in the Start menu) and type these commands one at a time, pressing Enter after each:

```
git clone git@github.com:jpmostika/mini-hedge-fund-analyst.git
cd mini-hedge-fund-analyst
pip install -r requirements.txt
```

> The last command installs all the libraries this program needs. It may take a few minutes.

---

### Step 2 — Create your configuration file

In your terminal, type:

**On Mac/Linux:**
```
cp .env.example .env
```

**On Windows:**
```
copy .env.example .env
```

This creates a file called `.env` where you store your settings. Now open that file in any text editor (Notepad works fine) and fill in your details.

---

### Step 3 — Fill in your settings

The `.env` file looks like this:

```
SEC_USER_AGENT=your-name your-email@example.com
ANTHROPIC_API_KEY=
POLYGON_API_KEY=
FMP_API_KEY=
FRED_API_KEY=
```

Here is what each line means:

| Setting | Do I need it? | What to put |
|---|---|---|
| `SEC_USER_AGENT` | **Yes — required** | Your name and email, e.g. `John Smith john@gmail.com` |
| `ANTHROPIC_API_KEY` | Only for AI features | Your API key from [console.anthropic.com](https://console.anthropic.com) |
| `POLYGON_API_KEY` | No | Leave blank — free data source is used automatically |
| `FMP_API_KEY` | No | Leave blank — only needed for earnings call transcripts |
| `FRED_API_KEY` | No | Leave blank — only needed for credit spread data |

**The minimum to get started:** just fill in `SEC_USER_AGENT` with your name and email.

**Example of a filled-in file:**
```
SEC_USER_AGENT=John Smith john@gmail.com
ANTHROPIC_API_KEY=sk-ant-api03-...
```

---

### Step 4 — Get an Anthropic API key (optional, for AI features)

Skip this step if you just want the quantitative system without the AI analysis.

1. Go to [console.anthropic.com](https://console.anthropic.com) and create a free account
2. Add a payment method — there's no subscription, you only pay for what you use
3. Click **API Keys** in the left menu, then **Create Key**
4. Copy the key (it starts with `sk-ant-`) and paste it into your `.env` file next to `ANTHROPIC_API_KEY=`

> **Cost guide:** A full analysis run on 20 long + 20 short candidates costs approximately $2–5 using Claude Sonnet. Results are cached for 30 days, so if you run again on the same stocks you pay nothing extra.

---

## Running the Program

The system has multiple layers that build on each other. Run them in this order the first time:

### 1. Pull market data
```
python run_data.py --no-filings --no-13f
```
> This downloads prices, financial ratios, short interest, and earnings dates for all 500+ stocks. Takes 5–8 minutes the first time, about 2 minutes after that.

---

### 2. Score all stocks
```
python run_scoring.py
```
> Ranks every stock from 0–100 across 8 factors. Takes about 30 seconds.

---

### 3. Run AI analysis *(optional — needs Anthropic key)*
```
python run_analysis.py --estimate-cost
```
> First run this to see how much it will cost. Then run:
```
python run_analysis.py
```
> This sends the top candidates to Claude for qualitative review. Takes a few minutes and costs ~$2–5.

---

### 4. Build the portfolio
```
python run_portfolio.py --whatif
```
> Shows you what the proposed portfolio would look like — which stocks to buy, which to short, and estimated costs. The `--whatif` flag means nothing actually happens, it's just a preview.

---

### 5. Run a risk check
```
python run_risk_check.py
```
> Checks circuit breakers, runs stress tests, and monitors factor crowding. Saves the results to `cache/risk_state.json`.

---

### 6. Launch the dashboard
```
python run_dashboard.py
```
> Then open your web browser and go to: **http://localhost:8502**

You'll see the JARVIS dashboard where you can explore all the data, chat with the AI, and read the generated investor letter.

---

## The Dashboard — 6 Pages

Once the dashboard is open in your browser, use the navigation bar at the top to switch between pages:

| Page | What you'll find |
|---|---|
| **I · Portfolio** | Chat with JARVIS, key metrics (VIX level, number of candidates, upcoming earnings), data source status |
| **II · Research** | A color-coded heatmap of factor scores for top stocks, expandable cards for each long/short candidate with AI analysis |
| **III · Risk** | Circuit breaker status, factor risk breakdown, stress test results for 6 crisis scenarios, correlation warnings |
| **IV · Performance** | Portfolio returns vs S&P 500, monthly returns calendar, what drove performance (beta vs stock-picking) |
| **V · Execution** | Trade queue and order history (connects to Alpaca when Layer 6 is built) |
| **VI · Letter** | A daily investor letter written by JARVIS — just click "Regenerate Letter" to get today's |

The dashboard automatically refreshes every 5 minutes while the market is open (9:30am–4:00pm ET).

---

## Do I Need All the API Keys?

No. Here is exactly what works with and without each key:

| Feature | Works without Anthropic key? |
|---|---|
| Downloading market data | ✅ Yes |
| Scoring all 500+ stocks | ✅ Yes |
| Building the portfolio | ✅ Yes |
| Risk checks and stress tests | ✅ Yes |
| Dashboard (most pages) | ✅ Yes |
| AI stock analysis reports | ❌ Needs Anthropic key |
| JARVIS chat | ❌ Needs Anthropic key |
| Daily investor letter | ❌ Needs Anthropic key |

**In short:** the quantitative system is completely free. The AI layer costs a few dollars per run.

---

## Setting Up Daily Automation (Windows)

To have the system automatically refresh data and re-score stocks every weekday at 5:15pm, run this once as an Administrator:

```
.\automation\setup_task_scheduler.ps1
```

This sets up a scheduled task that takes about 10 minutes to run and keeps your data fresh without you having to do anything.

---

## Troubleshooting Common Issues

**"pip is not recognized"**
→ Python was not added to your PATH during installation. Reinstall Python and check the "Add Python to PATH" box.

**"Permission denied" errors**
→ Try running your terminal as Administrator (right-click → Run as administrator).

**Dashboard shows no data**
→ Make sure you ran `run_scoring.py` before starting the dashboard. The dashboard reads from files that scoring creates.

**JARVIS says it's offline**
→ Your `ANTHROPIC_API_KEY` in the `.env` file is either missing or incorrect. Double-check it starts with `sk-ant-`.

**Prices look stale**
→ Run `python run_data.py --no-filings --no-13f` to refresh.

---

## All Available Commands

```
# Collect data
python run_data.py                           # Full refresh (includes SEC filings)
python run_data.py --no-filings --no-13f     # Fast daily refresh (~2 min)

# Score stocks
python run_scoring.py                        # Score all 500+ stocks
python run_scoring.py --ticker AAPL          # Score just one stock for inspection

# AI analysis
python run_analysis.py --estimate-cost       # See the cost before running
python run_analysis.py                       # Analyze top 20 long + 20 short
python run_analysis.py --ticker AAPL         # Deep-dive on one stock
python run_analysis.py --sector Technology   # Analyze one sector

# Portfolio
python run_portfolio.py --whatif             # Preview proposed trades (safe)
python run_portfolio.py --current            # Show current positions

# Risk
python run_risk_check.py                     # Full risk check
python run_risk_check.py --stress            # Include all 6 stress scenarios
python run_risk_check.py --tail-only         # Quick VIX check only
python run_risk_check.py --clear-halt        # Unlock system after a circuit breaker

# Dashboard
python run_dashboard.py                      # Open at http://localhost:8502
```

---

## Project Layout

```
mini-hedge-fund-analyst/
├── data/           Pulls and stores market data
├── factors/        Scores stocks on 8 factors
├── analysis/       Claude AI analysis modules
├── portfolio/      Builds and optimizes the portfolio
├── risk/           Risk monitoring and circuit breakers
├── reporting/      Performance reports and investor letters
├── dashboard/      The web dashboard (6 pages)
├── automation/     Scheduled daily run scripts
├── cache/          Local database — created automatically (not uploaded to GitHub)
├── output/         Reports, CSVs, logs — created automatically (not uploaded to GitHub)
├── config.yaml     All settings — safe to edit
└── .env            Your API keys — never share this file
```

---

## Technology Used

- **Python** — the programming language
- **SQLite** — a local database that stores all the market data
- **yfinance** — free stock price and financial data
- **SEC EDGAR** — free SEC filing data (insider trades, 10-K filings)
- **Anthropic Claude** — AI analysis via API
- **Streamlit** — the web dashboard framework
- **Plotly** — interactive charts
- **scipy** — mathematical optimization for portfolio construction

---

## License

MIT — free to use, modify, and share.

---

*Layers 1–5 and 7 complete. Layer 6 (automated trade execution via Alpaca) coming soon.*
