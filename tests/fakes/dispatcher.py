"""Fake dispatcher runner: prints the next canned response from $WS_DISPATCH (a JSON list of strings).

A response "EXIT:<n>" makes it exit with status n instead (a provider outage).
The last response repeats; the number of calls is kept in $WS_DISPATCH with a .count suffix.
"""

import json
import os
import sys
from pathlib import Path

responses = Path(os.environ["WS_DISPATCH"])
counter = responses.with_suffix(".count")
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
items = json.loads(responses.read_text())
response = items[min(n, len(items) - 1)]
if response.startswith("EXIT:"):  # simulate a provider outage
    sys.exit(int(response.removeprefix("EXIT:")))
print(response)
