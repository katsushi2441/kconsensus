"""Polis の合意点発見を自前で実装する。

**なぜ fork しないか**: Pol.is 本体は AGPL-3.0 で、売り物に取り込むとこちらも
AGPL に縛られる。アルゴリズム自体は公開されているので、同じことを MIT で書く。

やっていること（Pol.is と同じ筋）:

1. 賛否行列を作る（人 × 論点。賛成=+1 / 反対=-1 / 保留=0 / 未投票=欠測）
2. 欠測を列平均で埋めて中心化し、PCA で2次元に落とす
3. k-means で意見グループに分ける（k はシルエット係数で2〜5から選ぶ）
4. 各論点について、**グループをまたいだ賛成の低いほう**を合意スコアにする
   （どこか1グループだけが強く賛成している文は合意点ではない）
5. 対立スコアは、グループ間の賛成率の開きで測る

**推測しない**: 票が少ない論点はスコアを出さず「票が足りない」と返す。
グループ分けも、人数が足りなければ「分けられない」と返す。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# この人数・票数を下回るときは、数字を出さずに「足りない」と言う。
MIN_VOTERS_FOR_GROUPS = 8      # これ未満でグループ分けすると、1人が1グループになる
MIN_VOTES_PER_STATEMENT = 5    # 1論点あたりの最低票数
MIN_VOTES_PER_VOTER = 3        # 3問も押していない人は意見の型が取れない


@dataclass
class GroupStat:
    group: int
    n: int
    agree: float          # その論点への賛成率（保留・未投票を除く）
    votes: int


@dataclass
class StatementResult:
    statement_id: int
    votes: int
    agree: int
    disagree: int
    pass_: int
    agree_rate: float | None          # 全体の賛成率
    consensus: float | None           # 合意スコア（グループ横断の最小賛成率）
    divisive: float | None            # 対立スコア（グループ間の賛成率の開き）
    by_group: list[GroupStat] = field(default_factory=list)
    note: str = ""


@dataclass
class Analysis:
    n_voters: int
    n_statements: int
    n_votes: int
    n_groups: int | None
    groups: dict[int, int] = field(default_factory=dict)   # voter_id -> group
    statements: list[StatementResult] = field(default_factory=list)
    note: str = ""


def _matrix(votes: list[tuple[int, int, int]]) -> tuple[np.ndarray, list[int], list[int]]:
    """(voter_id, statement_id, value) の並びを行列にする。未投票は NaN。"""
    voters = sorted({v for v, _, _ in votes})
    stmts = sorted({s for _, s, _ in votes})
    vi = {v: i for i, v in enumerate(voters)}
    si = {s: i for i, s in enumerate(stmts)}
    m = np.full((len(voters), len(stmts)), np.nan)
    for v, s, val in votes:
        m[vi[v], si[s]] = val
    return m, voters, stmts


def _kmeans(x: np.ndarray, k: int, seed: int = 0, iters: int = 60) -> np.ndarray:
    """小さな k-means。scikit-learn を入れないために自前で持つ。"""
    rng = np.random.default_rng(seed)
    # k-means++ の初期化（1点目は乱択、以降は距離の二乗に比例して選ぶ）
    centers = [x[rng.integers(len(x))]]
    for _ in range(k - 1):
        d = np.min([((x - c) ** 2).sum(axis=1) for c in centers], axis=0)
        total = d.sum()
        centers.append(x[rng.integers(len(x))] if total <= 0
                       else x[rng.choice(len(x), p=d / total)])
    c = np.array(centers)
    labels = np.zeros(len(x), dtype=int)
    for _ in range(iters):
        d = ((x[:, None, :] - c[None, :, :]) ** 2).sum(axis=2)
        new = d.argmin(axis=1)
        if (new == labels).all():
            break
        labels = new
        for j in range(k):
            if (labels == j).any():
                c[j] = x[labels == j].mean(axis=0)
    return labels


def _silhouette(x: np.ndarray, labels: np.ndarray) -> float:
    """シルエット係数。k を決めるためだけに使う。"""
    uniq = np.unique(labels)
    if len(uniq) < 2:
        return -1.0
    d = np.sqrt(((x[:, None, :] - x[None, :, :]) ** 2).sum(axis=2))
    out = []
    for i in range(len(x)):
        same = labels == labels[i]
        same[i] = False
        if not same.any():
            continue
        a = d[i, same].mean()
        b = min(d[i, labels == g].mean() for g in uniq if g != labels[i])
        out.append((b - a) / max(a, b) if max(a, b) > 0 else 0.0)
    return float(np.mean(out)) if out else -1.0


def analyze(votes: list[tuple[int, int, int]], statement_ids: list[int] | None = None) -> Analysis:
    """票から意見グループと、合意点・対立点を出す。

    votes の value は 賛成=1 / 反対=-1 / 保留=0。
    """
    all_ids = sorted(set(statement_ids or []) | {s for _, s, _ in votes})
    if not votes:
        # 票が無くても論点は返す。画面が「論点が1つも無い」ように見えてしまうため。
        return Analysis(
            0, len(all_ids), 0, None, note="まだ票がありません",
            statements=[StatementResult(i, 0, 0, 0, 0, None, None, None, note="票がありません")
                        for i in all_ids])

    m, voters, stmts = _matrix(votes)
    # 押した数が少なすぎる人は、意見の型が取れないので行列から外す（票の集計には残す）
    keep = np.array([np.count_nonzero(~np.isnan(m[i])) >= MIN_VOTES_PER_VOTER
                     for i in range(len(voters))])
    res = Analysis(n_voters=len(voters), n_statements=len(all_ids), n_votes=len(votes), n_groups=None)

    labels = None
    if keep.sum() >= MIN_VOTERS_FOR_GROUPS:
        mk = m[keep]
        col = np.nanmean(mk, axis=0)
        col = np.where(np.isnan(col), 0.0, col)
        filled = np.where(np.isnan(mk), col, mk)
        centered = filled - filled.mean(axis=0)
        # PCA（共分散行列の固有ベクトル上位2本）。列が1本しかないときは分けない
        if centered.shape[1] >= 2:
            u, s, vt = np.linalg.svd(centered, full_matrices=False)
            xy = u[:, :2] * s[:2]
            best, best_k, best_labels = -2.0, None, None
            for k in range(2, min(5, keep.sum()) + 1):
                lab = _kmeans(xy, k, seed=k)
                if len(np.unique(lab)) < k:
                    continue
                sc = _silhouette(xy, lab)
                if sc > best:
                    best, best_k, best_labels = sc, k, lab
            if best_labels is not None:
                labels = best_labels
                res.n_groups = best_k
                kept_voters = [v for v, k_ in zip(voters, keep) if k_]
                res.groups = {v: int(g) for v, g in zip(kept_voters, labels)}
    if res.n_groups is None:
        res.note = (f"意見グループはまだ分けられません（3問以上押した人が{int(keep.sum())}人。"
                    f"{MIN_VOTERS_FOR_GROUPS}人から分けます）")

    vi = {v: i for i, v in enumerate(voters)}
    si = {s: i for i, s in enumerate(stmts)}
    for sid in all_ids:
        if sid not in si:
            res.statements.append(StatementResult(sid, 0, 0, 0, 0, None, None, None, note="票がありません"))
            continue
        col = m[:, si[sid]]
        agree = int(np.nansum(col == 1))
        disagree = int(np.nansum(col == -1))
        pass_ = int(np.nansum(col == 0))
        n = agree + disagree + pass_
        r = StatementResult(sid, n, agree, disagree, pass_, None, None, None)
        if n < MIN_VOTES_PER_STATEMENT:
            r.note = f"票が足りません（{n}票。{MIN_VOTES_PER_STATEMENT}票から出します）"
            res.statements.append(r)
            continue
        decided = agree + disagree
        r.agree_rate = (agree / decided) if decided else None
        if labels is not None and r.agree_rate is not None:
            kept_voters = [v for v, k_ in zip(voters, keep) if k_]
            rates = []
            for g in sorted(set(labels.tolist())):
                members = [v for v, lg in zip(kept_voters, labels) if lg == g]
                vals = [m[vi[v], si[sid]] for v in members]
                vals = [x for x in vals if not np.isnan(x)]
                a = sum(1 for x in vals if x == 1)
                d_ = sum(1 for x in vals if x == -1)
                rate = (a / (a + d_)) if (a + d_) else None
                r.by_group.append(GroupStat(int(g), len(members), rate if rate is not None else float("nan"), len(vals)))
                if rate is not None:
                    rates.append(rate)
            if len(rates) >= 2:
                # 合意点 = どのグループから見ても賛成が高い文。だから最小値で測る。
                r.consensus = float(min(rates))
                r.divisive = float(max(rates) - min(rates))
        res.statements.append(r)
    return res
