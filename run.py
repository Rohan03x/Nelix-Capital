"""
run.py — Launch the DCF Valuation Dashboard.

Usage:
    cd "c:\\Users\\Rohan\\Downloads\\New folder (4)"
    .venv\\Scripts\\python.exe run.py
"""

import sys
from pathlib import Path

# Ensure workspace root is on path
sys.path.insert(0, str(Path(__file__).parent))

from auto_valuation.learning.background_runner import start_learning_background_runner
from webapp.app import app

if __name__ == "__main__":
    start_learning_background_runner()
    app.run(debug=True, port=5000, host="127.0.0.1", use_reloader=False)
