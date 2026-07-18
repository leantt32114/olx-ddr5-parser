#!/usr/bin/env python3
"""
Мониторинг объявлений OLX.kz (DDR5 32GB, Алматы) через headless-браузер
Playwright, с отправкой новых объявлений в Telegram.

Версия для GitHub Actions: скрипт делает ОДНУ проверку за запуск и
завершается. Периодичность (например, раз в 5 минут) задаётся не внутри
скрипта, а расписанием (`schedule: cron`) в workflow-файле
.github/workflows/olx_monitor.yml — GitHub Actions сам запускает свежий
контейнер на каждую проверку, поэтому Playwright/браузер гарантированно
не остаётся висеть в памяти между проверками, и нет нужды в бесконечном
цикле или ручном закрытии по таймеру.

Список уже увиденных ID объявлений хранится в seen_ads.json прямо в
репозитории — workflow коммитит обновлённый файл обратно после каждого
запуска, поэтому состояние не теряется (в отличие от эфемерной ФС
облачных воркеров).

Переменные окружения (задаются как GitHub Actions Secrets):
    TELEGRAM_TOKEN
    TELEGRAM_CHAT_ID
"""

import os
import re
import sys
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
    "https://www.olx.kz/elektronika/kompyutery-i-komplektuyuschie/"
    "komplektuyuschie-i-aksesuary/alma-ata/q-DDR5-32GB/"
    "?search%5Border%5D=created_at:desc"
    "&search%5Bfilter_enum_subcategory%5D%5B0%5D=moduli-pamyati"
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

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
# ОДНОРАЗОВАЯ ПРОВЕРКА (для запуска из GitHub Actions)
# ---------------------------------------------------------------------------

def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не заданы через переменные окружения!")

    seen_ids = load_seen_ids()
    log.info("Загружено ранее увиденных объявлений: %d", len(seen_ids))

    try:
        ads = fetch_ads()
    except Exception as e:
        log.error("Не удалось загрузить страницу OLX: %s", e)
        sys.exit(1)  # ненулевой код — Actions пометит запуск как failed, но seen_ads.json не поменяется

    if not ads:
        log.warning("Объявления не найдены — ничего не меняю")
        return

    if not seen_ids:
        log.info("Первый запуск — сохраняю текущие объявления без отправки (%d шт.)", len(ads))
        save_seen_ids({ad["id"] for ad in ads})
        return

    new_ads = [ad for ad in ads if ad["id"] not in seen_ids]

    for ad in reversed(new_ads):  # от старых к новым
        log.info("Новое объявление: %s", ad["title"])
        send_telegram_message(format_ad_message(ad))
        seen_ids.add(ad["id"])
        time.sleep(1)

    save_seen_ids(seen_ids | {ad["id"] for ad in ads})
    log.info("Проверка завершена. Новых объявлений: %d", len(new_ads))


if __name__ == "__main__":
    main()
