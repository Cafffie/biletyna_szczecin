"""Szczecin Theatre (biletyna.pl) extractor implementation using the framework."""
import json
import random
import sys
import os
import time
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
from scrapers.biletyna_szczecin.biletyna_szczecin_config import (
    BASE_URL,
    COOKIE_ACCEPT_XPATH,
    DEFAULT_CITY,
    DEFAULT_COUNTRY,
    DEFAULT_CURRENCY,
    LISTING_URL,
    MAX_SCROLL_ATTEMPTS,
    REQUEST_DELAY,
    SCROLL_STABLE_ROUNDS,
)
from seleniumbase import SB

from utils.base_extractor import BaseExtractor
from utils.logger import setup_logger
from utils.scraping_helpers import (
    accept_cookies,
    get_scrape_datetime,
    human_scroll,
    normalize_country,
)

logger = setup_logger(__name__, log_to_file=False)

STATUS_AVAILABLE = "10"

# Fix Windows console encoding for Polish characters (UnicodeEncodeError)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


class BiletynaSzczecinExtractor(BaseExtractor):
    """Extractor for Szczecin theatre listings on biletyna.pl."""

    def __init__(self, **kwargs):
        """Initialize the Szczecin Theatre extractor with default settings."""
        super().__init__(
            site_id="biletyna_szczecin",
            **kwargs,
        )
        self.sb = None

    def _open_biletyna_page(self, sb, url, label):
        """Load a biletyna.pl URL, solving Cloudflare + cookie consent."""
        self.custom_logger.info(f"Opening page: [{label}] -> {url}")
        try:
            sb.driver.delete_all_cookies()
        except Exception:
            pass

        try:
            sb.uc_open_with_reconnect(url, reconnect_time=6)
        except Exception as e:
            self.custom_logger.warning(f"Failed to open {label} ({url}): {e}")
            return False

        if (
            "captcha" in sb.get_current_url().lower()
            or "distil" in sb.get_page_source().lower()
            or "just a moment" in sb.get_page_source().lower()
        ):
            self.custom_logger.info(
                f"Cloudflare challenge detected on [{label}] — clearing cookies and solving captcha"
            )

            try:
                sb.driver.delete_all_cookies()
            except Exception:
                pass
            sb.uc_gui_handle_captcha()
            time.sleep(random.uniform(2, 4))

        accept_cookies(
            sb.driver,
            COOKIE_ACCEPT_XPATH,
            once_per_domain=False,
            logger=self.custom_logger,
        )
        human_scroll(sb)
        sb.sleep(REQUEST_DELAY)

        success = "just a moment" not in sb.get_page_source().lower()
        if success:
            self.custom_logger.debug(f"Successfully loaded and verified [{label}]")
        else:
            self.custom_logger.warning(f"Cloudflare challenge did not clear for [{label}]")

        return success

    def safe_get(self, sb, url, wait=10):
        try:
            self.custom_logger.info("Loading URL: %s", url)
            sb.uc_open_with_reconnect(url, reconnect_time=wait if wait > 4 else 4)
            if (
                "captcha" in sb.get_current_url().lower()
                or "distil" in sb.get_page_source().lower()
            ):
                self.custom_logger.warning("Bot protection detected. Solving...")
                sb.uc_gui_handle_captcha()
                time.sleep(random.uniform(2, 4))
            self.custom_logger.info("Page loaded successfully: %s", url)
            return True
        except Exception as e:
            self.custom_logger.error(
                "Failed to load page: %s | Exception: %s", url, repr(e)
            )
            return None

    @staticmethod
    def _load_item_list(script_text):
        """Parse a <script type="application/ld+json"> body."""
        if not script_text:
            return None
        try:
            payload = json.loads(script_text)
        except (TypeError, ValueError):
            return None
        if isinstance(payload, dict) and payload.get("@type") == "ItemList":
            return payload
        return None

    def _collect_performance_events(self, sb):
        """Load the listing page and collect every TheaterEvent entry."""
        self.custom_logger.info("Starting performance event collection from listing...")
        if not self._open_biletyna_page(sb, LISTING_URL, "listing page"):
            self.custom_logger.warning(
                "Cloudflare challenge on listing page never cleared"
            )

        performance_events_by_key = {}
        previous_event_count = 0
        stable_rounds = 0
        scroll_attempt = 0

        while (
            stable_rounds < SCROLL_STABLE_ROUNDS
            and scroll_attempt <= MAX_SCROLL_ATTEMPTS
        ):
            soup = sb.get_beautiful_soup()
            found_in_round = 0
            for script_tag in soup.find_all("script", type="application/ld+json"):
                item_list = self._load_item_list(script_tag.get_text())
                if item_list is None:
                    continue
                for list_entry in item_list.get("itemListElement", []):
                    theater_event = list_entry.get("item", {})
                    event_url = theater_event.get("url")
                    start_date = theater_event.get("startDate")
                    if not event_url or not start_date:
                        continue
                    if (event_url, start_date) not in performance_events_by_key:
                        found_in_round += 1
                    performance_events_by_key[(event_url, start_date)] = theater_event

            current_count = len(performance_events_by_key)
            self.custom_logger.info(
                f"[Scroll Attempt {scroll_attempt}/{MAX_SCROLL_ATTEMPTS}] "
                f"Total events so far: {current_count} (+{found_in_round} new this round) | "
                f"Stable rounds: {stable_rounds}/{SCROLL_STABLE_ROUNDS}"
            )

            if current_count == previous_event_count:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous_event_count = current_count

            human_scroll(sb)
            sb.sleep(REQUEST_DELAY)
            scroll_attempt += 1

        self.custom_logger.info(
            f"Finished listing page scroll. Collected {len(performance_events_by_key)} total performance events."
        )
        return list(performance_events_by_key.values())

    @staticmethod
    def _clean_venue_url(raw_url):
        """Strip the per-performance tracking query string from a show URL."""
        if not raw_url:
            return raw_url
        return raw_url.split("?")[0]

    @staticmethod
    def _extract_event_id(raw_url):
        """Pull the per-performance ``eid`` query param from a URL."""
        if not raw_url:
            return None
        query = urlparse(raw_url).query
        values = parse_qs(query).get("eid")
        return values[0] if values else None

    @staticmethod
    def _parse_iso_datetime(raw_value):
        """Parse a schema.org ISO-8601 datetime string, or return None."""
        if not raw_value:
            return None
        try:
            return datetime.fromisoformat(raw_value)
        except ValueError:
            return None

    def _new_show_record(self, theater_event):
        """Build the initial per-show accumulator from its first performance."""
        location = theater_event.get("location", {}) or {}
        address_info = location.get("address", {}) or {}
        street_address = address_info.get("streetAddress")
        postal_code = address_info.get("postalCode")
        locality = address_info.get("addressLocality") or DEFAULT_CITY
        address_line = ", ".join(
            part
            for part in (street_address, f"{postal_code} {locality}".strip())
            if part
        )

        offers = theater_event.get("offers", {}) or {}

        return {
            "title": theater_event.get("name"),
            "venue_url": self._clean_venue_url(theater_event.get("url")),
            "venue": location.get("name"),
            "address": address_line or None,
            "city": locality,
            "country": normalize_country(address_info.get("addressCountry"))
            or DEFAULT_COUNTRY,
            "currency": offers.get("priceCurrency") or DEFAULT_CURRENCY,
            "upcoming_performances": [],
            "price": None,
            "remaining_capacities": [],
            "real_capacities": [],
            "event_ids_by_performance": {},
        }

    def _add_performance(self, show_record, theater_event):
        """Fold one TheaterEvent performance instance into a show record."""
        performance_datetime = self._parse_iso_datetime(theater_event.get("startDate"))
        if performance_datetime is None:
            return

        performance_date = performance_datetime.strftime("%Y-%m-%d")
        performance_time = performance_datetime.strftime("%H:%M")
        performance_entry = {"date": performance_date, "time": performance_time}
        if performance_entry not in show_record["upcoming_performances"]:
            show_record["upcoming_performances"].append(performance_entry)

        offers = theater_event.get("offers", {}) or {}
        low_price = offers.get("lowPrice")
        if isinstance(low_price, (int, float)):
            show_record["price"] = float(low_price)

        remaining_capacity = theater_event.get("remainingAttendeeCapacity")
        if isinstance(remaining_capacity, (int, float)):
            show_record["remaining_capacities"].append(int(remaining_capacity))

        event_id = self._extract_event_id(theater_event.get("url"))
        if event_id:
            performance_key = f"{performance_date} {performance_time}"
            show_record["event_ids_by_performance"][performance_key] = event_id

    def _find_sector_urls(self, sb, event_url):
        """From an event page, find every sector's numbered-seat-grid URL."""
        current_url = sb.get_current_url()
        if "/event/sector/id/" in current_url:
            self.custom_logger.debug(f"Direct redirect to sector URL: {current_url}")
            return {current_url}

        soup = sb.get_beautiful_soup()
        sector_urls = {
            urljoin(BASE_URL, a["href"])
            for a in soup.select('a[href^="/event/sector/id/"]')
            if a.get("href")
        }
        if sector_urls:
            self.custom_logger.debug(f"Found {len(sector_urls)} explicit sector URL(s)")
            return sector_urls

        if soup.select_one("div.place[data-row_number][data-place_number]"):
            self.custom_logger.debug("Inline seat map detected on event main page.")
            return {current_url}

        return set()

    @staticmethod
    def _sector_id_from_url(sector_url):
        """Pull the numeric sector id off a ``/event/sector/id/{id}`` URL."""
        return sector_url.rstrip("/").rsplit("/", 1)[-1]

    def _parse_sector_seats(self, sb, fallback_sector_id=None):
        """Parse the numbered seat grid on a sector page."""
        soup = sb.get_beautiful_soup()

        sector_label = None
        sector_container = soup.select_one("div.gnp_sector[data-sector_name]")
        if sector_container:
            raw_name = (sector_container.get("data-sector_name") or "").strip()
            if raw_name:
                sector_label = raw_name.title()
        if not sector_label and fallback_sector_id:
            sector_label = fallback_sector_id

        seats = []
        total_seat_count = 0
        for place in soup.select("div.place[data-row_number][data-place_number]"):
            total_seat_count += 1
            if place.get("data-place_status") != STATUS_AVAILABLE:
                continue
            price = place.get("data-place_price")
            row = place.get("data-row_number")
            number = place.get("data-place_number")
            if not price or not row or not number:
                continue
            seat_id = f"{row}-{number}"
            if sector_label:
                seat_id = f"{sector_label} {seat_id}"
            seats.append({"seat": seat_id, "ticket_price": float(price)})

        self.custom_logger.debug(
            f"Sector [{sector_label or 'Unknown'}]: Found {len(seats)} available seats out of {total_seat_count} total capacity"
        )
        return seats, total_seat_count

    def _scrape_seat_map(self, sb, event_id):
        """Fetch the real on-sale seat inventory for one performance instance."""
        event_url = f"{BASE_URL}/event/view/id/{event_id}"
        self.custom_logger.info(f"Scraping seat map for event ID: {event_id}")

        if not self._open_biletyna_page(sb, event_url, f"event {event_id}"):
            self.custom_logger.warning(f"Could not open event page for ID: {event_id}")
            return None, None

        sector_urls = self._find_sector_urls(sb, event_url)
        if not sector_urls:
            self.custom_logger.warning(f"No sector links or seat map found for event {event_id}")
            return None, None

        seats = []
        capacity = 0
        for sector_url in sector_urls:
            if sector_url != sb.get_current_url():
                if not self._open_biletyna_page(sb, sector_url, f"sector {sector_url}"):
                    self.custom_logger.warning(f"Failed to open sector URL: {sector_url}")
                    continue
            fallback_sector_id = self._sector_id_from_url(sector_url)
            sector_seats, sector_capacity = self._parse_sector_seats(
                sb, fallback_sector_id
            )
            seats.extend(sector_seats)
            capacity += sector_capacity

        self.custom_logger.info(
            f"Event ID {event_id} seat map complete: {len(seats)} available seats, total capacity: {capacity}"
        )
        return seats, capacity

    def _collect_seat_maps(self, sb, show_record):
        """Fetch the real seat map for every performance of one show."""
        perf_count = len(show_record["event_ids_by_performance"])
        self.custom_logger.info(
            f"Collecting seat maps for '{show_record['title']}' ({perf_count} performance(s))"
        )

        seat_pricing_by_performance = {}
        for idx, (performance_key, event_id) in enumerate(
            show_record["event_ids_by_performance"].items(), start=1
        ):
            self.custom_logger.info(
                f"  [{idx}/{perf_count}] Scraping performance: {performance_key} (Event ID: {event_id})"
            )
            seats, capacity = self._scrape_seat_map(sb, event_id)
            if capacity:
                show_record["real_capacities"].append(capacity)
            if seats:
                seat_pricing_by_performance[performance_key] = seats
            else:
                self.custom_logger.warning(
                    f"No real seat map for '{show_record['title']}' @ "
                    f"{performance_key} (event {event_id}) — "
                    "falling back to General Admission"
                )
        show_record["seat_pricing_by_performance"] = seat_pricing_by_performance

    def _finalize_show_record(self, show_record):
        """Turn a per-show accumulator into the final output row."""
        self.custom_logger.debug(f"Finalizing show record for '{show_record['title']}'")
        upcoming_performances = sorted(
            show_record["upcoming_performances"],
            key=lambda entry: (entry["date"], entry["time"]),
        )
        performance_dates = [entry["date"] for entry in upcoming_performances]
        open_date = min(performance_dates) if performance_dates else None
        close_date = max(performance_dates) if performance_dates else None

        if show_record["real_capacities"]:
            capacity = max(show_record["real_capacities"])
        elif show_record["remaining_capacities"]:
            capacity = max(show_record["remaining_capacities"])
        else:
            capacity = None
        price = show_record["price"]
        seat_pricing_by_performance = show_record.get("seat_pricing_by_performance", {})

        seat_pricing = {}
        for entry in upcoming_performances:
            performance_key = f"{entry['date']} {entry['time']}"
            real_seats = seat_pricing_by_performance.get(performance_key)
            if real_seats:
                seat_pricing[performance_key] = real_seats
            elif price is not None:
                seat_pricing[performance_key] = [
                    {"seat": "General Admission", "ticket_price": price}
                ]

        return {
            "title": show_record["title"],
            "venue_url": show_record["venue_url"],
            "category": None,
            "venue": show_record["venue"],
            "address": show_record["address"],
            "city": show_record["city"],
            "country": show_record["country"],
            "open_date": open_date,
            "close_date": close_date,
            "booking_start_date": None,
            "booking_end_date": close_date,
            "upcoming_performances": upcoming_performances,
            "capacity": capacity,
            "currency": show_record["currency"],
            "is_limited_run": bool(open_date and close_date),
            "seat_pricing": seat_pricing,
            "scrape_datetime": get_scrape_datetime(),
        }

    def extract(self) -> bytes:
        """Extract raw data from the Szczecin theatre listing on biletyna.pl."""
        self.custom_logger.info(f"Starting extraction from {LISTING_URL}")

        with SB(
            headless=True,
            uc=True,
            ad_block=False,
            cft=True,
            locale="en-US",
            browser="chrome",
        ) as sb:
            try:
                sb.driver.maximize_window()
            except Exception:
                pass

            theater_events = self._collect_performance_events(sb)
            self.custom_logger.info(
                f"Processing {len(theater_events)} raw performance instance(s)..."
            )

            shows_by_venue_url = {}
            for theater_event in theater_events:
                title = theater_event.get("name")
                venue_url = self._clean_venue_url(theater_event.get("url"))
                if not title or not venue_url:
                    continue

                if venue_url not in shows_by_venue_url:
                    if (
                        self.local_test
                        and self.show_count is not None
                        and len(shows_by_venue_url) >= self.show_count
                    ):
                        continue
                    shows_by_venue_url[venue_url] = self._new_show_record(theater_event)

                self._add_performance(shows_by_venue_url[venue_url], theater_event)

            total_shows = len(shows_by_venue_url)
            self.custom_logger.info(
                f"Grouped into {total_shows} unique show(s) to process."
            )

            if self.local_test:
                self.custom_logger.info(
                    f"Local test mode active — limited to {self.show_count} show(s)"
                )

            for idx, (venue_url, show_record) in enumerate(shows_by_venue_url.items(), start=1):
                if not show_record["upcoming_performances"]:
                    self.custom_logger.warning(
                        f"Skipping show [{idx}/{total_shows}] '{show_record['title']}' - No upcoming performances."
                    )
                    continue
                self.custom_logger.info(
                    f"Processing show [{idx}/{total_shows}]: '{show_record['title']}' ({venue_url})"
                )
                self._collect_seat_maps(sb, show_record)

        self.custom_logger.info("Finalizing show records and building output JSON...")
        all_events = []
        for show_record in shows_by_venue_url.values():
            if not show_record["upcoming_performances"]:
                continue
            event_metadata = self._finalize_show_record(show_record)
            all_events.append(event_metadata)
            self.log_record(event_metadata)

        combined_data = json.dumps(all_events, indent=2)
        self.custom_logger.info(f"Extraction completed successfully. Total valid shows parsed: {len(all_events)}")
        return combined_data.encode("utf-8")

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        self.custom_logger.info("Transforming extracted DataFrame columns...")
        if "capacity" in df.columns:
            df["capacity"] = pd.to_numeric(df["capacity"], errors="coerce").astype(
                "Int64"
            )
        return df

    def _parse(self, raw: bytes) -> pd.DataFrame:
        """Parse JSON content to DataFrame."""
        self.custom_logger.info("Starting data parsing into DataFrame")
        data = json.loads(raw.decode("utf-8"))
        df = pd.DataFrame(data)

        self.custom_logger.info(f"Parsing completed. Extracted DataFrame contains {len(df)} row(s)")
        return df


def main():
    """Example usage of the Szczecin Theatre extractor."""
    extractor = BiletynaSzczecinExtractor(
        save_csv_locally=False, csv_incremental_mode=False
    )
    result = extractor.run()
    logger.info(f"Extraction result status: {result.get('status')}")
    if result.get("status") != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
