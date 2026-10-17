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
  - The library is one GraphQL query against
    service-aggregation-layer.juno.ea.com, paged. Each owned product
    carries EA's ownershipMethods (e.g. REDEMPTION for a product key,
    PURCHASE, ENTITLEMENT_GRANT for subscriptions).

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
EA_WEB_CLIENT = "ORIGIN_JS_SDK"
# Without prompt=none this redirects a signed-out browser to signin.ea.com
# and back; with it, it returns JSON either way — a token or login_required.
EA_SIGNIN_URL = (f"{EA_AUTH}?client_id={EA_WEB_CLIENT}&response_type=token"
                 "&redirect_uri=nucleus:rest")
EA_TOKEN_URL = EA_SIGNIN_URL + "&prompt=none"
EA_GRAPHQL = "https://service-aggregation-layer.juno.ea.com/graphql"

COOKIE_FILE = ".eacookies"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36")

# The service rejects requests without a browser-style User-Agent by never
# answering, so every request carries one and a timeout.
TIMEOUT = 30

# MICRO_CONTENT, EXPANSION_PACK, BUNDLE_PACK and BASE_GAME fail the schema's
# validation for this field, so only these four are asked for.
PRODUCT_TYPES = ["DIGITAL_FULL_GAME", "PACKAGED_FULL_GAME",
                 "DIGITAL_EXTRA_CONTENT", "PACKAGED_EXTRA_CONTENT"]
# Games bought on Steam or Epic and linked to the EA account count as owned.
STOREFRONTS = ["EA", "STEAM", "EPIC"]
PAGE_SIZE = 500

OWNED_GAMES_QUERY = """
query ownedGames($next: String, $limit: Int, $type: [GameProductType!]!,
                 $storefronts: [UserGameProductStorefront!],
                 $platforms: [GamePlatform!]!) {
  me {
    ownedGameProducts(storefronts: $storefronts, productFound: true,
                      paging: {limit: $limit, next: $next}, type: $type,
                      platforms: $platforms) {
      next
      totalCount
      items {
        originOfferId
        product {
          name
          baseItem { title gameType }
          gameProductUser(storefronts: $storefronts) {
            ownershipMethods
            initialEntitlementDate
            status
          }
        }
      }
    }
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


def parse_owned_page(payload):
    """(items, next_cursor, total) from one GraphQL reply. Raises
    EaAuthRejected for an auth error and RuntimeError for any other error."""
    errors = payload.get("errors") or []
    if errors:
        if any((e.get("extensions") or {}).get("code") == "UNAUTHENTICATED"
               for e in errors):
            raise EaAuthRejected("EA's library service rejected the token.")
        raise RuntimeError("EA library query failed: "
                           + "; ".join(str(e.get("message")) for e in errors)[:300])
    page = (((payload.get("data") or {}).get("me") or {})
            .get("ownedGameProducts") or {})
    items = []
    for it in page.get("items") or []:
        product = it.get("product") or {}
        base = product.get("baseItem") or {}
        user = product.get("gameProductUser") or {}
        name = product.get("name") or base.get("title") or ""
        if not name:
            continue
        items.append({
            "offer_id": it.get("originOfferId") or "",
            "name": name,
            "base_title": base.get("title") or "",
            "game_type": base.get("gameType") or "",
            "ownership": [str(m) for m in user.get("ownershipMethods") or []],
            "entitled_at": user.get("initialEntitlementDate") or "",
            "status": user.get("status") or "",
        })
    return items, page.get("next"), page.get("totalCount")


def _all_cookies(driver):
    """Every cookie the browser holds. get_cookies() only returns the
    current page's domain, which would miss the accounts.ea.com session
    when the window ends on www.ea.com (or vice versa)."""
    try:
        return driver.execute_cdp_cmd("Network.getAllCookies", {})["cookies"]
    except Exception:
        return driver.get_cookies()


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
        # The last raw token-check reply, for diagnosing a sign-in that never
        # completes: {status, location, body, url}
        self.last_check = {}

    # ---------------- session ----------------

    def _record(self, state):
        self.conn_state = state
        self.conn_checked = time.time()
        return state

    def connection(self):
        return {"state": self.conn_state, "checked": self.conn_checked}

    def _fetch_token(self, session):
        """('ok', token) | ('signed_out', None) | ('offline', None)."""
        try:
            r = session.get(EA_TOKEN_URL, timeout=TIMEOUT, allow_redirects=False)
        except requests.RequestException as e:
            self.last_check = {"error": f"{e.__class__.__name__}: {e}"[:300]}
            return "offline", None
        location = r.headers.get("Location", "")
        self.last_check = {"status": r.status_code, "location": location[:300],
                           "body": (r.text or "")[:300]}
        return parse_token_response(r.status_code, r.text, location)

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
            opts = webdriver.ChromeOptions()
            opts.add_argument("--window-size=1100,850")
            opts.add_experimental_option("excludeSwitches", ["enable-logging"])
            driver = webdriver.Chrome(options=opts, service=chrome_service())
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

    def _query(self, token, cursor):
        body = {
            "operationName": "ownedGames",
            "query": OWNED_GAMES_QUERY,
            "variables": {"next": cursor, "limit": PAGE_SIZE,
                          "type": PRODUCT_TYPES, "storefronts": STOREFRONTS,
                          "platforms": ["PC"]},
        }
        r = requests.post(EA_GRAPHQL, json=body, timeout=TIMEOUT,
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
        """Every owned product as parse_owned_page() dicts."""
        token = self._access_token()
        owned, cursor, seen = [], "0", set()
        for _ in range(50):  # backstop against a pager loop
            items, cursor, _total = parse_owned_page(self._query(token, cursor))
            owned.extend(items)
            if not items or not cursor or cursor in seen:
                break
            seen.add(cursor)
        return owned


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
    print("ok.")

    print("Asking EA for your library...")
    try:
        owned = client.get_owned()
    except EaAuthRejected:
        print("REJECTED: EA's library service refused the web token. Copy the "
              "'ea.com pages talked to' line above (run with --login to see it) "
              "into the chat; one of those client ids is the next thing to try.")
        return 2
    except Exception as e:
        print(f"failed: {e.__class__.__name__}: {e}")
        return 1

    print(f"EA says you own {len(owned)} products.")
    methods = {}
    for g in owned:
        for m in g["ownership"] or ["(none listed)"]:
            methods[m] = methods.get(m, 0) + 1
    print("Ownership methods: " + ", ".join(f"{k}: {v}" for k, v in sorted(methods.items())))
    for g in owned[:15]:
        print(f"  {g['name']}  [{', '.join(g['ownership']) or '-'}]  {g['entitled_at'][:10]}")
    if len(owned) > 15:
        print(f"  ... and {len(owned) - 15} more")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_probe(sys.argv[1:]))
