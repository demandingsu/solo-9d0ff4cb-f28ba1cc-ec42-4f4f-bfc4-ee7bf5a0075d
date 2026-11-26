#!/usr/bin/env python3
"""黑盒试跑：对调球台、撤场重配、旧页面交分，以及决赛逐分记分。

仅用标准库，可对本机 gunicorn 或 `docker compose up` 后的服务运行：
    python3 scripts/e2e_trial.py http://127.0.0.1:5000

脚本会推进整届赛事（小组赛 -> 半决赛 -> 决赛逐分记分），请对一次性数据库或
`docker compose down -v` 后的全新卷运行，避免污染已有数据。

退出码 0 表示全部断言通过。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
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


class _FormCollector(HTMLParser):
    """从具体表单里按 action 收集隐藏字段（带 revision 的逐分表单）。"""

    def __init__(self, action: str):
        super().__init__()
        self.action = action
        self.in_form = False
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.in_form = a.get("action") == self.action
        elif tag == "input" and self.in_form:
            name = a.get("name")
            if name:
                self.fields[name] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


def form_fields(html: str, action: str) -> dict[str, str]:
    p = _FormCollector(action)
    p.feed(html)
    return p.fields


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


def rally_call(path: str, body: dict):
    status, text = call("POST", path, body=body)
    try:
        return status, json.loads(text)
    except ValueError:
        return status, {"raw": text}


def set_group_scores(plan: dict[str, tuple[int, int]]) -> None:
    form = {}
    for gid, (a, b) in plan.items():
        form[f"score_{gid}_p1"] = str(a)
        form[f"score_{gid}_p2"] = str(b)
    status, _ = call("POST", "/groups/scores", form=form)
    check("整表提交六场比分成功", status == 200, str(status))


def main() -> int:
    print(f"▶ 对 {BASE} 进行黑盒试跑")

    status, _ = call("GET", "/healthz")
    check("健康检查 /healthz=200", status == 200, str(status))

    # 1) 抓首页，按真实表单字段整表提交六场小组赛 ------------------------
    status, home = call("GET", "/")
    check("首页可打开且含整表表单", status == 200 and 'data-testid="group-score-form"' in home)

    fields = re.findall(r'name="(score_(\d+)_p([12]))"', home)
    check("解析出 12 个小组比分输入框", len(fields) == 12, f"实际 {len(fields)}")

    # 与 pytest 相同的排名剧本：A 组连环套各 1 胜 -> 抽签号决名次
    plan = {
        "1": (3, 0), "2": (1, 3), "3": (3, 1),
        "4": (3, 1), "5": (3, 0), "6": (3, 1),
    }
    set_group_scores(plan)

    st = get_state()
    a_order = [r["id"] for r in st["standings"]["A"]]
    check("A 组同胜场按抽签号排名为 1,2,3", a_order == [1, 2, 3], str(a_order))

    t1, t2 = st["tables"]["1"], st["tables"]["2"]
    check("一号台 = A1(1) 对 B2(5)",
          bool(t1) and {t1["player1_id"], t1["player2_id"]} == {1, 5})
    check("二号台 = B1(4) 对 A2(2)",
          bool(t2) and {t2["player1_id"], t2["player2_id"]} == {4, 2})
    m1, m2 = t1["id"], t2["id"]

    # 2) 旧页面 + 对调球台 + 撤场重配（回归项，保持原有覆盖） --------------
    status, old_page = call("GET", f"/matches/{m1}")
    mo = re.search(rf'action="(/matches/{m1}/score)"', old_page)
    check("旧场次页存在且表单指向固定场次 URL", status == 200 and bool(mo))
    old_action = mo.group(1) if mo else f"/matches/{m1}/score"

    status, _ = call("POST", old_action, form={"score1": "3", "score2": "1"})
    check("用旧页面登记 m1 比分 3:1 成功", status == 200, str(status))

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

    status, _ = call("POST", old_action, form={"score1": "3", "score2": "2"})
    check("换台后用旧页面更正比分成功", status == 200, str(status))
    x1 = next(x for x in get_state()["active_semifinals"] if x["id"] == m1)
    check("旧页面更正后的 3:2 已保存且球台仍是二号台",
          (x1["score1"], x1["score2"], x1["table_no"]) == (3, 2, 2))

    status, _ = call("POST", f"/matches/{m1}/withdraw", form={})
    check("撤下 m1 成功", status == 200, str(status))
    status, hist = call("GET", "/history")
    check("历史页可查撤下场次与比分 3 : 2",
          status == 200 and f"history-row-{m1}" in hist and "3 : 2" in hist)

    before_ids = {x["id"] for x in get_state()["matches"]}
    status, _ = call("POST", f"/matches/{m1}/re-pair", form={"table_no": ""})
    check("原班两人重新配对成功", status == 200, str(status))
    after = get_state()
    new_matches = [x for x in after["matches"] if x["id"] not in before_ids]
    check("重新配对生成了全新空白场次", len(new_matches) == 1,
          f"new={[x['id'] for x in new_matches]}")
    m3 = new_matches[0]["id"] if new_matches else -1

    status, stale_body = call("POST", old_action,
                              form={"score1": "9", "score2": "9"})
    check("旧页面给已撤下场次交分 -> HTTP 409", status == 409, str(status))
    check("退回页明确提示“刷新”", "刷新" in stale_body and "stale-warning" in stale_body)

    status, stale_json = call("POST", f"/api/matches/{m1}/score",
                              body={"score1": 5, "score2": 3})
    check("JSON 旧客户端同样收到 409/match_withdrawn",
          status == 409 and json.loads(stale_json).get("code") == "match_withdrawn")

    old = next(x for x in get_state()["matches"] if x["id"] == m1)
    check("被拒交分没有改动封存比分（仍 3:2）",
          (old["score1"], old["score2"]) == (3, 2))

    # 新场次记分 + 另一场记分 -> 决赛选手
    status, _ = call("POST", f"/matches/{m3}/score", form={"score1": "3", "score2": "1"})
    check("新配对场次可正常记分 3:1", status == 200, str(status))
    status, _ = call("POST", f"/matches/{m2}/score", form={"score1": "1", "score2": "3"})
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

    # 3) 决赛逐分记分 ------------------------------------------------------
    fid = st["final_match"]["id"]
    p1_name = st["final_match"]["player1_name"]
    p2_name = st["final_match"]["player2_name"]
    status, final_page = call("GET", f"/matches/{fid}")
    check("决赛场次页提供逐分记分开启表单",
          status == 200 and f'data-testid="rally-start-form"' in final_page)

    status, body = rally_call(f"/api/matches/{fid}/rally/start",
                              {"first_server": 1})
    check("选定首发球员后开启逐分记分", status == 200 and body.get("ok"), str(body)[:200])
    rally = body.get("rally") or {}
    check("开局比分 0:0、应发球员为首发方、版本 r1",
          (rally.get("score1"), rally.get("score2"),
           rally.get("serving_side"), rally.get("revision")) == (0, 0, 1, 1),
          str(rally))

    # 原网页表单与 API 都拒绝整场比分
    status, html_reject = call("POST", f"/matches/{fid}/score",
                               form={"score1": "5", "score2": "3"})
    check("开启后网页整场比分表单被拒 HTTP 409", status == 409, str(status))
    check("拒绝页给出逐分记分提示", "逐分记分" in html_reject)
    status, js_reject = call("POST", f"/api/matches/{fid}/score",
                             body={"score1": 5, "score2": 3})
    check("开启后整场比分 API 被拒 409/rally_mode_active",
          status == 409 and json.loads(js_reject).get("code") == "rally_mode_active")

    # 两张页面：首页与场次页拿到同一 revision
    status, final_page = call("GET", f"/matches/{fid}")
    status, home = call("GET", "/")
    rev_match = int(re.search(rf'data-testid="rally-panel-{fid}"[\s\S]*?data-revision="(\d+)"',
                              final_page).group(1))
    rev_home = int(re.search(rf'data-testid="rally-panel-{fid}"[\s\S]*?data-revision="(\d+)"',
                             home).group(1))
    check("两张页面显示同一进度版本", rev_match == rev_home, f"{rev_match}/{rev_home}")
    check("记分页显示当前比分与应发球员",
          'data-testid="rally-score"' in final_page
          and 'data-testid="rally-serving"' in final_page
          and p1_name in final_page)

    # 用两张页面的真实表单分别加分：只能有一笔生效
    page_fields = form_fields(final_page, f"/matches/{fid}/rally/point")
    home_fields = form_fields(home, f"/matches/{fid}/rally/point")
    check("两张页面的加分表单持有同一 revision",
          page_fields.get("revision") == home_fields.get("revision")
          == str(rev_match), f"{page_fields.get('revision')}/{home_fields.get('revision')}")
    check("首页表单带 origin=home，场次页表单带 origin=match",
          home_fields.get("origin") == "home"
          and page_fields.get("origin") == "match")
    first = dict(page_fields, side="1")
    second = dict(home_fields, side="2")
    status1, _ = call("POST", f"/matches/{fid}/rally/point", form=first)
    status2, conflict_html = call("POST", f"/matches/{fid}/rally/point", form=second)
    check("场次页先加分成功（302 回跳）", status1 in (200, 302), str(status1))
    check("首页持同一版本再提交被拒 409", status2 == 409, str(status2))
    check("旧页面提示刷新", "刷新" in conflict_html and "rally-stale-warning" in conflict_html)
    st = get_state()["final_rally"]
    check("两笔里只有一笔生效，比分为 1:0",
          (st["score1"], st["score2"], st["revision"]) == (1, 0, 2), str(st))

    # 平分换发：从 1:0 打到 10:10（先给客队补到 1:10，再给主队补到 10:10），
    # 全程不会提前触发“11 分且领先 2 分”的结束条件。
    fid_rev = st["revision"]
    for _ in range(10):
        status, body = rally_call(f"/api/matches/{fid}/rally/point",
                                  {"side": 2, "revision": fid_rev})
        assert status == 200, body
        fid_rev = body["rally"]["revision"]
    for _ in range(9):
        status, body = rally_call(f"/api/matches/{fid}/rally/point",
                                  {"side": 1, "revision": fid_rev})
        assert status == 200, body
        fid_rev = body["rally"]["revision"]
    st = get_state()["final_rally"]
    check("打到 10:10 平分", (st["score1"], st["score2"]) == (10, 10), str(st))
    check(f"10 平时按原两分轮转，仍是首发方({p1_name})发球",
          st["serving_side"] == 1, str(st["serving_side"]))

    status, body = rally_call(f"/api/matches/{fid}/rally/point",
                              {"side": 1, "revision": fid_rev})
    fid_rev = body["rally"]["revision"]
    st = body["rally"]
    check("10 平后第一分：11:10，换由对方发球",
          (st["score1"], st["score2"], st["serving_side"]) == (11, 10, 2), str(st))
    status, body = rally_call(f"/api/matches/{fid}/rally/point",
                              {"side": 2, "revision": fid_rev})
    fid_rev = body["rally"]["revision"]
    st = body["rally"]
    check("再打一分：11:11，又换回首发方发球",
          (st["score1"], st["score2"], st["serving_side"]) == (11, 11, 1), str(st))

    # 打到制胜分：12:11 -> 13:11
    side_win = 1
    for _ in range(2):
        status, body = rally_call(f"/api/matches/{fid}/rally/point",
                                  {"side": side_win, "revision": fid_rev})
        assert status == 200, body
        fid_rev = body["rally"]["revision"]
    st = body["rally"]
    check("一方领先 2 分且到 11 分后停止加分（13:11 结束）",
          (st["score1"], st["score2"], st["finished"]) == (13, 11, True), str(st))
    blocked = rally_call(f"/api/matches/{fid}/rally/point",
                         {"side": 2, "revision": fid_rev})
    check("结束后再加分被拒 400", blocked[0] == 400, str(blocked[0]))

    home = call("GET", "/")[1]
    check(f"首页显示决赛比分 13 : 11 与冠军{p1_name}",
          'data-testid="final-score-1">13' in home
          and 'data-testid="final-score-2">11' in home
          and 'data-testid="champion"' in home and f"冠军：{p1_name}" in home)

    # 撤分：撤回制胜分后恢复记分，冠军撤下
    status, body = rally_call(f"/api/matches/{fid}/rally/undo",
                              {"revision": fid_rev})
    check("撤回最近一分成功", status == 200 and body.get("ok"), str(status))
    rally = body["rally"]
    fid_rev = rally["revision"]
    check("撤回制胜分后比分回到 12:11 并恢复记分",
          (rally["score1"], rally["score2"], rally["finished"]) == (12, 11, False),
          str(rally))
    home = call("GET", "/")[1]
    check("撤分后首页撤下冠军",
          'data-testid="champion"' not in home and "冠军：" not in home)

    # 比分回到原样后，旧页面再提交仍要提示刷新
    stale_after_undo = rally_call(f"/api/matches/{fid}/rally/point",
                                  {"side": 2, "revision": fid_rev - 2})
    check("撤分后旧版本再提交 -> 409/rally_stale",
          stale_after_undo[0] == 409
          and stale_after_undo[1].get("code") == "rally_stale",
          str(stale_after_undo[0]))

    # 恢复记分并重新打到制胜分（12:11 -> 13:11）
    status, body = rally_call(f"/api/matches/{fid}/rally/point",
                              {"side": side_win, "revision": fid_rev})
    assert status == 200, body
    fid_rev = body["rally"]["revision"]
    st = body["rally"]
    check("撤分后继续加分可再次结束（13:11）",
          st["finished"] is True and st["score1"] - st["score2"] >= 2, str(st))
    check(f"再次结束后冠军恢复（{p1_name}）",
          get_state()["champion"]["player"]["name"] == p1_name)

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

