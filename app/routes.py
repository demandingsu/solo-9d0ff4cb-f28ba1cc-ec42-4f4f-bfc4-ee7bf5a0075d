"""Flask 路由：服务端渲染页面 + 同义 JSON API。"""
from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from .db import get_db
from .services import (
    DuplicateActivePair,
    MatchGoneError,
    active_semifinals,
    build_state,
    champion,
    create_repair,
    finalists_state,
    get_match,
    list_matches,
    move_to_table,
    record_score,
    repair_from_withdrawn,
    save_group_scores,
    swap_semifinal_tables,
    table_map,
    withdraw_match,
    match_to_dict,
)
from .tournament import ScoreError, pair_key

bp = Blueprint("web", __name__)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    if request.is_json:
        return True
    accept = request.accept_mimetypes
    return accept.best == "application/json" and accept["text/html"] == 0


def _pick(data, *keys):
    for k in keys:
        if k in data:
            v = data[k]
            return "" if v is None else v
    return ""


def extract_score_values() -> tuple:
    if request.is_json:
        d = request.get_json(silent=True) or {}
        if isinstance(d.get("scores"), list) and len(d["scores"]) == 2:
            return d["scores"][0], d["scores"][1]
        return (
            _pick(d, "score1", "s1", "p1_score", "player1_score"),
            _pick(d, "score2", "s2", "p2_score", "player2_score"),
        )
    return (
        _pick(request.form, "score1", "s1", "p1_score", "player1_score"),
        _pick(request.form, "score2", "s2", "p2_score", "player2_score"),
    )


def parse_group_json(payload: dict) -> dict:
    out: dict[int, tuple] = {}
    if isinstance(payload.get("entries"), list):
        for item in payload["entries"]:
            mid = int(item.get("match_id") or item.get("id"))
            out[mid] = (
                _pick(item, "score1", "s1"),
                _pick(item, "score2", "s2"),
            )
        return out

    scores = payload.get("scores", payload)
    if isinstance(scores, dict):
        for key, value in scores.items():
            if not str(key).isdigit():
                continue
            mid = int(key)
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                out[mid] = (value[0], value[1])
            elif isinstance(value, dict):
                out[mid] = (
                    _pick(value, "score1", "s1"),
                    _pick(value, "score2", "s2"),
                )
    elif isinstance(scores, list):
        for item in scores:
            mid = int(item.get("match_id") or item.get("id"))
            out[mid] = (
                _pick(item, "score1", "s1"),
                _pick(item, "score2", "s2"),
            )
    return out


def state_payload(conn) -> dict:
    s = build_state(conn)
    return {
        "standings": s["standings"],
        "group_matches": [dict(r) for r in s["group_matches"]],
        "desired_semifinals": s["desired_semifinals"],
        "tables": {str(k): match_to_dict(v) for k, v in s["tables"].items()},
        "active_semifinals": [match_to_dict(r) for r in s["active_semifinals"]],
        "finalists": s["finalists"],
        "final_match": match_to_dict(s["final_match"]),
        "champion": s["champion"],
        "matches": [match_to_dict(r) for r in s["all_matches"]],
    }


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #

@bp.get("/healthz")
def healthz():
    return {"ok": True}


@bp.get("/")
def index():
    conn = get_db()
    state = build_state(conn)
    return render_template("index.html", group_errors=[], draft={}, **state)


@bp.get("/matches/<int:match_id>")
def match_page(match_id: int):
    m = get_match(get_db(), match_id)
    if m is None:
        if wants_json():
            return jsonify(ok=False, error="not_found"), 404
        return render_template("404.html"), 404
    return render_template("match.html", m=m, stale_message=None, error=None,
                           draft1="", draft2="")


@bp.get("/history")
def history():
    rows = list_matches(get_db())
    players = get_db().execute(
        "SELECT * FROM players ORDER BY group_name, draw_no"
    ).fetchall()
    return render_template("history.html", matches=rows, players=players)


@bp.get("/api/state")
def api_state():
    return jsonify(state_payload(get_db()))


# --------------------------------------------------------------------------- #
# 小组赛整表提交
# --------------------------------------------------------------------------- #

@bp.post("/groups/scores")
def groups_scores():
    conn = get_db()

    if wants_json():
        entries = parse_group_json(request.get_json(silent=True) or {})
        try:
            save_group_scores(conn, entries)
        except ScoreError as exc:
            return jsonify(ok=False, error="validation_error",
                           message=str(exc)), 400
        return jsonify(ok=True, state=state_payload(conn))

    gm_rows = conn.execute("SELECT id FROM group_matches").fetchall()
    draft: dict[int, tuple[str, str]] = {}
    for r in gm_rows:
        gid = r["id"]
        a = _pick(request.form,
                  f"score_{gid}_p1", f"score_{gid}_1", f"m_{gid}_1")
        b = _pick(request.form,
                  f"score_{gid}_p2", f"score_{gid}_2", f"m_{gid}_2")
        draft[gid] = (a, b)

    try:
        save_group_scores(conn, draft)
    except ScoreError as exc:
        state = build_state(conn)
        return render_template(
            "index.html", group_errors=[str(exc)], draft=draft, **state
        ), 400

    flash("六场小组比分已整表保存，排名与半决赛对阵已更新。", "ok")
    return redirect(url_for("web.index") + "#semifinals")


