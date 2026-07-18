#!/usr/bin/env python3
"""
Мониторинг объявлений OLX.kz (DDR5 32GB, Алматы) с отправкой новых
объявлений в Telegram через бота.

Установка зависимостей:
    pip install requests beautifulsoup4

Настройка:
    1. Создайте бота через @BotFather в Telegram, получите TELEGRAM_TOKEN.
    2. Узнайте свой chat_id (например, написав боту @userinfobot,
       либо отправив /start своему боту и открыв
       https://api.telegram.org/bot<TOKEN>/getUpdates ).
    3. Впишите значения в переменные ниже (или задайте через переменные
       окружения TELEGRAM_TOKEN / TELEGRAM_CHAT_ID).
    4. Запустите: python olx_monitor.py
       Скрипт будет проверять объявления каждые CHECK_INTERVAL секунд.

Как это работает:
    - При каждой проверке скрипт скачивает страницу поиска OLX.
    - Извлекает карточки объявлений (ссылка, заголовок, цена).
    - ID объявления (последнее число в URL) сравнивается со списком
      уже отправленных ID, которые хранятся в файле seen_ads.json.
    - Новые объявления отправляются в Telegram и добавляются в файл.
"""

import os
import re
import json
import time
import logging
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------

SEARCH_URL = (
    "https://www.olx.kz/elektronika/kompyutery-i-komplektuyuschie/"
    "komplektuyuschie-i-aksesuary/alma-ata/q-DDR5-32GB/"
    "?search%5Border%5D=created_at:desc"
    "&search%5Bfilter_enum_subcategory%5D%5B0%5D=moduli-pamyati"
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "ВАШ_ТОКЕН_БОТА")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "ВАШ_CHAT_ID")

CHECK_INTERVAL = 300  # секунды между проверками (5 минут)
STATE_FILE = Path(__file__).parent / "seen_ads.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

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


# ---------------------------------------------------------------------------
# ПАРСИНГ OLX
# ---------------------------------------------------------------------------

def extract_ad_id(url: str) -> str:
    """Достаём уникальный ID объявления из ссылки (число перед .html)."""
    match = re.search(r"-ID(\w+)\.html", url)
    if match:
        return match.group(1)
    # запасной вариант — вся ссылка как ID
    return url


def fetch_ads() -> list[dict]:
    resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    ads = []

    # OLX обычно рендерит карточки объявлений в контейнерах с атрибутом
    # data-cy="l-card". Это может со временем поменяться — тогда нужно
    # будет обновить селекторы ниже, посмотрев актуальную верстку страницы.
    cards = soup.select('[data-cy="l-card"]')

    for card in cards:
        link_tag = card.select_one("a[href]")
        if not link_tag:
            continue

        href = link_tag["href"]
        if href.startswith("/"):
            href = "https://www.olx.kz" + href
        if "olx.kz" not in href:
            continue  # пропускаем внешние/рекламные блоки

        title_tag = card.select_one("h4, h6")
        title = title_tag.get_text(strip=True) if title_tag else "Без названия"

        price_tag = card.select_one('[data-testid="ad-price"]')
        price = price_tag.get_text(strip=True) if price_tag else "Цена не указана"

        ad_id = extract_ad_id(href)

        ads.append({
            "id": ad_id,
            "title": title,
            "price": price,
            "url": href,
        })

    return ads


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def send_telegram_message(text: str) -> None:
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
    return (
        f"🆕 <b>{ad['title']}</b>\n"
        f"💰 {ad['price']}\n"
        f"🔗 {ad['url']}"
    )


# ---------------------------------------------------------------------------
# ОСНОВНОЙ ЦИКЛ
# ---------------------------------------------------------------------------

def check_once(seen_ids: set) -> set:
    try:
        ads = fetch_ads()
    except requests.RequestException as e:
        log.error("Не удалось загрузить страницу OLX: %s", e)
        return seen_ids

    log.info("Найдено объявлений на странице: %d", len(ads))

    new_ads = [ad for ad in ads if ad["id"] not in seen_ids]

    if not seen_ids:
        # Первый запуск: просто запоминаем всё, что есть сейчас,
        # чтобы не заспамить чат старыми объявлениями.
        log.info("Первый запуск — сохраняю текущие объявления без отправки")
        return {ad["id"] for ad in ads}

    for ad in reversed(new_ads):  # отправляем от старых к новым
        log.info("Новое объявление: %s", ad["title"])
        send_telegram_message(format_ad_message(ad))
        seen_ids.add(ad["id"])
        time.sleep(1)  # не спамить Telegram API

    return seen_ids | {ad["id"] for ad in ads}


def main():
    if TELEGRAM_TOKEN == "ВАШ_ТОКЕН_БОТА" or TELEGRAM_CHAT_ID == "ВАШ_CHAT_ID":
        log.warning(
            "Заполните TELEGRAM_TOKEN и TELEGRAM_CHAT_ID в коде "
            "или через переменные окружения перед запуском!"
        )

    seen_ids = load_seen_ids()
    log.info("Загружено ранее увиденных объявлений: %d", len(seen_ids))

    while True:
        seen_ids = check_once(seen_ids)
        save_seen_ids(seen_ids)
        log.info("Ожидание %d секунд до следующей проверки...", CHECK_INTERVAL)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
