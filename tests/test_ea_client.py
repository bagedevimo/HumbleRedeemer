"""Unit tests for the EA client's parsing and paging.

A token counts only when EA hands one over; EA's own login_required answer
means signed out; anything else (HTML, a 5xx, junk) proves nothing and reads
as offline. The library parser must surface an auth rejection distinctly,
since that's the one failure that means the web-token approach itself
stopped working. Fake responses only, no network.
"""
import json

import pytest

from redeemer import ea_client
from redeemer.ea_client import (EaAuthRejected, EaClient, parse_owned_page,
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


def _page(items, next_cursor=None, total=None):
    return {"data": {"me": {"ownedGameProducts": {
        "next": next_cursor, "totalCount": total, "items": items}}}}


def _item(name, methods=("REDEMPTION",), offer="Origin.OFR.1", base_title=None):
    return {"originOfferId": offer, "product": {
        "name": name,
        "baseItem": {"title": base_title or name, "gameType": "BASE_GAME"},
        "gameProductUser": {"ownershipMethods": list(methods),
                            "initialEntitlementDate": "2021-03-04T10:00:00Z",
                            "status": "ACTIVE"}}}


def test_owned_page_flattens_items():
    items, cursor, total = parse_owned_page(_page(
        [_item("Mass Effect™ Legendary Edition", base_title="Mass Effect Legendary Edition")],
        next_cursor="500", total=1))
    assert cursor == "500" and total == 1
    assert items == [{
        "offer_id": "Origin.OFR.1",
        "name": "Mass Effect™ Legendary Edition",
        "base_title": "Mass Effect Legendary Edition",
        "game_type": "BASE_GAME",
        "ownership": ["REDEMPTION"],
        "entitled_at": "2021-03-04T10:00:00Z",
        "status": "ACTIVE",
    }]


def test_owned_page_skips_nameless_and_tolerates_missing_fields():
    items, _, _ = parse_owned_page(_page([
        {"originOfferId": "x", "product": {}},
        {"originOfferId": "y", "product": {"baseItem": {"title": "Only Base Title"}}},
    ]))
    assert [i["name"] for i in items] == ["Only Base Title"]
    assert items[0]["ownership"] == []


def test_unauthenticated_error_is_auth_rejected():
    payload = {"data": None, "errors": [{"message": "Not authenticated.",
                                         "extensions": {"code": "UNAUTHENTICATED"}}]}
    with pytest.raises(EaAuthRejected):
        parse_owned_page(payload)


def test_other_graphql_error_is_plain_runtime_error():
    payload = {"data": None, "errors": [{"message": "Graphql validation error"}]}
    with pytest.raises(RuntimeError) as e:
        parse_owned_page(payload)
    assert not isinstance(e.value, EaAuthRejected)
    assert "validation" in str(e.value)


def test_get_owned_walks_every_page(monkeypatch):
    pages = {"0": _page([_item("A")], next_cursor="500"),
             "500": _page([_item("B")], next_cursor="1000"),
             "1000": _page([], next_cursor=None)}
    asked = []
    client = EaClient()
    monkeypatch.setattr(client, "_access_token", lambda: "tok")
    monkeypatch.setattr(client, "_query", lambda tok, cur: asked.append(cur) or pages[cur])
    assert [g["name"] for g in client.get_owned()] == ["A", "B"]
    assert asked == ["0", "500", "1000"]


def test_get_owned_stops_on_a_repeating_cursor(monkeypatch):
    client = EaClient()
    monkeypatch.setattr(client, "_access_token", lambda: "tok")
    monkeypatch.setattr(client, "_query", lambda tok, cur: _page([_item("A")], next_cursor="500"))
    assert len(client.get_owned()) == 2  # "0" then "500" once; the repeat ends it


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


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, url, **kwargs):
        assert url == ea_client.EA_TOKEN_URL
        return self.response


def test_try_cookie_login_signed_out_drops_session(monkeypatch):
    monkeypatch.setattr(ea_client, "load_cookie_list", lambda path: [{"name": "sid", "value": "v"}])
    monkeypatch.setattr(ea_client, "make_session", lambda cookies: FakeSession(
        FakeResponse(200, json.dumps({"error": "login_required"}))))
    client = EaClient()
    assert client.try_cookie_login() is False
    assert client.conn_state == "signed_out" and client._session is None


def test_try_cookie_login_offline_keeps_session(monkeypatch):
    monkeypatch.setattr(ea_client, "load_cookie_list", lambda path: [{"name": "sid", "value": "v"}])
    monkeypatch.setattr(ea_client, "make_session", lambda cookies: FakeSession(
        FakeResponse(503, "")))
    client = EaClient()
    assert client.try_cookie_login() is False
    assert client.conn_state == "offline" and client._session is not None


def test_try_cookie_login_ok_caches_token(monkeypatch):
    monkeypatch.setattr(ea_client, "load_cookie_list", lambda path: [{"name": "sid", "value": "v"}])
    monkeypatch.setattr(ea_client, "make_session", lambda cookies: FakeSession(
        FakeResponse(200, json.dumps({"access_token": "tok"}))))
    client = EaClient()
    assert client.try_cookie_login() is True
    assert client.is_logged_in() and client._access_token() == "tok"
