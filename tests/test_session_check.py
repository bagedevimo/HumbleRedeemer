"""Unit tests for the session checks behind the Humble/Steam header lights.

A green light must mean the service itself confirmed the session. These pin
the classification: proof of a signed-in page is 'ok', proof of a signed-out
page is 'signed_out', and anything that proves nothing (network error,
maintenance 503, a bare 200) is 'offline', which keeps the saved session
instead of throwing it away. Fake sessions and drivers only, no network.
"""
import requests
from selenium.common.exceptions import TimeoutException, WebDriverException

from redeemer import humble_client, steam_client
from redeemer.humble_client import HumbleClient
from redeemer.steam_client import SteamClient

SIGNED_IN_PAGE = ('<div id="application_config" data-userinfo="{&quot;logged_in'
                  '&quot;:true,&quot;country_code&quot;:&quot;US&quot;}"></div>'
                  '<script>g_AccountID = 123456;</script>')
SIGNED_OUT_PAGE = ('<div id="application_config" data-userinfo="{&quot;logged_in'
                   '&quot;:false,&quot;country_code&quot;:&quot;US&quot;}"></div>'
                   '<script>g_AccountID = 0;</script>')


class FakeResponse:
    def __init__(self, status, text="", url=steam_client.STEAM_KEYS_PAGE):
        self.status_code = status
        self.text = text
        self.url = url


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.gets = 0

    def get(self, url, **kwargs):
        self.gets += 1
        if self.error:
            raise self.error
        return self.response


# ---------------- Steam ----------------

def test_steam_signed_in_page_is_ok():
    s = FakeSession(FakeResponse(200, SIGNED_IN_PAGE))
    assert SteamClient()._check_session(s) == "ok"


def test_steam_account_id_alone_is_ok():
    s = FakeSession(FakeResponse(200, "<script>g_AccountID = 98765;</script>"))
    assert SteamClient()._check_session(s) == "ok"


def test_steam_redirect_to_login_is_signed_out():
    login = "https://store.steampowered.com/login/?redir=account%2Fregisterkey"
    s = FakeSession(FakeResponse(200, SIGNED_OUT_PAGE, url=login))
    assert SteamClient()._check_session(s) == "signed_out"


def test_steam_logged_out_marker_is_signed_out():
    s = FakeSession(FakeResponse(200, SIGNED_OUT_PAGE))
    assert SteamClient()._check_session(s) == "signed_out"


def test_steam_maintenance_is_offline_not_green():
    s = FakeSession(FakeResponse(503, "Steam is down for maintenance"))
    assert SteamClient()._check_session(s) == "offline"


def test_steam_bare_200_elsewhere_proves_nothing():
    s = FakeSession(FakeResponse(200, "<html>captive portal</html>",
                                 url="http://portal.example/wifi"))
    assert SteamClient()._check_session(s) == "offline"


def test_steam_key_page_loading_is_proof_even_without_markers():
    # signed out, Steam redirects away from the key page, so landing on it
    # with a 200 means the session is good even if the header markers move
    s = FakeSession(FakeResponse(200, "<html>Activate a Product on Steam</html>"))
    assert SteamClient()._check_session(s) == "ok"


def test_steam_signed_out_evidence_wins_over_the_key_page_url():
    s = FakeSession(FakeResponse(200, SIGNED_OUT_PAGE))
    assert SteamClient()._check_session(s) == "signed_out"


def test_steam_network_error_is_offline():
    s = FakeSession(error=requests.ConnectionError("no route"))
    assert SteamClient()._check_session(s) == "offline"


def test_steam_recheck_drops_rejected_session_but_keeps_offline_one():
    c = SteamClient()
    c._session = FakeSession(FakeResponse(503))
    assert c.is_logged_in(recheck=True) is False
    assert c._session is not None and c.conn_state == "offline"

    c._session = FakeSession(FakeResponse(200, SIGNED_OUT_PAGE))
    assert c.is_logged_in(recheck=True) is False
    assert c._session is None and c.conn_state == "signed_out"


