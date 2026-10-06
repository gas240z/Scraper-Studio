# Scraper Studio

Сайты (BeautifulSoup / Selenium), PDF (pypdf) и изображения (OCR) в одном веб-интерфейсе.

## Установка
1. Python 3.10+, затем:  `pip install -r requirements.txt`
2. Tesseract OCR + языки rus/eng:
   - Windows: https://github.com/UB-Mannheim/tesseract/wiki (при установке отметьте Russian), затем раскомментируйте путь в app.py
   - Linux: `sudo apt install tesseract-ocr tesseract-ocr-rus`
3. Для режима Selenium нужен установленный Google Chrome (драйвер скачается сам).

## Запуск
`uvicorn app:app --reload`  ->  http://127.0.0.1:8000
