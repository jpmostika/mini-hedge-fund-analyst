"""Launch the Meridian Capital Partners JARVIS dashboard."""

import subprocess
import sys
from pathlib import Path

PORT = 8502
APP  = Path(__file__).parent / "dashboard" / "app.py"

print(f"Starting JARVIS dashboard at http://localhost:{PORT}")
print("Press Ctrl+C to stop.\n")

subprocess.run([
    sys.executable, "-m", "streamlit", "run",
    str(APP),
    "--server.port", str(PORT),
    "--server.headless", "true",
    "--browser.gatherUsageStats", "false",
    "--theme.base", "dark",
    "--theme.backgroundColor", "#0b0e17",
    "--theme.primaryColor", "#6366f1",
    "--theme.font", "sans serif",
])
