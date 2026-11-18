"""
取得 Census MARTS（Advance Monthly Retail Trade Survey）季調銷售額。三種來源輸出相同格式：

- fetch_zip：Census 時間序列網站的整包下載（排程預設使用，不需要 API key）
      https://www.census.gov/econ_getzippedfile/?programCode=MARTS
- read_zip：手動下載的同一個 zip 檔（備援）
- fetch_api：Census Data API（備援；需要 API key，2026/5/12 起所有資料查詢都要 key）

輸出：
  categories: {代碼: 說明}            例如 {"44X72": "Retail Trade and Food Services", ...}
  months:     ["1992-01", ...]          只保留至少有一個類別有數字的月份
  sa:         {代碼: {月份: 百萬美元}}  只取季調銷售額（SM、is_adj=1、非誤差資料）
  meta:       {"updated": "2026-11-17", "updated_raw": "..."}
"""
import csv
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

ZIP_URL = "https://www.census.gov/econ_getzippedfile/?programCode=MARTS"
API_URL = "https://api.census.gov/data/timeseries/eits/marts"
SCHEDULE_URLS = ["https://www.census.gov/retail/release_schedule.html",          # 零售調查自己的發布時程
                 "https://www.census.gov/economic-indicators/calendar-listview.html"]  # 全部經濟指標行事曆
# Census 網站前面有 Cloudflare：Python 預設的 User-Agent（Python-urllib/3.x）會被擋成 403，一定要自訂
USER_AGENT = "retail-sales-driver/1.0 (GitHub Actions; macro research dashboard)"
MONTHS_EN = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _get(url, tries=4, timeout=120):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (400, 401, 404):
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(10 * (k + 1))
    raise RuntimeError(f"連不上 Census（{url.split('?')[0]}）：{last}")


# ---------------------------------------------------------------- 整包下載（zip）
def fetch_zip():
    raw = _get(ZIP_URL)
    if not raw.startswith(b"PK"):
        head = raw[:200].decode("latin-1", "replace").replace("\n", " ")
        raise RuntimeError(f"Census 回傳的不是 zip 檔（可能被擋或網站維護中）：{head}")
    return parse_zip(raw)


def read_zip(path):
    with open(path, "rb") as f:
        return parse_zip(f.read())


