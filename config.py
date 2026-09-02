"""Configuration for the merged Poland-cities (biletyna.pl) scraper.

Originally scraped Szczecin only; now covers every city listing below in
a single run. Each URL is the same biletyna.pl listing page shape
(``/spektakl/<City>?city_id=<id>``), just filtered to a different city —
add another city here to bring it into the merged run.
"""

BASE_URL = "https://biletyna.pl"
LISTING_URLS = [
    "https://biletyna.pl/spektakl/Szczecin?city_id=106",
    "https://biletyna.pl/spektakl/Wroclaw?city_id=46",
    "https://biletyna.pl/spektakl/Bydgoszcz?city_id=2",
    "https://biletyna.pl/spektakl/Olsztyn?city_id=23",
    "https://biletyna.pl/spektakl/Bialystok?city_id=78",
]

# Last-resort city label — only used if a listing URL's city segment can't
# be parsed out of its path (should never happen; see _city_from_listing_url).
DEFAULT_CITY = "Szczecin"
DEFAULT_COUNTRY = "Poland"
DEFAULT_CURRENCY = "PLN"

# biletyna.pl serves its cookie banner through Cookiebot. The "allow all"
# button keeps this element id regardless of the page's display language.
COOKIE_ACCEPT_XPATH = "//*[@id='CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll']"

# The listing page lazy-loads additional performances as the visitor scrolls
# (the page ships a `data-infinity_scroll` attribute). Keep scrolling until
# the number of collected performances stops growing for this many
# consecutive rounds, capped by MAX_SCROLL_ATTEMPTS as a hard ceiling.
MAX_SCROLL_ATTEMPTS = 8
SCROLL_STABLE_ROUNDS = 2

REQUEST_DELAY = 2
