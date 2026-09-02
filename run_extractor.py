"""Poland-cities (biletyna.pl) extractor implementation using the framework.

Merges what used to be five separate single-city scrapers (Szczecin,
Wroclaw, Bydgoszcz, Olsztyn, Bialystok) into one — see LISTING_URLS in
the config. All five are the same biletyna.pl platform, just filtered
to a different city_id, so one pass of the existing Szczecin scrape
logic runs once per city and the results are merged.
"""
import json
import random
import sys
import time
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
from seleniumbase import SB

from scrapers.biletyna_szczecin.biletyna_szczecin_config import (
    BASE_URL,
    COOKIE_ACCEPT_XPATH,
    DEFAULT_CITY,
    DEFAULT_COUNTRY,
    DEFAULT_CURRENCY,
    LISTING_URLS,
    MAX_SCROLL_ATTEMPTS,
    REQUEST_DELAY,
    SCROLL_STABLE_ROUNDS,
)
from utils.base_extractor import BaseExtractor
from utils.logger import setup_logger
from utils.scraping_helpers import (
    accept_cookies,
    get_scrape_datetime,
    human_scroll,
    normalize_country,
)

logger = setup_logger(__name__, log_to_file=False)

# biletyna.pl fronts every page — including the plain listing — with a
# Cloudflare Turnstile "Just a moment..." interstitial. It reliably clears
# once per browser session by dropping any stale cookies and clicking the
# widget through SeleniumBase's UC-mode GUI helper, but that click is a real
# OS-level PyAutoGUI action: it only works in a headed session, never
# headless. Every page this extractor visits (listing, event, sector) needs
# the same treatment, since each is a fresh Cloudflare-eligible route —
# including each of the five city listings this extractor now cycles through.
#
# This extractor covers five biletyna.pl city listings directly (Szczecin,
# Wroclaw, Bydgoszcz, Olsztyn, Bialystok — see LISTING_URLS) — same platform
# as slupsktheatre/lodztheatre/tychytheatre, which cover other cities on the
# same site and haven't been folded in here. Keep them in sync: fixes to the
# Cloudflare/seat-map handling here almost certainly apply there too (and
# vice versa). In particular, some venues (confirmed on tychytheatre's Teatr
# Maly w Tychach) render their seat grid directly on the event page with no
# separate /event/sector/id/ URL at all — _find_sector_urls below falls back
# to treating the event page itself as the lone sector when that happens,
# instead of reporting no seat map.
STATUS_AVAILABLE = "10"


