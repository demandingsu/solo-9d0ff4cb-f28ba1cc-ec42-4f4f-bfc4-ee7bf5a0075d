"""不含 IO 的赛制规则：排名、半决赛对阵、比分校验。"""
from __future__ import annotations

from typing import Any, Iterable

GROUPS = ("A", "B")

# 比分合法范围（允许填局分或小分，裁判自行统一口径）
MIN_SCORE = 0
MAX_SCORE = 999


class ScoreError(ValueError):
    """比分录入不合法。"""


def pair_key(player1_id: int, player2_id: int) -> str:
    a, b = sorted((int(player1_id), int(player2_id)))
    return f"{a}-{b}"


def parse_score_pair(raw1: Any, raw2: Any) -> tuple[int | None, int | None]:
    """解析一对比分。

    两边都空白 => (None, None) 表示未赛；
    只填一边 / 非整数 / 越界 / 相等 => ScoreError。
    """
    def _blank(v: Any) -> bool:
        return v is None or (isinstance(v, str) and v.strip() == "")

    if _blank(raw1) and _blank(raw2):
        return None, None
    if _blank(raw1) or _blank(raw2):
        raise ScoreError("双方比分必须同时填写；尚未比赛请把两格都留空。")

    try:
        s1 = int(str(raw1).strip())
        s2 = int(str(raw2).strip())
    except (TypeError, ValueError):
        raise ScoreError("比分必须是整数。")

    for s in (s1, s2):
        if s < MIN_SCORE or s > MAX_SCORE:
            raise ScoreError(f"比分需在 {MIN_SCORE}–{MAX_SCORE} 之间。")
    if s1 == s2:
        raise ScoreError("乒乓球比赛必须分出胜负，双方比分不能相同。")
    return s1, s2


def compute_standings(
    players: Iterable[Any], group_matches: Iterable[Any], group_name: str
) -> list[dict]:
    """计算某组排名。

    排名规则（严格按需求）：
      1. 胜场数从多到少；
      2. 胜场相同，抽签号从小到大。
    不引入小分/胜负关系等额外排名规则。
    """
    players = [p for p in players if p["group_name"] == group_name]
    stats: dict[int, dict] = {
        p["id"]: {
            "id": p["id"],
            "name": p["name"],
            "group_name": p["group_name"],
            "draw_no": p["draw_no"],
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "played": 0,
            "points_for": 0,
            "points_against": 0,
        }
        for p in players
    }

    for m in group_matches:
        if m["group_name"] != group_name:
            continue
        s1 = m["score1"]
        s2 = m["score2"]
        if s1 is None or s2 is None:
            continue
        a = stats[m["player1_id"]]
        b = stats[m["player2_id"]]
        a["played"] += 1
        b["played"] += 1
        a["points_for"] += s1
        a["points_against"] += s2
        b["points_for"] += s2
        b["points_against"] += s1
        if s1 > s2:
            a["wins"] += 1
            b["losses"] += 1
        elif s2 > s1:
            b["wins"] += 1
            a["losses"] += 1
        else:
            # 正常入口不允许平分；保留兜底统计
            a["draws"] += 1
            b["draws"] += 1

    ranked = sorted(stats.values(), key=lambda r: (-r["wins"], r["draw_no"], r["id"]))
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx
    return ranked


def desired_semifinals(standings: dict[str, list[dict]]) -> list[dict]:
    """规则对阵：一号台 A1 对 B2；二号台 B1 对 A2。"""
    a = standings["A"]
    b = standings["B"]
    if len(a) < 2 or len(b) < 2:
        return []
    a1, a2 = a[0], a[1]
    b1, b2 = b[0], b[1]
    return [
        {
            "code": "SF1",
            "default_table": 1,
            "player1_id": a1["id"],
            "player2_id": b2["id"],
            "rule": "A1 对 B2",
            "rule_detail": f"A组第1（{a1['name']}） 对 B组第2（{b2['name']}）",
            "pair_key": pair_key(a1["id"], b2["id"]),
        },
        {
            "code": "SF2",
            "default_table": 2,
            "player1_id": b1["id"],
            "player2_id": a2["id"],
            "rule": "B1 对 A2",
            "rule_detail": f"B组第1（{b1['name']}） 对 A组第2（{a2['name']}）",
            "pair_key": pair_key(b1["id"], a2["id"]),
        },
    ]


def winner_id(match: Any) -> int | None:
    """比分为两个不等的非空整数时才有胜者。"""
    s1 = match["score1"] if "score1" in match.keys() else None  # noqa: SIM118
    s2 = match["score2"] if "score2" in match.keys() else None
    p1 = match["player1_id"]
    p2 = match["player2_id"]
    if s1 is None or s2 is None or p1 is None or p2 is None:
        return None
    if s1 == s2:
        return None
    return p1 if s1 > s2 else p2
