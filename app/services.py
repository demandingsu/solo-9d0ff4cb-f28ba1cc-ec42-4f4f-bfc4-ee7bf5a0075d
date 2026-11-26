"""所有写操作与页面状态组装都在这里，路由层只负责 HTTP。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .tournament import (
    GROUPS,
    ScoreError,
    compute_standings,
    desired_semifinals,
    pair_key,
    parse_score_pair,
    winner_id,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 查询 / 状态组装
# --------------------------------------------------------------------------- #

def list_players(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM players ORDER BY group_name, draw_no").fetchall()


def list_group_matches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT gm.*,
               p1.name AS player1_name, p1.draw_no AS player1_draw,
               p2.name AS player2_name, p2.draw_no AS player2_draw
          FROM group_matches gm
          JOIN players p1 ON p1.id = gm.player1_id
          JOIN players p2 ON p2.id = gm.player2_id
         ORDER BY gm.id
        """
    ).fetchall()


def standings(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    players = list_players(conn)
    gms = conn.execute("SELECT * FROM group_matches").fetchall()
    return {g: compute_standings(players, gms, g) for g in GROUPS}


def get_match(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT m.*,
               p1.name AS player1_name, p1.draw_no AS player1_draw,
               p1.group_name AS player1_group,
               p2.name AS player2_name, p2.draw_no AS player2_draw,
               p2.group_name AS player2_group
          FROM matches m
          LEFT JOIN players p1 ON p1.id = m.player1_id
          LEFT JOIN players p2 ON p2.id = m.player2_id
         WHERE m.id = ?
        """,
        (match_id,),
    ).fetchone()


def list_matches(
    conn: sqlite3.Connection,
    *,
    round_name: str | None = None,
    active_only: bool = False,
) -> list[sqlite3.Row]:
    sql = [
        """SELECT m.*, p1.name AS player1_name, p1.draw_no AS player1_draw,
                  p1.group_name AS player1_group,
                  p2.name AS player2_name, p2.draw_no AS player2_draw,
                  p2.group_name AS player2_group
             FROM matches m
             LEFT JOIN players p1 ON p1.id = m.player1_id
             LEFT JOIN players p2 ON p2.id = m.player2_id
            WHERE 1=1"""
    ]
    params: list[Any] = []
    if round_name:
        sql.append("AND m.round = ?")
        params.append(round_name)
    if active_only:
        sql.append("AND m.status = 'active'")
    sql.append("ORDER BY m.id DESC")
    return conn.execute(" ".join(sql), params).fetchall()


def active_semifinals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list_matches(conn, round_name="SF", active_only=True)


def table_map(conn: sqlite3.Connection) -> dict[int, sqlite3.Row | None]:
    rows = active_semifinals(conn)
    by_table: dict[int, sqlite3.Row | None] = {1: None, 2: None}
    for r in rows:
        if r["table_no"] in (1, 2):
            by_table[r["table_no"]] = r
    return by_table


def finalists_state(conn: sqlite3.Connection) -> dict:
    """只有“两场现行半决赛”且两场都有有效比分时，才给出决赛选手。"""
    sf = active_semifinals(conn)
    scored = [m for m in sf if winner_id(m) is not None]
    ready = len(sf) == 2 and len(scored) == 2

    winner_rows: list[dict] = []
    if ready:
        players = {p["id"]: p for p in list_players(conn)}
        for m in sorted(sf, key=lambda x: (x["table_no"] or 9, x["id"])):
            wid = winner_id(m)
            p = players.get(wid) if wid is not None else None
            winner_rows.append(
                {
                    "match_id": m["id"],
                    "table_no": m["table_no"],
                    "player": dict(p) if p else None,
                    "score1": m["score1"],
                    "score2": m["score2"],
                }
            )

    if ready:
        message = "两场现行半决赛均已登记比分，决赛选手已产生。"
    elif len(sf) < 2:
        message = f"当前只有 {len(sf)} 场现行半决赛（另有对阵已撤下/待重新配对），补齐两场并登记比分后才显示决赛选手。"
    else:
        message = f"两场现行半决赛中已有 {len(scored)} 场分出胜负，两场都有比分后才显示决赛选手。"

    return {
        "ready": ready,
        "count_active": len(sf),
        "count_scored": len(scored),
        "winners": winner_rows,
        "message": message,
    }


def final_match(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT m.*, p1.name AS player1_name, p2.name AS player2_name
             FROM matches m
             LEFT JOIN players p1 ON p1.id = m.player1_id
             LEFT JOIN players p2 ON p2.id = m.player2_id
            WHERE m.round = 'F' AND m.status = 'active'
            ORDER BY m.id DESC LIMIT 1"""
    ).fetchone()


def champion(conn: sqlite3.Connection) -> dict | None:
    f = final_match(conn)
    if f is None or winner_id(f) is None:
        return None
    wid = winner_id(f)
    p = conn.execute("SELECT * FROM players WHERE id = ?", (wid,)).fetchone()
    return {"player": dict(p) if p else None, "match_id": f["id"]}


def build_state(conn: sqlite3.Connection) -> dict:
    st = standings(conn)
    desired = desired_semifinals(st)
    return {
        "standings": st,
        "group_matches": list_group_matches(conn),
        "desired_semifinals": desired,
        "tables": table_map(conn),
        "active_semifinals": active_semifinals(conn),
        "finalists": finalists_state(conn),
        "final_match": final_match(conn),
        "champion": champion(conn),
        "all_matches": list_matches(conn),
    }


def match_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["winner_id"] = winner_id(row)
    return d


# --------------------------------------------------------------------------- #
# 小组赛：整表提交
# --------------------------------------------------------------------------- #

def save_group_scores(
    conn: sqlite3.Connection, entries: dict[int, tuple[Any, Any]]
) -> list[str]:
    """整表校验 + 原子保存六场比分，然后调和半决赛对阵。

    entries: {group_match_id: (raw1, raw2)}
    任一已填场次不合法 => 抛 ScoreError，数据库不变。
    """
    gm_rows = {r["id"]: r for r in conn.execute("SELECT * FROM group_matches")}
    parsed: dict[int, tuple[int | None, int | None]] = {}
    errors: list[str] = []

    for gm_id, row in gm_rows.items():
        raw = entries.get(gm_id, ("", ""))
        try:
            parsed[gm_id] = parse_score_pair(raw[0], raw[1])
        except ScoreError as exc:
            errors.append(f"第 {gm_id} 场（{row['group_name']}组）：{exc}")

    # 也检查调用方是否提交了不存在的场次 id
    for gm_id in entries:
        if gm_id not in gm_rows:
            errors.append(f"不存在的小组赛场次 id：{gm_id}")

    if errors:
        raise ScoreError("；".join(errors))

    ts = now_iso()
    # 校验已在上面全部完成；with conn 保证六场更新 + 对阵调和原子提交
    with conn:
        for gm_id, (s1, s2) in parsed.items():
            conn.execute(
                "UPDATE group_matches SET score1=?, score2=?, updated_at=? WHERE id=?",
                (s1, s2, ts if s1 is not None else None, gm_id),
            )
        reconcile_semifinals(conn)
        reconcile_final(conn)
    return []


# --------------------------------------------------------------------------- #
# 半决赛对阵调和（小组整表提交后“更新对阵”）
# --------------------------------------------------------------------------- #

def _insert_match(
    conn: sqlite3.Connection,
    *,
    round_name: str,
    code: str | None,
    table_no: int | None,
    p1: int,
    p2: int,
) -> int:
    cur = conn.execute(
        """INSERT INTO matches
           (round, bracket_code, table_no, player1_id, player2_id,
            status, pair_key, created_at)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
        (round_name, code, table_no, p1, p2, pair_key(p1, p2), now_iso()),
    )
    return int(cur.lastrowid)


def reconcile_semifinals(conn: sqlite3.Connection) -> None:
    """按最新排名把两条规则对阵与现行场次记录调和。

    - 同对球员仍在期望对阵中：保留同一条记录（比分、球台都保留），
      只更新 bracket_code；
    - 某条现行记录的 pair 已不在期望名单：
        * 从未记分且为自动生成的空壳 -> 直接删除，避免历史噪音；
        * 已记分 / 手动重配 -> 标记 withdrawn（lineup_update），比分可查；
      然后生成空白的新场次；
    - 新场次优先使用规则默认球台，若被保留场次占用则用另一张空台。
    """
    st = standings(conn)
    desired = desired_semifinals(st)
    if not desired:
        return

    active = conn.execute(
        "SELECT * FROM matches WHERE round='SF' AND status='active'"
    ).fetchall()
    by_pair = {m["pair_key"]: m for m in active}
    used_ids: set[int] = set()

    for d in desired:
        existing = by_pair.get(d["pair_key"])
        if existing is not None:
            used_ids.add(existing["id"])
            conn.execute(
                "UPDATE matches SET bracket_code=? WHERE id=?",
                (d["code"], existing["id"]),
            )

    # 下线不再符合对阵的现行场次
    for m in active:
        if m["id"] in used_ids:
            continue
        has_score = m["score1"] is not None or m["score2"] is not None
        manual_repair = m["bracket_code"] == "REPAIR"
        if has_score or manual_repair:
            conn.execute(
                """UPDATE matches
                      SET status='withdrawn', withdraw_reason='lineup_update',
                          withdrawn_at=?
                    WHERE id=?""",
                (now_iso(), m["id"]),
            )
        else:
            conn.execute("DELETE FROM matches WHERE id=?", (m["id"],))

    # 生成缺失对阵
    occupied_tables = {
        r["table_no"]
        for r in conn.execute(
            "SELECT table_no FROM matches WHERE round='SF' AND status='active'"
            " AND table_no IS NOT NULL"
        ).fetchall()
    }
    for d in desired:
        if d["pair_key"] in by_pair and by_pair[d["pair_key"]]["id"] in used_ids:
            continue
        table_no = d["default_table"]
        if table_no in occupied_tables:
            table_no = 2 if table_no == 1 else 1
        if table_no in occupied_tables:
            table_no = None
        _insert_match(
            conn,
            round_name="SF",
            code=d["code"],
            table_no=table_no,
            p1=d["player1_id"],
            p2=d["player2_id"],
        )
        if table_no is not None:
            occupied_tables.add(table_no)


def reconcile_final(conn: sqlite3.Connection) -> None:
    """决赛选手变化时调和唯一一条决赛记录（同样遵循身份/封存规则）。"""
    fs = finalists_state(conn)
    current = conn.execute(
        "SELECT * FROM matches WHERE round='F' AND status='active' ORDER BY id DESC"
    ).fetchall()

    if not fs["ready"]:
        # 选手尚未确定：未记分的空壳决赛记录删掉；记过分的封存
        for m in current:
            if m["score1"] is None and m["score2"] is None:
                conn.execute("DELETE FROM matches WHERE id=?", (m["id"],))
            else:
                conn.execute(
                    "UPDATE matches SET status='withdrawn',"
                    " withdraw_reason='lineup_update', withdrawn_at=? WHERE id=?",
                    (now_iso(), m["id"]),
                )
        return

    w1, w2 = fs["winners"][0]["player"]["id"], fs["winners"][1]["player"]["id"]
    key = pair_key(w1, w2)
    same = [m for m in current if m["pair_key"] == key]
    if same:
        return
    for m in current:
        if m["score1"] is None and m["score2"] is None:
            conn.execute("DELETE FROM matches WHERE id=?", (m["id"],))
        else:
            conn.execute(
                "UPDATE matches SET status='withdrawn',"
                " withdraw_reason='lineup_update', withdrawn_at=? WHERE id=?",
                (now_iso(), m["id"]),
            )
    _insert_match(
        conn, round_name="F", code="F1", table_no=None, p1=w1, p2=w2
    )


# --------------------------------------------------------------------------- #
# 比分登记 / 更正
# --------------------------------------------------------------------------- #

class MatchGoneError(Exception):
    """场次不存在或已撤下（旧页面交分被拒）。"""

    def __init__(self, message: str, *, withdrawn: bool = False, match=None):
        super().__init__(message)
        self.withdrawn = withdrawn
        self.match = match


def record_score(
    conn: sqlite3.Connection, match_id: int, raw1: Any, raw2: Any
) -> sqlite3.Row:
    m = get_match(conn, match_id)
    if m is None:
        raise MatchGoneError("场次不存在。")
    if m["status"] != "active":
        raise MatchGoneError(
            "这张页面上的对阵已经撤下，比分入口关闭。请刷新页面，按最新对阵记分。",
            withdrawn=True,
            match=m,
        )
    s1, s2 = parse_score_pair(raw1, raw2)
    if s1 is None:
        raise ScoreError("半决赛/决赛必须登记完整比分（两格都填）。")

    conn.execute(
        "UPDATE matches SET score1=?, score2=?, updated_at=? WHERE id=?",
        (s1, s2, now_iso(), match_id),
    )
    # 半决赛比分变化可能改变决赛选手
    if m["round"] == "SF":
        reconcile_final(conn)
    conn.commit()
    return get_match(conn, match_id)


# --------------------------------------------------------------------------- #
# 换台
# --------------------------------------------------------------------------- #

def swap_semifinal_tables(conn: sqlite3.Connection) -> bool:
    """对调两场现行半决赛的球台；比分挂在场次（这对球员）上，原样保留。"""
    rows = active_semifinals(conn)
    tabled = [m for m in rows if m["table_no"] in (1, 2)]
    if len(tabled) != 2:
        return False
    a, b = tabled[0], tabled[1]
    ta, tb = a["table_no"], b["table_no"]
    ts = now_iso()
    # 先同时腾空，避免部分唯一索引在两条 UPDATE 之间判定瞬时冲突
    conn.execute(
        "UPDATE matches SET table_no=NULL, updated_at=? WHERE id IN (?, ?)",
        (ts, a["id"], b["id"]),
    )
    conn.execute(
        "UPDATE matches SET table_no=?, updated_at=? WHERE id=?",
        (tb, ts, a["id"]),
    )
    conn.execute(
        "UPDATE matches SET table_no=?, updated_at=? WHERE id=?",
        (ta, ts, b["id"]),
    )
    conn.commit()
    return True


def move_to_table(conn: sqlite3.Connection, match_id: int, table_no: int) -> None:
    m = get_match(conn, match_id)
    if m is None or m["status"] != "active":
        raise MatchGoneError("场次不存在或已撤下，不能换台。", withdrawn=bool(m))
    if table_no not in (1, 2):
        raise ScoreError("球台编号只能是 1 或 2。")
    other = conn.execute(
        "SELECT id FROM matches WHERE round=? AND status='active'"
        " AND table_no=? AND id<>?",
        (m["round"], table_no, match_id),
    ).fetchone()
    if other is not None:
        raise ScoreError(f"{table_no} 号台还有另一场现行对阵，请先对调或撤下。")
    conn.execute(
        "UPDATE matches SET table_no=?, updated_at=? WHERE id=?",
        (table_no, now_iso(), match_id),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# 撤下 / 重新配对
# --------------------------------------------------------------------------- #

def withdraw_match(
    conn: sqlite3.Connection, match_id: int, reason: str = "manual"
) -> sqlite3.Row | None:
    m = get_match(conn, match_id)
    if m is None:
        return None
    if m["status"] == "active":
        conn.execute(
            "UPDATE matches SET status='withdrawn', withdraw_reason=?,"
            " withdrawn_at=?, updated_at=? WHERE id=?",
            (reason, now_iso(), now_iso(), match_id),
        )
        conn.commit()
        if m["round"] == "SF":
            reconcile_final(conn)
            conn.commit()
    return get_match(conn, match_id)


class DuplicateActivePair(Exception):
    def __init__(self, existing_id: int):
        self.existing_id = existing_id
        super().__init__(f"这两名球员已有现行场次 #{existing_id}。")


def create_repair(
    conn: sqlite3.Connection,
    *,
    player1_id: int,
    player2_id: int,
    table_no: int | None = None,
    source_id: int | None = None,
    round_name: str = "SF",
) -> int:
    """让两人重新配到一起：一定创建空白新场次，绝不继承旧比分。"""
    if int(player1_id) == int(player2_id):
        raise ScoreError("重新配对需要选择两名不同的球员。")
    players = {p["id"] for p in list_players(conn)}
    if int(player1_id) not in players or int(player2_id) not in players:
        raise ScoreError("请选择两名内置球员。")

    key = pair_key(player1_id, player2_id)
    dup = conn.execute(
        "SELECT id FROM matches WHERE round=? AND status='active' AND pair_key=?",
        (round_name, key),
    ).fetchone()
    if dup is not None:
        raise DuplicateActivePair(dup["id"])

    # 球台选择：指定台必须为空；未指定则用源场次旧台（若空）或第一张空台；
    # 两张台都满时拒绝再建，保证“两场现行半决赛”的业务不变量。
    occupied = {
        r["table_no"]
        for r in conn.execute(
            "SELECT table_no FROM matches WHERE round=? AND status='active'"
            " AND table_no IS NOT NULL",
            (round_name,),
        ).fetchall()
    }
    source = get_match(conn, source_id) if source_id else None
    chosen: int | None = None
    if table_no in (1, 2):
        if int(table_no) in occupied:
            raise ScoreError(f"{table_no} 号台已有现行对阵，请先撤下该场或使用对调。")
        chosen = int(table_no)
    elif source is not None and source["table_no"] in (1, 2) \
            and source["table_no"] not in occupied:
        chosen = source["table_no"]
    else:
        for t in (1, 2):
            if t not in occupied:
                chosen = t
                break
        if chosen is None and round_name == "SF":
            raise ScoreError("两张球台都有现行半决赛，请先撤下其中一场再重新配对。")

    # 重配场次 bracket_code 置空、withdraw_reason 借用为来源标记不合适，
    # 用 bracket_code='REPAIR' 表明它是人工重配（调和时优先封存而非删除）。
    new_id = _insert_match(
        conn,
        round_name=round_name,
        code="REPAIR",
        table_no=chosen,
        p1=int(player1_id),
        p2=int(player2_id),
    )
    conn.commit()
    reconcile_final(conn)
    conn.commit()
    return new_id


def repair_from_withdrawn(conn: sqlite3.Connection, source_id: int,
                          table_no: int | None = None) -> int:
    src = get_match(conn, source_id)
    if src is None or src["player1_id"] is None or src["player2_id"] is None:
        raise MatchGoneError("原对阵不存在，无法原样重配。")
    return create_repair(
        conn,
        player1_id=src["player1_id"],
        player2_id=src["player2_id"],
        table_no=table_no,
        source_id=source_id,
        round_name=src["round"],
    )
