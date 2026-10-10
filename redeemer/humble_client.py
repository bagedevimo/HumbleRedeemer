"""Humble Bundle client — a headless browser session managed for the web app.

Login is driven by the GUI (email/password posted to the API, then guard/2FA
codes the same way) instead of console prompts. Cookies persist to the same
.humblecookies file the original script uses, so existing sessions carry over.
"""
import json
import threading
import time
from base64 import b64encode

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException

from .cookie_store import load_cookie_list, save_cookie_list

HUMBLE_ORIGIN = "https://www.humblebundle.com/"
HUMBLE_LOGIN_PAGE = "https://www.humblebundle.com/login"
HUMBLE_KEYS_PAGE = "https://www.humblebundle.com/home/library"
HUMBLE_LOGIN_API = "https://www.humblebundle.com/processlogin"
HUMBLE_REDEEM_API = "https://www.humblebundle.com/humbler/redeemkey"
HUMBLE_SUB_PAGE = "https://www.humblebundle.com/subscription/"
HUMBLE_CHOOSE_CONTENT = "https://www.humblebundle.com/humbler/choosecontent"

COOKIE_FILE = ".humblecookies"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36")

GET_ORDERS_JS = '''
var done = arguments[arguments.length - 1];
var onlyKeys = %gamekeys%;
var getHumbleOrderDetails = async () => {
  try {
    var orders;
    if (onlyKeys && onlyKeys.length) {
      orders = onlyKeys.map(k => ({ gamekey: k }));
    } else {
      const response = await fetch('https://www.humblebundle.com/api/v1/user/order');
      orders = await response.json();
    }
    const orderDetailsPromises = orders.map(async (order) => {
      const url = `https://www.humblebundle.com/api/v1/order/${order['gamekey']}?all_tpkds=true`;
      const r = await fetch(url);
      return await r.json();
    });
    return await Promise.all(orderDetailsPromises);
  } catch (error) {
    console.error('Error:', error);
    return [];
  }
};
getHumbleOrderDetails().then(r => {done(r)});
'''

FETCH_POST_JS = '''
var done = arguments[arguments.length - 1];
var formData = new FormData();
const jsonData = JSON.parse(atob('{formData}'));
for (const key in jsonData) {{
    formData.append(key, jsonData[key])
}}
fetch("{url}", {{
  "headers": {{ "csrf-prevention-token": "{csrf}" }},
  "body": formData,
  "method": "POST",
}}).then(r => {{ r.json().then( v => {{done([r.status, v])}} ) }} );
'''

# Proof of a live session: the orders API answers a signed-in session with a
# JSON list and a signed-out one with 401. Anything else (network error,
# Cloudflare page, 5xx) proves nothing either way. Self-aborts so a dead
# network can't hold the browser for the 300s script timeout.
CHECK_SESSION_JS = '''
var done = arguments[arguments.length-1];
var ctl = new AbortController();
setTimeout(() => ctl.abort(), 20000);
fetch("https://www.humblebundle.com/api/v1/user/order",
      {credentials: "same-origin", signal: ctl.signal})
  .then(r => {
    if (r.status === 401 || (r.redirected && r.url.indexOf("/login") >= 0)) {
      done("signed_out"); return;
    }
    if (!r.ok) { done("offline"); return; }
    return r.json().then(j => done(Array.isArray(j) ? "ok" : "offline"),
                         () => done("offline"));
  })
  .catch(() => done("offline"));
'''


def _network_error(err):
    """chromedriver reports a navigation that couldn't reach the site
    (offline, DNS failure) as a WebDriverException carrying a net::ERR_ code.
    That is a dead connection, not a dead browser."""
    return "net::ERR_" in str(err)


def find_dict_keys(node, kv, parent=False):
    if isinstance(node, list):
        for i in node:
            yield from find_dict_keys(i, kv, parent)
    elif isinstance(node, dict):
        if kv in node:
            yield node if parent else node[kv]
        for j in node.values():
            yield from find_dict_keys(j, kv, parent)


