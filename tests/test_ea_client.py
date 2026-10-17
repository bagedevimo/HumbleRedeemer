"""Unit tests for the EA client's parsing and paging.

A token counts only when EA hands one over; EA's own login_required answer
means signed out; anything else (HTML, a 5xx, junk) proves nothing and reads
as offline. The library parsers must surface an auth rejection distinctly,
since that's the one failure that means the web-token approach itself
stopped working, and only active, non-lapsed grants count as owned. Fake responses only, no network.
"""
import json

import pytest

from redeemer import ea_client
from redeemer.ea_client import (EaAuthRejected, EaClient, build_library,
                                parse_entitlements_page, parse_offers,
                                parse_token_response)


def test_token_reply_with_access_token_is_ok():
    body = json.dumps({"access_token": "abc", "token_type": "Bearer", "expires_in": "3599"})
    assert parse_token_response(200, body) == ("ok", "abc")


def test_login_required_is_signed_out():
    body = json.dumps({"error_code": "login_required", "error": "login_required",
                       "error_number": "102100"})
    assert parse_token_response(200, body) == ("signed_out", None)


@pytest.mark.parametrize("status,text", [
    (200, "<html>maintenance</html>"),
    (503, ""),
    (200, json.dumps(["not", "a", "dict"])),
    (500, json.dumps({"access_token": "abc"})),  # a token on an error status proves nothing
    (200, json.dumps({"access_token": ""})),
])
def test_anything_else_is_offline(status, text):
    assert parse_token_response(status, text) == ("offline", None)


def _ent(product_id, status="ACTIVE", subscription=False, expired=False,
         granted="2021-03-04T10:00:00Z"):
    return {"productId": product_id, "entitlementTag": "ONLINE_ACCESS",
            "entitlementType": "DEFAULT", "groupName": "", "status": status,
            "grantDate": granted, "isSubscription": subscription,
            "isSubscriptionExpired": expired}


def _ents(items, next_cursor=None):
    return {"data": {"me": {"legacyEntitlements": {"next": next_cursor, "items": items}}}}


def _offers(*pairs):
    return {"data": {"legacyOffers": [
        {"id": i, "displayName": n, "displayType": "FullGame", "primaryMasterTitleId": "1"}
        for i, n in pairs]}}


UNAUTH = {"data": None, "errors": [{"message": "Not authenticated.",
                                    "extensions": {"code": "UNAUTHENTICATED"}}]}


def test_entitlements_page_flattens_items_and_skips_productless():
    items, cursor = parse_entitlements_page(_ents(
        [_ent("Origin.OFR.1", subscription=True), {"status": "ACTIVE"}], next_cursor="200"))
    assert cursor == "200"
    assert items == [{"product_id": "Origin.OFR.1", "tag": "ONLINE_ACCESS", "type": "DEFAULT",
                      "group": "", "status": "ACTIVE", "granted_at": "2021-03-04T10:00:00Z",
                      "subscription": True, "subscription_expired": False}]


def test_offers_keep_only_named_ones():
    payload = {"data": {"legacyOffers": [
        {"id": "a", "displayName": "It Takes Two", "displayType": "FullGame"},
        {"id": "b", "displayName": None}, None]}}
    assert parse_offers(payload) == {"a": {"name": "It Takes Two", "display_type": "FullGame",
                                           "master_title_id": ""}}


@pytest.mark.parametrize("parser", [parse_entitlements_page, parse_offers])
def test_unauthenticated_error_is_auth_rejected(parser):
    with pytest.raises(EaAuthRejected):
        parser(UNAUTH)


def test_other_graphql_error_is_plain_runtime_error():
    with pytest.raises(RuntimeError) as e:
        parse_entitlements_page({"data": None, "errors": [{"message": "Graphql validation error"}]})
    assert not isinstance(e.value, EaAuthRejected)
    assert "validation" in str(e.value)


