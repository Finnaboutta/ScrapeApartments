#!/usr/bin/env python3
"""
Scrape apartment listings from Viewit and Kijiji, keep only listings with >= 1 bedroom,
and persist unique results across runs.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
except ImportError:
    Page = None  # type: ignore[assignment]
    PlaywrightTimeoutError = Exception  # type: ignore[assignment]
    sync_playwright = None  # type: ignore[assignment]


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Listing:
    source: str
    address: str
    bedrooms: int
    price: int
    url: str
    raw_text: str = ""
    latitude: float | None = None
    longitude: float | None = None
    distance_km: float | None = None


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    parsed = parsed._replace(query="", fragment="")
    normalized_path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    parsed = parsed._replace(path=normalized_path)
    return urlunparse(parsed)


def extract_first_int(text: str) -> int | None:
    if not text:
        return None
    match = re.search(r"(\d+)", text.replace(",", ""))
    return int(match.group(1)) if match else None


def extract_price(text: str) -> int | None:
    if not text:
        return None
    match = re.search(r"\$\s*([\d,]+)", text)
    if match:
        return int(match.group(1).replace(",", ""))

    # fallback: any large-ish number
    match = re.search(r"\b([1-9]\d{2,6})\b", text.replace(",", ""))
    if match:
        return int(match.group(1))
    return None


def extract_bedrooms(text: str) -> int | None:
    if not text:
        return None

    lowered = text.lower()

    # Handle "Bachelor"/"Studio" as 0 bedrooms.
    if "bachelor" in lowered or "studio" in lowered:
        return 0

    # Common patterns like "1 bed", "2 bedroom", "3 bdrm"
    patterns = [
        r"(\d+)\s*(?:bed|beds|bedroom|bedrooms|bdrm|br)\b",
        r"\b(?:bed|beds|bedroom|bedrooms|bdrm|br)\s*(\d+)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            return int(match.group(1))

    return None


def extract_geo(item: dict) -> tuple[float | None, float | None]:
    geo = item.get("geo")
    if not isinstance(geo, dict):
        return None, None

    lat_val = geo.get("latitude")
    lon_val = geo.get("longitude")
    try:
        lat = float(lat_val) if lat_val is not None else None
        lon = float(lon_val) if lon_val is not None else None
    except (TypeError, ValueError):
        return None, None
    return lat, lon


def is_basement_unit(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False

    # Keep units that mention "with basement"; exclude basement apartments/units.
    if "with basement" in lowered:
        return False

    basement_patterns = [
        r"\bbasement\s+(apartment|unit|suite|rental)\b",
        r"\b(apartment|unit|suite|rental)\s+in\s+basement\b",
        r"\bbsmt\b",
        r"\blower\s+level\b",
    ]
    return any(re.search(pattern, lowered) for pattern in basement_patterns)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return earth_radius_km * c


def request_html(url: str, timeout: int = 25) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-CA,en;q=0.9"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.text


def request_json(url: str, params: dict[str, str], timeout: int = 25) -> list[dict]:
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-CA,en;q=0.9"},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def human_sleep(min_seconds: float, max_seconds: float) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def simulate_human_activity(page: Page) -> None:
    width = 1280
    height = 900

    for _ in range(random.randint(3, 6)):
        x = random.randint(50, width - 50)
        y = random.randint(100, height - 100)
        page.mouse.move(x, y, steps=random.randint(10, 30))
        human_sleep(0.2, 0.8)

    for _ in range(random.randint(4, 8)):
        delta = random.randint(250, 900)
        page.mouse.wheel(0, delta)
        human_sleep(0.4, 1.1)


def type_like_human(page: Page, selector: str, value: str, key_delay_ms: int = 110) -> None:
    field = page.locator(selector).first
    field.click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.type(value, delay=key_delay_ms)


def maybe_dismiss_cookie_banner(page: Page) -> None:
    candidates = [
        "a.cc-btn.cc-dismiss",
        "button.cc-btn.cc-dismiss",
        "text=Got it!",
    ]
    for selector in candidates:
        loc = page.locator(selector).first
        if loc.count() == 0:
            continue
        try:
            loc.click(timeout=1500)
            return
        except Exception:  # noqa: BLE001
            continue


def set_kijiji_radius_in_overlay(page: Page, target_km: int) -> bool:
    handle = page.locator("[data-reach-dialog-overlay] .rc-slider-handle[role='slider']").first
    rail = page.locator("[data-reach-dialog-overlay] .rc-slider").first
    radius_label = page.locator("[data-testid='location-footer-radius-units']").first
    if handle.count() == 0 or rail.count() == 0:
        return False

    if radius_label.count() == 0:
        return False

    rail_box = rail.bounding_box()
    handle_box = handle.bounding_box()
    if not rail_box or not handle_box:
        return False

    def drag_handle_to(x: float) -> None:
        hb = handle.bounding_box()
        if not hb:
            return
        cx = hb["x"] + hb["width"] / 2
        cy = hb["y"] + hb["height"] / 2
        x = min(max(rail_box["x"] + 1, x), rail_box["x"] + rail_box["width"] - 1)
        page.mouse.move(cx, cy, steps=8)
        page.mouse.down()
        page.mouse.move(x, cy, steps=20)
        page.mouse.up()
        human_sleep(0.2, 0.35)

    # User-defined logic:
    # 1) Slowly move left.
    # 2) If label is 2km or 3km, accept.
    # 3) If label reaches 1km, move right exactly once and accept.
    deadline = time.time() + 35.0
    while time.time() < deadline:
        label_text = radius_label.inner_text().strip().lower().replace(" ", "")
        km_match = re.search(r"(\d+)\s*km", label_text)
        km_val = int(km_match.group(1)) if km_match else None

        if km_val in {2, 3}:
            return True
        if km_val == 1:
            hb = handle.bounding_box()
            if not hb:
                return False
            current_x = hb["x"] + hb["width"] / 2
            # Exactly one nudge to the right from 1km.
            drag_handle_to(current_x + 10)
            return True

        hb = handle.bounding_box()
        if not hb:
            return False
        current_x = hb["x"] + hb["width"] / 2
        # Keep moving left slowly in 10px steps.
        drag_handle_to(current_x - 10)

    return False


def run_kijiji_filtered_flow(
    page: Page,
    kijiji_url: str,
    kijiji_pages: int,
    kijiji_delay_min: float,
    kijiji_delay_max: float,
    kijiji_location_query: str,
    kijiji_location_options: int,
    kijiji_radius_km: int,
) -> list[Listing]:
    page.goto(kijiji_url, wait_until="domcontentloaded", timeout=60000)
    human_sleep(1.2, 2.4)
    maybe_dismiss_cookie_banner(page)

    location_trigger = page.locator("[data-testid='location-name']").first
    if location_trigger.count():
        location_trigger.click(timeout=7000)
        human_sleep(0.4, 0.9)

    overlay = page.locator("[data-reach-dialog-overlay]").last
    if overlay.count():
        try:
            overlay.wait_for(state="visible", timeout=7000)
        except Exception:  # noqa: BLE001
            pass

    location_input_candidates = [
        "[data-reach-dialog-overlay] input[data-testid='location-autocomplete-input']",
        "[data-reach-dialog-overlay] input[placeholder*='city' i]",
        "[data-reach-dialog-overlay] input[placeholder*='location' i]",
        "[data-reach-dialog-overlay] input[aria-label*='location' i]",
        "[data-reach-dialog-overlay] input[role='combobox']",
    ]
    input_locator = None
    for selector in location_input_candidates:
        loc = page.locator(selector).first
        if loc.count() == 0:
            continue
        input_locator = loc
        break

    if input_locator is not None:
        input_locator.click(timeout=7000, force=True)
        page.keyboard.press("ControlOrMeta+a")
        page.keyboard.type(kijiji_location_query, delay=90)
        # Use keyboard-only location suggestion selection:
        # wait 5s, ArrowDown 0-4 times, then Enter.
        time.sleep(5)
        for _ in range(random.randint(0, 4)):
            page.keyboard.press("ArrowDown")
            human_sleep(0.15, 0.45)
        page.keyboard.press("Enter")
        human_sleep(0.8, 1.6)

    # Radius control appears after location choice.
    radius_ready = set_kijiji_radius_in_overlay(page, kijiji_radius_km)
    if not radius_ready:
        raise RuntimeError("Kijiji radius slider did not reach requested value; skipping Apply click.")
    human_sleep(0.5, 1.0)

    apply_btn = page.locator("[data-reach-dialog-overlay] [data-testid='set-location-button']").first
    if apply_btn.count():
        apply_btn.click(timeout=7000)
        human_sleep(1.0, 2.0)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001
            pass

    # Strict sequence: don't browse/scroll until filters are applied and modal is gone.
    try:
        page.locator("[data-reach-dialog-overlay]").last.wait_for(state="hidden", timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    try:
        page.wait_for_selector("[id^='listing-card-list-item-'], [data-testid*='listing-card']", timeout=15000)
    except Exception:  # noqa: BLE001
        pass

    kijiji_html_pages = [page.content()]

    for _ in range(1, max(1, kijiji_pages)):
        human_sleep(kijiji_delay_min, kijiji_delay_max)

        next_candidates = [
            "a[aria-label*='Next']",
            "a[title*='Next']",
            "a:has-text('Next')",
            "a:has-text('Suivant')",
        ]

        moved = False
        for selector in next_candidates:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            try:
                locator.click(timeout=7000)
                moved = True
                break
            except PlaywrightTimeoutError:
                continue

        if not moved:
            break

        page.wait_for_load_state("domcontentloaded", timeout=30000)
        human_sleep(1.5, 3.5)
        simulate_human_activity(page)
        kijiji_html_pages.append(page.content())

    kijiji_all: list[Listing] = []
    for html in kijiji_html_pages:
        kijiji_all.extend(parse_kijiji_html(html))

    dedup: dict[str, Listing] = {}
    for item in kijiji_all:
        dedup[item.url] = item
    return list(dedup.values())


def parse_json_ld(soup: BeautifulSoup) -> list[dict]:
    payloads: list[dict] = []
    for script in soup.select('script[type="application/ld+json"]'):
        content = (script.string or "").strip()
        if not content:
            continue
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            continue

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    payloads.append(item)
        elif isinstance(data, dict):
            payloads.append(data)

    return payloads


def parse_kijiji_html(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[Listing] = []

    # Strategy 0: parse Next.js payload with precise listing attributes + coordinates.
    next_data_node = soup.select_one("script#__NEXT_DATA__")
    if next_data_node and next_data_node.string:
        try:
            next_data = json.loads(next_data_node.string)
        except json.JSONDecodeError:
            next_data = None

        if isinstance(next_data, dict):
            def walk(obj: object) -> list[dict]:
                found: list[dict] = []
                if isinstance(obj, dict):
                    if obj.get("__typename") == "RealEstateListing":
                        found.append(obj)
                    for value in obj.values():
                        found.extend(walk(value))
                elif isinstance(obj, list):
                    for value in obj:
                        found.extend(walk(value))
                return found

            for item in walk(next_data):
                url = str(item.get("url", "")).strip()
                if not url:
                    continue
                url = normalize_url(urljoin("https://www.kijiji.ca", url))

                title = str(item.get("title", "")).strip()
                location = item.get("location") if isinstance(item.get("location"), dict) else {}
                address = str(location.get("address", "")).strip() or title

                price_obj = item.get("price") if isinstance(item.get("price"), dict) else {}
                raw_amount = price_obj.get("amount")
                price = None
                if isinstance(raw_amount, (int, float)):
                    # Kijiji RealEstateListing amount is in cents.
                    price = int(round(float(raw_amount) / 100.0))
                if price is None:
                    price = extract_price(str(item))

                bedrooms = None
                attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
                all_attrs = attrs.get("all") if isinstance(attrs.get("all"), list) else []
                for attr in all_attrs:
                    if not isinstance(attr, dict):
                        continue
                    if str(attr.get("canonicalName", "")) != "numberbedrooms":
                        continue
                    values = attr.get("canonicalValues")
                    if isinstance(values, list) and values:
                        try:
                            bedrooms = int(float(str(values[0])))
                        except (TypeError, ValueError):
                            bedrooms = None
                    break
                if bedrooms is None:
                    bedrooms = extract_bedrooms(f"{title} {item.get('description', '')}")

                coords = location.get("coordinates") if isinstance(location.get("coordinates"), dict) else {}
                lat = coords.get("latitude")
                lon = coords.get("longitude")
                try:
                    lat_val = float(lat) if lat is not None else None
                    lon_val = float(lon) if lon is not None else None
                except (TypeError, ValueError):
                    lat_val, lon_val = None, None

                raw_text = f"{title} {item.get('description', '')} {address}"
                if bedrooms is None or price is None:
                    continue
                if bedrooms >= 1:
                    listings.append(
                        Listing(
                            source="kijiji",
                            address=address or "Unknown",
                            bedrooms=bedrooms,
                            price=price,
                            url=url,
                            raw_text=raw_text,
                            latitude=lat_val,
                            longitude=lon_val,
                        )
                    )

    # Strategy 1: parse JSON-LD structured data
    for obj in parse_json_ld(soup):
        obj_type = str(obj.get("@type", "")).lower()
        if obj_type in {"itemlist", "searchresultspage"}:
            item_list = obj.get("itemListElement") or []
            for entry in item_list:
                if not isinstance(entry, dict):
                    continue
                item = entry.get("item", entry)
                if not isinstance(item, dict):
                    continue

                title = str(item.get("name", "")).strip()
                address = title
                lat, lon = extract_geo(item)
                bedrooms = extract_bedrooms(title) or extract_bedrooms(str(item))
                offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
                price = extract_price(str(offers.get("price", ""))) or extract_price(str(item))
                url = str(item.get("url", "")).strip()
                raw_text = f"{title} {item}"

                if url:
                    url = normalize_url(urljoin("https://www.kijiji.ca", url))

                if bedrooms is None or price is None or not url:
                    continue

                if bedrooms >= 1:
                    listings.append(
                        Listing(
                            source="kijiji",
                            address=address or "Unknown",
                            bedrooms=bedrooms,
                            price=price,
                            url=url,
                            raw_text=raw_text,
                            latitude=lat,
                            longitude=lon,
                        )
                    )

    # Strategy 2: fallback to scraping card-like elements
    if not listings:
        candidates = soup.select(
            "article, div.search-item, div[data-listing-id], li[data-listing-id], "
            "div[data-testid*='listing'], [id^='listing-card-list-item-']"
        )
        for node in candidates:
            text_blob = " ".join(node.stripped_strings)
            bedrooms = extract_bedrooms(text_blob)
            price = extract_price(text_blob)

            link = node.select_one("a[href]")
            url = normalize_url(urljoin("https://www.kijiji.ca", link.get("href", ""))) if link else ""

            title = ""
            title_node = node.select_one("h2, h3, [data-testid*='title']")
            if title_node:
                title = " ".join(title_node.stripped_strings)

            address = title or text_blob[:120]

            if bedrooms is None or price is None or not url:
                continue

            if bedrooms >= 1:
                listings.append(
                    Listing(
                        source="kijiji",
                        address=address,
                        bedrooms=bedrooms,
                        price=price,
                        url=url,
                        raw_text=text_blob,
                    )
                )

    dedup: dict[str, Listing] = {}
    for item in listings:
        existing = dedup.get(item.url)
        if existing is None:
            dedup[item.url] = item
            continue

        existing_score = 0
        item_score = 0
        if existing.latitude is not None and existing.longitude is not None:
            existing_score += 3
        if item.latitude is not None and item.longitude is not None:
            item_score += 3
        if len(existing.address) > 20:
            existing_score += 1
        if len(item.address) > 20:
            item_score += 1
        if len(existing.raw_text) > 40:
            existing_score += 1
        if len(item.raw_text) > 40:
            item_score += 1

        if item_score >= existing_score:
            dedup[item.url] = item
    return list(dedup.values())


def parse_kijiji_listings(search_url: str) -> list[Listing]:
    html = request_html(search_url)
    return parse_kijiji_html(html)


def parse_viewit_html(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[Listing] = []

    # Strategy 1: structured data first
    for obj in parse_json_ld(soup):
        obj_type = str(obj.get("@type", "")).lower()
        if obj_type == "itemlist":
            for entry in obj.get("itemListElement", []):
                if not isinstance(entry, dict):
                    continue
                item = entry.get("item", entry)
                if not isinstance(item, dict):
                    continue

                title = str(item.get("name", "")).strip()
                address = title
                lat, lon = extract_geo(item)
                bedrooms = extract_bedrooms(title) or extract_bedrooms(str(item))
                offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
                price = extract_price(str(offers.get("price", ""))) or extract_price(str(item))
                url = str(item.get("url", "")).strip()
                raw_text = f"{title} {item}"

                if url:
                    url = normalize_url(urljoin("https://www.viewit.ca", url))

                if bedrooms is None or price is None or not url:
                    continue

                if bedrooms >= 1:
                    listings.append(
                        Listing(
                            source="viewit",
                            address=address or "Unknown",
                            bedrooms=bedrooms,
                            price=price,
                            url=url,
                            raw_text=raw_text,
                            latitude=lat,
                            longitude=lon,
                        )
                    )

    # Strategy 1b: parse featured cards from CityPage markup.
    if not listings:
        for card in soup.select("article.cityFeatured-col"):
            link_node = card.select_one("a.featuredListing[href]")
            if not link_node:
                continue

            href = (link_node.get("href") or "").strip()
            url = normalize_url(urljoin("https://www.viewit.ca", href))
            if not url:
                continue

            price_node = card.select_one(".featuredListing-price")
            price = extract_price(price_node.get_text(" ", strip=True) if price_node else "")

            img = card.select_one("img[title], img[alt]")
            title_text = ""
            if img:
                title_text = (img.get("title") or img.get("alt") or "").strip()

            label_node = card.select_one(".featuredListing-name")
            label_text = label_node.get_text(" ", strip=True) if label_node else ""

            raw_text = f"{title_text} {label_text} {href}"
            bedrooms = extract_bedrooms(raw_text)

            # Address is usually in the image title, e.g. "Rental House 593 Euclid Ave, Toronto, ON".
            address = title_text or label_text or href
            address = re.sub(r"^\s*Rental\s+\w+\s+", "", address, flags=re.IGNORECASE).strip()

            if bedrooms is None or price is None:
                continue
            if bedrooms >= 1:
                listings.append(
                    Listing(
                        source="viewit",
                        address=address,
                        bedrooms=bedrooms,
                        price=price,
                        url=url,
                        raw_text=raw_text,
                    )
                )

    # Strategy 2: fallback selectors
    if not listings:
        candidates = soup.select(
            "article, .propertyCard, .listingCard, .listing, [data-testid*='listing']"
        )
        for node in candidates:
            text_blob = " ".join(node.stripped_strings)
            bedrooms = extract_bedrooms(text_blob)
            price = extract_price(text_blob)

            link = node.select_one("a[href]")
            url = normalize_url(urljoin("https://www.viewit.ca", link.get("href", ""))) if link else ""

            title = ""
            title_node = node.select_one("h2, h3, .address, [class*='address']")
            if title_node:
                title = " ".join(title_node.stripped_strings)

            address = title or text_blob[:120]

            if bedrooms is None or price is None or not url:
                continue

            if bedrooms >= 1:
                listings.append(
                    Listing(
                        source="viewit",
                        address=address,
                        bedrooms=bedrooms,
                        price=price,
                        url=url,
                        raw_text=text_blob,
                    )
                )

    dedup: dict[str, Listing] = {}
    for item in listings:
        dedup[item.url] = item
    return list(dedup.values())


def parse_viewit_results_page_html(html: str, max_per_page: int = 5) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[Listing] = []

    cards = soup.select("article.resultListing")
    for card in cards[:max_per_page]:
        link_node = card.select_one("a.resultListing-main[href]") or card.select_one("a.resultListing-photo[href]")
        if not link_node:
            continue

        href = (link_node.get("href") or "").strip()
        url = normalize_url(urljoin("https://www.viewit.ca", href))
        if not url:
            continue

        address_node = card.select_one("h2, h3, .resultListing-main h2, .resultListing-main h3")
        address = address_node.get_text(" ", strip=True) if address_node else href

        details_node = card.select_one(".resultListing-details-specs")
        details_text = details_node.get_text(" ", strip=True) if details_node else ""
        bedrooms = extract_bedrooms(f"{details_text} {href}")

        price_node = card.select_one(".resultListing-price")
        price = extract_price(price_node.get_text(" ", strip=True) if price_node else "")

        onmouseover = card.get("onmouseover") or ""
        lat_lon_match = re.search(r"showStaticMap\('\d+','([-\d.]+)','([-\d.]+)'\)", onmouseover)
        lat = float(lat_lon_match.group(1)) if lat_lon_match else None
        lon = float(lat_lon_match.group(2)) if lat_lon_match else None

        raw_text = " ".join(card.stripped_strings)
        if bedrooms is None or price is None:
            continue

        listings.append(
            Listing(
                source="viewit",
                address=address,
                bedrooms=bedrooms,
                price=price,
                url=url,
                raw_text=raw_text,
                latitude=lat,
                longitude=lon,
            )
        )

    dedup: dict[str, Listing] = {}
    for item in listings:
        dedup[item.url] = item
    return list(dedup.values())


def run_viewit_filtered_flow(
    page: Page,
    viewit_url: str,
    viewit_max_price: int,
    viewit_pages: int,
    viewit_bedroom_click_min: float,
    viewit_bedroom_click_max: float,
    viewit_pre_list_click_min: float,
    viewit_pre_list_click_max: float,
    viewit_page_wait_min: float,
    viewit_page_wait_max: float,
) -> list[Listing]:
    page.goto(viewit_url, wait_until="domcontentloaded", timeout=60000)
    human_sleep(1.2, 2.2)
    maybe_dismiss_cookie_banner(page)
    simulate_human_activity(page)

    type_like_human(page, "#maxPrice", str(viewit_max_price), key_delay_ms=120)
    human_sleep(0.5, 1.0)

    bedroom_targets = [
        ("#ctl00_ContentMain_ucSearchDetails1_chkBedroom1", "1"),
        ("#ctl00_ContentMain_ucSearchDetails1_chkBedroom2", "2"),
        ("#ctl00_ContentMain_ucSearchDetails1_chkBedroom3", "3+"),
    ]
    for _, label in bedroom_targets:
        # Click visible label text to match real user interaction on this control.
        label_locator = page.locator(
            "label.toggleBar-option span.toggleBar-option-label span",
            has_text=label,
        ).first
        if label_locator.count() == 0:
            continue
        label_locator.click(timeout=7000)
        human_sleep(viewit_bedroom_click_min, viewit_bedroom_click_max)

    human_sleep(viewit_pre_list_click_min, viewit_pre_list_click_max)
    page.locator("#ctl00_ContentMain_ucSearchDetails1_btnList").click(timeout=10000)
    page.wait_for_load_state("domcontentloaded", timeout=60000)

    def click_viewit_next(previous_first_href: str | None) -> bool:
        previous_index = ""
        idx_input = page.locator("#ctl00_ContentMain_UcListingsGrid_hidCurrentPageIndex").first
        if idx_input.count():
            try:
                previous_index = idx_input.input_value(timeout=1000)
            except Exception:  # noqa: BLE001
                previous_index = ""

        next_selectors = [
            "#ctl00_ContentMain_UcListingsGrid_UcSearchBar_UcPagination_lnkNext",
            "#ctl00_ContentMain_UcListingsGrid_UcPagination1_lnkNext",
            "li.page-arrow a.page-link:has(i.s-pagination-next-13px-darkestGray)",
        ]
        clicked = False
        for selector in next_selectors:
            next_link = page.locator(selector).first
            if next_link.count() == 0:
                continue
            try:
                next_link.scroll_into_view_if_needed(timeout=1500)
            except Exception:  # noqa: BLE001
                pass
            try:
                next_link.click(timeout=5000)
                clicked = True
                break
            except Exception:  # noqa: BLE001
                try:
                    next_link.dispatch_event("click")
                    clicked = True
                    break
                except Exception:  # noqa: BLE001
                    continue
        if not clicked:
            return False

        try:
            page.wait_for_function(
                """([oldHref, oldIdx]) => {
                    const first = document.querySelector("article.resultListing a.resultListing-main");
                    const newHref = first ? first.getAttribute("href") : "";
                    const idxEl = document.querySelector("#ctl00_ContentMain_UcListingsGrid_hidCurrentPageIndex");
                    const newIdx = idxEl ? idxEl.value : "";
                    return (oldHref && newHref && newHref !== oldHref) || (oldIdx !== newIdx);
                }""",
                arg=[previous_first_href or "", previous_index],
                timeout=15000,
            )
        except Exception:  # noqa: BLE001
            # Fallback for slower partial postbacks.
            human_sleep(2.0, 4.0)
        return True

    all_results: list[Listing] = []
    pages_to_visit = max(1, viewit_pages)
    for page_idx in range(pages_to_visit):
        human_sleep(viewit_page_wait_min, viewit_page_wait_max)
        maybe_dismiss_cookie_banner(page)
        simulate_human_activity(page)
        page_results = parse_viewit_results_page_html(page.content(), max_per_page=5)
        all_results.extend(page_results)

        if page_idx == pages_to_visit - 1:
            break

        current_first = ""
        first_link = page.locator("article.resultListing a.resultListing-main").first
        if first_link.count():
            current_first = first_link.get_attribute("href") or ""
        moved = click_viewit_next(current_first)
        if not moved:
            break

    dedup: dict[str, Listing] = {}
    for item in all_results:
        dedup[item.url] = item
    return list(dedup.values())


def parse_viewit_listings(search_url: str) -> list[Listing]:
    html = request_html(search_url)
    return parse_viewit_html(html)


def scrape_with_browser(
    viewit_url: str,
    kijiji_url: str,
    kijiji_pages: int,
    kijiji_delay_min: float,
    kijiji_delay_max: float,
    headed: bool,
    run_viewit: bool = True,
    run_kijiji: bool = True,
    viewit_max_price: int = 3300,
    viewit_pages: int = 3,
    viewit_bedroom_click_min: float = 1.0,
    viewit_bedroom_click_max: float = 2.0,
    viewit_pre_list_click_min: float = 1.0,
    viewit_pre_list_click_max: float = 5.0,
    viewit_page_wait_min: float = 5.0,
    viewit_page_wait_max: float = 25.0,
    kijiji_location_query: str = "Bloor Bathurst",
    kijiji_location_options: int = 6,
    kijiji_radius_km: int = 2,
) -> dict[str, list[Listing]]:
    if sync_playwright is None:
        raise RuntimeError(
            "Playwright is not installed. Install it with: pip install playwright && playwright install chromium"
        )

    results: dict[str, list[Listing]] = {"viewit": [], "kijiji": []}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed, slow_mo=120 if headed else 0)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-CA",
        )
        page = context.new_page()

        if run_viewit:
            results["viewit"] = run_viewit_filtered_flow(
                page=page,
                viewit_url=viewit_url,
                viewit_max_price=viewit_max_price,
                viewit_pages=viewit_pages,
                viewit_bedroom_click_min=viewit_bedroom_click_min,
                viewit_bedroom_click_max=viewit_bedroom_click_max,
                viewit_pre_list_click_min=viewit_pre_list_click_min,
                viewit_pre_list_click_max=viewit_pre_list_click_max,
                viewit_page_wait_min=viewit_page_wait_min,
                viewit_page_wait_max=viewit_page_wait_max,
            )

        if run_kijiji:
            results["kijiji"] = run_kijiji_filtered_flow(
                page=page,
                kijiji_url=kijiji_url,
                kijiji_pages=kijiji_pages,
                kijiji_delay_min=kijiji_delay_min,
                kijiji_delay_max=kijiji_delay_max,
                kijiji_location_query=kijiji_location_query,
                kijiji_location_options=kijiji_location_options,
                kijiji_radius_km=kijiji_radius_km,
            )

        context.close()
        browser.close()

    return results


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            address TEXT NOT NULL,
            bedrooms INTEGER NOT NULL,
            price INTEGER NOT NULL,
            url TEXT NOT NULL UNIQUE,
            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geocode_cache (
            address TEXT PRIMARY KEY,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Schema upgrades for Trello sync tracking.
    listing_cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
    if "sent_to_trello" not in listing_cols:
        conn.execute("ALTER TABLE listings ADD COLUMN sent_to_trello INTEGER NOT NULL DEFAULT 0")
    if "trello_card_id" not in listing_cols:
        conn.execute("ALTER TABLE listings ADD COLUMN trello_card_id TEXT")
    if "sent_to_trello_at" not in listing_cols:
        conn.execute("ALTER TABLE listings ADD COLUMN sent_to_trello_at TIMESTAMP")
    conn.commit()
    return conn


def resolve_trello_list_id(
    trello_key: str,
    trello_token: str,
    trello_list_id: str | None,
    trello_board_id: str | None,
) -> str:
    if trello_list_id:
        return trello_list_id
    if not trello_board_id:
        raise ValueError("Provide --trello-list-id (or TRELLO_LIST_ID) or --trello-board-id.")

    response = requests.get(
        f"https://api.trello.com/1/boards/{trello_board_id}/lists",
        params={"key": trello_key, "token": trello_token, "filter": "open", "fields": "id,name"},
        timeout=20,
    )
    response.raise_for_status()
    lists = response.json()
    if not isinstance(lists, list) or not lists:
        raise ValueError("No open lists found on the Trello board.")
    first = lists[0]
    if not isinstance(first, dict) or "id" not in first:
        raise ValueError("Unable to resolve Trello list from board.")
    return str(first["id"])


def sync_listings_to_trello(
    conn: sqlite3.Connection,
    trello_key: str,
    trello_token: str,
    trello_list_id: str | None,
    trello_board_id: str | None = None,
    limit: int = 500,
) -> tuple[int, int]:
    final_list_id = resolve_trello_list_id(trello_key, trello_token, trello_list_id, trello_board_id)
    rows = conn.execute(
        """
        SELECT id, source, address, bedrooms, price, url, first_seen_at
        FROM listings
        WHERE COALESCE(sent_to_trello, 0) = 0
        ORDER BY first_seen_at ASC
        LIMIT ?
        """,
        (max(1, limit),),
    ).fetchall()

    sent = 0
    failed = 0
    for row in rows:
        listing_id, source, address, bedrooms, price, url, first_seen_at = row
        card_name = f"{address} | {bedrooms}BR | ${price}"
        card_desc = (
            f"Source: {source}\n"
            f"Address: {address}\n"
            f"Bedrooms: {bedrooms}\n"
            f"Price: ${price}\n"
            f"URL: {url}\n"
            f"First Seen: {first_seen_at}\n"
        )
        try:
            response = requests.post(
                "https://api.trello.com/1/cards",
                params={
                    "key": trello_key,
                    "token": trello_token,
                    "idList": final_list_id,
                    "name": card_name,
                    "desc": card_desc,
                    "urlSource": url,
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            card_id = str(payload.get("id", "")).strip()
            conn.execute(
                """
                UPDATE listings
                SET sent_to_trello = 1,
                    trello_card_id = ?,
                    sent_to_trello_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (card_id, listing_id),
            )
            sent += 1
        except requests.RequestException:
            failed += 1
            continue

    conn.commit()
    return sent, failed


