"""End-to-end against a running LLMBrake Cloud (`wrangler dev`). Skipped unless LB_BASE is set."""

import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import pytest

from llmbrake import BudgetExceededError, ModelPrice, create_guard
from llmbrake.hosted import HostedStore

BASE = os.environ.get("LB_BASE")
pytestmark = pytest.mark.skipif(not BASE, reason="LB_BASE not set (run via cloud/test/run.mjs)")
PRICES = {
    "m": ModelPrice(input_per_mtok=0, output_per_mtok=1),
    "cheap": ModelPrice(input_per_mtok=0, output_per_mtok=0.1),
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _post_form(path, fields, cookie=None):
    req = urllib.request.Request(
        BASE + path,
        data=urllib.parse.urlencode(fields).encode(),
        method="POST",
        headers={"origin": BASE, **({"cookie": cookie} if cookie else {})},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        r = opener.open(req)
        return r.status, r.read().decode(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers


def signup():
    _, html, headers = _post_form("/signup", {"name": "py e2e"})
    return re.search(r"lbsk_[A-Za-z0-9_-]{20,}", html).group(0), headers["set-cookie"].split(";")[0]


def provider(log):
    def create(**p):
        time.sleep(0.03)
        log.append(p["model"])
        return {"model": p["model"], "usage": {"prompt_tokens": 0, "completion_tokens": 300_000}}

    return create


PARAMS = {"model": "m", "messages": [], "max_tokens": 300_000}


def test_threads_on_two_guards_share_one_budget():
    key, _ = signup()
    guards = [
        create_guard(1, prices=PRICES, store=HostedStore(key, base_url=BASE)).wrap(provider([])) for _ in range(2)
    ]
    ok, blocked = [], []

    def worker(i):
        try:
            guards[i % 2](customer_id="shared", **PARAMS)
            ok.append(i)
        except BudgetExceededError:
            blocked.append(i)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 4 and len(blocked) == 8
    v = HostedStore(key, base_url=BASE).customer("shared", datetime.now(timezone.utc).strftime("%Y-%m"))
    assert v["spentUsd"] == pytest.approx(1.2) and v["pendingUsd"] == 0 and v["status"] == "blocked"


def test_dashboard_budget_applies_and_downgrades():
    key, cookie = signup()
    log = []
    g = create_guard(
        100, prices=PRICES, downgrade_at=0.5, downgrade_map={"m": "cheap"}, store=HostedStore(key, base_url=BASE)
    )
    create = g.wrap(provider(log))
    create(customer_id="cus_1", **PARAMS)
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    status, _, _ = _post_form("/app/budget", {"customerId": "cus_1", "period": period, "budget": "0.5"}, cookie)
    assert status == 303
    create(customer_id="cus_1", **PARAMS)
    assert log == ["m", "cheap"]
    with pytest.raises(BudgetExceededError):
        for _ in range(20):
            create(customer_id="cus_1", **PARAMS)
