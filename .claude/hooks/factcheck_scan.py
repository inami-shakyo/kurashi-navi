#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""くらしの相談ナビ ファクトチェック用スキャナ

git の差分（追加行・削除行）だけを見て、出典の裏付けがない事実記述
（金額・電話番号・割合・年齢や期間の要件・法令名と改定時期）を洗い出す。
既存の記述を蒸し返さないため、判定対象は必ず差分に限る。

使い方（いずれも stdin にフックの JSON が来る想定）:
    factcheck_scan.py postedit   # PostToolUse。警告するだけでブロックしない
    factcheck_scan.py stopgate   # Stop。検知があれば decision:block を返す
    factcheck_scan.py report     # 人間向けのテキスト一覧（/factcheck から呼ぶ）
"""

import json
import os
import re
import subprocess
import sys

MAX_SHOWN = 40

# --- 検出パターン（ラベル付き） ---------------------------------------------
PATTERNS = [
    ("電話番号", re.compile(r"0\d{1,4}-\d{2,4}-\d{4}")),
    ("金額", re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:万円|円)")),
    ("割合", re.compile(r"\d+(?:\.\d+)?\s*(?:％|%|割)")),
    ("年齢・期間の要件", re.compile(r"\d+\s*(?:歳|か月|ヶ月|年度|級)")),
    ("法令・改定時期", re.compile(r"(?:令和|平成)\s*\d+\s*年(?:\s*\d+\s*月)?|第\s*\d+\s*条")),
]

# --- 除外：見た目の指定を含む行は対象外 -------------------------------------
STYLE_PATTERNS = [
    re.compile(r"(?:px|rem|em|vh|vw)\b"),
    re.compile(r"#[0-9a-fA-F]{3,8}\b"),
    re.compile(r"z-index"),
    re.compile(r"font-size"),
    re.compile(r"line-height"),
    re.compile(r"margin"),
    re.compile(r"padding"),
    re.compile(r"border-radius"),
    re.compile(r"rgba?\("),
    re.compile(r"translate"),
    re.compile(r"@media"),
]

JAPANESE = re.compile(r"[ぁ-んァ-ヶ一-龥]")

# 日本語が無くても制度データの項目行は対象にする（tel:"079-..." を取りこぼさない）
DATA_FIELD = re.compile(
    r"\b(?:tel|area|address|contact|name|hours|note|summary|target|support|flow|"
    r"cost|meyasu|basis|staffNote|detail|docs|tips)\s*:"
)

# 文単位の免除キー
EXEMPT = re.compile(r"【要確認】|出典|根拠：|https?://")


def run(args, cwd):
    """git を叩く。失敗しても例外にせず (ok, 出力) を返す。"""
    try:
        p = subprocess.run(
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError:
        return False, ""
    out = p.stdout.decode("utf-8", "replace")
    return p.returncode == 0, out


def project_dir():
    d = os.environ.get("CLAUDE_PROJECT_DIR")
    if d and os.path.isdir(d):
        return d
    ok, out = run(["git", "rev-parse", "--show-toplevel"], os.getcwd())
    if ok and out.strip():
        return out.strip()
    return os.getcwd()


def base_rev(mode, root):
    """比較元のリビジョンを決める。"""
    if mode == "postedit":
        return "HEAD"
    ok, out = run(["git", "merge-base", "HEAD", "origin/main"], root)
    if ok and out.strip():
        return out.strip()
    return "HEAD"


def diff_lines(root, base):
    """git diff -U0 を自分でパースして (種別, ファイル, 行番号, 本文) を得る。

    種別は "+"（追加行）か "-"（削除行）。対象は .html のみ。
    """
    ok, out = run(
        ["git", "diff", "-U0", "--no-color", base, "--", "*.html"], root
    )
    if not ok:
        return []

    hunk = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    rows = []
    path = None
    old_no = new_no = 0
    for line in out.split("\n"):
        if line.startswith("+++ "):
            p = line[4:].strip()
            path = None if p == "/dev/null" else re.sub(r"^b/", "", p)
            continue
        if line.startswith("--- "):
            continue
        m = hunk.match(line)
        if m:
            old_no = int(m.group(1))
            new_no = int(m.group(3))
            continue
        if path is None or not path.endswith(".html"):
            continue
        if line.startswith("+"):
            rows.append(("+", path, new_no, line[1:]))
            new_no += 1
        elif line.startswith("-"):
            rows.append(("-", path, old_no, line[1:]))
            old_no += 1
    return rows


def sentence_spans(line):
    """行を文に分ける。区切りは「。」・改行・データ中に2文字で現れる \\n。

    行単位で免除すると、数百字ある detail や meyasu の行末の【要確認】1つで
    行内の数字が全部免除されてしまうため、必ず文単位で判定する。
    """
    spans = []
    start = 0
    i = 0
    n = len(line)
    while i < n:
        if line[i] in ("。", "\n", "\r"):
            i += 1
            spans.append((start, i))
            start = i
        elif line[i] == "\\" and i + 1 < n and line[i + 1] == "n":
            i += 2
            spans.append((start, i))
            start = i
        else:
            i += 1
    if start < n:
        spans.append((start, n))
    return spans or [(0, n)]


def line_is_skipped(line):
    """見た目の指定を含む行／日本語を含まない行（データ項目行は除く）は対象外。"""
    for pat in STYLE_PATTERNS:
        if pat.search(line):
            return True
    if not JAPANESE.search(line):
        if not DATA_FIELD.search(line):
            return True
    return False


def find_values(line):
    """1行から (ラベル, 値, 文) を拾う。免除済みの文は捨てる。"""
    if line_is_skipped(line):
        return []

    spans = sentence_spans(line)
    exempt = [bool(EXEMPT.search(line[s:e])) for s, e in spans]

    hits = []
    for label, pat in PATTERNS:
        for m in pat.finditer(line):
            hits.append((m.start(), m.end(), label, m.group(0)))

    # 重複・内包を潰す（長い方を残す）
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept = []
    for h in hits:
        if any(k[0] <= h[0] and h[1] <= k[1] for k in kept):
            continue
        kept.append(h)

    out = []
    for start, end, label, value in kept:
        idx = 0
        for i, (s, e) in enumerate(spans):
            if s <= start < e:
                idx = i
                break
        if exempt[idx]:
            continue
        s, e = spans[idx]
        out.append((label, value.strip(), line[s:e].strip()))
    return out


def squeeze(text):
    return re.sub(r"\s+", "", text)


def load_fact_log(root):
    path = os.path.join(root, "docs", "fact-log.md")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def file_at(root, rev, path):
    ok, out = run(["git", "show", "%s:%s" % (rev, path)], root)
    return out if ok else ""


def current_file(root, path):
    try:
        with open(os.path.join(root, path), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def contains(haystack, value):
    if not haystack or not value:
        return False
    return value in haystack or squeeze(value) in squeeze(haystack)


def scan(mode):
    root = project_dir()
    base = base_rev(mode, root)
    rows = diff_lines(root, base)
    if not rows:
        return []

    fact_log = load_fact_log(root)
    base_cache = {}
    cur_cache = {}

    def base_text(path):
        if path not in base_cache:
            base_cache[path] = file_at(root, base, path)
        return base_cache[path]

    def cur_text(path):
        if path not in cur_cache:
            cur_cache[path] = current_file(root, path)
        return cur_cache[path]

    findings = []
    seen = set()
    for kind, path, lineno, line in rows:
        for label, value, sentence in find_values(line):
            # 台帳に載っていれば確認済み扱い
            if contains(fact_log, value):
                continue
            if kind == "+":
                # 比較元の同じファイルに既にある値なら、文章だけの書き直し
                if contains(base_text(path), value):
                    continue
                key = ("+", path, lineno, label, value)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    {
                        "kind": "追加",
                        "path": path,
                        "line": lineno,
                        "label": label,
                        "value": value,
                        "sentence": sentence,
                    }
                )
            else:
                # 削除行にあった値が現在のファイルのどこにも残っていなければ、
                # 既存の値を無断で書き換えた／消した疑い
                if contains(cur_text(path), value):
                    continue
                key = ("-", path, label, value)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    {
                        "kind": "既存の値の削除・変更",
                        "path": path,
                        "line": lineno,
                        "label": label,
                        "value": value,
                        "sentence": sentence,
                    }
                )
    return findings


def clip(text, width=80):
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= width else text[: width - 1] + "…"


def finding_lines(findings):
    out = []
    for f in findings[:MAX_SHOWN]:
        out.append(
            "- [%s] %s:%s %s「%s」\n    %s"
            % (f["kind"], f["path"], f["line"], f["label"], f["value"],
               clip(f["sentence"]))
        )
    rest = len(findings) - MAX_SHOWN
    if rest > 0:
        out.append("- ほか %d 件（表示は%d件まで）" % (rest, MAX_SHOWN))
    return out


HOWTO = """
対応は次の3つのいずれかです（推測で埋めるのは不可）:
  ① 一次情報で確認して docs/fact-log.md に出典URLと確認日を追記する
  ② 確認できないなら その記述に【要確認】を付ける
  ③ 書くのをやめる（省く）

