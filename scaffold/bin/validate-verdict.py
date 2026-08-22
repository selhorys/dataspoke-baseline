#!/usr/bin/env python3
"""Validate and normalize the scaffold evaluator result without third-party packages."""
import json
import sys

try:
    value = json.load(sys.stdin)
    if set(value) != {"verdict", "summary", "findings"}:
        raise ValueError("unexpected or missing top-level fields")
    if value["verdict"] not in {"APPROVE", "REVISE", "ESCALATE"}:
        raise ValueError("invalid verdict")
    if not isinstance(value["summary"], str) or not value["summary"]:
        raise ValueError("summary must be a non-empty string")
    if not isinstance(value["findings"], list):
        raise ValueError("findings must be an array")
    if value["verdict"] == "APPROVE" and value["findings"]:
        raise ValueError("APPROVE requires zero findings")
    if value["verdict"] != "APPROVE" and not value["findings"]:
        raise ValueError("REVISE and ESCALATE require at least one finding")
    for finding in value["findings"]:
        required = {"file", "severity", "finding", "fix"}
        if not isinstance(finding, dict) or not required <= set(finding) or set(finding) - required - {"line"}:
            raise ValueError("invalid finding fields")
        if finding["severity"] not in {"blocker", "major", "minor"}:
            raise ValueError("invalid finding severity")
        if any(not isinstance(finding[key], str) or not finding[key] for key in required - {"severity"}):
            raise ValueError("finding strings must be non-empty")
        if "line" in finding and (not isinstance(finding["line"], int) or finding["line"] < 1):
            raise ValueError("finding line must be a positive integer")
except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid evaluator verdict: {exc}")

json.dump(value, sys.stdout, separators=(",", ":"))
sys.stdout.write("\n")
