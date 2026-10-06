"""Scraper Studio — backend (FastAPI).
Запуск: uvicorn app:app --reload   ->  http://127.0.0.1:8000
"""
import io
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pytesseract
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel
from pypdf import PdfReader

from prices import enrich_prices, find_contacts, site_prices, text_extras



def find_tesseract() -> str | None:
    """Ищет tesseract: переменная TESSERACT_CMD, PATH, затем типичные папки Windows."""
    import os
    import shutil

    cands = [os.environ.get("TESSERACT_CMD"), shutil.which("tesseract"),
             r"C:\Program Files\Tesseract-OCR\tesseract.exe",
             r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
             os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
             os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe")]
    return next((c for c in cands if c and os.path.isfile(c)), None)


TESS = find_tesseract()
if TESS:
    pytesseract.pytesseract.tesseract_cmd = TESS
# Если не нашёлся, укажите путь вручную:
# pytesseract.pytesseract.tesseract_cmd = r"D:\Tools\Tesseract-OCR\tesseract.exe"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
app = FastAPI(title="Scraper Studio")


# ---------- Сайты ----------
class ScrapeReq(BaseModel):
    url: str
    engine: str = "bs4"          # "bs4" | "selenium"
    selector: str = ""           # CSS-селектор для выборки (необязательно)
    wait_selector: str = ""      # Selenium: ждать появления элемента
    timeout: int = 20
    headless: bool = True


def fetch_bs4(url: str, timeout: int) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


def fetch_selenium(url: str, timeout: int, wait_selector: str, headless: bool) -> str:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    opts = webdriver.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    for a in ("--no-sandbox", "--disable-gpu", "--window-size=1366,900", f"--user-agent={UA}"):
        opts.add_argument(a)
    driver = webdriver.Chrome(options=opts)  # драйвер ставится автоматически (Selenium Manager)
    try:
        driver.set_page_load_timeout(timeout)
        driver.get(url)
        if wait_selector:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector)))
        return driver.page_source
    finally:
        driver.quit()


def parse_html(html: str, base: str, selector: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    raw_prices = site_prices(soup, base, title)
    contacts = find_contacts(soup.get_text(" "), [a.get("href", "") for a in soup.find_all("a", href=True)])

    links = [{"text": a.get_text(" ", strip=True), "url": urljoin(base, a["href"])}
             for a in soup.find_all("a", href=True)]
    images = [urljoin(base, i["src"]) for i in soup.find_all("img", src=True)]
    tables = []
    for tb in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                for tr in tb.find_all("tr")]
        rows = [r for r in rows if r]
        if rows:
            tables.append(rows)

    if selector:
        if "://" in selector:
            raise HTTPException(400, "В поле «CSS-селектор» указана ссылка. Оставьте поле пустым "
                                     "или введите селектор, например: article.product_pod h3 a")
        try:
            matches = [el.get_text(" ", strip=True) for el in soup.select(selector)]
        except Exception:
            raise HTTPException(400, f"Некорректный CSS-селектор: {selector}")
        text = "\n".join(matches)
    else:
        text = soup.get_text("\n", strip=True)
    return {"title": title, "text": text, "links": links, "images": images,
            "tables": tables, "raw_prices": raw_prices, "contacts": contacts,
            "matches": len(text.splitlines()) if selector else None}


@app.post("/api/scrape")
def scrape(req: ScrapeReq):
    if urlparse(req.url).scheme not in ("http", "https"):
        raise HTTPException(400, "Нужна ссылка, начинающаяся с http:// или https://")
    try:
        html = (fetch_selenium(req.url, req.timeout, req.wait_selector, req.headless)
                if req.engine == "selenium" else fetch_bs4(req.url, req.timeout))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Не удалось загрузить страницу: {e}")
    data = parse_html(html, req.url, req.selector)
    data["prices"], data["price_summary"] = enrich_prices(data.pop("raw_prices"))
    data.update(url=req.url, engine=req.engine, html_size=len(html))
    return data


# ---------- PDF ----------
def ocr_image(img: Image.Image, lang: str) -> str:
    return pytesseract.image_to_string(img.convert("RGB"), lang=lang).strip()


def check_lang(lang: str) -> str:
    if not re.fullmatch(r"[a-z_]+(\+[a-z_]+)*", lang):
        raise HTTPException(400, "Некорректный код языка OCR")
    return lang


@app.post("/api/pdf")
def read_pdf(file: UploadFile = File(...), ocr: bool = Form(True), lang: str = Form("rus+eng")):
    lang = check_lang(lang)
    try:
        reader = PdfReader(io.BytesIO(file.file.read()))
        if reader.is_encrypted:
            reader.decrypt("")
        meta = {k.lstrip("/"): str(v) for k, v in (reader.metadata or {}).items()}
        pages, ocr_pages = [], 0
        for i, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            used_ocr = False
            if ocr and len(text) < 20:  # вероятно, скан: распознаём встроенные картинки
                try:
                    parts = [ocr_image(Image.open(io.BytesIO(im.data)), lang) for im in page.images]
                    text = "\n".join(p for p in parts if p) or text
                    used_ocr = True
                    ocr_pages += 1
                except pytesseract.TesseractNotFoundError:
                    raise HTTPException(500, "Tesseract не найден. Укажите путь к tesseract.exe в app.py (или переменную TESSERACT_CMD).")
                except Exception:
                    pass
            pages.append({"page": i, "text": text, "ocr": used_ocr})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Не удалось прочитать PDF: {e}")
    full = "\n\n".join(f"--- стр. {p['page']} ---\n{p['text']}" for p in pages)
    return {**text_extras(full), "title": meta.get("Title", file.filename), "text": full, "pages": pages,
            "page_count": len(pages), "ocr_pages": ocr_pages, "metadata": meta}


# ---------- OCR ----------
@app.post("/api/ocr")
def read_image(file: UploadFile = File(...), lang: str = Form("rus+eng")):
    lang = check_lang(lang)
    try:
        img = Image.open(io.BytesIO(file.file.read()))
    except Exception:
        raise HTTPException(400, "Файл не похож на изображение")
    try:
        text = ocr_image(img, lang)
    except pytesseract.TesseractNotFoundError:
        raise HTTPException(500, "Tesseract не найден. Укажите путь к tesseract.exe в app.py (или переменную TESSERACT_CMD).")
    except pytesseract.TesseractError as e:
        raise HTTPException(400, f"Ошибка OCR (проверьте языковой пакет '{lang}'): {e}")
    return {**text_extras(text), "title": file.filename, "text": text, "size": list(img.size), "lang": lang}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