def get_cached_geocode(conn: sqlite3.Connection, address: str) -> tuple[float, float] | None:
    row = conn.execute(
        "SELECT latitude, longitude FROM geocode_cache WHERE address = ?",
        (address.strip(),),
    ).fetchone()
    if not row:
        return None
    return float(row[0]), float(row[1])


def cache_geocode(conn: sqlite3.Connection, address: str, lat: float, lon: float) -> None:
    conn.execute(
        """
        INSERT INTO geocode_cache (address, latitude, longitude)
        VALUES (?, ?, ?)
        ON CONFLICT(address) DO UPDATE SET latitude = excluded.latitude, longitude = excluded.longitude
        """,
        (address.strip(), lat, lon),
    )
    conn.commit()


def geocode_address(address: str, conn: sqlite3.Connection) -> tuple[float, float] | None:
    cleaned = address.strip()
    if not cleaned:
        return None

    cached = get_cached_geocode(conn, cleaned)
    if cached:
        return cached

    # Nominatim works better with city context for short listing addresses.
    query = cleaned
    if "toronto" not in query.lower():
        query = f"{query}, Toronto, ON"

    try:
        matches = request_json(
            "https://nominatim.openstreetmap.org/search",
            {"q": query, "format": "jsonv2", "limit": "1"},
            timeout=20,
        )
    except requests.RequestException:
        return None

    if not matches:
        return None

    try:
        lat = float(matches[0]["lat"])
        lon = float(matches[0]["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    cache_geocode(conn, cleaned, lat, lon)
    return lat, lon


def apply_filters(
    listings: list[Listing],
    conn: sqlite3.Connection,
    center_lat: float,
    center_lon: float,
    max_price: int,
    start_radius_km: float,
    radius_step_km: float,
    max_radius_km: float,
) -> tuple[list[Listing], float]:
    filtered: list[Listing] = []

    for item in listings:
        if item.price > max_price:
            continue

        if is_basement_unit(f"{item.address} {item.raw_text}"):
            continue

        lat = item.latitude
        lon = item.longitude

        if lat is None or lon is None:
            coords = geocode_address(item.address, conn)
            if not coords:
                continue
            lat, lon = coords

        item.distance_km = haversine_km(center_lat, center_lon, lat, lon)
        filtered.append(item)

    filtered.sort(key=lambda x: x.distance_km if x.distance_km is not None else float("inf"))

    current_radius = start_radius_km
    within_radius = [x for x in filtered if x.distance_km is not None and x.distance_km <= current_radius]
    while not within_radius and current_radius < max_radius_km:
        current_radius = min(max_radius_km, current_radius + radius_step_km)
        within_radius = [x for x in filtered if x.distance_km is not None and x.distance_km <= current_radius]

    return within_radius, current_radius


def insert_new_listings(conn: sqlite3.Connection, listings: Iterable[Listing]) -> list[Listing]:
    new_rows: list[Listing] = []
    for item in listings:
        try:
            conn.execute(
                """
                INSERT INTO listings (source, address, bedrooms, price, url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (item.source, item.address, item.bedrooms, item.price, item.url),
            )
            new_rows.append(item)
        except sqlite3.IntegrityError:
            # Duplicate URL: already saved in a previous run.
            continue

    conn.commit()
    return new_rows


def to_output_dict(item: Listing) -> dict:
    output = {
        "address": item.address,
        "bedrooms": item.bedrooms,
        "price": item.price,
        "link": item.url,
        "source": item.source,
    }
    if item.distance_km is not None:
        output["distance_km"] = round(item.distance_km, 2)
    return output


def print_db_table(rows: list[tuple]) -> None:
    headers = ["source", "bed", "price", "first_seen_at", "address", "url"]
    widths = [8, 3, 7, 19, 42, 60]
    header_row = " | ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(header_row)
    print("-" * len(header_row))

    for source, address, bedrooms, price, url, first_seen_at in rows:
        values = [
            str(source)[: widths[0]],
            str(bedrooms)[: widths[1]],
            f"${price}"[: widths[2]],
            str(first_seen_at)[: widths[3]],
            str(address).replace("\n", " ")[: widths[4]],
            str(url)[: widths[5]],
        ]
        print(" | ".join(v.ljust(w) for v, w in zip(values, widths)))


def interactive_menu() -> list[str]:
    print("\nApartment Scraper Menu")
    print("1. Run full scrape (Viewit + Kijiji)")
    print("2. Run Viewit only")
    print("3. Run Kijiji only")
    print("4. Show DB entries")
    print("5. Reset DB")
    print("6. Sync unsent DB entries to Trello")

    while True:
        choice = input("\nChoose an option (1-6) [default 1]: ").strip()
        if not choice:
            choice = "1"
        if choice in {"1", "2", "3", "4", "5", "6"}:
            break
        print("Invalid choice. Please enter a number from 1 to 6.")

    if choice in {"1", "2", "3"}:
        args: list[str] = []
        db_path = input("DB path (default listings.db): ").strip()
        if db_path:
            args.extend(["--db-path", db_path])

        headless = input("Run headless browser? (y/N): ").strip().lower() == "y"
        if headless:
            args.append("--headless")

        if choice == "2":
            args.append("--viewit-only")
            viewit_pages = input("Viewit pages to scrape (default 3): ").strip()
            if viewit_pages.isdigit() and int(viewit_pages) > 0:
                args.extend(["--viewit-pages", viewit_pages])
        elif choice == "3":
            args.append("--kijiji-only")
            kijiji_pages = input("Kijiji pages to scrape (default 3): ").strip()
            if kijiji_pages.isdigit() and int(kijiji_pages) > 0:
                args.extend(["--kijiji-pages", kijiji_pages])
        else:
            kijiji_pages = input("Kijiji pages to scrape (default 3): ").strip()
            if kijiji_pages.isdigit() and int(kijiji_pages) > 0:
                args.extend(["--kijiji-pages", kijiji_pages])
            viewit_pages = input("Viewit pages to scrape (default 3): ").strip()
            if viewit_pages.isdigit() and int(viewit_pages) > 0:
                args.extend(["--viewit-pages", viewit_pages])
        return args

    if choice == "4":
        args: list[str] = ["--show-db"]
        db_path = input("DB path (default listings.db): ").strip()
        if db_path:
            args.extend(["--db-path", db_path])
        limit = input("How many rows to show? (default 100): ").strip()
        if not limit:
            limit = "100"
        if not limit.isdigit():
            limit = "100"
        args.extend(["--limit", limit])
        return args

    if choice == "5":
        args = ["--reset-db"]
        db_path = input("DB path to reset (default listings.db): ").strip()
        if db_path:
            args.extend(["--db-path", db_path])
        return args

    args = ["--sync-trello"]
    db_path = input("DB path (default listings.db): ").strip()
    if db_path:
        args.extend(["--db-path", db_path])
    list_id = input("Trello list ID (leave blank to use board ID): ").strip()
    board_id = ""
    if list_id:
        args.extend(["--trello-list-id", list_id])
    else:
        board_id = input("Trello board ID (used to pick first open list): ").strip()
        if board_id:
            args.extend(["--trello-board-id", board_id])
    limit = input("Max cards to send this run (default 500): ").strip()
    if limit.isdigit() and int(limit) > 0:
        args.extend(["--trello-sync-limit", limit])
    return args


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    _ = argv
    raw_argv = interactive_menu()

    parser = argparse.ArgumentParser(description="Scrape Viewit and Kijiji apartment listings.")
    parser.add_argument(
        "--kijiji-url",
        default=(
            "https://www.kijiji.ca/b-apartments-condos/canada/c37l0"
        ),
        help="Kijiji search URL for apartments/condos.",
    )
    parser.add_argument(
        "--viewit-url",
        default="https://www.viewit.ca/CityPage?CID=14",
        help="Viewit search URL.",
    )
    parser.add_argument(
        "--kijiji-pages",
        type=int,
        default=3,
        help="How many Kijiji pages to visit when using browser mode.",
    )
    parser.add_argument(
        "--kijiji-delay-min",
        type=float,
        default=20.0,
        help="Minimum delay (seconds) between Kijiji page visits in browser mode.",
    )
    parser.add_argument(
        "--kijiji-delay-max",
        type=float,
        default=30.0,
        help="Maximum delay (seconds) between Kijiji page visits in browser mode.",
    )
    parser.add_argument(
        "--kijiji-location-query",
        default="Bloor Bathurst",
        help="Location text typed into Kijiji location picker in browser mode.",
    )
    parser.add_argument(
        "--kijiji-location-options",
        type=int,
        default=6,
        help="Choose randomly among the first N Kijiji location suggestions.",
    )
    parser.add_argument(
        "--kijiji-radius-km",
        type=int,
        default=2,
        help="Kijiji radius target in km (currently optimized for 2km).",
    )
    parser.add_argument(
        "--viewit-max-price",
        type=int,
        default=3300,
        help="Max price set in Viewit search form before submitting list results.",
    )
    parser.add_argument(
        "--viewit-pages",
        type=int,
        default=3,
        help="How many Viewit list result pages to scrape (5 listings/page).",
    )
    parser.add_argument(
        "--viewit-bedroom-delay-min",
        type=float,
        default=1.0,
        help="Min delay between Viewit bedroom clicks.",
    )
    parser.add_argument(
        "--viewit-bedroom-delay-max",
        type=float,
        default=2.0,
        help="Max delay between Viewit bedroom clicks.",
    )
    parser.add_argument(
        "--viewit-before-list-click-delay-min",
        type=float,
        default=1.0,
        help="Min delay before clicking 'Show results in List'.",
    )
    parser.add_argument(
        "--viewit-before-list-click-delay-max",
        type=float,
        default=5.0,
        help="Max delay before clicking 'Show results in List'.",
    )
    parser.add_argument(
        "--viewit-page-wait-min",
        type=float,
        default=5.0,
        help="Min wait on each Viewit result page before scraping listings.",
    )
    parser.add_argument(
        "--viewit-page-wait-max",
        type=float,
        default=25.0,
        help="Max wait on each Viewit result page before scraping listings.",
    )
    parser.add_argument(
        "--http-only",
        action="store_true",
        help="Use HTTP requests only (disable browser automation).",
    )
    parser.add_argument(
        "--viewit-only",
        action="store_true",
        help="Run only Viewit scraping.",
    )
    parser.add_argument(
        "--kijiji-only",
        action="store_true",
        help="Run only Kijiji scraping.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser automation headless (default is headed).",
    )
    parser.add_argument(
        "--db-path",
        default="listings.db",
        help="SQLite DB path used for deduplication across runs.",
    )
    parser.add_argument(
        "--center-lat",
        type=float,
        default=43.66564,
        help="Center latitude for radius filtering (default: Bloor & Bathurst).",
    )
    parser.add_argument(
        "--center-lon",
        type=float,
        default=-79.41110,
        help="Center longitude for radius filtering (default: Bloor & Bathurst).",
    )
    parser.add_argument(
        "--start-radius-km",
        type=float,
        default=2.0,
        help="Initial radius in km from center point.",
    )
    parser.add_argument(
        "--radius-step-km",
        type=float,
        default=1.0,
        help="How much to expand radius (km) when no listings are found.",
    )
    parser.add_argument(
        "--max-radius-km",
        type=float,
        default=10.0,
        help="Maximum radius limit while expanding outward.",
    )
    parser.add_argument(
        "--max-price",
        type=int,
        default=3500,
        help="Maximum monthly rent in dollars.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Print all listings in DB instead of only newly found ones.",
    )
    parser.add_argument(
        "--show-db",
        action="store_true",
        help="Show DB entries in a readable table and exit.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max rows when using --show-db (default: 100).",
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Delete the SQLite DB file and exit.",
    )
    parser.add_argument(
        "--sync-trello",
        action="store_true",
        help="Create Trello cards for unsent DB entries and mark them as sent.",
    )
    parser.add_argument(
        "--trello-key",
        default="",
        help="Trello API key (or set TRELLO_KEY env var).",
    )
    parser.add_argument(
        "--trello-token",
        default="",
        help="Trello API token (or set TRELLO_TOKEN env var).",
    )
    parser.add_argument(
        "--trello-list-id",
        default="",
        help="Trello list ID to create cards in (or set TRELLO_LIST_ID env var).",
    )
    parser.add_argument(
        "--trello-board-id",
        default="",
        help="Fallback Trello board ID to resolve first open list.",
    )
    parser.add_argument(
        "--trello-sync-limit",
        type=int,
        default=500,
        help="Max unsent DB rows to sync to Trello per run.",
    )
    args = parser.parse_args(raw_argv)

    if args.viewit_only and args.kijiji_only:
        parser.error("Use only one of --viewit-only or --kijiji-only.")

    if args.reset_db:
        db_path = Path(args.db_path)
        if db_path.exists():
            os.remove(db_path)
            print(f"Deleted DB: {db_path}")
        else:
            print(f"DB not found: {db_path}")
        return 0

    if args.show_db:
        conn = init_db(Path(args.db_path))
        limit = max(1, args.limit)
        rows = conn.execute(
            """
            SELECT source, address, bedrooms, price, url, first_seen_at
            FROM listings
            ORDER BY first_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        print_db_table(rows)
        print(f"\nRows shown: {len(rows)}")
        total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        print(f"Total rows in DB: {total}")
        conn.close()
        return 0

    if args.sync_trello:
        trello_key = args.trello_key or os.getenv("TRELLO_KEY", "")
        trello_token = args.trello_token or os.getenv("TRELLO_TOKEN", "")
        trello_list_id = args.trello_list_id or os.getenv("TRELLO_LIST_ID", "")
        trello_board_id = args.trello_board_id or os.getenv("TRELLO_BOARD_ID", "")
        if not trello_key or not trello_token:
            print("Missing Trello credentials. Set --trello-key/--trello-token or TRELLO_KEY/TRELLO_TOKEN.")
            return 1

        conn = init_db(Path(args.db_path))
        try:
            sent, failed = sync_listings_to_trello(
                conn=conn,
                trello_key=trello_key,
                trello_token=trello_token,
                trello_list_id=trello_list_id or None,
                trello_board_id=trello_board_id or None,
                limit=args.trello_sync_limit,
            )
        except Exception as exc:  # noqa: BLE001
            conn.close()
            print(f"Trello sync failed: {exc}")
            return 1
        remaining = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE COALESCE(sent_to_trello, 0) = 0"
        ).fetchone()[0]
        conn.close()
        print(f"Trello sync complete. Sent: {sent}, Failed: {failed}, Remaining unsent: {remaining}")
        return 0

    all_scraped: list[Listing] = []
    use_browser = not args.http_only
    run_viewit = not args.kijiji_only
    run_kijiji = not args.viewit_only

    if use_browser:
        try:
            browser_results = scrape_with_browser(
                viewit_url=args.viewit_url,
                kijiji_url=args.kijiji_url,
                kijiji_pages=args.kijiji_pages,
                kijiji_delay_min=args.kijiji_delay_min,
                kijiji_delay_max=args.kijiji_delay_max,
                headed=not args.headless,
                run_viewit=run_viewit,
                run_kijiji=run_kijiji,
                viewit_max_price=args.viewit_max_price,
                viewit_pages=args.viewit_pages,
                viewit_bedroom_click_min=args.viewit_bedroom_delay_min,
                viewit_bedroom_click_max=args.viewit_bedroom_delay_max,
                viewit_pre_list_click_min=args.viewit_before_list_click_delay_min,
                viewit_pre_list_click_max=args.viewit_before_list_click_delay_max,
                viewit_page_wait_min=args.viewit_page_wait_min,
                viewit_page_wait_max=args.viewit_page_wait_max,
                kijiji_location_query=args.kijiji_location_query,
                kijiji_location_options=args.kijiji_location_options,
                kijiji_radius_km=args.kijiji_radius_km,
            )
            if run_kijiji:
                all_scraped.extend(browser_results["kijiji"])
                print(f"[kijiji] scraped {len(browser_results['kijiji'])} candidate listing(s)", file=sys.stderr)
            if run_viewit:
                all_scraped.extend(browser_results["viewit"])
                print(f"[viewit] scraped {len(browser_results['viewit'])} candidate listing(s)", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[browser-mode] failed: {exc}", file=sys.stderr)
            return 1
    else:
        scrape_targets: list[tuple[str, Callable[[str], list[Listing]], str]] = []
        if run_kijiji:
            scrape_targets.append(("kijiji", parse_kijiji_listings, args.kijiji_url))
        if run_viewit:
            scrape_targets.append(("viewit", parse_viewit_listings, args.viewit_url))
        for scraper_name, fn, url in scrape_targets:
            try:
                results = fn(url)
                all_scraped.extend(results)
                print(f"[{scraper_name}] scraped {len(results)} candidate listing(s)", file=sys.stderr)
            except requests.HTTPError as exc:
                print(f"[{scraper_name}] HTTP error: {exc}", file=sys.stderr)
            except requests.RequestException as exc:
                print(f"[{scraper_name}] request failed: {exc}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"[{scraper_name}] parse failed: {exc}", file=sys.stderr)

    conn = init_db(Path(args.db_path))
    filtered_listings, radius_used = apply_filters(
        listings=all_scraped,
        conn=conn,
        center_lat=args.center_lat,
        center_lon=args.center_lon,
        max_price=args.max_price,
        start_radius_km=args.start_radius_km,
        radius_step_km=args.radius_step_km,
        max_radius_km=args.max_radius_km,
    )
    new_only = insert_new_listings(conn, filtered_listings)

    if args.all:
        rows = conn.execute(
            "SELECT source, address, bedrooms, price, url FROM listings ORDER BY first_seen_at DESC"
        ).fetchall()
        output = [
            {
                "address": r[1],
                "bedrooms": r[2],
                "price": r[3],
                "link": r[4],
                "source": r[0],
            }
            for r in rows
        ]
    else:
        output = [to_output_dict(item) for item in new_only]

    print(json.dumps(output, indent=2, ensure_ascii=True))
    print(
        f"Filtered {len(filtered_listings)} listing(s) within {radius_used:.1f} km. "
        f"Saved {len(new_only)} new listing(s). Total in DB: "
        f"{conn.execute('SELECT COUNT(*) FROM listings').fetchone()[0]}",
        file=sys.stderr,
    )

    # Auto-sync newly/previously unsent rows after scraping, when Trello credentials are available.
    trello_key = args.trello_key or os.getenv("TRELLO_KEY", "")
    trello_token = args.trello_token or os.getenv("TRELLO_TOKEN", "")
    trello_list_id = args.trello_list_id or os.getenv("TRELLO_LIST_ID", "")
    trello_board_id = args.trello_board_id or os.getenv("TRELLO_BOARD_ID", "")
    if trello_key and trello_token and (trello_list_id or trello_board_id):
        try:
            sent, failed = sync_listings_to_trello(
                conn=conn,
                trello_key=trello_key,
                trello_token=trello_token,
                trello_list_id=trello_list_id or None,
                trello_board_id=trello_board_id or None,
                limit=args.trello_sync_limit,
            )
            remaining = conn.execute(
                "SELECT COUNT(*) FROM listings WHERE COALESCE(sent_to_trello, 0) = 0"
            ).fetchone()[0]
            print(
                f"[trello] auto-sync complete. Sent: {sent}, Failed: {failed}, Remaining unsent: {remaining}",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[trello] auto-sync skipped due to error: {exc}", file=sys.stderr)
    else:
        print(
            "[trello] auto-sync skipped (missing TRELLO_KEY/TRELLO_TOKEN and list/board id).",
            file=sys.stderr,
        )

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
