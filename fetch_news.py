#!/usr/bin/env python3
"""每天抓 RSS 新聞 → 擷取內文 → （可選）交給免費的 Gemini API 做整理與分析 → 輸出 docs/index.html。
只用 Python 內建模組，不需要 pip install。
沒有設定 GEMINI_API_KEY 時，仍會產生新聞清單，只是沒有 AI 整理與分析。
"""
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
PER_FEED = 8            # 每個來源最多顯示幾則
SUMMARY_MAX = 110       # RSS 原始摘要最多幾個字
ANALYZE_N = 12          # 交給 AI 分析的文章數
BODY_MAX = 1800         # 每篇內文最多送出幾個字
DEFAULT_MODEL = "gemini-3.8-flash"   # 可用環境變數 GEMINI_MODEL 改成你在 AI Studio 看到的模型名稱

# (分類, 顯示名稱, 網址, 是否顯示 RSS 摘要)
# 網址失效時程式會跳過並在頁尾列出，其他來源不受影響。
FEEDS = [
    ("國際", "BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", True),
    ("國際", "Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", True),
    ("國際", "NPR World", "https://feeds.npr.org/1004/rss.xml", True),
    ("國際", "自由時報・國際", "https://news.ltn.com.tw/rss/world.xml", True),
    ("國際", "Google 新聞・國際", "https://news.google.com/rss/headlines/section/topic/WORLD?hl=zh-TW&gl=TW&ceid=TW:zh-Hant", False),
    ("財經", "BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", True),
    ("財經", "自由時報・財經", "https://news.ltn.com.tw/rss/business.xml", True),
    ("財經", "Google 新聞・財經", "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=zh-TW&gl=TW&ceid=TW:zh-Hant", False),
    ("台灣", "Google 新聞・台灣", "https://news.google.com/rss/headlines/section/topic/NATION?hl=zh-TW&gl=TW&ceid=TW:zh-Hant", False),
]
CATEGORY_ORDER = ["國際", "財經", "台灣"]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (personal daily news page)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(el, *names):
    for c in el:
        if local(c.tag) in names and (c.text or "").strip():
            return c.text.strip()
    return ""


def atom_link(el):
    for c in el:
        if local(c.tag) == "link":
            if c.get("href") and c.get("rel", "alternate") == "alternate":
                return c.get("href")
            if (c.text or "").strip():
                return c.text.strip()
    return ""


def parse_date(text):
    if not text:
        return None
    try:
        d = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(TZ)


def clean(text):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_feed(raw):
    root = ET.fromstring(raw)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = clean(child_text(el, "title"))
        link = child_text(el, "link") if local(el.tag) == "item" else atom_link(el)
        if local(el.tag) == "item" and not link:
            link = atom_link(el)
        when = parse_date(child_text(el, "pubDate", "published", "updated", "date"))
        summary = clean(child_text(el, "description", "summary", "content"))
        if title and link and when:
            items.append({"title": title, "link": link, "when": when, "summary": summary})
    return items


def collect(target_date, fetcher=fetch):
    grouped = {c: [] for c in CATEGORY_ORDER}
    errors = []
    seen = set()
    for cat, name, url, show_summary in FEEDS:
        try:
            items = parse_feed(fetcher(url))
        except Exception as e:  # 單一來源失敗不影響其他來源
            errors.append(f"{name}（{type(e).__name__}）")
            continue
        day = [i for i in items if i["when"].date() == target_date]
        day.sort(key=lambda i: i["when"], reverse=True)
        for i in day[:PER_FEED]:
            key = i["link"] or i["title"]
            if key in seen:
                continue
            seen.add(key)
            s = i["summary"] if show_summary else ""
            if s.strip().lower() == i["title"].strip().lower():
                s = ""
            if len(s) > SUMMARY_MAX:
                s = s[:SUMMARY_MAX].rstrip() + "…"
            grouped[cat].append({**i, "source": name, "summary": s, "ai_summary": ""})
    return grouped, errors


# ---------- 內文擷取 ----------
class BodyParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.in_p = False
        self.buf = []
        self.paras = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "nav", "footer", "aside", "form"):
            self.skip += 1
        elif tag == "p" and not self.skip:
            self.in_p = True
            self.buf = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "nav", "footer", "aside", "form"):
            self.skip = max(0, self.skip - 1)
        elif tag == "p" and self.in_p:
            self.in_p = False
            t = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if len(t) >= 40:
                self.paras.append(t)

    def handle_data(self, data):
        if self.in_p and not self.skip:
            self.buf.append(data)


