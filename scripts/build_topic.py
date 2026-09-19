#!/usr/bin/env python3
"""kmontage news の反応と、giin の国会発言から、投票にかける論点を作る。

**AIに意見を作らせない。** LLM がするのは「実在する文から、賛成/反対を押せる
1文の命題に言い換える」ことだけ。どの発言・どのコメントが元かを必ず残し、
画面に出典として出す。

**コメント本文は投票対象にしない。** Yahooコメントは要点に整理してから使う
（kmontage news が既にそうしている線を、そのまま持ち込む）。国会発言は
著作権法40条1項で公開の政治上の演説等として扱えるので、抜粋を出典付きで出す。

  /usr/bin/python3 scripts/build_topic.py --tracker shohizei-genzei --slug shohizei
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app.store import connect, init_db  # noqa: E402

GIIN_DB = os.environ.get("KCONSENSUS_GIIN_DB", "/home/kojima/work/xb4g/giin/data/giin.sqlite")
KMONTAGE_JOBS = os.environ.get("KCONSENSUS_KMONTAGE_JOBS", "/home/kojima/work/kmontage/storage/jobs")
TRACKERS = os.environ.get("KCONSENSUS_TRACKERS", "/home/kojima/work/xb4g/giin/data/trackers.json")
# 対話・単発処理の既定は 0.3 直叩き（0.14 は rqdb4ai 経由のときだけ）
OLLAMA = os.environ.get("KCONSENSUS_OLLAMA", "http://127.0.0.1:11434")
MODEL = os.environ.get("KCONSENSUS_MODEL", "gemma4:12b-it-qat")


def llm(prompt: str, timeout: int = 300) -> str:
    """gemma4 は思考型なので think:false を必ず付ける（付けないと response が空になる）。"""
    body = json.dumps({
        "model": MODEL, "prompt": prompt, "stream": False, "think": False,
        "options": {"temperature": 0.2, "num_predict": 2048},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["response"]


def load_tracker(key: str) -> dict:
    d = json.load(open(TRACKERS, encoding="utf-8"))
    ts = d if isinstance(d, list) else d.get("trackers", d)
    hit = [t for t in ts if t["key"] == key]
    if not hit:
        raise SystemExit(f"トラッカーが見つかりません: {key}")
    return hit[0]


def speeches(key: str, limit: int = 40) -> list[dict]:
    """その論点の国会発言。質疑と政府答弁だけを、新しい順に。"""
    c = sqlite3.connect(GIIN_DB)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """SELECT date, house, meeting, speaker, kaiha_at, kind, body, speech_url
           FROM tracker_speech WHERE tracker=? AND kind IN ('q','gov') AND length(body) >= 200
           ORDER BY date DESC LIMIT ?""", (key, limit)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def news(words: list[str]) -> list[dict]:
    """kmontage news が拾った記事のうち、その論点の語を題名に含むもの。"""
    out = []
    for f in glob.glob(os.path.join(KMONTAGE_JOBS, "*", "news_opinions.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:      # noqa: BLE001
            continue
        s = d.get("sources") or {}
        yc = s.get("yahoo_comments") or []
        title = ((s.get("yahoo_meta") or {}).get("title") or "")
        if not yc or not any(w in title for w in words):
            continue
        out.append({"title": title, "url": d.get("url", ""), "comments": yc,
                    "x_replies": s.get("x_replies") or [], "job": os.path.basename(os.path.dirname(f))})
    out.sort(key=lambda x: -sum(c.get("empathy_count", 0) for c in x["comments"]))
    return out


PROMPT = """あなたは、世論調査の設問をつくる担当者です。

次の材料から、賛成・反対を押せる「論点」を日本語で {n} 個つくってください。

厳守すること:
- 材料に書かれていないことを足さない。あなたの意見を混ぜない。
- 1つの論点は1つのことだけを問う。「AもBも」と2つ入れない。
- 「〜すべきだ」「〜は必要だ」のように、賛否がはっきり割れる言い切りの形にする。
- 40字以内。
- 賛成寄り・反対寄りの両方の立場の論点を混ぜる。片方に偏らせない。
- 誰かを侮辱する言い方、人格への評価は入れない。争点だけを書く。

