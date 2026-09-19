#!/usr/bin/env python3
"""ニュースに実際に寄せられた反応を取り込み、論点ごとの賛否を読み取る。

**意見は作らない。** コメントの中身は実在のもので、ここでするのは
「この人はこの論点に賛成か反対か、触れていないか」の読み取り（分類）だけ。

**読み取りは推定なので、人が押した票とは別の表に入れる**（reaction_stance）。
画面でも別の欄に出し、必ず元のコメント本文を添えて、読み手が確かめられるようにする。

判定器の選定（2026-09-19・手で正解を付けた20組で実測）:
- jevlocal（Qwen3.5-4B 4bit・87ms）: 45〜50%。3択の当てずっぽう33%とほぼ変わらず、
  disagree に張り付いた。**位置の偏りではない**（並べ替えても80%同じ答え）。
  Jev は短い状態からの即断には効くが、長文の読解には乗らない。
- gemma4:12b-it-qat を1件ずつ: **75〜85%**。これを採用。
- gemma4 でまとめて聞く（1コメントで全論点）: 55〜65%。none に逃げて行列がスカスカになる。

  /usr/bin/python3 scripts/import_reactions.py --slug shohizei --words 消費税 減税
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.store import connect, init_db, init_reactions  # noqa: E402

KMONTAGE_JOBS = os.environ.get("KCONSENSUS_KMONTAGE_JOBS", "/home/kojima/work/kmontage/storage/jobs")
OLLAMA = os.environ.get("KCONSENSUS_OLLAMA", "http://192.168.0.3:11434")
MODEL = os.environ.get("KCONSENSUS_MODEL", "gemma4:12b-it-qat")

CRIT = {
    "agree": "このコメントを書いた人は、その主張に賛成している。同じ方向のことを言っている",
    "disagree": "このコメントを書いた人は、その主張に反対している。逆の方向のことを言っている",
    "none": "このコメントは、その主張について賛成とも反対とも言っていない。触れていない",
}


def judge(comment: str, statement: str) -> str:
    p = (f"主張: {statement}\n\nコメント: {comment}\n\n"
         "このコメントを書いた人は、上の主張に賛成していますか、反対していますか、触れていませんか。\n"
         + "\n".join(f"- {k}: {v}" for k, v in CRIT.items())
         + "\n\nはっきり読み取れないときは none。答えの語だけを1つ出力してください。")
    body = json.dumps({"model": MODEL, "prompt": p, "stream": False, "think": False,
                       "options": {"temperature": 0, "num_predict": 8}}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        t = json.load(r)["response"]
    m = re.search(r"agree|disagree|none", t.lower())
    return m.group(0) if m else "none"


def collect(words: list[str]) -> list[dict]:
    seen, out = set(), []
    for f in glob.glob(os.path.join(KMONTAGE_JOBS, "*", "news_opinions.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:      # noqa: BLE001
            continue
        s = d.get("sources") or {}
        title = ((s.get("yahoo_meta") or {}).get("title") or "")
        if not any(w in title for w in words):
            continue
        for c in (s.get("yahoo_comments") or []):
            txt = re.sub(r"\s+", " ", str(c.get("text") or "")).strip()
            if len(txt) < 30 or txt in seen:
                continue
            seen.add(txt)
            out.append({"platform": "Yahooコメント", "author": c.get("author") or "",
                        "text": txt[:520], "empathy": int(c.get("empathy_count") or 0),
                        "negative": int(c.get("negative_count") or 0),
                        "url": c.get("url") or "", "article": title[:160]})
        for x in (s.get("x_replies") or []):
            txt = re.sub(r"\s+", " ", str(x.get("text") or "")).strip()
            if len(txt) < 30 or txt in seen:
                continue
            seen.add(txt)
            out.append({"platform": "Xリプライ", "author": x.get("author") or "",
                        "text": txt[:520], "empathy": int(x.get("like_count") or 0),
                        "negative": 0, "url": x.get("url") or "", "article": title[:160]})
    out.sort(key=lambda r: -r["empathy"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--words", nargs="+", required=True, help="記事の題名に含む語")
    ap.add_argument("--limit", type=int, default=0, help="反応の上限（0=全部）")
    a = ap.parse_args()

    init_db()
    init_reactions()
    con = connect()
    t = con.execute("SELECT id, name FROM topic WHERE slug=?", (a.slug,)).fetchone()
    if not t:
        raise SystemExit(f"論点がありません: {a.slug}")
    stmts = [dict(r) for r in con.execute(
        "SELECT id, text FROM statement WHERE topic_id=? ORDER BY id", (t["id"],))]

    rs = collect(a.words)
    if a.limit:
        rs = rs[:a.limit]
    print(f"{t['name']}: 反応 {len(rs)}件 × 論点 {len(stmts)}件 = {len(rs) * len(stmts):,}回の読み取り")

    with con:
        for r in rs:
            con.execute(
                "INSERT OR IGNORE INTO reaction (topic_id, platform, author, text, empathy, negative, url, article) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (t["id"], r["platform"], r["author"], r["text"], r["empathy"], r["negative"],
                 r["url"], r["article"]))
    ids = {row["text"]: row["id"] for row in con.execute(
        "SELECT id, text FROM reaction WHERE topic_id=?", (t["id"],))}

    done = {(row[0], row[1]) for row in con.execute(
        """SELECT rs.reaction_id, rs.statement_id FROM reaction_stance rs
           JOIN reaction r ON r.id = rs.reaction_id WHERE r.topic_id=?""", (t["id"],))}
    todo = [(ids[r["text"]], s) for r in rs if r["text"] in ids for s in stmts
            if (ids[r["text"]], s["id"]) not in done]
    print(f"  未判定 {len(todo):,}回（済み {len(done):,}回）")

    t0, n_ag, n_dis, n_none = time.time(), 0, 0, 0
    for i, (rid, s) in enumerate(todo, 1):
        text = con.execute("SELECT text FROM reaction WHERE id=?", (rid,)).fetchone()[0]
        try:
            v = judge(text, s["text"])
        except Exception as e:      # noqa: BLE001
            print(f"  [{i}] 判定に失敗: {str(e)[:80]}")
            continue
        val = {"agree": 1, "disagree": -1, "none": 0}[v]
        n_ag += v == "agree"
        n_dis += v == "disagree"
        n_none += v == "none"
        with con:
            con.execute("INSERT OR REPLACE INTO reaction_stance "
                        "(reaction_id, statement_id, value, model, created_at) "
                        "VALUES (?,?,?,?,datetime('now'))", (rid, s["id"], val, MODEL))
        if i % 100 == 0 or i == len(todo):
            el = time.time() - t0
            print(f"  {i:,}/{len(todo):,}  経過{el / 60:.1f}分  "
                  f"残り約{(el / i) * (len(todo) - i) / 60:.0f}分  "
                  f"賛成{n_ag} 反対{n_dis} 触れず{n_none}", flush=True)
    con.close()
    print(f"完了: 賛成{n_ag} 反対{n_dis} 触れず{n_none}")


if __name__ == "__main__":
    main()
