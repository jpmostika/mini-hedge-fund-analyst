"""
One-shot overnight pipeline (unattended):
  1. Full-universe Form 4 insider ingestion (~4 hrs)
  2. Layer 2 rescore (now with the corrected, sign-aware insider factor)
  3. Ingest 10-Ks for the post-rescore candidate set (so risk_factors has data)
  4. Layer 3 Claude analysis on the de-biased candidates

Restores the AC sleep timeout on exit regardless of outcome.
"""
import logging
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("overnight")

HERE = Path(__file__).parent
PY = sys.executable


def _restore_sleep():
    try:
        subprocess.run(["powercfg", "/change", "standby-timeout-ac", "300"], check=False)
        subprocess.run(["powercfg", "/change", "hibernate-timeout-ac", "360"], check=False)
        log.info("AC sleep timeout restored to 300 min")
    except Exception as e:
        log.warning(f"could not restore sleep setting: {e}")


def main():
    t_start = time.time()
    try:
        # ---- STAGE 1: full-universe Form 4 -------------------------------- #
        log.info("===== STAGE 1: full-universe Form 4 insider ingestion =====")
        from data.db import get_connection
        from data.universe import get_equity_tickers
        from data.sec_data import refresh_sec_data

        conn = get_connection()
        tickers = get_equity_tickers(conn)
        conn.close()
        log.info(f"ingesting Form 4 for {len(tickers)} tickers")
        t0 = time.time()
        res = refresh_sec_data(tickers, forms=["4"])
        log.info(f"STAGE 1 done: {res} in {time.time()-t0:.0f}s")

        # ---- STAGE 2: Layer 2 rescore ------------------------------------ #
        log.info("===== STAGE 2: Layer 2 rescore =====")
        subprocess.run([PY, str(HERE / "run_scoring.py")], check=True, cwd=HERE)

        # ---- STAGE 3: 10-Ks for the post-rescore candidate set ----------- #
        log.info("===== STAGE 3: 10-K ingestion for new candidate set =====")
        from analysis.combined_score import compute_combined, get_top_candidates
        conn = get_connection()
        sel = compute_combined(conn, include_claude=False)   # quant-only selection
        longs, shorts = get_top_candidates(sel, n=20)
        cands = pd.concat([longs, shorts]).drop_duplicates("ticker")["ticker"].tolist()
        conn.close()
        log.info(f"candidate set ({len(cands)}): {cands}")
        (HERE / "cache" / "_candidates.txt").write_text(" ".join(cands))
        res = refresh_sec_data(cands, forms=["10-K"])  # skips already-cached
        log.info(f"STAGE 3 done: {res}")

        # ---- STAGE 4: Layer 3 analysis ----------------------------------- #
        log.info("===== STAGE 4: Layer 3 Claude analysis =====")
        subprocess.run([PY, str(HERE / "run_analysis.py")], check=True, cwd=HERE)

        log.info(f"===== OVERNIGHT PIPELINE COMPLETE in {(time.time()-t_start)/60:.1f} min =====")
    finally:
        _restore_sleep()


if __name__ == "__main__":
    main()
