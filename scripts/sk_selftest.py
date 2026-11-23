#!/usr/bin/env python
"""sk_selftest.py — 独立自检：确认入口能在给定输入上产出契约齐全的 result.json。"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sk_entry  # noqa: E402
import topic      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(prog="sk_selftest.py")
    ap.add_argument("--input", required=True, help="input directory (contains case.json)")
    ap.add_argument("--work", default=None)
    ap.add_argument("--output", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    work = args.work or os.path.join(tempfile.gettempdir(), "sk_selftest_work")
    out = args.output or os.path.join(tempfile.gettempdir(), "sk_selftest_out")
    os.makedirs(work, exist_ok=True)
    os.makedirs(out, exist_ok=True)

    sk_entry.run_once(args.input, work, out)

    rp = os.path.join(out, "result.json")
    report = {"ok": False, "result_path": rp, "problems": []}
    if not os.path.isfile(rp):
        report["problems"].append("result.json was not produced")
    else:
        with open(rp, encoding="utf-8") as fh:
            result = json.load(fh)
        with open(os.path.join(args.input, "case.json"), encoding="utf-8") as fh:
            case = json.load(fh)
        problems = topic.self_check(result, {"case": case})
        report["problems"] = problems
        report["ok"] = not problems
        report["status"] = result.get("status")
        report["contract_keys"] = sorted((result.get("contract") or {}).get("required_fields", []))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print("SELFTEST", "PASS" if report["ok"] else "FAIL")
        for p in report["problems"]:
            print("  -", p)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
