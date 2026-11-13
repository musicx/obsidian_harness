"""Fake dispatcher runner: prints the next canned response from $WS_DISPATCH (a JSON list of strings).

The last response repeats; the number of calls is kept in $WS_DISPATCH with a .count suffix.
"""

import json
import os
from pathlib import Path

responses = Path(os.environ["WS_DISPATCH"])
counter = responses.with_suffix(".count")
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
items = json.loads(responses.read_text())
print(items[min(n, len(items) - 1)])
