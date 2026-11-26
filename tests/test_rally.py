"""决赛逐分记分的行为测试：开始条件、换发、结束、撤回、双页乐观锁。"""
import re

import pytest

from app import create_app
from app.tournament import is_rally_finished, rally_serving_side


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "referee_rally.db"
    app = create_app({"TESTING": True, "DB_PATH": str(db_path)})
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def state(client):
    return client.get("/api/state").get_json()


def submit_group_scores(client):
    data = {
        "score_1_p1": "3", "score_1_p2": "0",
        "score_2_p1": "1", "score_2_p2": "3",
        "score_3_p1": "3", "score_3_p2": "1",
        "score_4_p1": "3", "score_4_p2": "1",
        "score_5_p1": "3", "score_5_p2": "0",
        "score_6_p1": "3", "score_6_p2": "1",
    }
    assert client.post("/groups/scores", data=data).status_code == 302


def make_final(client):
    """半决赛都登记后产生决赛：陈一鸣(1) 对 林浩然(2)。"""
    submit_group_scores(client)
    st = state(client)
    m1 = st["tables"]["1"]["id"]
    m2 = st["tables"]["2"]["id"]
    client.post(f"/api/matches/{m1}/score", json={"score1": 3, "score2": 1})
    client.post(f"/api/matches/{m2}/score", json={"score1": 1, "score2": 3})
    st = state(client)
    assert st["finalists"]["ready"] is True
    return st["final_match"]["id"]


def start(client, match_id, first_server=1):
    r = client.post(f"/api/matches/{match_id}/rally/start",
                    json={"first_server": first_server})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["rally"]


def point(client, match_id, side, revision):
    return client.post(f"/api/matches/{match_id}/rally/point",
                       json={"side": side, "revision": revision})


def undo(client, match_id, revision):
    return client.post(f"/api/matches/{match_id}/rally/undo",
                       json={"revision": revision})


# --------------------------------------------------------------------------- #
# 纯规则
# --------------------------------------------------------------------------- #

def test_serving_rotation_two_points_then_each_point_at_deuce():
    # 首发 1：1,1,2,2,...；10 平后每分换发
    seq = []
    s1 = s2 = 0
    for i in range(24):
        seq.append(rally_serving_side(s1, s2, 1))
        if i % 2 == 0:
            s1 += 1
        else:
            s2 += 1
    assert seq[:20] == [1, 1, 2, 2] * 5
    # 20 点打完是 10:10，接下来每分换发
    assert seq[20:24] == [1, 2, 1, 2]


def test_finish_rule():
    assert not is_rally_finished(10, 10)
    assert not is_rally_finished(11, 10)
    assert is_rally_finished(11, 9)
    assert is_rally_finished(12, 10)
    assert is_rally_finished(15, 13)


# --------------------------------------------------------------------------- #
# 开启条件 / 整场比分入口关闭
# --------------------------------------------------------------------------- #

def test_start_only_on_unscored_active_final(client):
    submit_group_scores(client)
    st = state(client)
    m1 = st["tables"]["1"]["id"]
    # 半决赛不能开启逐分记分
    r = client.post(f"/api/matches/{m1}/rally/start",
                    json={"first_server": 1})
    assert r.status_code == 409

    # 决赛尚未产生 -> 没有 final_match
    assert st["final_match"] is None

    fid = make_final(client)
    # 非法首发方
    bad = client.post(f"/api/matches/{fid}/rally/start",
                      json={"first_server": 3})
    assert bad.status_code == 409
    start(client, fid, 1)
    dup = client.post(f"/api/matches/{fid}/rally/start",
                      json={"first_server": 2})
    assert dup.status_code == 409


def test_start_rejected_when_final_already_has_whole_score(client):
    fid = make_final(client)
    assert client.post(f"/api/matches/{fid}/score",
                       json={"score1": 3, "score2": 1}).status_code == 200
    r = client.post(f"/api/matches/{fid}/rally/start",
                    json={"first_server": 1})
    assert r.status_code == 409
    assert "整场比分" in r.get_json()["message"]


def test_whole_score_form_and_api_rejected_after_rally_start(client):
    fid = make_final(client)
    start(client, fid, 1)

    js = client.post(f"/api/matches/{fid}/score",
                     json={"score1": 3, "score2": 1})
    assert js.status_code == 409
    assert js.get_json()["code"] == "rally_mode_active"

    html = client.post(f"/matches/{fid}/score",
                       data={"score1": "3", "score2": "1"})
    assert html.status_code == 409
    body = html.get_data(as_text=True)
    assert "逐分记分" in body and "match-notice" in body

    # 球台页入口同样被拒
    table = client.post(f"/tables/1/score",
                        data={"match_id": str(fid), "score1": "3",
                              "score2": "1"})
    assert table.status_code == 409

    # 比分没有被整场录入覆盖（仍为 0:0）
    m = next(x for x in state(client)["matches"] if x["id"] == fid)
    assert (m["score1"], m["score2"]) == (0, 0)