あわせて確認してください:
  ・既存の値を出典なしに書き換えていないか
  ・電話番号は ENTRY_CONTACTS（入口の窓口一覧）と各制度の contact の両方にあります。
    片方だけ直すと食い違うので、必ず両方を見ること
""".rstrip()


def warning_text(findings):
    body = "\n".join(finding_lines(findings))
    return (
        "事実チェック: 出典の裏付けが確認できない記述が %d 件あります。\n"
        "掲載しているのは生活保護・手当・貸付・医療費などの制度情報です。"
        "金額・電話番号・担当課・要件を1か所でも間違えると、"
        "困っている方が受けられるはずの支援を逃します。\n\n"
        "%s\n%s" % (len(findings), body, HOWTO)
    )


def read_hook_input():
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    if mode not in ("postedit", "stopgate", "report"):
        sys.stderr.write("usage: factcheck_scan.py {postedit|stopgate|report}\n")
        return 2

    hook_input = {} if mode == "report" else read_hook_input()

    if mode == "stopgate" and hook_input.get("stop_hook_active"):
        # 二重ブロックのループ防止
        return 0

    findings = scan(mode)

    if mode == "report":
        if not findings:
            print("未確認の事実は見つかりませんでした。")
            return 0
        print("未確認の可能性がある記述 %d 件" % len(findings))
        print("")
        print("\n".join(finding_lines(findings)))
        print("")
        print(HOWTO)
        return 0

    if not findings:
        return 0

    text = warning_text(findings)

    if mode == "postedit":
        print(
            json.dumps(
                {
                    "systemMessage": "事実チェック: 未確認の記述 %d 件" % len(findings),
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": text,
                    },
                },
                ensure_ascii=False,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "decision": "block",
                "reason": text
                + "\n\n報告は、✅出典で確認済みの内容と、"
                "⚠️確認できず【要確認】のまま残した内容を分けて書いてください。",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