class BiletynaSzczecinExtractor(BaseExtractor):
    """Extractor for biletyna.pl theatre listings across five Polish cities."""

    def __init__(self, **kwargs):
        """Initialize the merged biletyna.pl multi-city extractor with default settings."""
        super().__init__(
            site_id="biletyna_szczecin",
            **kwargs,
        )
        self.sb = None

    def _open_biletyna_page(self, sb, url, label):
        """Load a biletyna.pl URL, solving Cloudflare + cookie consent."""
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
                f"Cloudflare challenge on {label} — clearing cookies and solving captcha"
            )

            try:
                sb.driver.delete_all_cookies()
            except Exception:
                pass
            # sb.uc_gui_click_cf()
            sb.uc_gui_handle_captcha()
            time.sleep(random.uniform(2, 4))
            # sb.sleep(4)

        accept_cookies(
            sb.driver,
            COOKIE_ACCEPT_XPATH,
            once_per_domain=False,
            logger=self.custom_logger,
        )
        human_scroll(sb)
        sb.sleep(REQUEST_DELAY)

        return "just a moment" not in sb.get_page_source().lower()

    @staticmethod
    def _load_item_list(script_text):
        """Parse a <script type="application/ld+json"> body.

        Returns the schema.org ItemList payload embedded in the listing page,
        or None if this particular script tag holds something else (the page
        also ships an unrelated FAQPage block).
        """
        if not script_text:
            return None
        try:
            payload = json.loads(script_text)
        except (TypeError, ValueError):
            return None
        if isinstance(payload, dict) and payload.get("@type") == "ItemList":
            return payload
        return None

    @staticmethod
    def _city_from_listing_url(listing_url):
        """Pull the human-readable city name out of a listing URL.

        biletyna.pl listing URLs are shaped ``/spektakl/<City>?city_id=<id>``
        (e.g. "Szczecin", "Bialystok"). Used as a log label for the current
        city, and as the fallback city on a show record whose own schema.org
        address is ever missing ``addressLocality``.
        """
        path = urlparse(listing_url).path
        city = path.rstrip("/").rsplit("/", 1)[-1]
        return city or DEFAULT_CITY

    def _collect_performance_events(self, sb):
        """Loop over every configured city listing and collect every
        TheaterEvent entry across all of them.

        Each city listing renders one schema.org ItemList of TheaterEvent
        items per performance instance, then lazy-loads more as the visitor
        scrolls. Scrolling for a given city stops once that city's collected
        performance count stops growing for SCROLL_STABLE_ROUNDS consecutive
        rounds, then the next city listing starts fresh. All cities share one
        deduplicated map keyed by (event_url, start_date) — this also guards
        against the same show appearing on more than one city's listing.
        """
        performance_events_by_key = {}

        for listing_index, listing_url in enumerate(LISTING_URLS, start=1):
            city_name = self._city_from_listing_url(listing_url)
            self.custom_logger.info(
                f"[City {listing_index}/{len(LISTING_URLS)}] {city_name}: "
                f"loading listing — {listing_url}"
            )

            if not self._open_biletyna_page(
                sb, listing_url, f"{city_name} listing page"
            ):
                self.custom_logger.warning(
                    f"[City {listing_index}/{len(LISTING_URLS)}] {city_name}: "
                    "Cloudflare challenge never cleared — skipping this city"
                )
                continue

            # Track this city's own event count separately from the running
            # cross-city total, so "stable" reflects this city's scroll
            # having stopped growing — not the (already-large) global count.
            events_before_this_city = len(performance_events_by_key)
            previous_city_event_count = 0
            stable_rounds = 0
            scroll_attempt = 0

            while (
                stable_rounds < SCROLL_STABLE_ROUNDS
                and scroll_attempt <= MAX_SCROLL_ATTEMPTS
            ):
                soup = sb.get_beautiful_soup()
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
                        theater_event.setdefault("_fallback_city", city_name)
                        performance_events_by_key[
                            (event_url, start_date)
                        ] = theater_event

                current_city_event_count = (
                    len(performance_events_by_key) - events_before_this_city
                )
                if current_city_event_count == previous_city_event_count:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                previous_city_event_count = current_city_event_count

                human_scroll(sb)
                sb.sleep(REQUEST_DELAY)
                scroll_attempt += 1

            self.custom_logger.info(
                f"[City {listing_index}/{len(LISTING_URLS)}] {city_name}: "
                f"{previous_city_event_count} performance instance(s) found "
                f"({len(performance_events_by_key)} total across all cities so far)"
            )

        return list(performance_events_by_key.values())

    @staticmethod
    def _clean_venue_url(raw_url):
        """Strip the per-performance tracking query string from a show URL,
        and a trailing slash so "/spektakl/x" and "/spektakl/x/" — which are
        the same page — group under one dict key instead of two.
        """
        if not raw_url:
            return raw_url
        return raw_url.split("?")[0].rstrip("/")

    @staticmethod
    def _extract_event_id(raw_url):
        """Pull the per-performance ``eid`` query param biletyna.pl tags
        each TheaterEvent's ``url`` with — it's the id the purchase flow
        (``/event/view/id/{eid}``) needs, and the only place it appears.
        """
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
        # addressLocality is almost always present, but if it's ever missing
        # fall back to the city of the listing this event was collected
        # from (tagged in _collect_performance_events) rather than a single
        # hardcoded city — this scraper now covers five cities at once.
        locality = (
            address_info.get("addressLocality")
            or theater_event.get("_fallback_city")
            or DEFAULT_CITY
        )
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

        # offers.lowPrice is only the aggregate "starting from" price. It's
        # kept as a General Admission fallback for performances whose real
        # seat map can't be reached (Cloudflare didn't clear, sector page
        # changed shape, etc.) — see _collect_seat_maps / _finalize_show_record.
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
        """From an event page, find every sector's numbered-seat-grid URL.

        Most venues list one or more sectors to choose from
        (``/event/sector/id/{id}`` links); a single-sector venue sometimes
        skips straight to the seat grid itself via redirect, so the event
        page's own URL is checked too. Some venues go one step further and
        render the seat grid inline with no ``/event/sector/id/`` URL
        anywhere (no redirect, no link) — so if neither of those turn up
        anything but the page already has real seat divs on it, treat the
        event page itself as the lone sector rather than reporting no seat
        map at all.
        """
        current_url = sb.get_current_url()
        if "/event/sector/id/" in current_url:
            return {current_url}

        soup = sb.get_beautiful_soup()
        sector_urls = {
            urljoin(BASE_URL, a["href"])
            for a in soup.select('a[href^="/event/sector/id/"]')
            if a.get("href")
        }
        if sector_urls:
            return sector_urls

        if soup.select_one("div.place[data-row_number][data-place_number]"):
            return {current_url}

        return set()

    @staticmethod
    def _sector_id_from_url(sector_url):
        """Pull the numeric sector id off a ``/event/sector/id/{id}`` URL."""
        return sector_url.rstrip("/").rsplit("/", 1)[-1]

    def _parse_sector_seats(self, sb, fallback_sector_id=None):
        """Parse the numbered seat grid on a ``/event/sector/id/...`` page.

        Each real seat is a ``div.place`` with ``data-row_number`` +
        ``data-place_number`` identifying it and ``data-place_status``
        marking availability ("10" == on sale — see STATUS_AVAILABLE).
        Structural gaps in the grid (aisles) are ``div.place`` too but carry
        neither attribute, so the selector requires both.

        Every sector page also carries a human-readable label in its
        ``<div class="gnp_sector" data-sector_name="...">`` wrapper (e.g.
        "parter", "balkon", "loza prawa") — confirmed present whether the
        grid was reached via a dedicated sector URL or rendered inline on
        the event page. Each seat id is prefixed with that label
        (title-cased) so it reads like "Parter 1-1" instead of a bare
        "1-1". The label doubles as the disambiguator a multi-sector venue
        needs — row+number is only unique *within* a sector, so two
        sectors can otherwise both have a "1-1" — falling back to the
        opaque numeric sector id only on the rare page that ships no
        ``data-sector_name`` at all, so seats never silently collide.
        Row and number are joined with "-" rather than concatenated
        directly: some venues use numeric row numbers, where row "1" +
        number "28" and row "12" + number "8" would otherwise both
        stringify to the same "128". Neither value is ever parsed as a
        number, so a venue using Roman numeral rows or lettered seat
        numbers (e.g. reduced-mobility seats labeled "A"/"B"/"C"/"D") is
        handled the same way — the id is built from whatever string each
        attribute holds.

        Returns (seats, total_seat_count) — total_seat_count is every real
        seat div in this sector regardless of data-place_status, i.e.
        available + unavailable, which is what "capacity" means. Counting
        only the ones that pass the availability filter below would make
        capacity collapse to "how many are left", not the venue's actual
        seat count.
        """
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
        return seats, total_seat_count

    def _scrape_seat_map(self, sb, event_id):
        """Fetch the real on-sale seat inventory for one performance instance.

        Walks the purchase flow biletyna.pl requires to reach individual
        seats: the event page (which lists sector(s)) then each sector's
        numbered seat grid. Returns (None, None) if the event page itself
        couldn't be reached, so callers can fall back to the General
        Admission price instead of recording "zero seats on sale".

        Returns (seats, capacity) — capacity is summed across every sector
        (available + unavailable in each), the venue's real total for this
        performance, not just what's currently on sale.
        """
        event_url = f"{BASE_URL}/event/view/id/{event_id}"

        self.custom_logger.info(f"Starting seat-map scraping for event ID={event_id}")

        if not self._open_biletyna_page(sb, event_url, f"event {event_id}"):
            return None, None

        sector_urls = self._find_sector_urls(sb, event_url)
        if not sector_urls:
            self.custom_logger.warning(f"No sector links found for event {event_id}")
            return None, None

        seats = []
        capacity = 0
        for sector_url in sector_urls:
            if sector_url != sb.get_current_url():
                if not self._open_biletyna_page(sb, sector_url, f"sector {sector_url}"):
                    continue
            fallback_sector_id = self._sector_id_from_url(sector_url)
            sector_seats, sector_capacity = self._parse_sector_seats(
                sb, fallback_sector_id
            )
            seats.extend(sector_seats)
            capacity += sector_capacity

        self.custom_logger.info(
            f"Seat-map result for event ID={event_id}: "
            f"{len(seats)} seat(s) on sale, capacity={capacity}"
        )
        return seats, capacity

    def _collect_seat_maps(self, sb, show_record):
        """Fetch the real seat map for every performance of one show,
        storing results on the accumulator for _finalize_show_record to
        prefer over the General Admission fallback.
        """

        self.custom_logger.info(
            f"Starting seat-map collection for show: " f"{show_record['title']!r}"
        )

        seat_pricing_by_performance = {}
        for performance_key, event_id in show_record[
            "event_ids_by_performance"
        ].items():
            seats, capacity = self._scrape_seat_map(sb, event_id)
            if capacity:
                # available + unavailable seats actually counted off the
                # real seat grid — this is what "capacity" means. Recorded
                # even when this performance turned out sold out (seats
                # empty, capacity > 0), since the venue's seat count is
                # unaffected by which of them happen to be free right now.
                show_record["real_capacities"].append(capacity)
            if seats:
                seat_pricing_by_performance[performance_key] = seats
                self.custom_logger.info(
                    f"  {show_record['title']!r} @ {performance_key}: "
                    f"{len(seats)} seat(s) found, capacity={capacity}"
                )
            else:
                self.custom_logger.warning(
                    f"No real seat map for {show_record['title']!r} @ "
                    f"{performance_key} (event {event_id}) — "
                    "falling back to General Admission"
                )
        show_record["seat_pricing_by_performance"] = seat_pricing_by_performance

    def _dedupe_shows(self, shows_by_venue_url):
        """Collapse shows that are really the same production at the same
        venue but ended up under two different venue_url keys.

        Grouping by venue_url in extract() already prevents most duplicates,
        but merging five separate city listings into one run opens a new
        gap: biletyna.pl can mint a fresh numeric show id for what a viewer
        would recognise as the identical show/venue (e.g. a re-listing, or
        the show turning up on more than one city's page under a slightly
        different URL). Dedupe on (title, venue) case-insensitively instead;
        the first-seen venue_url is kept and absorbs the duplicate's
        performances/event ids rather than silently dropping that data.
        """
        deduped = {}
        kept_venue_url_by_identity = {}

        for venue_url, show_record in shows_by_venue_url.items():
            title_norm = (show_record["title"] or "").strip().casefold()
            venue_norm = (show_record["venue"] or "").strip().casefold()
            identity_key = (title_norm, venue_norm)

            kept_venue_url = kept_venue_url_by_identity.get(identity_key)
            if kept_venue_url is None:
                kept_venue_url_by_identity[identity_key] = venue_url
                deduped[venue_url] = show_record
                continue

            self.custom_logger.info(
                f"Duplicate show detected: {show_record['title']!r} — "
                f"merging {venue_url} into {kept_venue_url}"
            )
            kept_record = deduped[kept_venue_url]
            for performance in show_record["upcoming_performances"]:
                if performance not in kept_record["upcoming_performances"]:
                    kept_record["upcoming_performances"].append(performance)
            kept_record["event_ids_by_performance"].update(
                show_record["event_ids_by_performance"]
            )
            kept_record["remaining_capacities"].extend(
                show_record["remaining_capacities"]
            )
            kept_record["real_capacities"].extend(show_record["real_capacities"])
            if kept_record["price"] is None:
                kept_record["price"] = show_record["price"]

        removed_count = len(shows_by_venue_url) - len(deduped)
        if removed_count:
            self.custom_logger.info(f"Removed {removed_count} duplicate show(s)")
        return deduped

    def _log_show_summary(self, record):
        """Log one plain 'label: value' line per field for a finalized show,
        so a run is easy to grep/eyeball without digging through the raw
        JSON output.
        """
        seat_pricing = record.get("seat_pricing") or {}
        total_seats_found = sum(len(seats) for seats in seat_pricing.values())

        self.custom_logger.info(f"title: {record['title']}")
        self.custom_logger.info(f"open dates: {record['open_date']}")
        self.custom_logger.info(f"close dates: {record['close_date']}")
        self.custom_logger.info(f"venue: {record['venue']}")
        self.custom_logger.info(f"capacity: {record['capacity']}")
        self.custom_logger.info(f"seats: {total_seats_found}")
        self.custom_logger.info(f"city: {record['city']}")
        self.custom_logger.info(f"address: {record['address']}")
        self.custom_logger.info(f"currency: {record['currency']}")

    def _finalize_show_record(self, show_record):
        """Turn a per-show accumulator into the final output row."""
        upcoming_performances = sorted(
            show_record["upcoming_performances"],
            key=lambda entry: (entry["date"], entry["time"]),
        )
        performance_dates = [entry["date"] for entry in upcoming_performances]
        open_date = min(performance_dates) if performance_dates else None
        close_date = max(performance_dates) if performance_dates else None
        # Prefer the real seat-grid count (available + unavailable, scraped
        # directly off each sector's seat divs) — remaining_capacities is
        # schema.org's remainingAttendeeCapacity, which is only how many
        # seats are LEFT, not the venue's actual total; it's kept only as a
        # fallback for a performance whose seat grid couldn't be reached at
        # all (Cloudflare didn't clear, sector page changed shape, etc.).
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
        """Extract raw data from all configured biletyna.pl city listings."""
        self.custom_logger.info(
            f"Starting extraction from {len(LISTING_URLS)} city listing(s): "
            + ", ".join(self._city_from_listing_url(u) for u in LISTING_URLS)
        )

        # headless=False + uc=True is required, not a preference: the
        # Cloudflare Turnstile widget only ever resolves through
        # uc_gui_click_cf()'s real OS-level GUI click. ad_block=False +
        # cft=True are the existing Windows-Chrome-launch fixes.
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
                f"Found {len(theater_events)} performance instance(s) "
                f"across {len(LISTING_URLS)} cities"
            )

            # Group performance instances into shows by venue_url (a show
            # can have many performances but shares one venue_url), capped
            # globally across all cities combined in local_test mode.
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

            if self.local_test:
                self.custom_logger.info(
                    f"Local test mode — limited to {self.show_count} show(s)"
                )

            shows_by_venue_url = self._dedupe_shows(shows_by_venue_url)

            total_shows = len(shows_by_venue_url)
            self.custom_logger.info(
                f"Grouped into {total_shows} distinct show(s) — "
                "starting seat-map collection"
            )

            # Finalize and log each show right after its own seat maps are
            # collected, instead of collecting seat maps for every show
            # first and only then logging summaries — so progress is
            # visible show-by-show instead of arriving in one batch at the
            # very end of the run.
            all_events = []
            for show_index, show_record in enumerate(
                shows_by_venue_url.values(), start=1
            ):
                if not show_record["upcoming_performances"]:
                    continue
                self.custom_logger.info(
                    f"[Show {show_index}/{total_shows}] "
                    f"{show_record['title']!r} ({show_record['city']})"
                )
                self._collect_seat_maps(sb, show_record)

                event_metadata = self._finalize_show_record(show_record)
                all_events.append(event_metadata)
                self.log_record(event_metadata)
                self._log_show_summary(event_metadata)

        combined_data = json.dumps(all_events, indent=2)
        self.custom_logger.info(f"Extraction completed. Total shows: {len(all_events)}")
        return combined_data.encode("utf-8")

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        if "capacity" in df.columns:
            df["capacity"] = pd.to_numeric(df["capacity"], errors="coerce").astype(
                "Int64"
            )
        return df

    def _parse(self, raw: bytes) -> pd.DataFrame:
        """Parse JSON content to DataFrame."""
        self.custom_logger.info("Starting data parsing")
        data = json.loads(raw.decode("utf-8"))
        df = pd.DataFrame(data)

        self.custom_logger.info(f"Parsing completed. Extracted {len(df)} events")
        return df


def main():
    """Example usage of the Szczecin Theatre extractor."""
    extractor = BiletynaSzczecinExtractor(
        save_csv_locally=False, csv_incremental_mode=False
    )
    result = extractor.run()
    logger.info(f"Extraction result: {result}")
    if result.get("status") != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
