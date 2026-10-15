"""Compare two folders written by tools/record_outputs.py.

    python tools/compare_outputs.py testdata/python testdata/go

Values that differ on every run are replaced before comparing: client order
id tags, UUIDs, timestamps, and the quantities and prices that depend on the
market (fills, balances, crypto quantities). The list of recent orders is
compared by shape only. For argument errors the last stderr line is
compared, without the program name. expires_at, which the Go version doesn't print, is ignored.

Prints each difference and exits 1 if there are any.
"""

import json
import re
import sys
from pathlib import Path

_TAG = re.compile(r"p0-\d+")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z")
# Fields whose values depend on the market or the moment, not on the code.
_VOLATILE = {"filled_qty", "filled_avg_price", "status", "updated_at", "filled_at", "canceled_at"}
_IGNORED = {"expires_at"}


def _normalize(value, key=None):
    if key in _VOLATILE:
        return "<volatile>"
    if isinstance(value, dict):
        normalized = {k: _normalize(v, k) for k, v in value.items() if k not in _IGNORED}
        # A crypto order's quantity follows the price at the fill.
        if value.get("asset_class") == "crypto" and normalized.get("qty") is not None:
            normalized["qty"] = "<volatile>"
        return normalized
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        value = _TAG.sub("<tag>", value)
        value = _UUID.sub("<uuid>", value)
        return _TIME.sub("<time>", value)
    return value


def _shape(value):
    """Key names only, for outputs whose values change between runs."""
    if isinstance(value, dict):
        return sorted(set(value) - _IGNORED)
    if isinstance(value, list):
        return sorted({tuple(_shape(item)) for item in value})
    return type(value).__name__


def _compare(name: str, old: dict, new: dict) -> list:
    problems = []
    if old["exit_code"] != new["exit_code"]:
        problems.append(f"exit code {old['exit_code']} -> {new['exit_code']}")
    if old["exit_code"] == 2 or new["exit_code"] == 2:
        last = [r["stderr_tail"][-1].split("error: ", 1)[-1] if r["stderr_tail"] else "" for r in (old, new)]
        if last[0] != last[1]:
            problems.append(f"argument error {last[0]!r} -> {last[1]!r}")
        return problems
    if name.endswith(("balance", "orders-recent")):
        old_out, new_out = _shape(old["stdout"]), _shape(new["stdout"])
    else:
        old_out, new_out = _normalize(old["stdout"]), _normalize(new["stdout"])
    if old_out != new_out:
        problems.append(f"stdout\n    {json.dumps(old_out)}\n -> {json.dumps(new_out)}")
    return problems


def main() -> None:
    old_dir, new_dir = (Path(arg) for arg in sys.argv[1:3])
    failed = False
    for old_path in sorted(old_dir.glob("*.json")):
        new_path = new_dir / old_path.name
        if not new_path.exists():
            print(f"{old_path.name}: missing from {new_dir}")
            failed = True
            continue
        old = json.loads(old_path.read_text(encoding="utf-8"))
        new = json.loads(new_path.read_text(encoding="utf-8"))
        problems = _compare(old_path.stem, old, new)
        print(f"{old_path.name}: {'same' if not problems else 'DIFFERENT'}")
        for problem in problems:
            print(f"  {problem}")
        failed |= bool(problems)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
