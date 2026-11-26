#!/usr/bin/env python3
"""并发压测：多进程同时用同一 revision 加分/撤回，验证“只能一笔生效”。

    python3 scripts/race_check.py http://127.0.0.1:5556

仅标准库。需先让赛事推进到“逐分记分已开启”的决赛（脚本会自己完成）。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5560").rstrip("/")


def call(method, path, *, form=None, body=None):
    data = None
    headers = {}
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def state():
    return json.loads(call("GET", "/api/state")[1])


def main() -> int:
    plan = {"1": (3, 0), "2": (1, 3), "3": (3, 1),
            "4": (3, 1), "5": (3, 0), "6": (3, 1)}
    form = {}
    for gid, (a, b) in plan.items():
        form[f"score_{gid}_p1"] = str(a)
        form[f"score_{gid}_p2"] = str(b)
    call("POST", "/groups/scores", form=form)
    st = state()
    m1 = st["tables"]["1"]["id"]
    m2 = st["tables"]["2"]["id"]
    call("POST", f"/api/matches/{m1}/score", body={"score1": 3, "score2": 1})
    call("POST", f"/api/matches/{m2}/score", body={"score1": 1, "score2": 3})
    st = state()
    fid = st["final_match"]["id"]
    status, text = call("POST", f"/api/matches/{fid}/rally/start",
                        body={"first_server": 1})
    assert status == 200, text
    rev = json.loads(text)["rally"]["revision"]

    def fire(i):
        side = 1 if i % 2 == 0 else 2
        return call("POST", f"/api/matches/{fid}/rally/point",
                    body={"side": side, "revision": rev})

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(fire, range(32)))

    ok = [r for r in results if r[0] == 200]
    stale = [r for r in results if r[0] == 409]
    other = [r for r in results if r[0] not in (200, 409)]
    st = state()["final_rally"]
    print(f"32 个并发加分：成功 {len(ok)}，409 {len(stale)}，其他 {len(other)}")
    if other:
        print("其他响应：", other[:3])
    assert len(ok) == 1, f"应只有一笔生效，实际 {len(ok)}"
    assert st["score1"] + st["score2"] == 1, st
    assert st["revision"] == rev + 1, st

    # 并发撤回：同样只能一笔
    rev2 = st["revision"]
    with ThreadPoolExecutor(max_workers=16) as ex:
        undos = list(ex.map(
            lambda _: call("POST", f"/api/matches/{fid}/rally/undo",
                           body={"revision": rev2}), range(16)))
    ok_u = [r for r in undos if r[0] == 200]
    st = state()["final_rally"]
    print(f"16 个并发撤回：成功 {len(ok_u)}")
    assert len(ok_u) == 1, f"应只有一笔撤回生效，实际 {len(ok_u)}"
    assert (st["score1"], st["score2"]) == (0, 0), st
    print("✅ 并发“只一笔生效”验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
