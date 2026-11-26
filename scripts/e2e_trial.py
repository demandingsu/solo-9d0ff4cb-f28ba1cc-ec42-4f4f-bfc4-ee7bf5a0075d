#!/usr/bin/env python3
"""黑盒试跑：对调球台 + 撤场重配 + 用旧页面交分。

仅用标准库，可对本机 gunicorn 或 `docker compose up` 后的服务运行：
    python3 scripts/e2e_trial.py http://127.0.0.1:5000

退出码 0 表示全部断言通过。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000").rstrip("/")

failures: list[str] = []
passed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failures.append(f"{name} {detail}")
        print(f"  ❌ {name} {detail}")


def call(method: str, path: str, *, form=None, body=None):
    data = None
    headers = {}
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        BASE + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get_state():
    status, text = call("GET", "/api/state")
    assert status == 200, (status, text[:300])
    return json.loads(text)


def main() -> int:
    print(f"▶ 对 {BASE} 进行黑盒试跑")

    status, _ = call("GET", "/healthz")
    check("健康检查 /healthz=200", status == 200, str(status))

    # 1) 抓首页，按真实表单字段整表提交六场小组赛 ------------------------
    status, home = call("GET", "/")
    check("首页可打开且含整表表单", status == 200 and 'data-testid="group-score-form"' in home)

    fields = re.findall(r'name="(score_(\d+)_p([12]))"', home)
    # 与 pytest 相同的排名剧本：A 组连环套各 1 胜 -> 抽签号决名次
    plan = {
        "1": (3, 0), "2": (1, 3), "3": (3, 1),
        "4": (3, 1), "5": (3, 0), "6": (3, 1),
    }
    form: dict[str, str] = {}
    for full, gid, side in fields:
        form[full] = str(plan[gid][int(side) - 1])
    check("解析出 12 个小组比分输入框", len(fields) == 12, f"实际 {len(fields)}")

    status, html = call("POST", "/groups/scores", form=form)
    check("整表提交六场比分成功(2xx/3xx 后落地页 200)", status == 200, str(status))

    st = get_state()
    a_order = [r["id"] for r in st["standings"]["A"]]
    check("A 组同胜场按抽签号排名为 1,2,3", a_order == [1, 2, 3], str(a_order))

    t1, t2 = st["tables"]["1"], st["tables"]["2"]
    check("一号台 = A1(1) 对 B2(5)",
          t1 and {t1["player1_id"], t1["player2_id"]} == {1, 5})
    check("二号台 = B1(4) 对 A2(2)",
          t2 and {t2["player1_id"], t2["player2_id"]} == {4, 2})
    m1, m2 = t1["id"], t2["id"]

    # 2) 打开并保存“旧页面” ------------------------------------------------
    status, old_page = call("GET", f"/matches/{m1}")
    m = re.search(rf'action="(/matches/{m1}/score)"', old_page)
    check("旧场次页存在且表单指向固定场次 URL", status == 200 and bool(m))
    old_action = m.group(1) if m else f"/matches/{m1}/score"

    status, _ = call("POST", old_action, form={"score1": "3", "score2": "1"})
    check("用旧页面登记 m1 比分 3:1 成功", status == 200, str(status))

    # 3) 对调球台 -----------------------------------------------------------
    status, _ = call("POST", "/semifinals/swap-tables", form={})
    check("对调球台请求成功", status == 200, str(status))
    st = get_state()
    x1 = next(x for x in st["active_semifinals"] if x["id"] == m1)
    x2 = next(x for x in st["active_semifinals"] if x["id"] == m2)
    check("对调后 m1 到二号台、m2 到一号台",
          x1["table_no"] == 2 and x2["table_no"] == 1,
          f"m1={x1['table_no']}, m2={x2['table_no']}")
    check("换台后 m1 已录比分 3:1 照旧",
          (x1["score1"], x1["score2"]) == (3, 1),
          f"{x1['score1']}:{x1['score2']}")

    # 4) 换台后仍用同一张旧页面交分（更正为 3:2） ---------------------------
    status, _ = call("POST", old_action, form={"score1": "3", "score2": "2"})
    check("换台后用旧页面更正比分成功", status == 200, str(status))
    x1 = next(x for x in get_state()["active_semifinals"] if x["id"] == m1)
    check("旧页面更正后的 3:2 已保存且球台仍是二号台",
          (x1["score1"], x1["score2"], x1["table_no"]) == (3, 2, 2))

    # 旧的“一号台球台页”上下文带 match_id 交分，也应被接受
    status, _ = call("POST", "/tables/1/score",
                     form={"match_id": str(m1), "score1": "3", "score2": "0"})
    check("旧一号台页面带 match_id 交分仍有效（场次仍 active）",
          status == 200, str(status))
    x1 = next(x for x in get_state()["active_semifinals"] if x["id"] == m1)
    check("该次交分 3:0 记到了 m1 这对球员名下",
          (x1["score1"], x1["score2"]) == (3, 0))

    # 5) 撤下 m1 ------------------------------------------------------------
    status, _ = call("POST", f"/matches/{m1}/withdraw", form={})
    check("撤下 m1 成功", status == 200, str(status))
    st = get_state()
    old = next(x for x in st["matches"] if x["id"] == m1)
    check("m1 状态 withdrawn 且比分 3:0 保留",
          old["status"] == "withdrawn" and (old["score1"], old["score2"]) == (3, 0))

    status, hist = call("GET", "/history")
    check("历史页可查撤下场次与比分 3 : 0",
          status == 200 and f'history-row-{m1}' in hist and "3 : 0" in hist)

    # 6) 原班两人重新配对 ---------------------------------------------------
    before_ids = {x["id"] for x in get_state()["matches"]}
    # 对调后 m1 在二号台，撤下腾出的也是二号台；留空让系统沿用源球台
    status, page = call("POST", f"/matches/{m1}/re-pair", form={"table_no": ""})
    check("重新配对请求成功并落到新场次页", status == 200 and "match-page-" in page,
          str(status))
    after = get_state()
    new_matches = [x for x in after["matches"] if x["id"] not in before_ids]
    check("重新配对生成了全新场次 id", len(new_matches) == 1,
          f"new={[x['id'] for x in new_matches]}")
    m3 = new_matches[0]["id"] if new_matches else -1
    if new_matches:
        nm = new_matches[0]
        check("新场次为空白比分、active、同两名球员、回到二号台",
              nm["status"] == "active"
              and nm["score1"] is None and nm["score2"] is None
              and {nm["player1_id"], nm["player2_id"]} == {1, 5}
              and nm["table_no"] == 2)

    # 7) 关键：撤场重配后，再用旧页面交分必须被退回并提示刷新 ---------------
    status, stale_body = call("POST", old_action,
                              form={"score1": "9", "score2": "9"})
    check("旧页面给已撤下场次交分 -> HTTP 409", status == 409, str(status))
    check("退回页明确提示“刷新”", "刷新" in stale_body and "stale-warning" in stale_body)

    status, stale_json = call("POST", f"/api/matches/{m1}/score",
                              body={"score1": 5, "score2": 3})
    check("JSON 旧客户端同样收到 409/match_withdrawn",
          status == 409 and json.loads(stale_json).get("code") == "match_withdrawn")

    old = next(x for x in get_state()["matches"] if x["id"] == m1)
    check("被拒交分没有改动封存比分（仍 3:0）",
          (old["score1"], old["score2"]) == (3, 0))

    # 8) 新场次记分 + 另一场记分 -> 决赛选手 -------------------------------
    status, new_page = call("GET", f"/matches/{m3}")
    mm = re.search(rf'action="(/matches/{m3}/score)"', new_page)
    check("新场次页存在独立交分入口", status == 200 and bool(mm))
    if mm:
        status, _ = call("POST", mm.group(1), form={"score1": "3", "score2": "1"})
        check("新配对场次可正常记分 3:1", status == 200, str(status))

    status, _ = call("POST", f"/matches/{m2}/score",
                     form={"score1": "1", "score2": "3"})
    check("第二场半决赛记分成功", status == 200, str(status))

    st = get_state()
    check("两场现行半决赛都有比分后 finalists.ready=True",
          st["finalists"]["ready"] is True, str(st["finalists"]))
    winner_ids = {w["player"]["id"] for w in st["finalists"]["winners"]}
    check("决赛选手为 陈一鸣(1) 与 林浩然(2)", winner_ids == {1, 2}, str(winner_ids))

    status, home = call("GET", "/")
    check("首页显示决赛选手姓名",
          status == 200 and "finalist-names" in home
          and "陈一鸣" in home and "林浩然" in home)

    print()
    if failures:
        print(f"❌ {len(failures)} 项失败 / {passed} 项通过：")
        for f in failures:
            print("  -", f)
        return 1
    print(f"🎉 全部 {passed} 项黑盒检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