def test_match_page_hides_whole_score_form_but_shows_rally(client):
    fid = make_final(client)
    page = client.get(f"/matches/{fid}").get_data(as_text=True)
    assert "rally-start-form" in page
    assert f'data-testid="score-form-{fid}"' in page

    start(client, fid, 2)
    page = client.get(f"/matches/{fid}").get_data(as_text=True)
    assert f'data-testid="rally-panel-{fid}"' in page
    assert "rally-start-form" not in page


# --------------------------------------------------------------------------- #
# 加分 / 换发 / 结束
# --------------------------------------------------------------------------- #

def test_rally_scores_serving_and_finish_with_champion_on_home(client):
    fid = make_final(client)
    st = start(client, fid, 1)
    assert (st["score1"], st["score2"]) == (0, 0)
    assert st["serving_side"] == 1
    rev = st["revision"]

    # 加到 10:10（双方轮换，每两分换发）
    for i in range(20):
        side = 1 if i % 2 == 0 else 2
        r = point(client, fid, side, rev)
        assert r.status_code == 200
        rev = r.get_json()["rally"]["revision"]

    st = state(client)["final_rally"]
    assert (st["score1"], st["score2"]) == (10, 10)
    assert st["serving_side"] == 1  # 按两分轮转，第 21 分仍由首发方发球

    # 10 平后每分换发
    r = point(client, fid, 1, rev).get_json()["rally"]
    rev = r["revision"]
    assert (r["score1"], r["score2"]) == (11, 10)
    assert r["serving_side"] == 2
    r = point(client, fid, 2, rev).get_json()["rally"]
    rev = r["revision"]
    assert (r["score1"], r["score2"]) == (11, 11)
    assert r["serving_side"] == 1

    # 打到 11:9 -> 结束（先用另一路径：撤回至 10:10，再连续两分）
    # 当前 11:11，继续：12:11、13:11 -> 结束
    r = point(client, fid, 1, rev).get_json()["rally"]
    rev = r["revision"]
    assert r["finished"] is False
    r = point(client, fid, 1, rev).get_json()["rally"]
    rev = r["revision"]
    assert (r["score1"], r["score2"]) == (13, 11)
    assert r["finished"] is True and r["winner_side"] == 1

    # 结束后不能继续加分
    blocked = point(client, fid, 2, rev)
    assert blocked.status_code == 400
    assert "分出胜负" in blocked.get_json()["message"]

    st = state(client)
    assert st["champion"]["player"]["name"] == "陈一鸣"
    assert st["final_match"]["score1"] == 13
    assert st["final_match"]["score2"] == 11

    home = client.get("/").get_data(as_text=True)
    assert 'data-testid="champion"' in home
    assert "冠军：陈一鸣" in home
    assert 'data-testid="final-score-1">13' in home


def test_undo_winning_point_resumes_and_removes_champion(client):
    fid = make_final(client)
    st = start(client, fid, 2)
    rev = st["revision"]
    # 快速打到 11:9 结束（11 分给 p1，9 分给 p2）
    for _ in range(9):
        rev = point(client, fid, 1, rev).get_json()["rally"]["revision"]
        rev = point(client, fid, 2, rev).get_json()["rally"]["revision"]
    for _ in range(2):
        rev = point(client, fid, 1, rev).get_json()["rally"]["revision"]

    st = state(client)
    assert st["champion"]["player"]["name"] == "陈一鸣"
    assert st["final_rally"]["finished"] is True

    # 撤回制胜分 -> 11:9 -> 10:9，恢复记分，冠军撤下
    r = undo(client, fid, rev)
    assert r.status_code == 200
    st = r.get_json()["rally"]
    rev = st["revision"]
    assert (st["score1"], st["score2"]) == (10, 9)
    assert st["finished"] is False
    assert st["winner_side"] is None

    st = state(client)
    assert st["champion"] is None
    home = client.get("/").get_data(as_text=True)
    assert 'data-testid="champion"' not in home
    assert "冠军：" not in home

    # 恢复后可以继续加分并再次结束
    r = point(client, fid, 1, rev).get_json()["rally"]
    rev = r["revision"]
    assert (r["score1"], r["score2"]) == (11, 9)
    assert r["finished"] is True
    assert state(client)["champion"]["player"]["name"] == "陈一鸣"