@bp.post("/api/groups/scores")
def api_groups_scores():
    return groups_scores()


# --------------------------------------------------------------------------- #
# 比分登记（按场次）——旧页面交分的核心入口
# --------------------------------------------------------------------------- #

def _score_response(match_id: int, a, b):
    conn = get_db()
    js = wants_json()
    try:
        m = record_score(conn, match_id, a, b)
    except MatchGoneError as exc:
        if js:
            return jsonify(
                ok=False,
                error="match_withdrawn",
                code="match_withdrawn",
                message=str(exc),
                match_id=match_id,
                refresh_url=url_for("web.index"),
            ), 409
        return render_template(
            "match.html", m=exc.match, stale_message=str(exc),
            error=None, draft1="", draft2=""
        ), 409
    except ScoreError as exc:
        if js:
            return jsonify(ok=False, error="validation_error",
                           message=str(exc)), 400
        m = get_match(conn, match_id)
        return render_template(
            "match.html", m=m, stale_message=None, error=str(exc),
            draft1=a, draft2=b
        ), 400

    if js:
        return jsonify(ok=True, match=match_to_dict(m),
                       finalists=finalists_state(conn))
    flash(f"场次 #{match_id} 的比分已保存。", "ok")
    table = m["table_no"]
    anchor = f"#table-{table}" if table in (1, 2) else f"#match-{match_id}"
    return redirect(url_for("web.index") + anchor)


@bp.post("/matches/<int:match_id>/score")
def match_score(match_id: int):
    a, b = extract_score_values()
    return _score_response(match_id, a, b)


@bp.post("/semifinals/<int:match_id>/score")
def semifinal_score_alias(match_id: int):
    a, b = extract_score_values()
    return _score_response(match_id, a, b)


@bp.post("/semifinal/<int:match_id>/score")
def semifinal_score_alias2(match_id: int):
    a, b = extract_score_values()
    return _score_response(match_id, a, b)


@bp.post("/api/matches/<int:match_id>/score")
def api_match_score(match_id: int):
    a, b = extract_score_values()
    return _score_response(match_id, a, b)


@bp.post("/tables/<int:table_no>/score")
def table_score(table_no: int):
    """球台页入口：以隐藏 match_id 为准。

    解析顺序：match_id -> pair_key/两名球员 -> 该台当前占用者。
    只要解析到的场次仍然 active，即使已经对调到别的球台，旧球台页面交分也有效；
    场次撤下后对应记录为 withdrawn -> 409 提示刷新。
    """
    conn = get_db()
    data = (request.get_json(silent=True) or {}) if request.is_json \
        else request.form
    match_id = _pick(data, "match_id", "id")

    if not match_id:
        p1 = _pick(data, "player1_id", "p1")
        p2 = _pick(data, "player2_id", "p2")
        key = _pick(data, "pair_key")
        if not key and str(p1).strip() and str(p2).strip():
            try:
                key = pair_key(int(p1), int(p2))
            except (TypeError, ValueError):
                key = ""
        if key:
            row = conn.execute(
                "SELECT id FROM matches WHERE status='active' AND pair_key=?"
                " ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
            if row is not None:
                match_id = row["id"]

    if not match_id:
        m = table_map(conn).get(table_no)
        if m is None:
            msg = f"{table_no} 号台当前没有现行场次。"
            if wants_json():
                return jsonify(ok=False, error="empty_table", message=msg), 409
            flash(msg, "err")
            return redirect(url_for("web.index") + f"#table-{table_no}")
        match_id = m["id"]
    try:
        match_id = int(match_id)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="bad_match_id"), 400
    a, b = extract_score_values()
    return _score_response(match_id, a, b)


# --------------------------------------------------------------------------- #
# 对调球台 / 换台
# --------------------------------------------------------------------------- #

@bp.post("/semifinals/swap-tables")
def swap_tables():
    conn = get_db()
    ok = swap_semifinal_tables(conn)
    if wants_json():
        if not ok:
            return jsonify(
                ok=False, error="need_two_semifinals",
                message="需要恰好两场有球台编号的现行半决赛才能对调。"
            ), 409
        return jsonify(ok=True, state=state_payload(conn))
    if ok:
        flash("两张球台已对调；各场比分跟随球员/对阵保留不变。", "ok")
    else:
        flash("当前需要恰好两场现行半决赛才能对调球台。", "err")
    return redirect(url_for("web.index") + "#semifinals")


@bp.post("/api/semifinals/swap-tables")
def api_swap_tables():
    return swap_tables()


@bp.post("/swap-tables")
def swap_tables_short_alias():
    return swap_tables()


