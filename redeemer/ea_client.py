"""EA client — visible-browser sign-in, owned-library sync.

EA publishes no API for "which games do I own", and the EA app pins its
certificates, but the app's library comes from a GraphQL service that also
answers web tokens. So:

  - Sign-in opens a VISIBLE browser window on EA's own sign-in page. You
    sign in there (password and any captcha never touch this app); the
    session cookies are saved to .eacookies.
  - A short-lived access token comes from EA's web auth endpoint
    (client ORIGIN_JS_SDK, prompt=none), which answers a signed-in cookie
    jar with JSON instead of a login page. No browser is needed for that
    after the first sign-in.
  - The library comes from service-aggregation-layer.juno.ea.com in two
    GraphQL steps: the account's entitlements (paged), then the catalog
    offers behind them for their names.

The endpoint and field names were learned from the open-source Maxima
launcher (github.com/ArmchairDevelopers/Maxima); none of its code is used.

Run `python -m redeemer.ea_client` to sign in and check the whole path
against your account without touching the database.
"""
import json
import threading
import time

import requests
from selenium import webdriver

from .cookie_store import load_cookie_list, save_cookie_list
from .driverutil import chrome_service

EA_AUTH = "https://accounts.ea.com/connect/auth"
EA_GRAPHQL = "https://service-aggregation-layer.juno.ea.com/graphql"

# Web auth clients that hand a signed-in cookie jar a token with
# prompt=none, tried in order. Without prompt=none they redirect a
# signed-out browser to signin.ea.com and back.
#   ORIGIN_JS_SDK       answers with a JSON body (or login_required JSON)
#   DX_DROP_JS_WEB_APP  www.ea.com's own client; answers with a redirect to
#                       its auth page carrying the token in the fragment
TOKEN_CLIENTS = [
    ("ORIGIN_JS_SDK", "nucleus:rest"),
    ("DX_DROP_JS_WEB_APP", "https://www.ea.com/static-pages/auth.html"),
]


def auth_url(client_id, redirect_uri, silent=True):
    from urllib.parse import urlencode
    params = {"client_id": client_id, "response_type": "token",
              "redirect_uri": redirect_uri}
    if silent:
        params["prompt"] = "none"
    return f"{EA_AUTH}?{urlencode(params)}"


# Sign-in starts the way www.ea.com's own "Sign in" does, since that's the
# flow EA's sign-in page is used to seeing from real browsers.
EA_SIGNIN_URL = auth_url(*TOKEN_CLIENTS[1], silent=False)

COOKIE_FILE = ".eacookies"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

# The service rejects requests without a browser-style User-Agent by never
# answering, so every request carries one and a timeout.
TIMEOUT = 30

# Entitlements from every storefront count: games bought on Steam or Epic
# and linked to the EA account show up too.
STOREFRONTS = ["EA", "STEAM", "EPIC"]
PAGE_SIZE = 200
OFFER_BATCH = 50

# The EA app's own library query (me.ownedGameProducts) answers web tokens
# with an empty list, even for an account that owns games. The older
# entitlements list behind it does answer them, so the library is built
# from that: entitlements say what the account holds, catalog offers say
# what each one is called.
ENTITLEMENTS_QUERY = """
query entitlements($limit: Int, $next: String,
                   $storefronts: [UserLegacyEntitlementStorefront!]!) {
  me {
    legacyEntitlements(paging: {limit: $limit, next: $next},
                       storefronts: $storefronts, status: [ACTIVE, DISABLED]) {
      next
      items {
        productId
        entitlementTag
        entitlementType
        groupName
        status
        grantDate
        isSubscription
        isSubscriptionExpired
      }
    }
  }
}
"""

OFFERS_QUERY = """
query offers($offerIds: [String!]!, $locale: Locale) {
  legacyOffers(offerIds: $offerIds, locale: $locale) {
    id
    displayName
    displayType
    primaryMasterTitleId
  }
}
"""


class EaAuthRejected(RuntimeError):
    """EA's library service refused the token (HTTP 401/UNAUTHENTICATED)."""


