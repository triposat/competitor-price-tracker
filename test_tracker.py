"""Deterministic tests for the alerting logic — no API calls.

Mocks the fetchers and seeds history, so it verifies the parts that are easy to get
wrong: alert-on-change (not every run), in-stock gating, cross-currency safety, and
history accumulation. Run with: `python test_tracker.py`
"""
import csv
import io
import os
import tempfile
from contextlib import redirect_stdout

os.environ.setdefault("SCRAPINGBEE_API_KEY", "dummy-for-tests")  # tracker reads this at import
os.environ["UNDERCUT_THRESHOLD"] = "0.05"
os.environ["OUR_CURRENCY"] = "USD"

import tracker  # noqa: E402  (must follow the env setup above)

FAKE: dict[str, dict] = {}
tracker.DISPATCH = {"amazon": lambda s, ident: FAKE[ident]}  # noqa: E731


def run(targets_rows, history_rows=None):
    d = tempfile.mkdtemp()
    tpath, hpath = os.path.join(d, "t.csv"), os.path.join(d, "h.csv")
    with open(tpath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["our_sku", "source", "identifier", "our_price"])
        w.writerows(targets_rows)
    if history_rows is not None:
        with open(hpath, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "our_sku", "competitor", "comp_price", "currency", "in_stock", "our_price"])
            w.writerows(history_rows)
    buf = io.StringIO()
    with redirect_stdout(buf):
        tracker.track(tpath, hpath)
    alerts = sum(1 for ln in buf.getvalue().splitlines() if ln.startswith("ALERT:"))
    with open(hpath) as f:
        rows = sum(1 for _ in f) - 1
    return alerts, rows


TARGETS = [["A", "amazon", "IDA", "10.00"], ["B", "amazon", "IDB", "10.00"], ["C", "amazon", "IDC", "10.00"]]
HIST = [["t", "A", "amazon", "8.0", "USD", "True", "10.0"],
        ["t", "B", "amazon", "8.0", "USD", "False", "10.0"],
        ["t", "C", "amazon", "12.0", "USD", "True", "10.0"]]


def _set(ida, idb=(8.0, False), idc=(12.0, True)):
    global FAKE
    FAKE = {"IDA": {"price": ida[0], "currency": "USD", "in_stock": ida[1]},
            "IDB": {"price": idb[0], "currency": "USD", "in_stock": idb[1]},
            "IDC": {"price": idc[0], "currency": "USD", "in_stock": idc[1]}}


def test_new_undercut_fires_once_oos_and_nonundercut_silent():
    _set((8.0, True))
    assert run(TARGETS) == (1, 3)


def test_persistent_undercut_does_not_respam():
    _set((8.0, True))
    assert run(TARGETS, HIST) == (0, 6)


def test_further_drop_realerts():
    _set((6.0, True))
    assert run(TARGETS, HIST) == (1, 6)


def test_new_crossing_alerts():
    _set((8.0, True), idc=(9.0, True))
    assert run(TARGETS, HIST) == (1, 6)


def test_cross_currency_not_compared():
    global FAKE
    FAKE = {"IDA": {"price": 5.0, "currency": "EUR", "in_stock": True}}
    assert run([["A", "amazon", "IDA", "10.00"]]) == (0, 1)


def test_proxy_params():
    assert tracker._proxy_params(None) == {"render_js": "false"}
    assert tracker._proxy_params("premium") == {"premium_proxy": "true", "render_js": "true"}
    assert tracker._proxy_params("stealth") == {"stealth_proxy": "true"}


def test_parse_price_text():
    assert tracker._parse_price_text("£51.77") == (51.77, "GBP")
    assert tracker._parse_price_text("$69.00") == (69.0, "USD")
    assert tracker._parse_price_text("1.299,00 €") == (1299.0, "EUR")   # EU format
    assert tracker._parse_price_text("1,299.00") == (1299.0, None)      # US format, no symbol


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError:
            failed += 1
            print(f"  [FAIL] {t.__name__}")
    print("\nALL PASS" if not failed else f"\n{failed} FAILED")
    raise SystemExit(1 if failed else 0)