def test_home_rally_panel_also_scores(client):
    fid = make_final(client)
    start(client, fid, 1)
    home = client.get("/").get_data(as_text=True)
    assert f'data-testid="rally-panel-{fid}"' in home
    assert 'name="origin" value="home"' in home
    rev = int(re.search(r'data-revision="(\d+)"', home).group(1))

    r = client.post(f"/matches/{fid}/rally/point",
                    data={"side": "1", "revision": str(rev),
                          "origin": "home"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/#finalists")
    st = state(client)["final_rally"]
    assert (st["score1"], st["score2"]) == (1, 0)


# --------------------------------------------------------------------------- #
# 双页并发 / 旧页面裁决
# --------------------------------------------------------------------------- #

def test_two_pages_same_revision_only_one_write_wins(client):
    fid = make_final(client)
    start(client, fid, 1)
    home = client.get("/").get_data(as_text=True)
    page = client.get(f"/matches/{fid}").get_data(as_text=True)
    rev_home = int(re.search(r'data-revision="(\d+)"', home).group(1))
    rev_page = int(re.search(r'data-revision="(\d+)"', page).group(1))
    assert rev_home == rev_page

    first = point(client, fid, 1, rev_home)
    assert first.status_code == 200
    second = point(client, fid, 2, rev_page)
    assert second.status_code == 409
    assert second.get_json()["code"] == "rally_stale"
    assert "刷新" in second.get_json()["message"]

    st = state(client)["final_rally"]
    assert (st["score1"], st["score2"]) == (1, 0)  # 只生效一笔

    # 撤回也只允许一笔生效
    rev = st["revision"]
    assert undo(client, fid, rev).status_code == 200
    stale_undo = undo(client, fid, rev)
    assert stale_undo.status_code == 409


def test_stale_page_rejected_even_when_score_returns_to_same(client):
    fid = make_final(client)
    st = start(client, fid, 1)
    rev = st["revision"]
    page = client.get(f"/matches/{fid}").get_data(as_text=True)
    page_rev = int(re.search(r'data-revision="(\d+)"', page).group(1))
    assert page_rev == rev

    # 另一张页面加分后，又撤回：比分回到 0:0，但 revision 已经变化
    r = point(client, fid, 1, rev).get_json()["rally"]
    rev = r["revision"]
    r = undo(client, fid, rev).get_json()["rally"]
    assert (r["score1"], r["score2"]) == (0, 0)

    stale = point(client, fid, 1, page_rev)
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "rally_stale"

    # HTML 旧页面提交：收到 409 + 刷新提示
    html = client.post(f"/matches/{fid}/rally/point",
                       data={"side": "1", "revision": str(page_rev),
                             "origin": "match"})
    assert html.status_code == 409
    body = html.get_data(as_text=True)
    assert "rally-stale-warning" in body and "刷新" in body


def test_withdraw_final_closes_rally_and_champion(client):
    fid = make_final(client)
    st = start(client, fid, 1)
    rev = st["revision"]
    for _ in range(11):
        rev = point(client, fid, 1, rev).get_json()["rally"]["revision"]
    assert state(client)["champion"] is not None

    client.post(f"/api/matches/{fid}/withdraw")
    st = state(client)
    assert st["champion"] is None
    assert st["final_match"] is None

    r = point(client, fid, 2, rev)
    assert r.status_code == 409
    assert "撤下" in r.get_json()["message"]


def test_json_does_not_report_winner_while_rally_in_progress(client):
    fid = make_final(client)
    st = start(client, fid, 1)
    rev = st["revision"]
    r = point(client, fid, 1, rev).get_json()["rally"]
    rev = r["revision"]
    st = state(client)
    assert st["final_match"]["winner_id"] is None
    assert st["final_rally"]["finished"] is False
    assert st["champion"] is None
    frow = next(m for m in st["matches"] if m["id"] == fid)
    assert frow["winner_id"] is None

    # 打到制胜分后，JSON 与冠军才一致地给出胜者
    for _ in range(10):
        rev = point(client, fid, 1, rev).get_json()["rally"]["revision"]
    st = state(client)
    assert st["final_rally"]["finished"] is True
    assert st["final_match"]["winner_id"] == st["champion"]["player"]["id"]
    assert next(m for m in st["matches"] if m["id"] == fid)["winner_id"] == \
        st["champion"]["player"]["id"]
