"""Moscow road congestion score (ЦОДД 'баллы', 0-10) for 2025 from the public
Telegram channel of Deptrans "Дептранс Оперативно" (t.me/DtOperativno).

The channel posts "По данным ЦОДД, на дорогах N баллов" several times on many
(not all) days, mostly at ~14:00 and ~18:00-19:00 MSK. We scrape the public web
preview https://t.me/s/DtOperativno?before=<id>, parse every post, pull the
*current* score (forecast phrases such as "ожидается до N баллов" are skipped)
and the average flow speed ("средняя скорость ... N км/ч") when present.

Outputs (next to this script):
  traffic_moscow_2025.csv        date,value,source_url   value = daily MAX score
  traffic_moscow_2025_posts.csv  post-level: dt_msk,score,speed_kmh,source_url

Usage:  python traffic_fetch.py            # crawl 2025 (ids 19894..24472); cached posts are kept,
                                           # delete dtoperativno_2025.jsonl for a full re-scrape
        python traffic_fetch.py --offline  # re-parse cached jsonl only
No key needed. ~150 page requests, ~2-3 minutes.
"""
import csv, html, json, os, re, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

CHAN = "DtOperativno"
START_BEFORE = 24473          # crawl backwards from here (24472 = 2026-01-01 00:30 MSK, filtered out)
STOP_DATE = "2024-12-31"      # stop when a page reaches this date (UTC)
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "dtoperativno_2025.jsonl")
MSK = timezone(timedelta(hours=3))

msg_re = re.compile(r'<div class="tgme_widget_message_wrap.*?(?=<div class="tgme_widget_message_wrap|\Z)', re.S)
post_re = re.compile(r'data-post="[^/]+/(\d+)"')
time_re = re.compile(r'<time datetime="([^"]+)"')
text_re = re.compile(r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', re.S)
score_re = re.compile(r'(\d{1,2})\s*бал(?:л|о)', re.I)          # «балл…» и опечатка «балов» (пост 20587)
speed_re = re.compile(r'(?:средн\w*\s+скорост\w*|(?:общ\w*\s+)?скорост\w*\s+(?:движения\s+)?поток\w*)[^.\d]{0,40}?(\d{2})\s*км/ч', re.I)
FORECAST = re.compile(r'(ожида|прогноз|может|возмож|будет|до\s*$|достиг)', re.I)


def clean(s):
    s = re.sub(r'<br\s*/?>', '\n', s)
    return html.unescape(re.sub(r'<[^>]+>', '', s)).strip()


def crawl():
    seen = {}
    if os.path.exists(CACHE):
        for line in open(CACHE, encoding="utf-8"):
            d = json.loads(line); seen[d["id"]] = d
    before = START_BEFORE
    with open(CACHE, "a", encoding="utf-8") as f:
        while True:
            url = f"https://t.me/s/{CHAN}?before={before}"
            for _ in range(4):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
                    break
                except Exception as e:  # noqa
                    print("retry", e, file=sys.stderr); time.sleep(3)
            else:
                break
            ids, mind = [], None
            for b in msg_re.findall(page):
                m = post_re.search(b)
                if not m:
                    continue
                pid = int(m.group(1)); ids.append(pid)
                t = time_re.search(b); dt = t.group(1) if t else ""
                tx = text_re.search(b)
                if dt and (mind is None or dt < mind):
                    mind = dt
                if pid not in seen:
                    rec = {"id": pid, "dt": dt, "text": clean(tx.group(1)) if tx else ""}
                    seen[pid] = rec; f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if not ids:
                break
            print(min(ids), mind, file=sys.stderr)
            if mind and mind[:10] < STOP_DATE:
                break
            before = min(ids); time.sleep(0.7)
    return list(seen.values())


def current_score(text):
    for m in score_re.finditer(text):
        ctx = text[max(0, m.start() - 35):m.start()]
        if FORECAST.search(ctx):
            continue
        v = int(m.group(1))
        if 0 <= v <= 10:
            return v
    return None


def main():
    rows = crawl() if "--offline" not in sys.argv else [json.loads(l) for l in open(CACHE, encoding="utf-8")]
    obs = []
    for r in rows:
        if not r["dt"]:
            continue
        dt = datetime.fromisoformat(r["dt"]).astimezone(MSK)
        if dt.year != 2025:
            continue
        sc = current_score(r["text"])
        sp = speed_re.search(r["text"])
        if sc is None and not sp:
            continue
        obs.append((dt, sc, int(sp.group(1)) if sp else None, f"https://t.me/{CHAN}/{r['id']}"))
    obs.sort()
    with open(os.path.join(HERE, "traffic_moscow_2025_posts.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["dt_msk", "score", "speed_kmh", "source_url"])
        for dt, sc, sp, u in obs:
            w.writerow([dt.strftime("%Y-%m-%d %H:%M"), "" if sc is None else sc, "" if sp is None else sp, u])
    daily = {}
    for dt, sc, sp, u in obs:
        if sc is None:
            continue
        d = dt.date().isoformat()
        if d not in daily or sc > daily[d][0]:
            daily[d] = (sc, u)
    with open(os.path.join(HERE, "traffic_moscow_2025.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["date", "value", "source_url"])
        for d in sorted(daily):
            w.writerow([d, daily[d][0], daily[d][1]])
    print(f"posts with values: {len(obs)}; days with score: {len(daily)}", file=sys.stderr)


if __name__ == "__main__":
    main()