def make_session(cookies):
    """A requests.Session carrying browser cookies, each on its own domain —
    EA's sign-in cookies live on accounts.ea.com, signin.ea.com and .ea.com."""
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    for c in cookies:
        try:
            s.cookies.set(c["name"], c["value"],
                          domain=c.get("domain", ".ea.com"),
                          path=c.get("path", "/"))
        except Exception:
            pass
    return s


def parse_token_response(status_code, text, location=""):
    """('ok', token) | ('signed_out', None) | ('offline', None) from the
    prompt=none auth reply. Only a token or EA's own login_required answer
    counts as proof either way; anything else proves nothing.

    EA answers with a JSON body; a redirect to nucleus:rest carrying the
    token in its fragment or query is accepted too, since that's the other
    shape an OAuth implicit-grant reply can take."""
    if location and 300 <= status_code < 400:
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(location)
        for part in (parsed.fragment, parsed.query):
            q = parse_qs(part)
            if q.get("access_token", [""])[0]:
                return "ok", q["access_token"][0]
            if q.get("error", [""])[0] == "login_required":
                return "signed_out", None
        return "offline", None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return "offline", None
    if not isinstance(data, dict):
        return "offline", None
    token = data.get("access_token")
    if status_code == 200 and isinstance(token, str) and token:
        return "ok", token
    if data.get("error") == "login_required" or data.get("error_code") == "login_required":
        return "signed_out", None
    return "offline", None


def _raise_for_errors(payload):
    """Raise EaAuthRejected for an auth error, RuntimeError for any other."""
    errors = payload.get("errors") or []
    if not errors:
        return
    if any((e.get("extensions") or {}).get("code") == "UNAUTHENTICATED"
           for e in errors):
        raise EaAuthRejected("EA's library service rejected the token.")
    raise RuntimeError("EA library query failed: "
                       + "; ".join(str(e.get("message")) for e in errors)[:300])


def parse_entitlements_page(payload):
    """(entitlements, next_cursor) from one legacyEntitlements reply."""
    _raise_for_errors(payload)
    page = (((payload.get("data") or {}).get("me") or {})
            .get("legacyEntitlements") or {})
    out = []
    for it in page.get("items") or []:
        if not it.get("productId"):
            continue
        out.append({
            "product_id": it["productId"],
            "tag": it.get("entitlementTag") or "",
            "type": it.get("entitlementType") or "",
            "group": it.get("groupName") or "",
            "status": it.get("status") or "",
            "granted_at": it.get("grantDate") or "",
            "subscription": bool(it.get("isSubscription")),
            "subscription_expired": bool(it.get("isSubscriptionExpired")),
        })
    return out, page.get("next")


def parse_offers(payload):
    """{offer_id: {name, display_type, master_title_id}} from a
    legacyOffers reply. Offers the catalog doesn't name are left out."""
    _raise_for_errors(payload)
    out = {}
    for o in (payload.get("data") or {}).get("legacyOffers") or []:
        if o and o.get("id") and o.get("displayName"):
            out[o["id"]] = {"name": o["displayName"],
                            "display_type": o.get("displayType") or "",
                            "master_title_id": o.get("primaryMasterTitleId") or ""}
    return out


def build_library(entitlements, offers):
    """One entry per named product. `owned` means the entitlement is
    active and not a lapsed subscription grant (EA Play titles leave when
    the subscription does). Entitlements with no catalog name, which are
    mostly internal access flags, are dropped."""
    library = {}
    for e in entitlements:
        offer = offers.get(e["product_id"])
        if offer is None:
            continue
        owned = (e["status"] == "ACTIVE"
                 and not (e["subscription"] and e["subscription_expired"]))
        entry = library.get(e["product_id"])
        if entry is not None and (entry["owned"] or not owned):
            continue  # keep the first active grant of a product
        library[e["product_id"]] = {
            "offer_id": e["product_id"],
            "name": offer["name"],
            "display_type": offer["display_type"],
            "master_title_id": offer["master_title_id"],
            "status": e["status"],
            "granted_at": e["granted_at"],
            "subscription": e["subscription"],
            "owned": owned,
        }
    return sorted(library.values(), key=lambda g: g["name"].lower())


def _all_cookies(driver):
    """Every cookie the browser holds. get_cookies() only returns the
    current page's domain, which would miss the accounts.ea.com session
    when the window ends on www.ea.com (or vice versa)."""
    try:
        return driver.execute_cdp_cmd("Network.getAllCookies", {})["cookies"]
    except Exception:
        return driver.get_cookies()


