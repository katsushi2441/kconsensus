#!/usr/bin/env python3
"""手元のCSV（アンケートの自由記述など）から、論点・賛否の読み取りまでを作る。

**この道具の入口はこちら。** giin（国会会議録）や kmontage（ニュースへの反応）から
作る入口は当社の環境に合わせたもので、ふつうはこのCSVの入口を使う。

できること:
  1. 自由記述のCSVを読む
  2. そこから「賛成・反対を押せる論点」を立てる（既にあるならCSVで渡してもよい）
  3. 1件ずつ「この人はこの論点に賛成か・反対か・触れていないか」を読み取る
  4. 意見グループと合意点が出る（画面は /t/<slug>/ ）

**意見は作らない。** LLM がするのは、実在する記述の言い換えと分類だけ。
**読み取りは推定なので、人が押した票とは別に集計する。**

CSVの形（1行目は見出し。文字コードは UTF-8）:
  opinions.csv   text[必須], author, weight
  statements.csv text[必須]              ← 省略すると自由記述から立てる

使い方:
  /usr/bin/python3 scripts/import_csv.py --slug shain2026 --name "社内アンケート2026" \\
      --opinions opinions.csv
  /usr/bin/python3 scripts/import_csv.py --slug shain2026 --name "..." \\
      --opinions opinions.csv --statements statements.csv
  # 論点だけ先に見たいとき
  /usr/bin/python3 scripts/import_csv.py --slug x --name x --opinions o.csv --dry-run
"""
from __future__ import annotations

import argparse
import csv
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

OLLAMA = os.environ.get("KCONSENSUS_OLLAMA", "http://127.0.0.1:11434")
MODEL = os.environ.get("KCONSENSUS_MODEL", "gemma4:12b-it-qat")

CRIT = {
    "agree": "この人は、その主張に賛成している。同じ方向のことを言っている",
    "disagree": "この人は、その主張に反対している。逆の方向のことを言っている",
    "none": "この記述は、その主張について賛成とも反対とも言っていない。触れていない",
}

MAKE = """あなたは、世論調査の設問をつくる担当者です。

次の自由記述から、賛成・反対を押せる「論点」を日本語で {n} 個つくってください。

厳守すること:
- 記述に書かれていないことを足さない。あなたの意見を混ぜない。
- 1つの論点は1つのことだけを問う。「AもBも」と2つ入れない。
- 「〜すべきだ」「〜は必要だ」のように、賛否がはっきり割れる言い切りの形にする。
- 40字以内。
- 賛成寄り・反対寄りの両方の立場の論点を混ぜる。片方に偏らせない。
- 誰かを侮辱する言い方、人格への評価は入れない。争点だけを書く。

出力は JSON 配列だけ。説明文を書かない。
各要素: {{"text": "論点", "basis": "元にした記述の要点(60字以内)"}}

## 自由記述
{body}
"""


def llm(prompt: str, num_predict: int = 2048, timeout: int = 300) -> str:
    """gemma4 は思考型なので think:false を必ず付ける（付けないと response が空になる）。"""
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False, "think": False,
                       "options": {"temperature": 0, "num_predict": num_predict}}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["response"]


def judge(opinion: str, statement: str) -> str:
    p = (f"主張: {statement}\n\n記述: {opinion}\n\n"
         "この記述を書いた人は、上の主張に賛成していますか、反対していますか、触れていませんか。\n"
         + "\n".join(f"- {k}: {v}" for k, v in CRIT.items())
         + "\n\nはっきり読み取れないときは none。答えの語だけを1つ出力してください。")
    t = llm(p, num_predict=8)
    m = re.search(r"agree|disagree|none", t.lower())
    return m.group(0) if m else "none"


