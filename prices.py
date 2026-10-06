"""Поиск цен на странице/в тексте, конвертация валют (USD, EUR, AZN) и поиск контактов."""
import re
import statistics
import time
from collections import Counter
from urllib.parse import urljoin

import requests
from bs4 import Comment

CUR = {"$": "USD", "us$": "USD", "€": "EUR", "£": "GBP", "₼": "AZN", "₽": "RUB", "₸": "KZT",
       "₴": "UAH", "₺": "TRY", "₹": "INR", "usd": "USD", "eur": "EUR", "gbp": "GBP", "azn": "AZN",
       "rub": "RUB", "uah": "UAH", "kzt": "KZT", "manat": "AZN", "манат": "AZN", "руб": "RUB", "грн": "UAH"}
_L = "A-Za-zА-Яа-яЁё"
SYM = r"(?:US\$|[$€£₼₽₸₴₺₹])"
COD = rf"(?<![{_L}])(?:USD|EUR|GBP|AZN|RUB|UAH|KZT|manat|манат|руб|грн)(?![{_L}])"
NUM = r"\d{1,3}(?:[ \u00a0,.]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
PRICE_RE = re.compile(rf"(?P<c1>{SYM}|{COD})\.?\s?(?P<n1>{NUM})|(?P<n2>{NUM})\s?(?P<c2>{SYM}|{COD})", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{7,}\d(?!\w)")


def parse_num(s: str):
    s = s.replace("\u00a0", "").replace(" ", "")
    try:
        if "," in s and "." in s:
            dec = "," if s.rfind(",") > s.rfind(".") else "."
            thou = "." if dec == "," else ","
            s = s.replace(thou, "").replace(dec, ".")
        elif "," in s:
            a, b = s.rsplit(",", 1)
            s = a.replace(",", "") + "." + b if len(b) <= 2 else s.replace(",", "")
        elif s.count(".") > 1:
            s = s.replace(".", "")
        elif "." in s:
            a, b = s.split(".")
            if len(b) == 3 and a != "0":
                s = a + b
        return float(s)
    except ValueError:
        return None


def iter_prices(text: str):
    for m in PRICE_RE.finditer(text):
        cur = CUR.get((m.group("c1") or m.group("c2")).lower())
        amt = parse_num(m.group("n1") or m.group("n2"))
        if cur and amt and amt > 0:
            yield m.group(0).strip(), amt, cur


# ---------- где на странице цена и чья она ----------
def nearest_name(node, base):
    p = node.parent
    for _ in range(5):
        if p is None or p.name in ("html", "body", "[document]"):
            break
        if len(PRICE_RE.findall(p.get_text(" "))) > 1:   # контейнер уже охватывает несколько товаров
            break
        for c in p.find_all(["h1", "h2", "h3", "h4", "h5", "a"], limit=8):
            t = c.get("title")
            if not t and c.name != "a":
                inner = c.find("a", title=True)
                t = inner.get("title") if inner else None
            t = (t or c.get_text(" ", strip=True)).strip()
            if len(t) >= 3 and not PRICE_RE.search(t):
                a = c if c.name == "a" else c.find("a", href=True)
                href = a.get("href") if a else None
                return t[:120], (urljoin(base, href) if href else "")
        p = p.parent
    return "", ""


def site_prices(soup, base, title=""):
    items, seen = [], set()
    for node in soup.find_all(string=True):
        if len(items) >= 400:
            break
        s = str(node)
        if isinstance(node, Comment) or len(s) > 200 or not any(ch.isdigit() for ch in s):
            continue
        found = list(iter_prices(s))
        if not found:
            continue
        raw, amt, cur = found[0]
        name, link = nearest_name(node, base)
        name = name or title
        if (name, amt, cur) in seen:
            continue
        seen.add((name, amt, cur))
        items.append({"name": name, "raw": raw, "amount": amt, "currency": cur, "url": link})
    return items


def text_prices(text):
    items, prev = [], ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        found = list(iter_prices(line))
        if found:
            raw, amt, cur = found[0]
            name = PRICE_RE.sub("", line).strip(" .:-—|\t") or prev
            items.append({"name": name[:120], "raw": raw, "amount": amt, "currency": cur, "url": ""})
        else:
            prev = line
        if len(items) >= 400:
            break
    return items


# ---------- курсы валют ----------
_cache = {"t": 0.0, "data": None}
FALLBACK = {"USD": 1, "EUR": 0.92, "GBP": 0.78, "AZN": 1.7, "RUB": 90, "TRY": 34,
            "UAH": 41, "KZT": 480, "INR": 83}


def get_rates():
    if _cache["data"] and time.time() - _cache["t"] < 3600:
        return _cache["data"]
    try:
        j = requests.get("https://open.er-api.com/v6/latest/USD", timeout=6).json()
        data = {"rates": j["rates"], "source": "open.er-api.com", "updated": j.get("time_last_update_utc", "")}
        _cache.update(t=time.time(), data=data)
    except Exception:
        data = {"rates": FALLBACK, "source": "запасные курсы (нет доступа к интернету)", "updated": ""}
        _cache.update(t=time.time() - 3300, data=data)   # повторить попытку через 5 минут
    return data


def enrich_prices(items):
    if not items:
        return [], None
    rt = get_rates()
    r = rt["rates"]
    for it in items:
        rate = r.get(it["currency"])
        if rate:
            usd = it["amount"] / rate
            it.update(usd=round(usd, 2), eur=round(usd * r.get("EUR", 0), 2), azn=round(usd * r.get("AZN", 0), 2))
        else:
            it.update(usd=None, eur=None, azn=None)
    summ = {"count": len(items), "currencies": dict(Counter(i["currency"] for i in items)),
            "rates": {k: r.get(k) for k in ("EUR", "AZN", "GBP", "RUB", "TRY")},
            "source": rt["source"], "updated": rt["updated"]}
    conv = [(i, it) for i, it in enumerate(items) if it["usd"] is not None]
    if conv:
        vals = [it["usd"] for _, it in conv]
        avg, med = statistics.mean(vals), statistics.median(vals)
        summ.update(min_i=min(conv, key=lambda x: x[1]["usd"])[0], max_i=max(conv, key=lambda x: x[1]["usd"])[0],
                    avg_usd=round(avg, 2), avg_eur=round(avg * r.get("EUR", 0), 2), avg_azn=round(avg * r.get("AZN", 0), 2),
                    med_usd=round(med, 2), med_eur=round(med * r.get("EUR", 0), 2), med_azn=round(med * r.get("AZN", 0), 2))
    return items, summ


# ---------- контакты ----------
def find_contacts(text, hrefs=()):
    emails, phones = set(EMAIL_RE.findall(text)), []
    for h in hrefs:
        low = h.lower()
        if low.startswith("mailto:"):
            emails.add(h[7:].split("?")[0])
        elif low.startswith("tel:"):
            phones.append(h[4:])
    for m in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", m)
        if 9 <= len(digits) <= 15 and (m.startswith("+") or re.search(r"[\s()-]", m)) \
                and not re.match(r"\d{4}-\d{2}-\d{2}", m):
            phones.append(m.strip())
    return {"emails": sorted(e for e in emails if e)[:100], "phones": list(dict.fromkeys(phones))[:100]}


def text_extras(text):
    prices, summ = enrich_prices(text_prices(text))
    return {"prices": prices, "price_summary": summ, "contacts": find_contacts(text)}