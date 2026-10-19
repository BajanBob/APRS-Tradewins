# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Android entry point.

python-for-android's 'webview' bootstrap runs this file, waits until port
8073 answers, then shows http://127.0.0.1:8073/ full-screen in a WebView.
"""
import sys

sys.argv = ["app.py", "--no-browser", "--host", "127.0.0.1", "--port", "8073"]

import app  # noqa: E402

app.main()
