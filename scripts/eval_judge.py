#!/usr/bin/env python3
"""賛否の読み取りを、手で正解を付けた組で測る。

**なぜ要るか**: 読み取りが当たっていなければ、そこから作る「意見グループ」も
「合意点」も意味がない。配線が通っていることと、答えが合っていることは別物
（jevlocal で Qwen3-1.7B を使ったとき、並べ替えると常に最後を選ぶ偏りがあり、
8択で5/10しか当たらなかった。同じ罠を踏まないため、**選択肢の順番を変えて2回**測る）。

正解は実際の Yahooコメントと論点から、こちらで読んで付けたもの。
迷う組はわざと入れていない（判定が割れる組で精度を語っても意味がないため）。

  /usr/bin/python3 scripts/eval_judge.py
  /usr/bin/python3 scripts/eval_judge.py --judge gemma    # 比較用
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.request

JEV = "http://127.0.0.1:18370/v1/systemone"
OLLAMA = "http://192.168.0.3:11434/api/generate"
GEMMA = "gemma4:12b-it-qat"

C0 = ("2年間だけ食料品の税率を1％に下げて、戻す時は現金給付で不満を抑えるというなら、"
      "結局は増税への反発をいつものように金で抑える発想でしかない。下げる、戻す、給付するを"
      "繰り返せば制度は複雑になり、現場も家計も振り回されるだけになるだろう。本当に必要なのは、"
      "その場しのぎの給付ではなく、先まで見通せる一貫した支援策と社会保障の設計だ")
C2 = ("消費減税を巡る議論がまとまらなかったからといって、公約の実現を先送りする理由にはならない。"
      "選挙で減税を掲げて国民の支持を得た以上、政権には公約を果たす責任がある。国民が求めているのは"
      "結論の出ない協議ではなく、家計を支える減税の実行だ")
C4 = ("食品の消費税が下がれば確かに年間数万円は助かる。でも、社会保険料、所得税・住民税、"
      "納得できない補助金や事業、巨額の基金の使い残し。ここを放置したまま、しかも食品減税はたった2年間。"
      "本気なら「食品減税の恒久化＋社会保険料の引下げ＋無駄な支出の整理＋穴埋め増税なし」までセットで")
C5 = ("無駄に手数料や時間を要する集めて配るぐらいなら、社会保険料や厚生年金保険料、所得税や住民税などを"
      "大幅に引き下げて手取りをあげる方が得策かと思います")
C7 = ("食料品の消費税を1％まで下げておきながら、2年後には一気に8％へ戻し、その負担増を現金給付で和らげる。"
      "これでは何のための減税なのか分かりません。必要なのは一時的な給付で増税の痛みを薄めることではなく、"
      "負担そのものを継続的に軽くすることです")

S = {
    1: "食料品の消費税率を1％へ引き下げるべきだ",
    3: "消費税減税よりも、所得連動型給付のみを行うべきだ",
    4: "消費税の仕組みをシンプルにするために制度を見直すべきだ",
    5: "所得に関わらず国民全員に一律給付を行うべきだ",
    6: "消費税減税よりも、所得税の減税や還付を行うべきだ",
    10: "消費税減税は、企業の「値上げ」を招くだけなので不要だ",
    13: "給付制度の導入にあたり、まずは支出の洗い直しを行うべきだ",
    14: "消費税減税よりも、住民税・所得税の減税と社会保険料還付を行うべきだ",
}

# (コメント, 論点id, 正解)  正解は agree / disagree / none
GOLD = [
    (C0, 5, "disagree"),    # 給付で不満を抑える発想でしかない、と否定している
    (C0, 4, "agree"),       # 制度が複雑になる、と複雑化を問題視している
    (C0, 3, "disagree"),    # 給付ではなく一貫した支援と言っている
    (C0, 13, "none"),       # 支出の洗い直しには触れていない
    (C2, 1, "agree"),       # 減税の実行を求めている
    (C2, 3, "disagree"),    # 減税をやれ、と言っている
    (C2, 10, "disagree"),   # 減税不要とは正反対
    (C2, 14, "none"),       # 住民税・所得税には触れていない
    (C4, 1, "agree"),       # 食品減税の恒久化を求めている
    (C4, 13, "agree"),      # 無駄な支出の整理を求めている
    (C4, 10, "disagree"),   # 減税不要とは逆
    (C4, 5, "none"),        # 一律給付には触れていない
    (C5, 5, "disagree"),    # 集めて配るぐらいなら、と給付を否定
    (C5, 14, "agree"),      # 住民税・所得税の引下げと社会保険料を求めている
    (C5, 6, "agree"),       # 所得税の引下げを求めている
    (C5, 4, "none"),        # 制度の複雑さには触れていない
    (C7, 1, "agree"),       # 減税そのものは求めている（一時的なのを批判）
    (C7, 5, "disagree"),    # 現金給付で痛みを薄めるのを否定
    (C7, 10, "disagree"),
    (C7, 13, "none"),
]

CRIT = {
    "agree": "このコメントを書いた人は、その主張に賛成している。同じ方向のことを言っている",
    "disagree": "このコメントを書いた人は、その主張に反対している。逆の方向のことを言っている",
    "none": "このコメントは、その主張について賛成とも反対とも言っていない。触れていない",
}


def jev(comment: str, statement: str, order: list[str]) -> str:
    body = json.dumps({
        "state": {"主張": statement, "コメント": comment},
        "questions": {"stance": {"type": "choice",
                                 "criteria": {k: CRIT[k] for k in order},
                                 "instructions": {"task": "コメントを書いた人が、主張に賛成か反対か、触れていないかを選ぶ",
                                                  "rule": "はっきり読み取れないときは none を選ぶ"}}},
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(JEV, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    return d["answers"]["stance"]["choice"]


def gemma(comment: str, statement: str, order: list[str]) -> str:
    p = (f"主張: {statement}\n\nコメント: {comment}\n\n"
         "このコメントを書いた人は、上の主張に賛成していますか、反対していますか、触れていませんか。\n"
         + "\n".join(f"- {k}: {CRIT[k]}" for k in order)
         + "\n\nはっきり読み取れないときは none。答えの語だけを1つ出力してください。")
    body = json.dumps({"model": GEMMA, "prompt": p, "stream": False, "think": False,
                       "options": {"temperature": 0, "num_predict": 8}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        t = json.load(r)["response"]
    m = re.search(r"agree|disagree|none", t.lower())
    return m.group(0) if m else "none"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", choices=["jev", "gemma", "gemma-batch"], default="jev")
    a = ap.parse_args()
    if a.judge == "gemma-batch":
        run_batch(); return
    fn = jev if a.judge == "jev" else gemma
    orders = [["agree", "disagree", "none"], ["none", "disagree", "agree"]]
    res = []
    for comment, sid, want in GOLD:
        got = [fn(comment, S[sid], o) for o in orders]
        res.append((sid, want, got))
    ok0 = sum(1 for _, w, g in res if g[0] == w)
    ok1 = sum(1 for _, w, g in res if g[1] == w)
    same = sum(1 for _, _, g in res if g[0] == g[1])
    n = len(res)
    print(f"判定器: {a.judge}   組: {n}")
    print(f"  並び順A で正解 {ok0}/{n} ({ok0 / n:.0%})")
    print(f"  並び順B(逆) で正解 {ok1}/{n} ({ok1 / n:.0%})")
    print(f"  並べ替えても同じ答え {same}/{n} ({same / n:.0%})  ← 低いと位置の偏りを疑う")
    print("\n  外した組:")
    for sid, w, g in res:
        if g[0] != w or g[1] != w:
            print(f"    論点{sid:2} 正解={w:8} A={g[0]:8} B={g[1]}")



def gemma_batch(comment: str, order: list[str]) -> dict[int, str]:
    """1コメントで全論点をまとめて聞く（2,464回→176回に減らせるか）。"""
    lines = "\n".join(f"{i}. {t}" for i, t in S.items())
    p = (f"コメント: {comment}\n\n次の主張のそれぞれについて、このコメントを書いた人が\n"
         "賛成しているか、反対しているか、触れていないかを判定してください。\n\n"
         f"{lines}\n\n"
         + "\n".join(f"- {k}: {CRIT[k]}" for k in order)
         + "\n\nはっきり読み取れないときは none。\n"
           'JSON だけを出力: {"<番号>": "agree|disagree|none", ...}')
    body = json.dumps({"model": GEMMA, "prompt": p, "stream": False, "think": False,
                       "options": {"temperature": 0, "num_predict": 512}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        t = json.load(r)["response"]
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {}
    out = {}
    for k, v in d.items():
        mm = re.search(r"\d+", str(k))
        vv = re.search(r"agree|disagree|none", str(v).lower())
        if mm and vv:
            out[int(mm.group(0))] = vv.group(0)
    return out


def run_batch() -> None:
    orders = [["agree", "disagree", "none"], ["none", "disagree", "agree"]]
    comments = []
    for c, _, _ in GOLD:
        if c not in comments:
            comments.append(c)
    cache = {}
    for ci, c in enumerate(comments):
        for oi, o in enumerate(orders):
            cache[(ci, oi)] = gemma_batch(c, o)
    res = []
    for comment, sid, want in GOLD:
        ci = comments.index(comment)
        res.append((sid, want, [cache[(ci, 0)].get(sid, "none"), cache[(ci, 1)].get(sid, "none")]))
    n = len(res)
    ok0 = sum(1 for _, w, g in res if g[0] == w)
    ok1 = sum(1 for _, w, g in res if g[1] == w)
    same = sum(1 for _, _, g in res if g[0] == g[1])
    print(f"判定器: gemma-batch（1コメントで全論点）   組: {n}  呼び出し {len(comments)}回×2")
    print(f"  並び順A で正解 {ok0}/{n} ({ok0 / n:.0%})")
    print(f"  並び順B(逆) で正解 {ok1}/{n} ({ok1 / n:.0%})")
    print(f"  並べ替えても同じ答え {same}/{n} ({same / n:.0%})")
    print("\n  外した組:")
    for sid, w, g in res:
        if g[0] != w or g[1] != w:
            print(f"    論点{sid:2} 正解={w:8} A={g[0]:8} B={g[1]}")


if __name__ == "__main__":
    main()
