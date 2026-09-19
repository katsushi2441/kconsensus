"""合意点の計算が正しいかを、答えが分かっている合成票で確かめる。

**この合成票は検証にしか使わない。** 公開画面には、実在の発言と、人が実際に
押した票しか出さない（AIや乱数が作った「世論」を画面に出さないため）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.polis import MIN_VOTERS_FOR_GROUPS, analyze


def build(n_per_group: int = 12) -> list[tuple[int, int, int]]:
    """2つの陣営を作る。
      論点0: 陣営Aだけが賛成（対立点）
      論点1: 陣営Bだけが賛成（対立点）
      論点2: 両陣営が賛成（これが合意点。これを見つけられるかが試験）
      論点3: 両陣営が反対
    """
    votes = []
    vid = 0
    for _ in range(n_per_group):          # 陣営A
        votes += [(vid, 0, 1), (vid, 1, -1), (vid, 2, 1), (vid, 3, -1)]
        vid += 1
    for _ in range(n_per_group):          # 陣営B
        votes += [(vid, 0, -1), (vid, 1, 1), (vid, 2, 1), (vid, 3, -1)]
        vid += 1
    return votes


def test_finds_two_groups():
    a = analyze(build())
    assert a.n_groups == 2, f"2グループに分かれるはず: {a.n_groups}"
    assert a.n_voters == 24


def test_consensus_statement_wins():
    a = analyze(build())
    by = {s.statement_id: s for s in a.statements}
    # 論点2 だけが「どちらの陣営から見ても賛成」なので、合意スコアが最大になる
    assert by[2].consensus == 1.0
    assert by[0].consensus == 0.0 and by[1].consensus == 0.0
    top = max(a.statements, key=lambda s: (s.consensus if s.consensus is not None else -1))
    assert top.statement_id == 2, "合意点として論点2が選ばれるはず"


def test_divisive_statements_flagged():
    a = analyze(build())
    by = {s.statement_id: s for s in a.statements}
    assert by[0].divisive == 1.0 and by[1].divisive == 1.0
    assert by[2].divisive == 0.0, "全員が賛成した文は対立点ではない"


def test_too_few_voters_says_so():
    """人数が足りないときに、当てずっぽうでグループを作らないこと。"""
    votes = build(n_per_group=2)          # 4人
    a = analyze(votes)
    assert a.n_groups is None
    assert "分けられません" in a.note
    assert str(MIN_VOTERS_FOR_GROUPS) in a.note


def test_statement_without_votes_is_not_scored():
    a = analyze(build(), statement_ids=[0, 1, 2, 3, 99])
    by = {s.statement_id: s for s in a.statements}
    assert by[99].votes == 0 and by[99].consensus is None
    assert "票がありません" in by[99].note


def test_no_votes_at_all():
    a = analyze([], statement_ids=[1, 2])
    assert a.n_voters == 0 and a.n_groups is None
    assert "まだ票がありません" in a.note


def test_three_groups_with_noise():
    """雑音（15%が気まぐれに押す）と保留を混ぜ、未投票も作った3陣営。

    きれいな合成票だけで通しても意味がないので、現実に近い形でも
    合意点が浮かぶかを見る。
    """
    import random
    rng = random.Random(20260919)
    # 論点5 だけ全陣営が賛成。論点0〜2 は陣営ごとに割れる。論点3,4 は全員反対。
    base = {
        0: [1, -1, -1], 1: [-1, 1, -1], 2: [-1, -1, 1],
        3: [-1, -1, -1], 4: [-1, -1, -1], 5: [1, 1, 1],
    }
    votes, vid = [], 0
    for g in range(3):
        for _ in range(14):
            for sid, vals in base.items():
                if rng.random() < 0.12:        # 未投票
                    continue
                v = vals[g]
                if rng.random() < 0.15:        # 気まぐれ
                    v = rng.choice([1, -1, 0])
                votes.append((vid, sid, v))
            vid += 1
    a = analyze(votes)
    assert a.n_groups is not None and a.n_groups >= 2, f"グループが出ない: {a.n_groups}"
    top = max(a.statements, key=lambda s: (s.consensus if s.consensus is not None else -1))
    assert top.statement_id == 5, f"合意点は論点5のはず: {top.statement_id}"
    by = {s.statement_id: s for s in a.statements}
    assert by[5].divisive < 0.35, f"合意点の対立スコアが高すぎる: {by[5].divisive}"
    most = max(a.statements, key=lambda s: (s.divisive if s.divisive is not None else -1))
    assert most.statement_id in (0, 1, 2), f"対立点は0〜2のはず: {most.statement_id}"


def test_no_votes_still_lists_statements():
    """票が0でも論点は返す（画面が「論点が無い」ように見えないこと）。"""
    a = analyze([], statement_ids=[7, 8, 9])
    assert [s.statement_id for s in a.statements] == [7, 8, 9]
    assert all(s.votes == 0 and s.note == "票がありません" for s in a.statements)
