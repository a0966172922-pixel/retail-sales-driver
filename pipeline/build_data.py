"""
產生 Retail Sales Driver 的資料檔。

  從 Census 整包下載（排程使用，不需要 key）：
      python pipeline/build_data.py --out data/retail.json

  從手動下載的 zip（備援；在 currentdata 網頁或下面網址下載 MARTS-mf.zip）：
      https://www.census.gov/econ_getzippedfile/?programCode=MARTS
      python pipeline/build_data.py --zip MARTS-mf.zip --out data/retail.json

  整包下載失敗時，若有設定環境變數 CENSUS_API_KEY，會自動改用 Census Data API。
"""
import argparse
import json
import os
import sys
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sources  # noqa: E402

TOLERANCE = 2          # 百萬美元：官方彙總與細項加總容許的四捨五入誤差（實測歷來都是 0）
MIN_MONTHS = 25


def load_structure():
    with open(os.path.join(HERE, "structure.json"), encoding="utf-8") as f:
        return json.load(f)


def leaf_ids(struct):
    nodes = {n["id"]: n for n in struct["nodes"]}
    leaves = {l["id"] for l in struct["leaves"]}
    memo = {}

    def walk(i):
        if i in leaves:
            return [i]
        if i not in memo:
            memo[i] = [x for k in nodes[i]["kids"] for x in walk(k)]
        return memo[i]
    return {i: walk(i) for i in nodes}


def check_categories(struct, categories, with_desc):
    want = dict(struct["census_categories"])
    problems = []
    for code in want:
        if code not in categories:
            problems.append(f"Census 沒有類別 {code}（{want[code]}）")
    for code in categories:
        if code not in want:
            problems.append(f"Census 多了新類別 {code}（{categories[code]}）")
    if with_desc:
        norm = lambda s: " ".join("".join(c.lower() if c.isalnum() else " " for c in s).split())
        for code, desc in want.items():
            got = categories.get(code)
            if got and SequenceMatcher(None, norm(desc), norm(got)).ratio() < 0.6:
                problems.append(f"{code} 原本是「{desc}」，現在是「{got}」")
    if problems:
        raise RuntimeError("Census 的類別和 structure.json 不同，可能改了分類（例如改用 2022 年版 NAICS），"
                           "需要更新 pipeline/structure.json：\n  " + "\n  ".join(problems[:15]))


def build(struct, categories, months_all, sa, meta, source):
    check_categories(struct, categories, with_desc=(source != "api"))
    head = sa.get("44X72", {})
    if not head:
        raise RuntimeError("沒有零售及餐飲服務總計（44X72）的季調資料")
    first, last = min(head), max(head)
    months = [m for m in months_all if first <= m <= last]
    codes = [c for c, _ in struct["census_categories"]]
    holes = [f"{c} {m}" for c in codes for m in months if m not in sa.get(c, {})]
    if holes:
        raise RuntimeError(f"有 {len(holes)} 個缺值，例如：{', '.join(holes[:8])}")
    if len(months) < MIN_MONTHS:
        raise RuntimeError(f"月份太少（{len(months)} 個月）")
    # 月份要連續
    def nxt(m):
        y, mo = map(int, m.split("-"))
        return f"{y + mo // 12}-{mo % 12 + 1:02d}"
    for a, b in zip(months, months[1:]):
        if nxt(a) != b:
            raise RuntimeError(f"月份不連續：{a} 之後是 {b}")

    # 最細項目（含 441−441X 等差額）
    leaves = {}
    for l in struct["leaves"]:
        vals = [round(sum(sa[c][m] for c in l["plus"]) - sum(sa[c][m] for c in l["minus"])) for m in months]
        neg = [months[i] for i, v in enumerate(vals) if v <= 0]
        if neg:
            raise RuntimeError(f"最細項目 {l['id']}（{l['zh']}）在 {neg[0]} 等 {len(neg)} 個月不是正數，類別定義可能改變")
        leaves[l["id"]] = vals

    # 驗證：每個有 Census 官方序列的節點 = 底下最細項目加總
    lid = leaf_ids(struct)
    problems = []
    for n in struct["nodes"]:
        c = n.get("census")
        if not c:
            continue
        for i, m in enumerate(months):
            s = sum(leaves[x][i] for x in lid[n["id"]])
            if abs(s - sa[c][m]) > TOLERANCE:
                problems.append(f"{n['zh']}（{c}）{m}：細項加總 {s:,.0f}，官方 {sa[c][m]:,.0f}")
                break
    if problems:
        raise RuntimeError("細項加總與 Census 官方彙總不符，類別定義可能改變：\n  " + "\n  ".join(problems))

    y, mo = months[-1].split("-")
    upd = meta.get("updated", "")
    label = f"Census {upd.replace('-', '/')} 版" if upd else "Census"
    data = {
        "source": source,
        "updated": upd,
        "vintage": f"{label} · 資料至 {y} 年 {int(mo)} 月",
        "nextRelease": "",
        "months": months,
        "leaves": leaves,
        "structure": {k: struct[k] for k in ("leaves", "nodes", "views", "fixed_slots", "picker")},
    }
    data["structure"]["leaves"] = [{k: l[k] for k in ("id", "zh", "en", "naics") if k in l} | ({"note": l["note"]} if "note" in l else {})
                                   for l in struct["leaves"]]
    summary = (f"{months[0]} → {months[-1]}（{len(months)} 個月）| 最細項目 {len(leaves)} | "
               f"總計 {sa['44X72'][months[-1]]:,.0f} 百萬美元 | 版本 {meta.get('updated_raw') or '—'}")
    return data, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", help="手動下載的 MARTS-mf.zip（不指定就從 Census 下載）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-calendar", action="store_true", help="不查下次發布日期")
    a = ap.parse_args()

    try:
        struct = load_structure()
        if a.zip:
            got, source = sources.read_zip(a.zip), "file"
        else:
            try:
                got, source = sources.fetch_zip(), "zip"
            except RuntimeError as e:
                key = os.environ.get("CENSUS_API_KEY", "").strip()
                if not key:
                    raise
                print(f"::warning::整包下載失敗，改用 Census Data API：{e}")
                got, source = sources.fetch_api(key), "api"
        data, summary = build(struct, *got, source)

        # 下次發布日期：查不到時沿用舊檔裡還沒過期的日期，避免資料沒變卻因為這一欄而重新部署
        nxt = "" if a.no_calendar else sources.fetch_next_release(data["updated"] or data["months"][-1] + "-28")
        if not nxt and os.path.exists(a.out):
            try:
                old = json.load(open(a.out, encoding="utf-8")).get("nextRelease", "")
                if old and old > (data["updated"] or ""):
                    nxt = old
            except (OSError, ValueError):
                pass
        data["nextRelease"] = nxt
    except Exception as e:
        # GitHub 的 Annotations 只顯示每則錯誤的第一行，所以每一行分開輸出
        for line in str(e).splitlines():
            if line.strip():
                print(f"::error::{line.strip()}")
        sys.exit(1)

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(data["vintage"], "|", summary, "| 下次發布", data["nextRelease"] or "—", "| 來源", data["source"])


if __name__ == "__main__":
    main()