def test_library_owned_means_active_and_not_a_lapsed_subscription():
    ents = [_ent("bought"), _ent("revoked", status="DISABLED"),
            _ent("ea_play_live", subscription=True),
            _ent("ea_play_gone", subscription=True, expired=True),
            _ent("access_flag")]  # no catalog name: an internal flag, dropped
    offers = {k: {"name": k, "display_type": "FullGame", "master_title_id": ""}
              for k in ("bought", "revoked", "ea_play_live", "ea_play_gone")}
    parsed, _ = parse_entitlements_page(_ents(ents))
    lib = {g["offer_id"]: g["owned"] for g in build_library(parsed, offers)}
    assert lib == {"bought": True, "revoked": False, "ea_play_live": True, "ea_play_gone": False}


def test_library_prefers_an_active_grant_over_a_disabled_duplicate():
    offers = {"x": {"name": "X", "display_type": "FullGame", "master_title_id": ""}}
    for order in ([_ent("x", status="DISABLED"), _ent("x")], [_ent("x"), _ent("x", status="DISABLED")]):
        parsed, _ = parse_entitlements_page(_ents(order))
        (game,) = build_library(parsed, offers)
        assert game["owned"] is True


class FakeGql:
    """Stands in for EaClient._gql: entitlement pages by cursor, offer names
    by id, and an optional set of tokens to refuse."""
    def __init__(self, pages, names, refuse=()):
        self.pages, self.names, self.refuse = pages, names, set(refuse)
        self.cursors, self.offer_batches = [], []

    def __call__(self, token, operation, query, variables):
        if token in self.refuse:
            return UNAUTH
        if operation == "entitlements":
            self.cursors.append(variables["next"])
            return self.pages[variables["next"]]
        self.offer_batches.append(list(variables["offerIds"]))
        return _offers(*[(i, self.names[i]) for i in variables["offerIds"] if i in self.names])


def test_get_owned_walks_every_page_then_names_them(monkeypatch):
    gql = FakeGql({"0": _ents([_ent("a")], next_cursor="200"),
                   "200": _ents([_ent("b")], next_cursor="400"),
                   "400": _ents([], next_cursor=None)},
                  {"a": "Alpha", "b": "Beta"})
    client = EaClient()
    monkeypatch.setattr(client, "_access_token", lambda: "tok")
    monkeypatch.setattr(client, "_gql", gql)
    assert [g["name"] for g in client.get_owned()] == ["Alpha", "Beta"]
    assert gql.cursors == ["0", "200", "400"]
    assert gql.offer_batches == [["a", "b"]]


def test_get_owned_stops_on_a_repeating_cursor(monkeypatch):
    gql = FakeGql({"0": _ents([_ent("a")], next_cursor="200"),
                   "200": _ents([_ent("a")], next_cursor="200")}, {"a": "Alpha"})
    client = EaClient()
    monkeypatch.setattr(client, "_access_token", lambda: "tok")
    monkeypatch.setattr(client, "_gql", gql)
    assert len(client.get_owned()) == 1
    assert gql.cursors == ["0", "200"]


def test_get_owned_names_offers_in_batches(monkeypatch):
    ids = [f"id{i:03d}" for i in range(ea_client.OFFER_BATCH + 5)]
    gql = FakeGql({"0": _ents([_ent(i) for i in ids])}, {i: i.upper() for i in ids})
    client = EaClient()
    monkeypatch.setattr(client, "_access_token", lambda: "tok")
    monkeypatch.setattr(client, "_gql", gql)
    assert len(client.get_owned()) == len(ids)
    assert [len(b) for b in gql.offer_batches] == [ea_client.OFFER_BATCH, 5]


def test_redirect_carrying_token_in_fragment_is_ok():
    assert parse_token_response(
        302, "", "nucleus:rest#access_token=tok&token_type=Bearer") == ("ok", "tok")


def test_redirect_with_login_required_is_signed_out():
    assert parse_token_response(
        302, "", "nucleus:rest?error=login_required") == ("signed_out", None)


def test_redirect_to_signin_page_proves_nothing():
    assert parse_token_response(
        302, "", "https://signin.ea.com/p/juno/login?fid=abc") == ("offline", None)


