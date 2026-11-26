"""端到端行为测试：排名、整表提交、换台、撤下、重配、旧页面交分、决赛选手。"""
import re

import pytest

from app import create_app


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "referee_test.db"
    app = create_app({"TESTING": True, "DB_PATH": str(db_path)})
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def submit_tie_group_scores(client):
    """A 组三人各胜 1 场 -> 全部 1 胜，必须按抽签号 1,2,3 排名。

    1 胜 2（3:0）；3 胜 1（3:1）；2 胜 3（3:1）。
    B 组：4 两胜（3:1,3:0），5 胜 6（3:1）=> 4,5,6。
    """
    data = {
        "score_1_p1": "3", "score_1_p2": "0",
        "score_2_p1": "1", "score_2_p2": "3",
        "score_3_p1": "3", "score_3_p2": "1",
        "score_4_p1": "3", "score_4_p2": "1",
        "score_5_p1": "3", "score_5_p2": "0",
        "score_6_p1": "3", "score_6_p2": "1",
    }
    return client.post("/groups/scores", data=data)


def state(client):
    return client.get("/api/state").get_json()


# --------------------------------------------------------------------------- #
# 基础数据 / 排名
# --------------------------------------------------------------------------- #

def test_seed_has_six_group_matches(client):
    st = state(client)
    assert len(st["group_matches"]) == 6
    assert [p["draw_no"] for p in st["standings"]["A"]] == [1, 2, 3]
    assert [p["draw_no"] for p in st["standings"]["B"]] == [4, 5, 6]


def test_standings_wins_then_draw_number(client):
    resp = submit_tie_group_scores(client)
    assert resp.status_code == 302

    st = state(client)
    a = st["standings"]["A"]
    # 三人都是 1 胜 1 负，按抽签号升序
    assert [(r["id"], r["wins"], r["rank"]) for r in a] == [
        (1, 1, 1), (2, 1, 2), (3, 1, 3)
    ]
    b = st["standings"]["B"]
    assert [(r["id"], r["wins"]) for r in b] == [(4, 2), (5, 1), (6, 0)]


def test_semifinal_pairing_rules_after_submit(client):
    submit_tie_group_scores(client)
    st = state(client)
    t1, t2 = st["tables"]["1"], st["tables"]["2"]
    # 一号台 A1(1) 对 B2(5)；二号台 B1(4) 对 A2(2)
    assert {t1["player1_id"], t1["player2_id"]} == {1, 5}
    assert {t2["player1_id"], t2["player2_id"]} == {4, 2}
    assert t1["table_no"] == 1 and t2["table_no"] == 2
    assert t1["status"] == t2["status"] == "active"


def test_group_form_renders_all_six_inputs(client):
    html = client.get("/").get_data(as_text=True)
    assert 'data-testid="group-score-form"' in html
    for gid in range(1, 7):
        assert f'name="score_{gid}_p1"' in html
        assert f'name="score_{gid}_p2"' in html


def test_invalid_group_table_is_rejected_atomically(client):
    data = {
        "score_1_p1": "3", "score_1_p2": "3",  # 平分非法
        "score_2_p1": "1", "score_2_p2": "3",
    }
    resp = client.post("/groups/scores", data=data)
    assert resp.status_code == 400
    assert "不能相同" in resp.get_data(as_text=True)
    # 数据库没有写入任何比分
    for gm in state(client)["group_matches"]:
        assert gm["score1"] is None and gm["score2"] is None

    js = client.post("/api/groups/scores",
                     json={"scores": {"1": [5, 5]}})
    assert js.status_code == 400
    assert js.get_json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# 决赛选手显示条件
# --------------------------------------------------------------------------- #