def parse_zip(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    # zip 裡另有一個檔名是絕對路徑 "/README" 的說明檔，直接略過，只讀 CSV
    names = [n for n in z.namelist() if n.lower().endswith(".csv")]
    if not names:
        raise RuntimeError(f"zip 裡找不到 CSV（內容：{z.namelist()}）")
    return parse_csv(z.read(names[0]).decode("latin-1"))


def _sections(text):
    """CSV 以空行分成 CATEGORIES、DATA TYPES、ERROR TYPES、GEO LEVELS、TIME PERIODS、NOTES、DATA UPDATED ON、DATA 八段。"""
    out = {}
    for block in re.split(r"\r?\n[ \t]*\r?\n", text):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if lines:
            out[lines[0].strip()] = lines[1:]
    return out


def _table(lines):
    return list(csv.DictReader(io.StringIO("\n".join(lines))))


def _month(per_name):
    # "Oct-2026" → "2026-10"
    m = re.fullmatch(r"([A-Z][a-z]{2})-(\d{4})", per_name.strip())
    if not m:
        raise RuntimeError(f"看不懂的月份格式：{per_name}")
    return f"{m.group(2)}-{MONTHS_EN.index(m.group(1)) + 1:02d}"


def parse_csv(text):
    sec = _sections(text)
    need = ["CATEGORIES", "DATA TYPES", "TIME PERIODS", "DATA"]
    miss = [s for s in need if s not in sec]
    if miss:
        raise RuntimeError(f"CSV 少了這些區段：{', '.join(miss)}（找到的區段：{', '.join(sec)}）")

    categories, cat_of = {}, {}
    for r in _table(sec["CATEGORIES"]):
        code = r["cat_code"].strip()
        desc = r["cat_desc"].split(":", 1)[-1].strip()          # "441: Motor Vehicle..." → "Motor Vehicle..."
        categories[code] = desc
        cat_of[r["cat_idx"]] = code
    dt = {r["dt_idx"]: r["dt_code"].strip() for r in _table(sec["DATA TYPES"])}
    per = {r["per_idx"]: _month(r["per_name"]) for r in _table(sec["TIME PERIODS"])}
    sm = [k for k, v in dt.items() if v == "SM"]
    if not sm:
        raise RuntimeError(f"CSV 裡沒有銷售額（SM）資料類型：{list(dt.values())}")

    sa = {c: {} for c in categories}
    bad = set()
    for r in _table(sec["DATA"]):
        if r.get("et_idx", "0") != "0" or r["dt_idx"] not in sm or r["is_adj"] != "1":
            continue
        v = r["val"].strip()
        try:
            sa[cat_of[r["cat_idx"]]][per[r["per_idx"]]] = float(v)
        except ValueError:
            bad.add(v)          # "NA"、"Z" 等：當作缺值
    months = sorted({m for s in sa.values() for m in s})

    upd_raw = " ".join(sec.get("DATA UPDATED ON", [])).strip()
    return categories, months, sa, {"updated": _parse_updated(upd_raw), "updated_raw": upd_raw, "non_numeric": sorted(bad)}


def _parse_updated(s):
    # "Tuesday, 17-Nov-26 07:30:44 EST" → "2026-11-17"
    m = re.search(r"(\d{1,2})-([A-Z][a-z]{2})-(\d{2,4})", s)
    if not m:
        return ""
    y = int(m.group(3))
    y = y + 2000 if y < 100 else y
    return f"{y}-{MONTHS_EN.index(m.group(2)) + 1:02d}-{int(m.group(1)):02d}"


# ---------------------------------------------------------------- Census Data API（備援）
def fetch_api(key):
    if not key:
        raise RuntimeError("沒有設定 CENSUS_API_KEY，無法使用 API 備援")
    q = urllib.parse.urlencode({
        "get": "data_type_code,seasonally_adj,category_code,cell_value,error_data",
        "for": "us:*", "time": "from 1992", "key": key,
    })
    raw = _get(f"{API_URL}?{q}")
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("Census API 回傳的不是 JSON（key 可能無效）：" + raw[:200].decode("latin-1", "replace"))
    head, rows = rows[0], rows[1:]
    ix = {h: i for i, h in enumerate(head)}
    yes = lambda s: str(s).strip().lower() in ("yes", "y", "1", "true")
    sa, cats = {}, {}
    for r in rows:
        if r[ix["data_type_code"]] != "SM" or not yes(r[ix["seasonally_adj"]]) or yes(r[ix["error_data"]]):
            continue
        code, t = r[ix["category_code"]], r[ix["time"]][:7]
        cats.setdefault(code, "")
        try:
            sa.setdefault(code, {})[t] = float(r[ix["cell_value"]])
        except (TypeError, ValueError):
            pass
    months = sorted({m for s in sa.values() for m in s})
    return cats, months, sa, {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "updated_raw": "API", "non_numeric": []}


# ---------------------------------------------------------------- 下次發布日期（選用，失敗不影響更新）
FULL_MONTHS = "January February March April May June July August September October November December".split()
_MON = "|".join(FULL_MONTHS)


def fetch_next_release(after):
    """找出 after（YYYY-MM-DD）之後最近一次零售銷售初值的發布日。

    零售調查的時程頁是「資料月份 → 發布日期」的表格（例如 "November 2026 December 16, 2026"）；
    經濟指標行事曆是 "Advance Monthly Sales for Retail and Food Services December 16, 2026 8:30 AM"。
    兩頁都查，取 after 之後最早的日期。
    """
    pats = [rf"(?:{_MON}) \d{{4}} ((?:{_MON}) \d{{1,2}}, \d{{4}})",
            rf"Advance Monthly Sales for Retail and Food Services\s+((?:{_MON}) \d{{1,2}}, \d{{4}})"]
    found = []
    for url, pat in zip(SCHEDULE_URLS, pats):
        try:
            html = _get(url, tries=2, timeout=60).decode("utf-8", "replace")
        except RuntimeError:
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
        for m in re.finditer(pat, text):
            mo, d, y = re.match(r"(\w+) (\d+), (\d+)", m.group(1)).groups()
            iso = f"{y}-{FULL_MONTHS.index(mo) + 1:02d}-{int(d):02d}"
            if iso > after:
                found.append(iso)
        if found:
            break
    return min(found) if found else ""