def fetch_body(url, fetcher=fetch):
    """抓文章頁面並取出段落文字。失敗或抓不到就回傳空字串。"""
    if "news.google.com" in url:   # Google 新聞連結是跳轉頁，抓不到內文
        return ""
    try:
        raw = fetcher(url)
        p = BodyParser()
        p.feed(raw.decode("utf-8", errors="replace"))
        return " ".join(p.paras)[:BODY_MAX]
    except Exception:
        return ""


# ---------- AI 分析（可選）----------
def pick_for_analysis(grouped, n=ANALYZE_N):
    pools = {c: [i for i in grouped[c]] for c in CATEGORY_ORDER}
    for c in pools:   # 有直接連結的文章優先（才抓得到內文）
        pools[c].sort(key=lambda i: "news.google.com" in i["link"])
    picked = []
    while len(picked) < n and any(pools.values()):
        for c in CATEGORY_ORDER:
            if pools[c] and len(picked) < n:
                picked.append(pools[c].pop(0))
    return picked


def build_prompt(date, picked):
    lines = []
    for idx, i in enumerate(picked):
        text = i.get("body") or i["summary"] or "（沒有內文，只有標題）"
        lines.append(f"[{idx}] 來源：{i['source']}\n標題：{i['title']}\n內文：{text}\n")
    return (
        f"你是新聞編輯助理。以下是 {date} 從各媒體抓取的文章，每篇有編號、來源、標題與內文片段。\n"
        "請只根據提供的內容，用繁體中文輸出 JSON，不要輸出其他文字。\n"
        "規則：\n"
        "1. 不得加入內文沒有的事實。\n"
        "2. 單一來源的說法、或內文提到尚未證實的消息，必須註明。\n"
        "3. 分析要區分事實與推論，推論要明說是推論。\n"
        "4. 不給投資建議，不預測結果。\n"
        "5. 只有標題、內文不足時，summary 請寫「資料不足，僅依標題」。\n"
        "6. 用自己的話改寫，不要照抄原文。\n"
        "輸出格式：\n"
        '{"headline":"一句話總結昨天","stories":[{"id":編號,"summary":"2到3句"}],'
        '"analysis":[{"title":"主題","body":"3到5句，標明哪些是推論"}],"watch":["接下來要留意的事"]}\n'
        "analysis 請寫 3 個主題，watch 請寫 3 到 4 項。\n\n"
        + "\n".join(lines)
    )


def call_gemini(prompt, key, model):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["candidates"][0]["content"]["parts"][0]["text"]


def analyze(date, grouped, fetcher=fetch, ai=None):
    """回傳 (analysis_dict 或 None, 錯誤訊息或 None)。"""
    if ai is None:
        return None, "未設定 GEMINI_API_KEY，略過 AI 分析"
    picked = pick_for_analysis(grouped)
    if not picked:
        return None, None
    for i in picked:
        i["body"] = fetch_body(i["link"], fetcher)
    try:
        data = json.loads(ai(build_prompt(date, picked)))
        for s in data.get("stories", []):
            idx = s.get("id")
            if isinstance(idx, int) and 0 <= idx < len(picked) and isinstance(s.get("summary"), str):
                picked[idx]["ai_summary"] = s["summary"]
        for i in picked:
            i.pop("body", None)
        return {
            "headline": str(data.get("headline", "")),
            "analysis": [a for a in data.get("analysis", []) if isinstance(a, dict)],
            "watch": [str(w) for w in data.get("watch", [])],
        }, None
    except Exception as e:
        for i in picked:
            i.pop("body", None)
        return None, f"AI 分析失敗（{type(e).__name__}）"