def test_finalists_only_after_both_semifinals_scored(client):
    submit_tie_group_scores(client)
    st = state(client)
    assert st["finalists"]["ready"] is False
    home = client.get("/").get_data(as_text=True)
    assert "finalists-pending" in home

    m1 = st["tables"]["1"]["id"]
    m2 = st["tables"]["2"]["id"]

    r = client.post(f"/api/matches/{m1}/score", json={"score1": 3, "score2": 1})
    assert r.status_code == 200
    assert r.get_json()["finalists"]["ready"] is False

    # 只填一边 -> 400
    bad = client.post(f"/api/matches/{m2}/score", json={"score1": 3})
    assert bad.status_code == 400

    r = client.post(f"/api/matches/{m2}/score", json={"score1": 1, "score2": 3})
    assert r.status_code == 200
    body = r.get_json()
    assert body["finalists"]["ready"] is True
    winner_ids = {w["player"]["id"] for w in body["finalists"]["winners"]}
    # m1 胜者 1；m2 (p4 vs p2) 客队 p2 3:1 胜 -> 2
    assert winner_ids == {1, 2}

    home = client.get("/").get_data(as_text=True)
    assert "finalist-names" in home
    assert "陈一鸣" in home and "林浩然" in home


# --------------------------------------------------------------------------- #
# 换台：同一对球员换台，比分照旧；旧页面仍可交分
# --------------------------------------------------------------------------- #

def test_swap_tables_scores_follow_pair_and_old_page_works(client):
    submit_tie_group_scores(client)
    st = state(client)
    m1, m2 = st["tables"]["1"]["id"], st["tables"]["2"]["id"]

    # 用“旧页面”的表单交分（独立场次页）
    page = client.get(f"/matches/{m1}").get_data(as_text=True)
    assert f'action="/matches/{m1}/score"' in page
    assert client.post(f"/matches/{m1}/score",
                       data={"score1": "3", "score2": "1"}).status_code == 302

    # 对调球台
    assert client.post("/semifinals/swap-tables").status_code == 302
    st = state(client)
    a = next(m for m in st["active_semifinals"] if m["id"] == m1)
    b = next(m for m in st["active_semifinals"] if m["id"] == m2)
    assert a["table_no"] == 2 and b["table_no"] == 1
    # 已录比分仍然挂在原场次（这对球员）上
    assert (a["score1"], a["score2"]) == (3, 1)

    # 旧页面（固定 /matches/<id>）继续交分/更正 -> 有效
    resp = client.post(f"/matches/{m1}/score",
                       data={"score1": "3", "score2": "2"})
    assert resp.status_code == 302
    a = next(m for m in state(client)["active_semifinals"] if m["id"] == m1)
    assert (a["score1"], a["score2"]) == (3, 2)
    assert a["table_no"] == 2  # 没有因为交分而弹回旧台

    # 旧的“一号台球台页”带隐藏 match_id 交分也应被接受（场次仍 active）
    resp = client.post("/tables/1/score",
                       data={"match_id": str(m1), "score1": "3", "score2": "0"})
    assert resp.status_code == 302
    a = next(m for m in state(client)["active_semifinals"] if m["id"] == m1)
    assert (a["score1"], a["score2"]) == (3, 0)


def test_swap_api_and_move_table(client):
    submit_tie_group_scores(client)
    st = state(client)
    m1, m2 = st["tables"]["1"]["id"], st["tables"]["2"]["id"]
    client.post(f"/api/matches/{m1}/score", json={"score1": 3, "score2": 2})

    # 两张台都有比赛时，手动换到被占球台 -> 400
    r = client.post(f"/matches/{m1}/table", data={"table_no": "2"})
    assert r.status_code == 302  # flash 错误后回跳
    st = state(client)
    assert next(m for m in st["active_semifinals"] if m["id"] == m1)["table_no"] == 1

    # 撤下 m2 后 m1 可以换到 2 号台，比分保留
    client.post(f"/matches/{m2}/withdraw")
    r = client.post(f"/matches/{m1}/table", data={"table_no": "2"})
    assert r.status_code == 302
    m = next(x for x in state(client)["active_semifinals"] if x["id"] == m1)
    assert m["table_no"] == 2 and (m["score1"], m["score2"]) == (3, 2)


# --------------------------------------------------------------------------- #
# 撤下 / 历史可查 / 重配 / 旧页面被拒并提示刷新
# --------------------------------------------------------------------------- #

