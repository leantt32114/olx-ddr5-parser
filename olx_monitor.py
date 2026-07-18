#!/usr/bin/env python3
"""
Мониторинг объявлений OLX.kz (DDR5 32GB, Алматы) через headless-браузер
Playwright, с отправкой новых объявлений в Telegram.

Почему Playwright, а не requests/BeautifulSoup:
    OLX защищён Cloudflare, и частые запросы "голым" HTTP-клиентом с
    IP облачного сервера (Render) быстро получают 403. Настоящий
    headless-браузер проходит проверку Cloudflare гораздо надёжнее.

Экономия памяти на бесплатном тарифе Render (512 МБ):
    - Браузер и контекст открываются заново на каждую проверку и
      полностью закрываются (browser.close()) сразу после неё —
      между проверками в памяти не остаётся ни одного процесса Chromium.
    - Используется только Chromium (не ставим Firefox/WebKit).
    - Закрытие идёт через try/finally, поэтому происходит даже при ошибке.

Установка (локально):
    pip install -r requirements.txt
    playwright install --with-deps chromium

Настройка:
    Переменные окружения TELEGRAM_TOKEN и TELEGRAM_CHAT_ID
    (на Render задаются в разделе Environment).

Запуск:
    python olx_monitor.py
"""

import os
import re
import json
import time
import logging
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------

SEARCH_URL = (
    https://www.olx.kz/elektronika/kompyutery-i-komplektuyuschie/komplektuyuschie-i-aksesuary/alma-ata/q-DDR5-32GB/?search%5Border%5D=filter_float_price:desc&search%5Bfilter_float_price:from%5D=100000&search%5Bfilter_float_price:to%5D=200000
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL = 300  # 5 минут
STATE_FILE = Path(__file__).parent / "seen_ads.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("olx_monitor")


# ---------------------------------------------------------------------------
# СОХРАНЕНИЕ / ЗАГРУЗКА УЖЕ ВИДЕННЫХ ОБЪЯВЛЕНИЙ
# ---------------------------------------------------------------------------

def load_seen_ids() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            log.warning("Не удалось прочитать %s, начинаю с пустого списка", STATE_FILE)
    return set()


def save_seen_ids(seen_ids: set) -> None:
    STATE_FILE.write_text(
        json.dumps(sorted(seen_ids), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def extract_ad_id(url: str) -> str:
    match = re.search(r"-ID(\w+)\.html", url)
    return match.group(1) if match else url


# ---------------------------------------------------------------------------
# PLAYWRIGHT: ПОЛУЧЕНИЕ ОБЪЯВЛЕНИЙ
# ---------------------------------------------------------------------------

def fetch_ads() -> list[dict]:
    """
    Открывает браузер, загружает страницу поиска, извлекает объявления
    и полностью закрывает браузер перед выходом из функции — независимо
    от того, произошла ошибка или нет.
    """
    ads: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-extensions",
                "--disable-background-networking",
            ],
        )
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="ru-RU",
                viewport={"width": 1280, "height": 800},
            )
            try:
                page = context.new_page()
                page.set_default_timeout(30000)

                page.goto(SEARCH_URL, wait_until="domcontentloaded")

                try:
                    page.wait_for_selector('[data-cy="l-card"]', timeout=20000)
                except PWTimeoutError:
                    log.warning("Карточки объявлений не появились за 20с — "
                                "возможно, изменилась вёрстка или сработала защита")

                cards = page.query_selector_all('[data-cy="l-card"]')
                log.info("Найдено карточек на странице: %d", len(cards))

                for card in cards:
                    link_el = card.query_selector("a[href]")
                    if not link_el:
                        continue
                    href = link_el.get_attribute("href") or ""
                    if href.startswith("/"):
                        href = "https://www.olx.kz" + href
                    if "olx.kz" not in href:
                        continue

                    title_el = card.query_selector("h4, h6")
                    title = title_el.inner_text().strip() if title_el else "Без названия"

                    price_el = card.query_selector('[data-testid="ad-price"]')
                    price = price_el.inner_text().strip() if price_el else "Цена не указана"

                    ads.append({
                        "id": extract_ad_id(href),
                        "title": title,
                        "price": price,
                        "url": href,
                    })
            finally:
                context.close()
        finally:
            browser.close()

    return ads


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def send_telegram_message(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не заданы — сообщение не отправлено")
        return
    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(api_url, data=payload, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Ошибка отправки в Telegram: %s", e)


def format_ad_message(ad: dict) -> str:
    return f"🆕 <b>{ad['title']}</b>\n💰 {ad['price']}\n🔗 {ad['url']}"


# ---------------------------------------------------------------------------
# ОСНОВНОЙ ЦИКЛ
# ---------------------------------------------------------------------------

def check_once(seen_ids: set) -> set:
    try:
        ads = fetch_ads()
    except Exception as e:  # ловим и ошибки Playwright (таймауты, краши браузера)
        log.error("Не удалось загрузить страницу OLX: %s", e)
        return seen_ids

    if not ads:
        log.warning("Объявления не найдены — пропускаю эту проверку")
        return seen_ids

    if not seen_ids:
        log.info("Первый запуск — сохраняю текущие объявления без отправки (%d шт.)", len(ads))
        return {ad["id"] for ad in ads}

    new_ads = [ad for ad in ads if ad["id"] not in seen_ids]

    for ad in reversed(new_ads):  # от старых к новым
        log.info("Новое объявление: %s", ad["title"])
        send_telegram_message(format_ad_message(ad))
        seen_ids.add(ad["id"])
        time.sleep(1)

    return seen_ids | {ad["id"] for ad in ads}


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning(
            "TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не заданы через переменные окружения!"
        )

    seen_ids = load_seen_ids()
    log.info("Загружено ранее увиденных объявлений: %d", len(seen_ids))

    while True:
        start = time.time()
        seen_ids = check_once(seen_ids)
        save_seen_ids(seen_ids)
        elapsed = time.time() - start
        log.info("Проверка заняла %.1fс. Ожидание %d секунд до следующей...",
                  elapsed, CHECK_INTERVAL)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
