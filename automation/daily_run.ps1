# Meridian Capital Partners — Daily automation script
# Runs at 17:15 weekdays via Windows Task Scheduler
# Refreshes prices, short interest, estimates, earnings calendar; rescores all factors.
# Estimated runtime: ~10 minutes

$PROJECT = "c:\Users\jpmos\OneDrive\Jarvis\ls_equity_fund"
$PYTHON  = (Get-Command python).Source
$LOG     = "$PROJECT\output\automation.log"

Set-Location $PROJECT

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts  $msg" | Tee-Object -FilePath $LOG -Append
}

Log "=== Daily automation starting ==="

# Step 1: Refresh prices, short interest, estimates, earnings calendar
Log "Step 1: Refreshing market data..."
& $PYTHON run_data.py --no-filings --no-13f 2>&1 | Tee-Object -FilePath $LOG -Append
Log "Step 1 complete."

# Step 2: Re-score universe
Log "Step 2: Rescoring universe..."
& $PYTHON run_scoring.py --no-market-fetch 2>&1 | Tee-Object -FilePath $LOG -Append
Log "Step 2 complete."

# Step 3: Risk check
Log "Step 3: Risk check..."
& $PYTHON run_risk_check.py 2>&1 | Tee-Object -FilePath $LOG -Append
Log "Step 3 complete."

Log "=== Daily automation complete ==="