class FakeResponse:
    def __init__(self, status, text, headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


LOGIN_REQUIRED = FakeResponse(200, json.dumps({"error": "login_required"}))
DX_LOGIN_REQUIRED = FakeResponse(302, "", {"Location":
    "https://www.ea.com/static-pages/auth.html#error_code=login_required&error=login_required"})


class FakeSession:
    """Answers each auth client's prompt=none request: `by_client` maps a
    client_id to its FakeResponse; clients not listed get `default`."""
    def __init__(self, default, **by_client):
        self.default = default
        self.by_client = by_client
        self.asked = []

    def get(self, url, **kwargs):
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(url).query)
        assert url.startswith(ea_client.EA_AUTH) and q["prompt"] == ["none"]
        client = q["client_id"][0]
        self.asked.append(client)
        return self.by_client.get(client, self.default)


def _with_session(monkeypatch, session):
    monkeypatch.setattr(ea_client, "load_cookie_list", lambda path: [{"name": "sid", "value": "v"}])
    monkeypatch.setattr(ea_client, "make_session", lambda cookies: session)


def test_try_cookie_login_signed_out_drops_session(monkeypatch):
    _with_session(monkeypatch, FakeSession(LOGIN_REQUIRED, DX_DROP_JS_WEB_APP=DX_LOGIN_REQUIRED))
    client = EaClient()
    assert client.try_cookie_login() is False
    assert client.conn_state == "signed_out" and client._session is None


def test_try_cookie_login_offline_keeps_session(monkeypatch):
    _with_session(monkeypatch, FakeSession(FakeResponse(503, "")))
    client = EaClient()
    assert client.try_cookie_login() is False
    assert client.conn_state == "offline" and client._session is not None


def test_one_client_signed_out_one_offline_is_offline(monkeypatch):
    _with_session(monkeypatch, FakeSession(FakeResponse(503, ""), ORIGIN_JS_SDK=LOGIN_REQUIRED))
    client = EaClient()
    assert client.try_cookie_login() is False
    assert client.conn_state == "offline"


def test_try_cookie_login_ok_caches_token(monkeypatch):
    session = FakeSession(FakeResponse(200, json.dumps({"access_token": "tok"})))
    _with_session(monkeypatch, session)
    client = EaClient()
    assert client.try_cookie_login() is True
    assert client.is_logged_in() and client._access_token() == "tok"
    assert client.token_client == "ORIGIN_JS_SDK" and session.asked == ["ORIGIN_JS_SDK"]


def test_falls_back_to_website_client_for_the_token(monkeypatch):
    _with_session(monkeypatch, FakeSession(LOGIN_REQUIRED, DX_DROP_JS_WEB_APP=FakeResponse(
        302, "", {"Location": "https://www.ea.com/static-pages/auth.html#access_token=dx&expires_in=3599"})))
    client = EaClient()
    assert client.try_cookie_login() is True
    assert client._access_token() == "dx" and client.token_client == "DX_DROP_JS_WEB_APP"


def test_get_owned_retries_with_the_next_client_when_a_token_is_rejected(monkeypatch):
    _with_session(monkeypatch, FakeSession(FakeResponse(200, json.dumps({"access_token": "js"})),
                                           DX_DROP_JS_WEB_APP=FakeResponse(
        302, "", {"Location": "https://www.ea.com/static-pages/auth.html#access_token=dx"})))
    client = EaClient()
    assert client.try_cookie_login() is True
    monkeypatch.setattr(client, "_gql", FakeGql({"0": _ents([_ent("a")])}, {"a": "A"}, refuse={"js"}))
    assert [g["name"] for g in client.get_owned()] == ["A"]
    assert client.token_client == "DX_DROP_JS_WEB_APP"


def test_get_owned_gives_up_once_every_client_is_rejected(monkeypatch):
    _with_session(monkeypatch, FakeSession(FakeResponse(200, json.dumps({"access_token": "js"})),
                                           DX_DROP_JS_WEB_APP=FakeResponse(
        302, "", {"Location": "https://www.ea.com/static-pages/auth.html#access_token=dx"})))
    client = EaClient()
    client.try_cookie_login()
    monkeypatch.setattr(client, "_gql", FakeGql({}, {}, refuse={"js", "dx"}))
    with pytest.raises(EaAuthRejected) as e:
        client.get_owned()
    assert "ORIGIN_JS_SDK, DX_DROP_JS_WEB_APP" in str(e.value)