def test_steam_cookie_restore_while_offline_keeps_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / steam_client.COOKIE_FILE).write_text('{"sessionid": "x"}')
    monkeypatch.setattr(SteamClient, "_check_session", lambda self, s: "offline")
    c = SteamClient()
    assert c.try_cookie_login() is False
    assert c._session is not None and c.conn_state == "offline"

    # the watcher re-verifies the held session; once Steam answers it's green
    monkeypatch.setattr(SteamClient, "_check_session", lambda self, s: "ok")
    c.refresh_connection()
    assert c.conn_state == "ok" and c.is_logged_in()


def test_steam_no_cookie_file_is_signed_out(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = SteamClient()
    c.conn_state = "offline"
    assert c.try_cookie_login() is False
    assert c.conn_state == "signed_out"


# ---------------- Humble ----------------

class FakeDriver:
    def __init__(self, url="https://www.humblebundle.com/home/library",
                 answer="ok", get_error=None, script_error=None, load_url=None):
        self.current_url = url
        self.answer = answer
        self.get_error = get_error
        self.script_error = script_error
        self.load_url = load_url  # where a get() lands (None = the URL asked for)
        self.loads = []

    def get(self, url):
        self.loads.append(url)
        if self.get_error:
            raise self.get_error
        self.current_url = self.load_url or url

    def execute_async_script(self, script):
        if self.script_error:
            raise self.script_error
        return self.answer


def _humble(driver, logged_in=True):
    c = HumbleClient()
    c._driver = driver
    c._logged_in = logged_in
    return c


def test_humble_check_passes_through_the_api_verdict():
    for answer in ("ok", "signed_out", "offline"):
        d = FakeDriver(answer=answer)
        assert _humble(d)._check_session(d) == answer


def test_humble_off_origin_page_reloads_before_checking():
    # Chrome left on its error page: the fetch would run without Humble's
    # cookies and wrongly read as signed out, so it must reload Humble first
    d = FakeDriver(url="chrome-error://chromewebdata/", answer="ok")
    assert _humble(d)._check_session(d) == "ok"
    assert d.loads == [humble_client.HUMBLE_KEYS_PAGE]


def test_humble_still_unreachable_is_offline_not_signed_out():
    d = FakeDriver(url="chrome-error://chromewebdata/", answer="signed_out",
                   load_url="chrome-error://chromewebdata/")
    assert _humble(d)._check_session(d) == "offline"


def test_humble_net_error_is_offline_and_keeps_the_browser():
    err = WebDriverException("unknown error: net::ERR_INTERNET_DISCONNECTED")
    d = FakeDriver(url="chrome-error://chromewebdata/", get_error=err)
    c = _humble(d)
    assert c._check_session(d) == "offline"
    assert c._driver is d


def test_humble_script_timeout_is_offline():
    d = FakeDriver(script_error=TimeoutException("script timeout"))
    assert _humble(d)._check_session(d) == "offline"


def test_humble_recheck_drops_rejected_session_but_keeps_offline_one():
    c = _humble(FakeDriver(answer="offline"))
    assert c.is_logged_in(recheck=True) is False
    assert c._logged_in is True and c.conn_state == "offline"

    c._driver.answer = "signed_out"
    assert c.is_logged_in(recheck=True) is False
    assert c._logged_in is False and c.conn_state == "signed_out"


def test_humble_watcher_upgrades_offline_session_to_green():
    c = _humble(FakeDriver(answer="offline"))
    c.refresh_connection()
    assert c.conn_state == "offline"
    c._driver.answer = "ok"
    c.refresh_connection()
    assert c.conn_state == "ok"


def test_humble_no_cookie_file_is_signed_out(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = _humble(None, logged_in=False)
    c.conn_state = "offline"
    assert c.try_cookie_login() is False
    assert c.conn_state == "signed_out"