class HumbleClient:
    def __init__(self):
        self._driver = None
        self._lock = threading.RLock()
        self._logged_in = False
        self._pending_payload = None  # login payload awaiting guard/2FA
        # What the last check against Humble proved: 'ok' (signed in),
        # 'offline' (no answer) or 'signed_out'. Drives the header light.
        self.conn_state = "signed_out"
        self.conn_checked = 0.0

    # ---------------- driver ----------------

    def _ensure_driver(self):
        if self._driver is not None:
            return self._driver
        from .driverutil import chrome_service, firefox_service
        drivers = [
            (webdriver.Chrome, webdriver.ChromeOptions, chrome_service),
            (webdriver.Firefox, webdriver.FirefoxOptions, firefox_service),
        ]
        errors = []
        for d, opt, service in drivers:
            try:
                options = opt()
                if d == webdriver.Chrome:
                    options.add_argument("--headless=new")
                    options.add_argument(f"user-agent={USER_AGENT}")
                    # keep Chrome's internal chatter (GCM registration etc.)
                    # out of the app's console
                    options.add_experimental_option("excludeSwitches", ["enable-logging"])
                else:
                    options.add_argument("-headless")
                    options.set_preference("general.useragent.override", USER_AGENT)
                self._driver = d(options=options, service=service())
                self._driver.set_script_timeout(300)
                return self._driver
            except WebDriverException as e:
                errors.append(str(e).splitlines()[0] if str(e) else repr(e))
        raise RuntimeError(
            "Could not start a headless browser (needs Chrome or Firefox installed). "
            + " | ".join(errors)
        )

    def shutdown(self):
        with self._lock:
            if self._driver is not None:
                # Grab the service process first: if quit() fails or hangs,
                # a surviving chromedriver inherits the app's server socket
                # and holds the port after we exit.
                proc = getattr(getattr(self._driver, "service", None),
                               "process", None)
                try:
                    self._driver.quit()
                except Exception:
                    pass
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self._driver = None
                self._logged_in = False

    def _driver_crashed(self, err):
        """The headless browser died mid-call (Chrome update, OOM, killed
        process). Drop it so the next call starts a fresh one and restores
        the session from cookies, instead of failing forever until restart."""
        self.shutdown()
        self._record("offline")  # lets the session watcher restore it
        first = str(err).splitlines()[0] if str(err) else err.__class__.__name__
        return RuntimeError(
            "The Humble browser session crashed and was reset — try the same "
            f"action again; your sign-in restores from saved cookies. ({first})")

    def _post(self, url, payload):
        driver = self._ensure_driver()
        json_payload = b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        try:
            csrf = driver.get_cookie("csrf_cookie")
            csrf = csrf["value"] if csrf else ""
            script = FETCH_POST_JS.format(formData=json_payload, url=url, csrf=csrf)
            return driver.execute_async_script(script)
        except WebDriverException as e:
            raise self._driver_crashed(e) from e

    # ---------------- session ----------------

    def _record(self, state):
        self.conn_state = state
        self.conn_checked = time.time()
        return state

    def connection(self):
        return {"state": self.conn_state, "checked": self.conn_checked}

    def _check_session(self, driver):
        """Ask Humble whether the browser's session is signed in: 'ok',
        'signed_out' or 'offline'. Caller holds the lock."""
        try:
            if not driver.current_url.startswith(HUMBLE_ORIGIN):
                # A load that failed while offline leaves Chrome on its error
                # page. The check's fetch must run from Humble's own origin to
                # carry the session cookies, or it reads as signed out.
                driver.get(HUMBLE_KEYS_PAGE)
                if not driver.current_url.startswith(HUMBLE_ORIGIN):
                    return "offline"
            return driver.execute_async_script(CHECK_SESSION_JS) or "offline"
        except TimeoutException:
            return "offline"
        except WebDriverException as e:
            if _network_error(e):
                return "offline"
            raise self._driver_crashed(e) from e

    def is_logged_in(self, recheck=False):
        """Without recheck: whether a session is held (it may be unverified
        while Humble is unreachable). With recheck: whether Humble confirms
        it right now; a session Humble rejects is dropped."""
        # Non-blocking: if another thread holds the driver (login, sync,
        # reveal), report the last known state instead of stalling the UI.
        if not self._lock.acquire(blocking=False):
            return self._logged_in and (not recheck or self.conn_state == "ok")
        try:
            if not recheck or not self._logged_in or self._driver is None:
                return self._logged_in
            state = self._record(self._check_session(self._driver))
            if state == "signed_out":
                self._logged_in = False
            return state == "ok"
        finally:
            self._lock.release()

    def try_cookie_login(self):
        """Attempt to restore a previous session from .humblecookies."""
        with self._lock:
            cookies = load_cookie_list(COOKIE_FILE)
            if not cookies:
                if not self._logged_in:
                    self._record("signed_out")
                return False
            driver = self._ensure_driver()
            try:
                driver.get(HUMBLE_LOGIN_PAGE)
                # cookies only attach to the page's own domain: if Humble
                # didn't load (offline), don't claim a session; the watcher
                # retries the restore once Humble answers
                if not driver.current_url.startswith(HUMBLE_ORIGIN):
                    self._record("offline")
                    return False
                for cookie in cookies:
                    try:
                        driver.add_cookie(cookie)
                    except Exception:
                        pass
                driver.get(HUMBLE_KEYS_PAGE)
            except TimeoutException:
                self._record("offline")
                return False
            except WebDriverException as e:
                if _network_error(e):
                    self._record("offline")
                    return False
                raise self._driver_crashed(e) from e
            state = self._record(self._check_session(driver))
            self._logged_in = state != "signed_out"
            return state == "ok"

    def refresh_connection(self):
        """Periodic check behind the header light: re-verify a held session,
        or retry the saved cookies if Humble was unreachable before. Skips
        while a job holds the browser; the job is exercising the session."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._logged_in:
                self.is_logged_in(recheck=True)
            elif self.conn_state == "offline":
                self.try_cookie_login()
        finally:
            self._lock.release()

    def login(self, email=None, password=None, code=None, guard=None):
        """Drive the Humble login flow. Returns a dict:
        {status: 'ok' | 'needs_guard' | 'needs_2fa' | 'error', message: str}
        Call first with email+password; if needs_guard/needs_2fa, call again
        with guard= or code= (email/password not required on the retry).
        """
        with self._lock:
            driver = self._ensure_driver()
            if email and password:
                driver.get(HUMBLE_LOGIN_PAGE)
                self._pending_payload = {
                    "access_token": "",
                    "access_token_provider_id": "",
                    "goto": "/",
                    "qs": "",
                    "username": email,
                    "password": password,
                }
            if self._pending_payload is None:
                return {"status": "error", "message": "Enter email and password first."}
            payload = self._pending_payload
            if guard:
                payload["guard"] = guard.strip().upper()
            if code:
                payload["code"] = code.strip()

            status, login_json = self._post(HUMBLE_LOGIN_API, payload)
            if status not in (200, 401):
                return {"status": "error",
                        "message": f"Humble responded with HTTP {status}."}

            if isinstance(login_json, dict):
                if "errors" in login_json and "username" in login_json["errors"]:
                    return {"status": "error",
                            "message": login_json["errors"]["username"][0]}
                if login_json.get("user_terms_opt_in_data", {}).get("needs_to_opt_in"):
                    return {"status": "error",
                            "message": "Humble updated its TOS — sign in once in a "
                                       "normal browser to accept it, then retry."}
                if status != 200 and "humble_guard_required" in login_json:
                    return {"status": "needs_guard",
                            "message": "Humble emailed you a security code — enter it below."}
                if status != 200 and "two_factor_required" in login_json:
                    return {"status": "needs_2fa",
                            "message": "Enter your two-factor authentication code."}
                if status != 200 and "errors" in login_json:
                    return {"status": "error", "message": str(login_json["errors"])}

            # Looks like success; make Humble confirm the session before the
            # light goes green (an unrecognized reply would otherwise land here)
            state = self._record(self._check_session(driver))
            if state == "signed_out":
                self._logged_in = False
                return {"status": "error",
                        "message": "Humble didn't accept the sign-in. Try again."}
            self._pending_payload = None
            self._logged_in = True
            try:
                save_cookie_list(COOKIE_FILE, driver.get_cookies())
            except Exception:
                pass
            return {"status": "ok", "message": "Signed in to Humble."}

    # ---------------- data ----------------

    def fetch_orders(self, gamekeys=None):
        """Fetch orders with full tpk details. With gamekeys, fetch only those
        orders (used to refresh months after claiming Choice games)."""
        script = GET_ORDERS_JS.replace("%gamekeys%", json.dumps(gamekeys or []))
        with self._lock:
            driver = self._ensure_driver()
            try:
                return driver.execute_async_script(script)
            except WebDriverException as e:
                raise self._driver_crashed(e) from e

    def get_requests_session(self):
        """A requests.Session carrying the browser's Humble cookies — used for
        plain page fetches (Choice month pages) without going through JS."""
        import requests
        with self._lock:
            driver = self._ensure_driver()
            cookies = driver.get_cookies()
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        for c in cookies:
            session.cookies.set(c["name"], c["value"],
                                domain=c["domain"].replace("www.", ""),
                                path=c.get("path", "/"))
        return session

    def get_month_data(self, requests_session, choice_url):
        """Fetch a Choice month's page and pull out its content-choice JSON."""
        r = requests_session.get(HUMBLE_SUB_PAGE + choice_url, timeout=60)
        marker = '<script id="webpack-monthly-product-data" type="application/json">'
        if marker not in r.text:
            raise RuntimeError(f"Could not read Choice data for {choice_url} "
                               f"(HTTP {r.status_code})")
        raw = r.text.split(marker)[1].split("</script>")[0].strip()
        return json.loads(raw)["contentChoiceOptions"]

    def get_choice_months(self, order_details, progress=None):
        """Yield every Choice/Monthly order that still has claimable games.

        Each yielded month dict gains: choice_data, available_choices,
        parent_identifier, uses_choices.
        """
        months = [m for m in order_details if "choice_url" in m.get("product", {})]
        months.sort(key=lambda m: m.get("created", ""))
        session = self.get_requests_session()

        for i, month in enumerate(months):
            if progress:
                progress(i + 1, len(months), month["product"].get("human_name", ""))
            if not (month.get("choices_remaining", 0) > 0
                    or month["product"].get("is_subs_v3_product", False)):
                continue
            chosen_games = set(find_dict_keys(month.get("tpkd_dict", {}), "machine_name"))
            month["choice_data"] = self.get_month_data(
                session, month["product"]["choice_url"])
            if not month["choice_data"].get("canRedeemGames", True):
                continue

            v3 = not month["choice_data"].get("usesChoices", True)
            ccd = month["choice_data"]["contentChoiceData"]
            if v3:
                identifier = "initial"
                choice_options = ccd["game_data"]
            else:
                identifier = "initial" if "initial" in ccd else "initial-classic"
                if identifier not in ccd:
                    for key in ccd:
                        if isinstance(ccd[key], dict) and "content_choices" in ccd[key]:
                            identifier = key
                choice_options = ccd[identifier]["content_choices"]

            month["available_choices"] = [
                game for _, game in choice_options.items()
                if set(find_dict_keys(game, "machine_name")).isdisjoint(chosen_games)
            ]
            month["parent_identifier"] = identifier
            month["uses_choices"] = not v3
            if month["available_choices"]:
                yield month

    def choose_content(self, order_gamekey, parent_identifier, display_machine_name):
        """Claim one Choice game. Returns (ok, message)."""
        payload = {
            "gamekey": order_gamekey,
            "parent_identifier": parent_identifier,
            "chosen_identifiers[]": display_machine_name,
            "is_multikey_and_from_choice_modal": "false",
        }
        with self._lock:
            status, res = self._post(HUMBLE_CHOOSE_CONTENT, payload)
        if status == 200 and isinstance(res, dict) and res.get("success"):
            return True, "ok"
        msg = res.get("error_msg", str(res)) if isinstance(res, dict) else f"HTTP {status}"
        return False, str(msg)

    def reveal_key(self, gamekey, machine_name, keyindex, gift=False):
        """Reveal ('redeem' in Humble terms) a key so it exposes its value.
        With gift=True, Humble creates a one-time gift link instead of
        exposing the raw key (the same thing its "Reveal as gift" button does).
        Returns (ok, key_or_link_or_error_message).
        """
        payload = {"keytype": machine_name, "key": gamekey, "keyindex": keyindex}
        if gift:
            payload["gift"] = "true"
        with self._lock:
            status, resp = self._post(HUMBLE_REDEEM_API, payload)
        if status != 200 or not isinstance(resp, dict) or "error_msg" in resp or not resp.get("success"):
            msg = resp.get("error_msg", f"HTTP {status}") if isinstance(resp, dict) else f"HTTP {status}"
            return False, msg
        # Gift reveals return a giftkey token; normal reveals return the key
        giftkey = resp.get("giftkey", "")
        if giftkey:
            return True, f"https://www.humblebundle.com/gift?key={giftkey}"
        key = resp.get("key", "")
        if isinstance(key, dict):
            # Some entries return a nested object (e.g. gift links)
            key = key.get("key", "") or json.dumps(key)
        return True, str(key)
