"""Record alpaca's stdout and exit code for a fixed set of scenarios.

Runs against the paper account only. Replay the scenarios against a new build
of alpaca and compare the output folder with testdata/python (recorded from
the former Python version) using tools/compare_outputs.py, to check that the
JSON contract hasn't changed. Needs Python with python-dotenv.

    go build -o alpaca.exe ./cmd/alpaca
    python tools/record_outputs.py --env .env --out testdata/go \
        --cmd "./alpaca.exe" --option F261016C00014000

Each scenario is saved as <out>/<NN>-<name>.json with the arguments, the exit
code, the parsed stdout and the last lines of stderr. Orders it opens are
closed again by later scenarios; nothing is left open unless a step fails.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from dotenv import dotenv_values


def _scenarios(tag: str, option: str) -> list:
    """(name, args, env overrides) in run order; ids carry the run tag so reruns don't collide."""
    long_id, short_id, crypto_id, call_id = (f"{tag}-{name}" for name in ("long", "short", "crypto", "call"))
    short_args = ["--asset", "f", "--amount", "30", "--position", "short"]
    return [
        ("balance", ["--balance"], {}),
        ("orders-recent", ["--orders"], {}),
        ("open-long-stock", ["--asset", "F", "--amount", "5", "--position", "long", "--client_order_id", long_id], {}),
        # Alpaca rejects a short while a buy of the same stock is open (wash trade).
        ("open-short-wash-trade", [*short_args, "--client_order_id", f"{tag}-wash"], {}),
        ("open-long-crypto", ["--asset", "BTC", "--amount", "10", "--position", "LONG", "--client_order_id", crypto_id], {}),
        ("open-call", ["--asset", option, "--amount", "5", "--position", "call", "--client_order_id", call_id], {}),
        ("open-default-id-error", ["--asset", option, "--amount", "0.01", "--position", "call"], {}),
        ("orders-by-id", ["--orders", f"{{{long_id},{crypto_id},{call_id},{long_id}}}"], {}),
        ("orders-missing-id", ["--orders", f"{tag}-missing"], {}),
        ("close-unfilled", ["--close", f"{long_id},{call_id}"], {}),
        ("open-short-stock", [*short_args, "--client_order_id", short_id], {}),
        ("close-unfilled-short", ["--close", short_id], {}),
        ("close-filled-crypto", ["--close", crypto_id, "--client_order_id", f"{tag}-crypto-close"], {}),
        ("close-again", ["--close", crypto_id], {}),
        ("close-missing", ["--close", f"{tag}-missing"], {}),
        ("error-option-as-long", ["--asset", option, "--amount", "5", "--position", "long"], {}),
        ("error-put-on-call", ["--asset", option, "--amount", "5", "--position", "put"], {}),
        ("error-crypto-short", ["--asset", "BTC/USD", "--amount", "10", "--position", "short"], {}),
        ("args-no-action", [], {}),
        ("args-amount-without-asset", ["--balance", "--amount", "5"], {}),
        ("args-asset-without-position", ["--asset", "F", "--amount", "5"], {}),
        ("args-bad-position", ["--asset", "F", "--amount", "5", "--position", "sideways"], {}),
        ("args-negative-amount", ["--asset", "F", "--amount", "-1", "--position", "long"], {}),
        ("args-two-actions", ["--balance", "--orders"], {}),
        ("args-close-many-with-id", ["--close", "a,b", "--client_order_id", "x"], {}),
        ("args-id-too-long", ["--asset", "F", "--amount", "5", "--position", "long", "--client_order_id", "x" * 129], {}),
        ("args-empty-ids", ["--orders", "{,}"], {}),
        ("missing-api-key", ["--balance"], {"ALPACA_API_KEY": ""}),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cmd", required=True, help='command that runs alpaca, e.g. "./alpaca.exe"')
    parser.add_argument("--env", required=True, help=".env with the paper account's keys")
    parser.add_argument("--out", required=True, help="folder for the recorded outputs")
    parser.add_argument("--option", required=True, help="cheap OCC call contract to buy and cancel")
    parser.add_argument("--logdirectory", default="logs", help="alpaca's log folder for these runs (LOG_DIRECTORY)")
    parser.add_argument("--tag", default=f"p0-{int(time.time())}", help="prefix for client order ids")
    args = parser.parse_args()

    values = {key: value or "" for key, value in dotenv_values(args.env).items()}
    if "paper-api" not in values.get("ALPACA_BASE_URL", ""):
        sys.exit(f"ALPACA_BASE_URL in {args.env} is not the paper endpoint; refusing to place orders")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    command = shlex.split(args.cmd, posix=os.name != "nt")
    # alpaca takes its log settings from the environment, which wins over its .env.
    log_env = {"LOG_DIRECTORY": args.logdirectory, "LOG_NAME": "record.log"}

    for number, (name, scenario_args, overrides) in enumerate(_scenarios(args.tag, args.option), start=1):
        env = {**os.environ, **values, **log_env, **overrides, "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [*command, *scenario_args],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        try:
            stdout = json.loads(result.stdout) if result.stdout.strip() else None
        except json.JSONDecodeError:
            stdout = {"unparsed": result.stdout}
        record = {
            "args": scenario_args,
            "env_overrides": sorted(overrides),
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr_tail": result.stderr.strip().splitlines()[-3:],
        }
        path = out / f"{number:02d}-{name}.json"
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"{path.name}: exit {result.returncode}")


if __name__ == "__main__":
    main()
