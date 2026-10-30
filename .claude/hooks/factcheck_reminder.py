#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UserPromptSubmit フック。毎回のやりとりの先頭でファクトチェックのルールを入れ直す。

毎回指示しなくても事実確認が働くようにするのが目的なので、
プロンプトの内容にかかわらず同じルールを注入する。
"""

import json
import sys

RULES = """【ファクトチェックのルール（くらしの相談ナビ）】
・金額・電話番号・担当課・年齢や期間の要件・法令名は、一次情報で確認できたものだけを書く。
・確認できないものは推測で埋めず、【要確認】を付ける（他自治体の相場からの推測は不可）。
・既存の数字・固有名詞は、出典で変更を確認できた場合以外は書き換えない。
  電話番号は ENTRY_CONTACTS（入口の窓口一覧）と各制度の contact の両方にあるので、
  直すときは必ず両方を見る。
・確認したら docs/fact-log.md に出典URLと確認日を追記する。
・報告では、✅出典で確認した内容と、⚠️確認できず【要確認】のままにした内容を分けて書く。
詳しくは CLAUDE.md を参照。"""


def main():
    try:
        if sys.stdin is not None and not sys.stdin.isatty():
            sys.stdin.read()  # 読み捨て
    except Exception:
        pass

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": RULES,
                },
                "suppressOutput": True,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