def read_csv(path: str, need: str = "text") -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"中身がありません: {path}")
    if need not in rows[0]:
        raise SystemExit(f"{path} に列 '{need}' がありません（見出し行: {list(rows[0])}）")
    out = []
    for r in rows:
        t = re.sub(r"\s+", " ", str(r.get(need) or "")).strip()
        if len(t) < 10:
            continue
        out.append({"text": t[:520], "author": str(r.get("author") or "")[:60],
                    "weight": int(float(r.get("weight") or 0) or 0)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--opinions", required=True)
    ap.add_argument("--statements")
    ap.add_argument("--lead", default="")
    ap.add_argument("--n", type=int, default=14, help="立てる論点の数（--statements があれば無視）")
    ap.add_argument("--limit", type=int, default=0, help="読み込む自由記述の上限（0=全部）")
    ap.add_argument("--dry-run", action="store_true", help="DBに書かず、立てた論点を表示するだけ")
    a = ap.parse_args()

    ops = read_csv(a.opinions)
    if a.limit:
        ops = ops[:a.limit]
    if not ops:
        raise SystemExit("使える自由記述がありませんでした（10字未満は捨てています）")
    print(f"自由記述 {len(ops)}件")

    if a.statements:
        stmts = [{"text": x["text"][:100], "basis": ""} for x in read_csv(a.statements)]
        print(f"論点 {len(stmts)}件（CSVから）")
    else:
        body = "\n".join(f"- {o['text'][:200]}" for o in ops[:60])
        raw = llm(MAKE.format(n=a.n, body=body))
        m = re.search(r"\[.*\]", raw, re.S)
        if not m:
            raise SystemExit(f"論点を組み立てられませんでした（応答の先頭200字）: {raw[:200]}")
        stmts = [x for x in json.loads(m.group(0))
                 if isinstance(x, dict) and str(x.get("text") or "").strip()]
        print(f"論点 {len(stmts)}件（自由記述から作成）")
    for i, s in enumerate(stmts, 1):
        print(f"  {i:2}. {s['text']}")
    if a.dry_run:
        return

    init_db()
    init_reactions()
    con = connect()
    with con:
        con.execute("INSERT OR REPLACE INTO topic (slug, tracker, name, lead, updated_at) "
                    "VALUES (?,?,?,?,datetime('now'))", (a.slug, "", a.name, a.lead))
        tid = con.execute("SELECT id FROM topic WHERE slug=?", (a.slug,)).fetchone()[0]
        con.execute("DELETE FROM statement WHERE topic_id=? AND source='csv'", (tid,))
        for s in stmts:
            con.execute("INSERT INTO statement (topic_id, text, origin, basis, source, created_at) "
                        "VALUES (?,?,'public',?, 'csv', datetime('now'))",
                        (tid, s["text"][:100], str(s.get("basis") or "")[:200]))
        for o in ops:
            con.execute("INSERT OR IGNORE INTO reaction "
                        "(topic_id, platform, author, text, empathy, negative, url, article) "
                        "VALUES (?,'自由記述',?,?,?,0,'',?)",
                        (tid, o["author"], o["text"], o["weight"], Path(a.opinions).name))
    sids = [dict(r) for r in con.execute(
        "SELECT id, text FROM statement WHERE topic_id=? ORDER BY id", (tid,))]
    rids = [dict(r) for r in con.execute(
        "SELECT id, text FROM reaction WHERE topic_id=?", (tid,))]
    done = {(r[0], r[1]) for r in con.execute(
        """SELECT rs.reaction_id, rs.statement_id FROM reaction_stance rs
           JOIN reaction r ON r.id = rs.reaction_id WHERE r.topic_id=?""", (tid,))}
    todo = [(r, s) for r in rids for s in sids if (r["id"], s["id"]) not in done]
    print(f"読み取り {len(todo):,}回（{len(rids)}件 × {len(sids)}論点）")

    t0 = time.time()
    n = {"agree": 0, "disagree": 0, "none": 0}
    for i, (r, s) in enumerate(todo, 1):
        try:
            v = judge(r["text"], s["text"])
        except Exception as e:      # noqa: BLE001
            print(f"  [{i}] 判定に失敗: {str(e)[:80]}")
            continue
        n[v] += 1
        with con:
            con.execute("INSERT OR REPLACE INTO reaction_stance "
                        "(reaction_id, statement_id, value, model, created_at) "
                        "VALUES (?,?,?,?,datetime('now'))",
                        (r["id"], s["id"], {"agree": 1, "disagree": -1, "none": 0}[v], MODEL))
        if i % 100 == 0 or i == len(todo):
            el = time.time() - t0
            print(f"  {i:,}/{len(todo):,}  経過{el / 60:.1f}分  "
                  f"残り約{(el / i) * (len(todo) - i) / 60:.0f}分  "
                  f"賛成{n['agree']} 反対{n['disagree']} 触れず{n['none']}", flush=True)
    con.close()
    print(f"完了: 賛成{n['agree']} 反対{n['disagree']} 触れず{n['none']}")
    print(f"画面: /t/{a.slug}/")


if __name__ == "__main__":
    main()
