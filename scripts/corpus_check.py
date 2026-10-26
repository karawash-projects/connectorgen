"""Generate + contract-test many OpenAPI / Swagger 2.0 specs; group failures by cause.

    python scripts/corpus_check.py SPEC_OR_DIR [...] [--jobs 8] [--timeout 120] [--json out.json]

Each spec runs in its own subprocess with a time limit, so one pathological spec
can't hang or crash the run. Point it at any folder of specs to find robustness problems before users do.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORKER = r'''
import json, sys, time, traceback
t0 = time.perf_counter()
out = {"spec": sys.argv[1]}
try:
    from connectorgen import connector_from_openapi
    from connectorgen.validate import run_contract_tests
    c = connector_from_openapi(sys.argv[1])
    out["generate_s"] = round(time.perf_counter() - t0, 2)
    out.update(actions=len(c.actions), paginated=sum(a.pagination.style != "none" for a in c.actions),
               polling=sum(t.kind == "polling" for t in c.triggers),
               webhooks=sum(t.kind == "webhook" for t in c.triggers),
               auth=[s.type for s in c.auth], notes=sum(len(a.notes) for a in c.actions),
               no_base_url=not c.base_url, size_kb=len(c.model_dump_json()) // 1024)
    report = run_contract_tests(c)
    out.update(passed=report.passed, failed=report.failed, skipped=report.skipped,
               schema_errors=report.schema_errors,
               failures=[f"{r.action}: {r.failures[0]}" for r in report.results if r.failures][:5],
               skip_reasons=[r.warnings[0] for r in report.results if r.status == "skip"][:5])
    out["status"] = "ok" if report.ok else "contract_failures"
except Exception as e:
    out["status"] = "error"
    out["error"] = f"{type(e).__name__}: {e}"[:300]
    out["where"] = traceback.extract_tb(e.__traceback__)[-1].filename.split("connectorgen/")[-1] \
        + f":{traceback.extract_tb(e.__traceback__)[-1].lineno}"
out["total_s"] = round(time.perf_counter() - t0, 2)
print(json.dumps(out))
'''


def run_one(spec: Path, timeout: float) -> dict:
    try:
        proc = subprocess.run([sys.executable, "-c", WORKER, str(spec)], capture_output=True,
                              text=True, timeout=timeout)
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        if line.startswith("{"):
            return json.loads(line)
        return {"spec": str(spec), "status": "crash", "error": proc.stderr.strip()[-300:]}
    except subprocess.TimeoutExpired:
        return {"spec": str(spec), "status": "timeout", "error": f"over {timeout:.0f}s"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--json")
    args = ap.parse_args()
    specs: list[Path] = []
    for p in map(Path, args.paths):
        if p.is_dir():
            for pattern in ("openapi.yaml", "openapi.json", "swagger.yaml", "swagger.json"):
                specs += sorted(p.rglob(pattern))
        else:
            specs.append(p)
    start = time.time()
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda s: run_one(s, args.timeout), specs))

    status = Counter(r["status"] for r in results)
    ok = [r for r in results if r["status"] in ("ok", "contract_failures")]
    actions = sum(r.get("actions", 0) for r in ok)
    passed = sum(r.get("passed", 0) for r in ok)
    failed = sum(r.get("failed", 0) for r in ok)
    skipped = sum(r.get("skipped", 0) for r in ok)
    print(f"{len(results)} specs in {time.time() - start:.0f}s: " +
          ", ".join(f"{k} {v}" for k, v in status.most_common()))
    print(f"actions: {actions:,} generated; contract tests {passed:,} passed, {failed:,} failed, "
          f"{skipped:,} skipped ({passed / max(1, passed + failed + skipped):.2%} pass)")
    print(f"paginated actions: {sum(r.get('paginated', 0) for r in ok):,}; polling triggers: "
          f"{sum(r.get('polling', 0) for r in ok):,}; webhook triggers: {sum(r.get('webhooks', 0) for r in ok):,}")
    print(f"specs without a base URL: {sum(r.get('no_base_url', False) for r in ok)}; "
          f"slowest: {max((r.get('total_s', 0) for r in results), default=0):.1f}s")

    def group(key: str, items: list[str]) -> None:
        if items:
            print(f"\n{key}:")
            for msg, n in Counter(items).most_common(12):
                print(f"  {n:>4}  {msg}")

    import re
    norm = lambda s: re.sub(r"'[^']*'|\"[^\"]*\"|\d+", "…", s)[:140]  # noqa: E731
    group("errors (by cause)", [f"{r.get('where', '?')}: {norm(r['error'])}" for r in results
                                if r["status"] in ("error", "crash", "timeout")])
    group("contract failures (by cause)", [norm(f.split(": ", 1)[1]) for r in ok for f in r.get("failures", [])])
    group("skips (by cause)", [norm(s) for r in ok for s in r.get("skip_reasons", [])])
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
