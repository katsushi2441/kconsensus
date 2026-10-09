# Kurage 合意形成AI（合意点マップ）（kconsensus）

2026-10-09 に表示名を「Kurage 合意点マップ」から「Kurage 合意形成AI（合意点マップ）」に変えた（しばらく括弧書きで旧名を残す）。

賛否を集めて、**意見がどう割れているか**と、**割れた人たちがそろって賛成した点**を出す。
Pol.is がやっている合意点の発見を、自前で実装したもの。

## なぜ fork しないか

Pol.is 本体は AGPL-3.0。売り物に取り込むとこちらも AGPL に縛られる。
アルゴリズムは公開されているので、同じ筋を MIT で書いた（`app/polis.py`）。

## 材料を作らない

- 論点は **国会会議録の実発言**（giin の `tracker_speech`）と、
  **kmontage news が集めたニュースへの反応** から立てる。
- LLM がするのは「賛否を押せる1文に言い換える」ことだけ。意見の中身は作らない。
- **Yahooコメントの本文は投票対象にしない。** 要点に整理してから論点にする
  （kmontage news が既に引いている線をそのまま使う）。国会発言は
  著作権法40条1項の公開の政治上の演説等として、抜粋を出典付きで出す。
- **合成票は `tests/` にしか使わない。** 公開画面には、実在の発言と、
  人が実際に押した票だけを出す。

## 足りないときは足りないと書く

- 3問以上押した人が8人未満 → 意見グループを作らない
- 1論点の票が5票未満 → スコアを出さない

## 動かす

```
systemctl --user start kconsensus       # 127.0.0.1:18379
/usr/bin/python3 scripts/build_topic.py --tracker shohizei-genzei --slug shohizei --dry-run
/usr/bin/python3 -m pytest tests/ -q
```

論点の生成は Ollama `gemma4:12b-it-qat`（0.3 直叩き・`think:false` 必須）。