# ---------- 輸出頁面 ----------
CSS = """
:root{--bg:#eff2ef;--surface:#fbfcfb;--ink:#18211d;--muted:#58655e;--line:#d2dad4;--accent:#8c2f3d;--tint:#f6e9eb;
box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
@media (prefers-color-scheme:dark){:root{--bg:#101613;--surface:#18211c;--ink:#e7eee9;--muted:#9aa9a0;--line:#2a372f;--accent:#e58c9a;--tint:#2b1a1e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:400 16px/1.75 "Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:28px 18px 56px}
h1{margin:0;font-size:1.7rem;line-height:1.3}
h2{margin:32px 0 10px;font-size:1.2rem;color:var(--accent)}
.when{color:var(--muted);font-size:.9rem;margin:6px 0 0}
.lead{margin:18px 0 0;padding:14px 16px;background:var(--surface);border:1px solid var(--line);border-left:5px solid var(--accent);border-radius:10px;font-weight:500}
.ai{background:var(--tint);border:1px solid var(--line);border-radius:12px;padding:4px 18px 12px}
.ai h3{margin:16px 0 4px;font-size:1rem;color:var(--accent)}
.ai p{margin:0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin-top:10px}
.card a{color:var(--ink);font-weight:500;text-decoration:none}
.card a:hover{text-decoration:underline}
.meta{color:var(--muted);font-size:.8rem;margin:2px 0 0}
.sum{margin:4px 0 0;color:var(--muted);font-size:.92rem}
.body{margin:6px 0 0;font-size:.95rem}
.none{color:var(--muted)}
.fine{color:var(--muted);font-size:.85rem;margin-top:28px}
.fine a{color:var(--accent)}
ul.watch{margin:0;padding-left:20px}
a:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
"""


def render(target_date, grouped, errors, archive_dates, analysis=None, ai_note=None):
    e = html.escape
    wk = "一二三四五六日"[target_date.weekday()]
    out = [
        '<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
        f"<title>昨日新聞 {target_date.isoformat()}</title>",
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;700&display=swap">',
        f"<style>{CSS}</style></head><body><main>",
        f"<h1>{target_date.year} 年 {target_date.month} 月 {target_date.day} 日（週{wk}）新聞</h1>",
        f'<p class="when">更新時間：{datetime.now(TZ).strftime("%Y-%m-%d %H:%M")}（台北）· 程式自動抓取各媒體 RSS</p>',
    ]
    if analysis:
        if analysis["headline"]:
            out.append(f'<p class="lead">{e(analysis["headline"])}</p>')
        out.append("<h2>分析</h2><div class=\"ai\">")
        for a in analysis["analysis"]:
            out.append(f'<h3>{e(str(a.get("title", "")))}</h3><p>{e(str(a.get("body", "")))}</p>')
        out.append("</div>")
        if analysis["watch"]:
            out.append("<h2>接下來要留意</h2><ul class=\"watch\">" + "".join(f"<li>{e(w)}</li>" for w in analysis["watch"]) + "</ul>")
        out.append('<p class="meta">分析與重點整理由 AI（Gemini）根據上方文章內容自動產生，可能有誤，僅供參考，不構成投資建議。</p>')
    total = 0
    for cat in CATEGORY_ORDER:
        out.append(f"<h2>{e(cat)}</h2>")
        if not grouped[cat]:
            out.append('<p class="none">這一天沒有抓到這個分類的文章。</p>')
        for i in grouped[cat]:
            total += 1
            out.append('<div class="card">')
            out.append(f'<a href="{e(i["link"], quote=True)}" target="_blank" rel="noopener">{e(i["title"])}</a>')
            out.append(f'<p class="meta">{e(i["source"])} · {i["when"].strftime("%H:%M")}</p>')
            if i.get("ai_summary"):
                out.append(f'<p class="body">{e(i["ai_summary"])}</p>')
            elif i["summary"]:
                out.append(f'<p class="sum">{e(i["summary"])}</p>')
            out.append("</div>")
    out.append('<p class="fine">')
    if ai_note:
        out.append(e(ai_note) + "<br>")
    if errors:
        out.append("本次抓取失敗的來源：" + "、".join(e(x) for x in errors) + "<br>")
    if archive_dates:
        out.append("過去的日期：" + " · ".join(f'<a href="{d}.html">{d}</a>' for d in archive_dates))
    out.append("</p></main></body></html>")
    return "\n".join(out), total


def main():
    target = datetime.now(TZ).date() - timedelta(days=1)
    out_dir = Path("docs")
    out_dir.mkdir(exist_ok=True)
    grouped, errors = collect(target)
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL
    ai = (lambda p: call_gemini(p, key, model)) if key else None
    analysis, ai_note = analyze(target.isoformat(), grouped, fetch, ai)
    archive = sorted(
        (p.stem for p in out_dir.glob("20??-??-??.html") if p.stem != target.isoformat()),
        reverse=True,
    )[:30]
    page, total = render(target, grouped, errors, archive, analysis, ai_note)
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    (out_dir / f"{target.isoformat()}.html").write_text(page, encoding="utf-8")
    print(f"{target}: {total} 則；來源失敗：{errors or '無'}；AI：{'成功' if analysis else ai_note}")
    if total == 0 and len(errors) == len(FEEDS):
        sys.exit("所有來源都抓取失敗")


if __name__ == "__main__":
    main()