出力は JSON 配列だけ。説明文を書かない。
各要素: {{"text": "論点", "from": "diet" または "public", "basis": "元にした発言・意見の要点(60字以内)"}}
from は、国会の発言から立てた論点なら diet、ニュースへの反応から立てた論点なら public。

## 国会での発言（質疑と政府答弁）
{diet}

## ニュースへの反応（共感数の多い順。本文そのままではなく要点として扱うこと）
{public}
"""


def clip(s: str, n: int) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracker", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--n", type=int, default=14)
    ap.add_argument("--dry-run", action="store_true", help="DBに書かず、作った論点を表示するだけ")
    a = ap.parse_args()

    t = load_tracker(a.tracker)
    sp = speeches(a.tracker)
    nw = news(t["words"])
    print(f"材料: 国会発言 {len(sp)}件 / ニュース {len(nw)}件"
          f"（Yahooコメント {sum(len(x['comments']) for x in nw)}件）")
    if not sp and not nw:
        raise SystemExit("材料がありません")

    diet = "\n".join(
        f"- {r['date']} {r['speaker']}（{r['kaiha_at'] or '政府'}）: {clip(r['body'], 300)}"
        for r in sp[:18])
    pub = "\n".join(
        f"- 共感{c.get('empathy_count', 0)}／うーん{c.get('negative_count', 0)}: {clip(c.get('text'), 160)}"
        for x in nw for c in sorted(x["comments"], key=lambda c: -c.get("empathy_count", 0))[:6])

    raw = llm(PROMPT.format(n=a.n, diet=diet, public=pub or "（該当する反応はありません）"))
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        raise SystemExit(f"論点を組み立てられませんでした（応答の先頭200字）: {raw[:200]}")
    items = json.loads(m.group(0))
    items = [x for x in items if isinstance(x, dict) and clip(x.get("text"), 100)]
    print(f"論点 {len(items)}件")
    for i, x in enumerate(items, 1):
        print(f"  {i:2}. [{x.get('from', '?'):6}] {x['text']}")
        if x.get("basis"):
            print(f"      根拠: {clip(x['basis'], 70)}")
    if a.dry_run:
        return

    init_db()
    con = connect()
    with con:
        con.execute(
            "INSERT OR REPLACE INTO topic (slug, tracker, name, lead, updated_at) "
            "VALUES (?,?,?,?,datetime('now'))",
            (a.slug, a.tracker, t["name"], t.get("lead", "")))
        tid = con.execute("SELECT id FROM topic WHERE slug=?", (a.slug,)).fetchone()[0]
        con.execute("DELETE FROM statement WHERE topic_id=? AND source='seed'", (tid,))
        for x in items:
            con.execute(
                "INSERT INTO statement (topic_id, text, origin, basis, source, created_at) "
                "VALUES (?,?,?,?, 'seed', datetime('now'))",
                (tid, clip(x["text"], 100), x.get("from", "public"), clip(x.get("basis"), 200)))
        con.execute("DELETE FROM evidence WHERE topic_id=?", (tid,))
        for r in sp[:24]:
            con.execute(
                "INSERT INTO evidence (topic_id, kind, date, who, affiliation, body, url) "
                "VALUES (?,?,?,?,?,?,?)",
                (tid, "diet", r["date"], r["speaker"], r["kaiha_at"] or "政府",
                 clip(r["body"], 420), r["speech_url"]))
        for x in nw:
            con.execute(
                "INSERT INTO evidence (topic_id, kind, date, who, affiliation, body, url) "
                "VALUES (?,?,?,?,?,?,?)",
                (tid, "news", "", clip(x["title"], 160), "Yahoo!ニュース",
                 f"Yahooコメント{len(x['comments'])}件・"
                 f"共感合計{sum(c.get('empathy_count', 0) for c in x['comments']):,}",
                 x["url"]))
    print(f"保存しました: topic={a.slug} 論点{len(items)}件 / 出典{len(sp[:24]) + len(nw)}件")


if __name__ == "__main__":
    main()
