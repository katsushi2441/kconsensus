#!/usr/bin/env python3
"""Kurage 合意点マップ — 賛否を集めて、意見の割れ方と、それでも一致する点を出す。

Pol.is がやっている「合意点の発見」を自前で実装したもの（本体は AGPL-3.0 なので
fork せず、同じ筋を MIT で書いた。計算は app/polis.py）。

**画面に出すのは、実在の発言と、人が実際に押した票だけ。**
- 論点は、国会会議録の実発言と、kmontage news が集めたニュースへの反応から立てる。
  AI がするのは言い換えだけで、意見の中身は作らない（scripts/build_topic.py）。
- 票が足りないときは、数字を出さずに「足りない」と書く。推測で埋めない。
- 合成票は検証（tests/）にしか使わない。公開画面には出さない。
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .polis import MIN_VOTERS_FOR_GROUPS, MIN_VOTES_PER_STATEMENT, analyze
from .store import connect, init_db

HERE = Path(__file__).resolve().parent
PUBLIC = os.environ.get("KCONSENSUS_PUBLIC_BASE", "https://kurage.exbridge.jp/kconsensus.php/")
app = FastAPI(title="Kurage 合意点マップ")
init_db()


class Ballot(BaseModel):
    statement_id: int
    value: int          # 賛成=1 / 反対=-1 / 保留=0


def _voter(response: Response, kc_voter: str | None) -> str:
    """cookie の乱数だけで数える。氏名も IP も持たない。"""
    if kc_voter and len(kc_voter) >= 16:
        return kc_voter
    v = secrets.token_urlsafe(16)
    response.set_cookie("kc_voter", v, max_age=60 * 60 * 24 * 365,
                        samesite="lax", httponly=False)
    return v


def _topic(slug: str):
    c = connect()
    t = c.execute("SELECT * FROM topic WHERE slug=?", (slug,)).fetchone()
    c.close()
    if not t:
        raise HTTPException(404, "その論点はありません")
    return dict(t)


@app.get("/")
def index():
    return FileResponse(HERE / "templates" / "index.html")


@app.get("/style.css")
def style():
    return FileResponse(HERE / "templates" / "style.css", media_type="text/css")


@app.get("/api/topics")
def api_topics():
    c = connect()
    rows = [dict(r) for r in c.execute(
        """SELECT t.slug, t.name, t.lead,
                  (SELECT COUNT(*) FROM statement s WHERE s.topic_id=t.id) statements,
                  (SELECT COUNT(*) FROM vote v JOIN statement s ON s.id=v.statement_id
                    WHERE s.topic_id=t.id) votes
           FROM topic t ORDER BY t.updated_at DESC""")]
    c.close()
    return {"topics": rows}


@app.get("/api/topic/{slug}")
def api_topic(slug: str, response: Response, kc_voter: str | None = Cookie(default=None)):
    t = _topic(slug)
    voter = _voter(response, kc_voter)
    c = connect()
    stmts = [dict(r) for r in c.execute(
        "SELECT id, text, origin, basis FROM statement WHERE topic_id=? ORDER BY id", (t["id"],))]
    mine = {r["statement_id"]: r["value"] for r in c.execute(
        """SELECT v.statement_id, v.value FROM vote v JOIN statement s ON s.id=v.statement_id
           WHERE s.topic_id=? AND v.voter=?""", (t["id"], voter))}
    ev = [dict(r) for r in c.execute(
        "SELECT kind, date, who, affiliation, body, url FROM evidence WHERE topic_id=? ORDER BY kind, date DESC",
        (t["id"],))]
    c.close()
    for s in stmts:
        s["my_vote"] = mine.get(s["id"])
    return {"topic": {k: t[k] for k in ("slug", "name", "lead", "tracker")},
            "statements": stmts, "evidence": ev,
            "voted": len(mine), "total": len(stmts)}


@app.post("/api/topic/{slug}/vote")
def api_vote(slug: str, b: Ballot, response: Response, kc_voter: str | None = Cookie(default=None)):
    if b.value not in (-1, 0, 1):
        return JSONResponse({"error": "賛成(1)・反対(-1)・保留(0) のどれかを送ってください"},
                            status_code=400)
    t = _topic(slug)
    voter = _voter(response, kc_voter)
    c = connect()
    own = c.execute("SELECT 1 FROM statement WHERE id=? AND topic_id=?",
                    (b.statement_id, t["id"])).fetchone()
    if not own:
        c.close()
        return JSONResponse({"error": "その論点にその設問はありません"}, status_code=400)
    with c:
        c.execute("INSERT INTO vote (statement_id, voter, value, created_at) "
                  "VALUES (?,?,?,datetime('now')) "
                  "ON CONFLICT(statement_id, voter) DO UPDATE SET value=excluded.value",
                  (b.statement_id, voter, b.value))
        n = c.execute("""SELECT COUNT(*) FROM vote v JOIN statement s ON s.id=v.statement_id
                         WHERE s.topic_id=? AND v.voter=?""", (t["id"], voter)).fetchone()[0]
    c.close()
    return {"ok": True, "voted": n}


@app.get("/api/topic/{slug}/result")
def api_result(slug: str):
    t = _topic(slug)
    c = connect()
    ids = [r[0] for r in c.execute("SELECT id FROM statement WHERE topic_id=? ORDER BY id", (t["id"],))]
    texts = {r["id"]: dict(r) for r in c.execute(
        "SELECT id, text, origin, basis FROM statement WHERE topic_id=?", (t["id"],))}
    votes = [(r["voter"], r["statement_id"], r["value"]) for r in c.execute(
        """SELECT v.voter, v.statement_id, v.value FROM vote v
           JOIN statement s ON s.id=v.statement_id WHERE s.topic_id=?""", (t["id"],))]
    c.close()
    vmap = {v: i for i, v in enumerate(sorted({v for v, _, _ in votes}))}
    a = analyze([(vmap[v], s, val) for v, s, val in votes], statement_ids=ids)
    out = []
    for s in a.statements:
        d = texts.get(s.statement_id, {})
        out.append({
            "id": s.statement_id, "text": d.get("text", ""), "origin": d.get("origin"),
            "basis": d.get("basis"), "votes": s.votes, "agree": s.agree,
            "disagree": s.disagree, "pass": s.pass_, "agree_rate": s.agree_rate,
            "consensus": s.consensus, "divisive": s.divisive, "note": s.note,
            "by_group": [{"group": g.group, "n": g.n, "agree": None if g.agree != g.agree else g.agree,
                          "votes": g.votes} for g in s.by_group],
        })
    scored = [x for x in out if x["consensus"] is not None]
    return {
        "topic": {k: t[k] for k in ("slug", "name", "lead")},
        "n_voters": a.n_voters, "n_votes": a.n_votes, "n_groups": a.n_groups,
        "note": a.note, "statements": out,
        "consensus_top": sorted(scored, key=lambda x: -x["consensus"])[:5],
        "divisive_top": sorted(scored, key=lambda x: -(x["divisive"] or 0))[:5],
        "thresholds": {"voters_for_groups": MIN_VOTERS_FOR_GROUPS,
                       "votes_per_statement": MIN_VOTES_PER_STATEMENT},
    }


@app.get("/healthz")
def healthz():
    c = connect()
    n = c.execute("SELECT COUNT(*) FROM vote").fetchone()[0]
    c.close()
    return {"ok": True, "votes": n}


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return f"User-agent: *\nAllow: /\nSitemap: {PUBLIC}sitemap.xml\n"


@app.get("/sitemap.xml")
def sitemap():
    c = connect()
    slugs = [r[0] for r in c.execute("SELECT slug FROM topic ORDER BY slug")]
    c.close()
    urls = [""] + [f"t/{s}/" for s in slugs]
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{PUBLIC}{u}</loc></url>" for u in urls) + "</urlset>")
    return PlainTextResponse(body, media_type="application/xml")


@app.get("/t/{slug}/", response_class=HTMLResponse)
def topic_page(slug: str):
    _topic(slug)
    return FileResponse(HERE / "templates" / "topic.html")


@app.get("/api/topic/{slug}/reading")
def api_reading(slug: str):
    """ニュースへの反応から**読み取った**賛否。人が押した票とは別に返す。

    これは推定であって投票ではない。画面でも必ず分けて出し、
    判定のもとになったコメント本文を添える（読み手が自分で確かめられるように）。
    """
    t = _topic(slug)
    c = connect()
    ids = [r[0] for r in c.execute("SELECT id FROM statement WHERE topic_id=? ORDER BY id", (t["id"],))]
    texts = {r["id"]: dict(r) for r in c.execute(
        "SELECT id, text, origin FROM statement WHERE topic_id=?", (t["id"],))}
    try:
        rows = [(r["reaction_id"], r["statement_id"], r["value"]) for r in c.execute(
            """SELECT rs.reaction_id, rs.statement_id, rs.value FROM reaction_stance rs
               JOIN reaction r ON r.id = rs.reaction_id
               WHERE r.topic_id=? AND rs.value != 0""", (t["id"],))]
        n_react = c.execute("SELECT COUNT(*) FROM reaction WHERE topic_id=?", (t["id"],)).fetchone()[0]
        n_read = c.execute(
            """SELECT COUNT(*) FROM reaction_stance rs JOIN reaction r ON r.id = rs.reaction_id
               WHERE r.topic_id=?""", (t["id"],)).fetchone()[0]
        samples = {}
        for r in c.execute(
            """SELECT rs.statement_id, rs.value, r.text, r.author, r.platform, r.empathy, r.negative, r.url
               FROM reaction_stance rs JOIN reaction r ON r.id = rs.reaction_id
               WHERE r.topic_id=? AND rs.value != 0 ORDER BY r.empathy DESC""", (t["id"],)):
            k = (r["statement_id"], r["value"])
            if len(samples.get(k, [])) < 2:
                samples.setdefault(k, []).append(
                    {"text": r["text"], "author": r["author"], "platform": r["platform"],
                     "empathy": r["empathy"], "negative": r["negative"], "url": r["url"]})
    except Exception:      # noqa: BLE001  取り込み前は表が無い
        c.close()
        return {"ready": False, "note": "反応の読み取りはまだ取り込んでいません"}
    c.close()
    if not rows:
        return {"ready": False, "note": "反応の読み取りはまだ取り込んでいません",
                "reactions": n_react, "judged": n_read}
    a = analyze(rows, statement_ids=ids)
    out = []
    for s in a.statements:
        d = texts.get(s.statement_id, {})
        out.append({"id": s.statement_id, "text": d.get("text", ""), "origin": d.get("origin"),
                    "read": s.votes, "agree": s.agree, "disagree": s.disagree,
                    "agree_rate": s.agree_rate, "consensus": s.consensus, "divisive": s.divisive,
                    "note": s.note,
                    "samples": {"agree": samples.get((s.statement_id, 1), []),
                                "disagree": samples.get((s.statement_id, -1), [])}})
    scored = [x for x in out if x["consensus"] is not None]
    return {
        "ready": True, "reactions": n_react, "judged": n_read,
        "n_readers": a.n_voters, "n_stances": a.n_votes, "n_groups": a.n_groups, "note": a.note,
        "statements": out,
        "consensus_top": sorted(scored, key=lambda x: -x["consensus"])[:5],
        "divisive_top": sorted(scored, key=lambda x: -(x["divisive"] or 0))[:5],
        "accuracy": {"judge": "gemma4:12b-it-qat",
                     "measured": "手で正解を付けた20組で 15〜17/20（75〜85%）",
                     "compared": "jevlocal は 9〜10/20（45〜50%）で採用せず"},
    }