def test_withdraw_history_repair_and_stale_old_page_rejected(client):
    submit_tie_group_scores(client)
    st = state(client)
    m1 = st["tables"]["1"]["id"]
    m2 = st["tables"]["2"]["id"]

    client.post(f"/matches/{m1}/score", data={"score1": "3", "score2": "2"})

    # 撤下
    assert client.post(f"/matches/{m1}/withdraw").status_code == 302
    st = state(client)
    old = next(m for m in st["matches"] if m["id"] == m1)
    assert old["status"] == "withdrawn"
    assert (old["score1"], old["score2"]) == (3, 2)
    assert st["tables"]["1"] is None
    assert st["finalists"]["ready"] is False

    # 历史页可查比分
    hist = client.get("/history").get_data(as_text=True)
    assert f'data-testid="history-row-{m1}"' in hist
    assert "3 : 2" in hist

    # GET 旧页面：封存信息 + 刷新/重配入口
    page = client.get(f"/matches/{m1}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "sealed-box" in html and "重新配对" in html

    # 用旧页面交分 -> 409 退回并提示刷新（HTML）
    resp = client.post(f"/matches/{m1}/score",
                       data={"score1": "9", "score2": "9"})
    assert resp.status_code == 409
    body = resp.get_data(as_text=True)
    assert "stale-warning" in body and "刷新" in body

    # JSON 客户端同样 409 + 机器可读代码
    js = client.post(f"/api/matches/{m1}/score", json={"score1": 5, "score2": 3})
    assert js.status_code == 409
    assert js.get_json()["code"] == "match_withdrawn"

    # 旧比分没有被那次被拒请求改动
    old = next(m for m in state(client)["matches"] if m["id"] == m1)
    assert (old["score1"], old["score2"]) == (3, 2)

    # 原班两人重新配对 -> 新 id、空白比分
    resp = client.post(f"/matches/{m1}/re-pair", data={"table_no": "1"})
    assert resp.status_code == 302
    new_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    assert new_id != m1
    new_m = next(m for m in state(client)["matches"] if m["id"] == new_id)
    assert new_m["status"] == "active"
    assert new_m["score1"] is None and new_m["score2"] is None
    assert {new_m["player1_id"], new_m["player2_id"]} == {1, 5}
    assert new_m["table_no"] == 1

    # 旧页面仍然被拒；新场次页面可以正常记分
    assert client.post(f"/matches/{m1}/score",
                       data={"score1": "1", "score2": "3"}).status_code == 409
    ok = client.post(f"/api/matches/{new_id}/score",
                     json={"score1": 3, "score2": 0})
    assert ok.status_code == 200

    # 重复给同一对球员建现行场次 -> 409
    dup = client.post(f"/api/matches/{m1}/repait", json={})
    assert dup.status_code == 409
    assert dup.get_json()["error"] == "duplicate_active_pair"

    # 另一场也记分后，决赛选手正常出现（新 m 胜者 1，m2 客胜 2）
    client.post(f"/api/matches/{m2}/score", json={"score1": 1, "score2": 3})
    st = state(client)
    assert st["finalists"]["ready"] is True
    assert {w["player"]["id"] for w in st["finalists"]["winners"]} == {1, 2}


def test_generic_pairing_of_same_two_also_starts_blank(client):
    submit_tie_group_scores(client)
    st = state(client)
    m1 = st["tables"]["1"]["id"]
    client.post(f"/api/matches/{m1}/score", json={"score1": 3, "score2": 1})
    client.post(f"/api/matches/{m1}/withdraw")

    # 通过通用配对接口让“以后再配到一起”
    r = client.post("/pairings",
                    json={"player1_id": 1, "player2_id": 5, "table_no": 1})
    assert r.status_code == 200
    body = r.get_json()
    assert body["match"]["score1"] is None
    assert body["match"]["status"] == "active"

    # 旧 id 依旧拒绝交分
    assert client.post(f"/api/matches/{m1}/score",
                       json={"score1": 9, "score2": 9}).status_code == 409


# --------------------------------------------------------------------------- #
# 小组对阵更新对已有半决赛记录的影响
# --------------------------------------------------------------------------- #

def test_group_resubmit_with_changed_lineup_replaces_blank_provisional(client):
    # 初始临时对阵：1-5 / 4-2
    # 改一组完全不同的结果，让 A 组 p3 成为 A1
    data = {
        "score_1_p1": "0", "score_1_p2": "3",  # 2 胜 1
        "score_2_p1": "0", "score_2_p2": "3",  # 3 胜 1
        "score_3_p1": "1", "score_3_p2": "3",  # 3 胜 2 => p3 两胜 A1
        "score_4_p1": "3", "score_4_p2": "0",
        "score_5_p1": "3", "score_5_p2": "0",
        "score_6_p1": "0", "score_6_p2": "3",  # 6 胜 5 => B: 4,6,5? 4两胜
    }
    assert client.post("/groups/scores", data=data).status_code == 302
    st = state(client)
    pairs = {tuple(sorted((m["player1_id"], m["player2_id"])))
             for m in st["active_semifinals"]}
    # A1=3, A2=2 ; B1=4(2胜), B2=6 或? B: 4 wins2; 6: beat5 =>1; 5:0?
    # m4 4 beats5, m5 4 beats6, m6 6 beats5 -> 6 one win,5 zero => B2=6
    assert pairs == {tuple(sorted((3, 6))), tuple(sorted((4, 2)))}


def test_scored_semifinal_is_archived_when_lineup_changes(client):
    submit_tie_group_scores(client)
    m1 = state(client)["tables"]["1"]["id"]
    client.post(f"/api/matches/{m1}/score", json={"score1": 3, "score2": 1})

    # 相同排名再次整表提交（更正小组比分但赢家不变）-> 半决赛比分保留
    again = submit_tie_group_scores(client)
    assert again.status_code == 302
    m = next(x for x in state(client)["matches"] if x["id"] == m1)
    assert m["status"] == "active" and (m["score1"], m["score2"]) == (3, 1)

    # 颠覆 A 组排名 -> 旧 m1 对阵不再需要：有比分 => 封存可查，新场空白
    data = {
        "score_1_p1": "0", "score_1_p2": "3",
        "score_2_p1": "0", "score_2_p2": "3",
        "score_3_p1": "1", "score_3_p2": "3",
        "score_4_p1": "3", "score_4_p2": "1",
        "score_5_p1": "3", "score_5_p2": "0",
        "score_6_p1": "3", "score_6_p2": "1",
    }
    client.post("/groups/scores", data=data)
    m = next(x for x in state(client)["matches"] if x["id"] == m1)
    assert m["status"] == "withdrawn"
    assert m["withdraw_reason"] == "lineup_update"
    assert (m["score1"], m["score2"]) == (3, 1)
    assert client.post(f"/api/matches/{m1}/score",
                       json={"score1": 1, "score2": 3}).status_code == 409


def test_swap_requires_two_active_semifinals(client):
    submit_tie_group_scores(client)
    m1 = state(client)["tables"]["1"]["id"]
    client.post(f"/matches/{m1}/withdraw")
    r = client.post("/api/semifinals/swap-tables")
    assert r.status_code == 409
    assert r.get_json()["error"] == "need_two_semifinals"


def test_table_endpoint_resolves_by_pair_after_swap(client):
    submit_tie_group_scores(client)
    st = state(client)
    m1 = st["tables"]["1"]["id"]
    client.post("/semifinals/swap-tables")  # m1 -> 2 号台

    # 不带 match_id，只带两名球员，旧一号台页面也能找到已换到二号台的场次
    r = client.post("/tables/1/score",
                    data={"player1_id": "1", "player2_id": "5",
                          "score1": "3", "score2": "2"})
    assert r.status_code == 302
    m = next(x for x in state(client)["matches"] if x["id"] == m1)
    assert (m["score1"], m["score2"], m["table_no"]) == (3, 2, 2)

    # 语义化别名同样可用
    r = client.post(f"/semifinals/{m1}/score",
                    json={"score1": 3, "score2": 1})
    assert r.status_code == 200


def test_healthz(client):
    assert client.get("/healthz").get_json() == {"ok": True}