@bp.post("/matches/<int:match_id>/table")
def change_table(match_id: int):
    conn = get_db()
    raw = _pick(request.form, "table_no") if not request.is_json \
        else _pick(request.get_json(silent=True) or {}, "table_no")
    try:
        table_no = int(raw)
        move_to_table(conn, match_id, table_no)
    except (ScoreError, ValueError) as exc:
        if wants_json():
            return jsonify(ok=False, message=str(exc)), 400
        flash(str(exc), "err")
    except MatchGoneError as exc:
        if wants_json():
            return jsonify(ok=False, code="match_withdrawn",
                           message=str(exc)), 409
        return render_template(
            "match.html", m=exc.match, stale_message=str(exc),
            error=None, draft1="", draft2=""
        ), 409
    else:
        flash(f"场次 #{match_id} 已调到 {table_no} 号台，比分照旧保留。", "ok")
    return redirect(request.referrer or url_for("web.index"))


# --------------------------------------------------------------------------- #
# 撤下
# --------------------------------------------------------------------------- #

@bp.post("/matches/<int:match_id>/withdraw")
def withdraw(match_id: int):
    conn = get_db()
    m = withdraw_match(conn, match_id)
    if m is None:
        if wants_json():
            return jsonify(ok=False, error="not_found"), 404
        return render_template("404.html"), 404
    if wants_json():
        return jsonify(ok=True, match=match_to_dict(m),
                       state=state_payload(conn))
    flash(f"场次 #{match_id} 已撤下，对阵与比分封存在“历史对阵”中可查。", "ok")
    return redirect(url_for("web.history"))


@bp.post("/api/matches/<int:match_id>/withdraw")
def api_withdraw(match_id: int):
    return withdraw(match_id)


# --------------------------------------------------------------------------- #
# 重新配对（同一对球员以后再配到一起 = 全新空白场次）
# --------------------------------------------------------------------------- #

@bp.post("/matches/<int:match_id>/re-pair")
def re_pair(match_id: int):
    conn = get_db()
    raw_table = _pick(request.form, "table_no") if not request.is_json \
        else _pick(request.get_json(silent=True) or {}, "table_no")
    table_no = int(raw_table) if str(raw_table).strip() in ("1", "2") else None
    try:
        new_id = repair_from_withdrawn(conn, match_id, table_no=table_no)
    except DuplicateActivePair as exc:
        if wants_json():
            return jsonify(ok=False, error="duplicate_active_pair",
                           existing_id=exc.existing_id,
                           message=str(exc)), 409
        flash(str(exc) + " 请直接到该现行场次记分。", "err")
        return redirect(url_for("web.match_page", match_id=exc.existing_id))
    except (ScoreError, MatchGoneError) as exc:
        if wants_json():
            return jsonify(ok=False, message=str(exc)), 400
        flash(str(exc), "err")
        return redirect(url_for("web.history"))

    if wants_json():
        return jsonify(ok=True, new_match_id=new_id,
                       match=match_to_dict(get_match(conn, new_id)))
    flash(f"已用原班两人重新配对为新场次 #{new_id}，比分为空白，请重新记分。", "ok")
    return redirect(url_for("web.match_page", match_id=new_id))


@bp.post("/api/matches/<int:match_id>/repait")
def api_repair_alias(match_id: int):
    """兼容拼写 /repait 的 API 别名。"""
    return re_pair(match_id)


@bp.post("/pairings")
def create_pairing():
    conn = get_db()
    if request.is_json:
        d = request.get_json(silent=True) or {}
    else:
        d = request.form
    p1 = _pick(d, "player1_id", "p1")
    p2 = _pick(d, "player2_id", "p2")
    table_raw = _pick(d, "table_no")
    table_no = int(table_raw) if str(table_raw).strip() in ("1", "2") else None
    try:
        new_id = create_repair(
            conn,
            player1_id=int(p1),
            player2_id=int(p2),
            table_no=table_no,
        )
    except DuplicateActivePair as exc:
        if wants_json():
            return jsonify(ok=False, error="duplicate_active_pair",
                           existing_id=exc.existing_id), 409
        flash(str(exc), "err")
        return redirect(url_for("web.match_page", match_id=exc.existing_id))
    except (ScoreError, ValueError, TypeError) as exc:
        if wants_json():
            return jsonify(ok=False, message=f"配对参数无效：{exc}"), 400
        flash(f"配对参数无效：{exc}", "err")
        return redirect(url_for("web.history"))

    if wants_json():
        return jsonify(ok=True, new_match_id=new_id,
                       match=match_to_dict(get_match(conn, new_id)))
    flash(f"新配对场次 #{new_id} 已建立，比分空白。", "ok")
    return redirect(url_for("web.match_page", match_id=new_id))


# 兼容一些自动化脚本可能使用的语义化别名
@bp.post("/matches/<int:match_id>/repait")
def repair_alias_form(match_id: int):
    return re_pair(match_id)