def _signin_driver():
    """A visible Chrome that doesn't announce it's automated. EA's sign-in
    page refuses a stock Selenium Chrome ("Your request cannot be
    completed. Service limitations apply."): it shows the "controlled by
    automated software" switch and sets navigator.webdriver. Chrome's own
    User-Agent is kept, since a spoofed one that disagrees with the
    browser's other signals is its own giveaway."""
    opts = webdriver.ChromeOptions()
    opts.add_argument("--window-size=1100,850")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    opts.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=opts, service=chrome_service())
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"})
    except Exception:
        pass  # not Chrome, or an old driver — the flags above still apply
    return driver


def _quit_driver(driver):
    proc = getattr(getattr(driver, "service", None), "process", None)
    try:
        driver.quit()
    except Exception:
        pass
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass


class EaClient:
    def __init__(self):
        self._lock = threading.RLock()
        self._session = None
        self._token = None
        self._token_expires = 0.0
        self.login_state = {"status": "idle", "message": ""}
        # What the last check against EA proved: 'ok' (signed in),
        # 'offline' (no answer) or 'signed_out'. Drives the header light.
        self.conn_state = "signed_out"
        self.conn_checked = 0.0
        # The last raw token-check reply per client, for diagnosing a
        # sign-in that never completes: {client: {status, location, body}}
        self.last_check = {}
        # which TOKEN_CLIENTS entry the held token came from
        self.token_client = ""

    # ---------------- session ----------------

    def _record(self, state):
        self.conn_state = state
        self.conn_checked = time.time()
        return state

    def connection(self):
        return {"state": self.conn_state, "checked": self.conn_checked}

    def _fetch_client_token(self, session, client_id, redirect_uri):
        """One client's prompt=none answer: ('ok', token) | ('signed_out',
        None) | ('offline', None)."""
        try:
            r = session.get(auth_url(client_id, redirect_uri),
                            timeout=TIMEOUT, allow_redirects=False)
        except requests.RequestException as e:
            self.last_check[client_id] = {"error": f"{e.__class__.__name__}: {e}"[:300]}
            return "offline", None
        location = r.headers.get("Location", "")
        self.last_check[client_id] = {"status": r.status_code,
                                      "location": location[:300],
                                      "body": (r.text or "")[:300]}
        return parse_token_response(r.status_code, r.text, location)

    def _fetch_token(self, session, skip=()):
        """The first token any client hands over, as (state, token); sets
        token_client. Signed out only when every client says so — one
        client answering oddly is no proof the session is gone."""
        states = []
        for client_id, redirect_uri in TOKEN_CLIENTS:
            if client_id in skip:
                continue
            state, token = self._fetch_client_token(session, client_id, redirect_uri)
            if state == "ok":
                self.token_client = client_id
                return state, token
            states.append(state)
        if states and all(s == "signed_out" for s in states):
            return "signed_out", None
        return "offline", None

    def is_logged_in(self):
        return self._session is not None and self.conn_state == "ok"

    def try_cookie_login(self):
        with self._lock:
            cookies = load_cookie_list(COOKIE_FILE)
            if not cookies:
                if self._session is None:
                    self._record("signed_out")
                return False
            session = make_session(cookies)
            state, token = self._fetch_token(session)
            self._record(state)
            if state == "signed_out":
                self._session = None
                return False
            # offline: keep the saved session so a later check can confirm it
            self._session = session
            if token:
                self._token, self._token_expires = token, time.time() + 50 * 60
            return state == "ok"

    def refresh_connection(self):
        if self._session is not None or self.conn_state == "offline":
            self.try_cookie_login()

    def _access_token(self):
        """A live token, refreshed from the saved cookies when it's near
        expiry (EA's last about an hour)."""
        with self._lock:
            if self._token and time.time() < self._token_expires:
                return self._token
            if self._session is None and not self.try_cookie_login():
                raise RuntimeError("Not signed in to EA — use the EA chip to sign in.")
            state, token = self._fetch_token(self._session)
            self._record(state)
            if state == "signed_out":
                self._session = None
                raise RuntimeError("Your EA sign-in expired — sign in again.")
            if not token:
                raise RuntimeError("Can't reach EA right now. Try again shortly.")
            self._token, self._token_expires = token, time.time() + 50 * 60
            return token

    def account_name(self):
        """The signed-in EA ID (display name), or '' if EA won't say."""
        try:
            payload = self._gql(self._access_token(), "me",
                                "query me { me { player { displayName } } }", {})
            return ((((payload.get("data") or {}).get("me") or {})
                     .get("player") or {}).get("displayName") or "")
        except Exception:
            return ""

    # ---------------- interactive sign-in ----------------

    def start_interactive_login(self):
        """Open a visible browser window for the user to sign in to EA.
        Runs in a background thread; poll login_state / is_logged_in()."""
        with self._lock:
            if self.login_state["status"] == "waiting":
                return False, "An EA sign-in window is already open."
            self.login_state = {"status": "waiting",
                                "message": "Browser window opened — sign in to EA there."}
        threading.Thread(target=self.interactive_login, daemon=True).start()
        return True, "opened"

    def interactive_login(self, timeout=600, discover=None):
        """Blocking sign-in in a visible window. Done once EA hands the
        browser's cookies a token. `discover`, if given, is called with the
        signed-in driver before it closes (the probe uses it)."""
        driver = None
        try:
            driver = _signin_driver()
            driver.get(EA_SIGNIN_URL)
            deadline = time.time() + timeout
            session = None
            closed = False
            last_jar = None
            while time.time() < deadline:
                time.sleep(3)
                try:
                    cookies = _all_cookies(driver)
                    self.last_check["url"] = driver.current_url[:300]
                except Exception:
                    closed = True
                    break
                # Only ask EA when the cookies changed: polling the auth
                # endpoint every few seconds for ten minutes would risk a
                # rate limit, and nothing changes while you type a password.
                jar = frozenset((c.get("domain"), c.get("name"), c.get("value"))
                                for c in cookies)
                if jar == last_jar:
                    continue
                last_jar = jar
                candidate = make_session(cookies)
                state, token = self._fetch_token(candidate)
                if state == "ok":
                    session = candidate
                    with self._lock:
                        self._session = session
                        self._token = token
                        self._token_expires = time.time() + 50 * 60
                        self._record("ok")
                    try:
                        save_cookie_list(COOKIE_FILE, cookies)
                    except Exception:
                        pass
                    break
            if session is None:
                self.login_state = {"status": "error",
                                    "message": "EA sign-in window was closed." if closed
                                    else "EA sign-in timed out."}
                return False
            if discover is not None:
                discover(driver)
            self.login_state = {"status": "ok", "message": "Signed in to EA."}
            return True
        except Exception as e:
            self.login_state = {"status": "error", "message": f"EA sign-in failed: {e!r}"}
            return False
        finally:
            if driver is not None:
                _quit_driver(driver)

    # ---------------- library ----------------

    def _gql(self, token, operation, query, variables):
        r = requests.post(EA_GRAPHQL, timeout=TIMEOUT,
                          json={"operationName": operation, "query": query,
                                "variables": variables},
                          headers={"User-Agent": USER_AGENT,
                                   "Authorization": f"Bearer {token}"})
        try:
            payload = r.json()
        except ValueError:
            raise RuntimeError(f"EA's library service returned non-JSON (HTTP {r.status_code}).")
        if r.status_code == 401 and not payload.get("errors"):
            raise EaAuthRejected("EA's library service rejected the token.")
        return payload

    def get_owned(self):
        """Every product on the account as build_library() dicts. If the library
        service refuses one client's token, the next client's is tried."""
        token = self._access_token()
        rejected = []
        while True:
            try:
                return self._get_owned_with(token)
            except EaAuthRejected:
                rejected.append(self.token_client)
                with self._lock:
                    self._token = None
                    state, token = (self._fetch_token(self._session, skip=rejected)
                                    if self._session is not None else ("signed_out", None))
                if state != "ok":
                    raise EaAuthRejected(
                        "EA's library service rejected the token from every web "
                        f"sign-in client tried ({', '.join(rejected)}).")
                self._token, self._token_expires = token, time.time() + 50 * 60

    def _get_owned_with(self, token):
        entitlements, cursor, seen = [], "0", set()
        for _ in range(50):  # backstop against a pager loop
            page, cursor = parse_entitlements_page(self._gql(
                token, "entitlements", ENTITLEMENTS_QUERY,
                {"limit": PAGE_SIZE, "next": cursor, "storefronts": STOREFRONTS}))
            entitlements.extend(page)
            if not page or not cursor or cursor in seen:
                break
            seen.add(cursor)
        ids = sorted({e["product_id"] for e in entitlements})
        offers = {}
        for i in range(0, len(ids), OFFER_BATCH):
            offers.update(parse_offers(self._gql(
                token, "offers", OFFERS_QUERY,
                {"offerIds": ids[i:i + OFFER_BATCH], "locale": "en"})))
        return build_library(entitlements, offers)


# ---------------- probe: python -m redeemer.ea_client ----------------

_FIND_AUTH_JS = """
return performance.getEntriesByType('resource').map(e => e.name)
  .filter(u => u.indexOf('connect/auth') >= 0 || u.indexOf('juno.ea.com') >= 0);
"""


def _discover_web_clients(driver):
    """Which auth client_ids ea.com's own pages use. Only needed if the
    ORIGIN_JS_SDK token is ever refused; printed for the developer."""
    from urllib.parse import parse_qs, urlparse
    seen = set()
    for page in ("https://www.ea.com/", "https://www.ea.com/games/library"):
        try:
            driver.get(page)
            time.sleep(6)
            for url in driver.execute_script(_FIND_AUTH_JS) or []:
                q = parse_qs(urlparse(url).query)
                label = q.get("client_id", [urlparse(url).netloc])[0]
                seen.add(label)
        except Exception as e:
            print(f"  (couldn't inspect {page}: {e.__class__.__name__})")
    print("  ea.com pages talked to: " + (", ".join(sorted(seen)) or "nothing seen"))


def _probe(argv):
    force_login = "--login" in argv
    client = EaClient()
    discovered = []

    if force_login or not client.try_cookie_login():
        print("Opening a Chrome window — sign in to EA there (10 minutes max).")
        ok = client.interactive_login(
            discover=lambda d: discovered.append(_discover_web_clients(d)))
        if not ok:
            print("Sign-in did not complete:", client.login_state["message"])
            print("Last token check (paste this into the chat):")
            for k, v in client.last_check.items():
                print(f"  {k}: {v}")
            return 1
        print(f"Signed in. Session saved to {COOKIE_FILE}.")
    else:
        print(f"Using the saved session in {COOKIE_FILE}.")

    print("Fetching an access token... ", end="")
    try:
        client._access_token()
    except RuntimeError as e:
        print("failed:", e)
        for k, v in client.last_check.items():
            print(f"  {k}: {v}")
        return 1
    print(f"ok (from {client.token_client}).")

    print("Asking EA for your library...")
    try:
        owned = client.get_owned()
    except EaAuthRejected as e:
        print(f"REJECTED: {e} Copy the "
              "'ea.com pages talked to' line above (run with --login to see it) "
              "into the chat; one of those client ids is the next thing to try.")
        return 2
    except Exception as e:
        print(f"failed: {e.__class__.__name__}: {e}")
        return 1

    name = client.account_name()
    print(f"Signed in as: {name or '(EA did not say)'} (token from {client.token_client})")
    held = [g for g in owned if g["owned"]]
    print(f"EA lists {len(owned)} products on this account, {len(held)} currently owned.")
    kinds = {}
    for g in held:
        kinds[g["display_type"] or "?"] = kinds.get(g["display_type"] or "?", 0) + 1
    print("By type: " + (", ".join(f"{k}: {v}" for k, v in sorted(kinds.items())) or "none"))
    for g in owned[:25]:
        flags = [g["display_type"] or "?"]
        if g["subscription"]:
            flags.append("subscription")
        if not g["owned"]:
            flags.append(g["status"].lower() or "not owned")
        print(f"  {g['name']}  [{', '.join(flags)}]  {g['granted_at'][:10]}")
    if len(owned) > 25:
        print(f"  ... and {len(owned) - 25} more")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_probe(sys.argv[1:]))
