"""Unit tests for the giveaway page's friends feature: reading what the user
typed, telling a hidden library apart from an empty one, and working out
which friends lack each spare.

The visibility call is the one that matters most: a friend whose library
is private must come out as "can't check", never as "owns nothing", or the
page would offer them games they already have.
"""
import pytest

from redeemer import friends
from redeemer.jobs import refresh_friends
from redeemer.store import Store

SID_A = "76561197960287930"
SID_B = "76561198000000001"


# ---------------- input parsing ----------------

@pytest.mark.parametrize("text,expected", [
    (SID_A, ("steamid", SID_A)),
    (f"  {SID_A}\n", ("steamid", SID_A)),
    (f"https://steamcommunity.com/profiles/{SID_A}/", ("steamid", SID_A)),
    (f"steamcommunity.com/profiles/{SID_A}?tab=games", ("steamid", SID_A)),
    ("https://steamcommunity.com/id/gabelogannewell/", ("vanity", "gabelogannewell")),
    ("http://STEAMCOMMUNITY.com/id/some_name", ("vanity", "some_name")),
    ("gabelogannewell", ("vanity", "gabelogannewell")),
])
def test_parse_friend_input_accepts(text, expected):
    assert friends.parse_friend_input(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", "12345", "86561197960287930", "has spaces in it", "bad!chars",
])
def test_parse_friend_input_rejects(text):
    kind, reason = friends.parse_friend_input(text)
    assert kind is None
    assert reason


# ---------------- visibility ----------------

def test_classify_visible_library():
    vis, lib = friends.classify_owned_response({"response": {
        "game_count": 2, "games": [{"appid": 10, "name": "CS"},
                                   {"appid": 20, "name": "TFC"}]}})
    assert vis == friends.PUBLIC
    assert lib == {10: "CS", 20: "TFC"}


def test_classify_visible_but_empty_library():
    assert friends.classify_owned_response({"response": {"game_count": 0}}) == \
        (friends.PUBLIC, {})


@pytest.mark.parametrize("payload", [{"response": {}}, {}, None])
def test_classify_hidden_library_is_not_empty(payload):
    assert friends.classify_owned_response(payload) == (friends.GAMES_PRIVATE, None)


# ---------------- who lacks what ----------------

def spare(id, name, steam_app_id=None, match_appid=None, match_name=""):
    return {"id": id, "human_name": name, "steam_app_id": steam_app_id,
            "match_appid": match_appid, "match_name": match_name}


def test_annotate_by_appid_and_private_friends():
    libs = {SID_A: {440: "Team Fortress 2"}, SID_B: None}
    out = friends.annotate_spares(
        [spare(1, "Team Fortress 2", 440), spare(2, "Portal", 400)], libs)
    assert out[0]["friends_lacking"] == []
    assert out[1]["friends_lacking"] == [SID_A]
    # the private friend is never counted as lacking either game
    assert out[0]["friends_unknown"] == [SID_B]
    assert out[1]["friends_unknown"] == [SID_B]
    assert out[0]["friends_by_name"] is False


def test_annotate_falls_back_to_match_appid():
    libs = {SID_A: {400: "Portal"}}
    out = friends.annotate_spares([spare(1, "Portal (Humble)", match_appid=400)], libs)
    assert out[0]["friends_lacking"] == []


def test_annotate_without_appid_uses_normalized_title():
    libs = {SID_A: {400: "Portal 2"}, SID_B: {}}
    out = friends.annotate_spares([spare(1, "PORTAL 2!"), spare(2, "Hades")], libs)
    assert out[0]["friends_lacking"] == [SID_B]
    assert out[0]["friends_by_name"] is True
    assert out[1]["friends_lacking"] == [SID_A, SID_B]


def test_annotate_with_no_friends_is_harmless():
    out = friends.annotate_spares([spare(1, "Portal", 400)], {})
    assert out[0]["friends_lacking"] == [] and out[0]["friends_unknown"] == []


# ---------------- store + refresh ----------------

class FakeSteam:
    def __init__(self, summaries, libraries, broken=()):
        self.summaries = summaries
        self.libraries = libraries
        self.broken = set(broken)

    def get_player_summaries(self, api_key, steam_ids):
        return {s: self.summaries[s] for s in steam_ids if s in self.summaries}

    def get_friend_library(self, api_key, steam_id):
        if steam_id in self.broken:
            raise RuntimeError("boom")
        return self.libraries[steam_id]


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


def test_refresh_records_visibility_and_libraries(store):
    sid_c = "76561198000000002"
    for sid in (SID_A, SID_B, sid_c):
        store.add_friend(sid)
    steam = FakeSteam(
        summaries={
            SID_A: {"personaname": "Alice", "communityvisibilitystate": 3},
            SID_B: {"personaname": "Bob", "communityvisibilitystate": 1},
            sid_c: {"personaname": "Cara", "communityvisibilitystate": 3},
        },
        libraries={
            SID_A: (friends.PUBLIC, {440: "Team Fortress 2"}),
            SID_B: (friends.GAMES_PRIVATE, None),
            sid_c: (friends.GAMES_PRIVATE, None),
        })

    assert refresh_friends(store, steam, "key", [SID_A, SID_B, sid_c]) == []

    by_sid = {f["steamid"]: f for f in store.get_friends()}
    assert by_sid[SID_A]["visibility"] == "public"
    assert by_sid[SID_A]["game_count"] == 1
    # whole profile private vs. only game details hidden
    assert by_sid[SID_B]["visibility"] == "profile_private"
    assert by_sid[sid_c]["visibility"] == "games_private"
    assert store.get_friend_libraries() == {
        SID_A: {440: "Team Fortress 2"}, SID_B: None, sid_c: None}


def test_refresh_keeps_going_and_keeps_cache_when_one_friend_fails(store):
    store.add_friend(SID_A)
    store.add_friend(SID_B)
    summaries = {SID_A: {"personaname": "Alice", "communityvisibilitystate": 3},
                 SID_B: {"personaname": "Bob", "communityvisibilitystate": 3}}
    good = FakeSteam(summaries, {SID_A: (friends.PUBLIC, {440: "TF2"}),
                                 SID_B: (friends.PUBLIC, {400: "Portal"})})
    refresh_friends(store, good, "key", [SID_A, SID_B])

    flaky = FakeSteam(summaries, {SID_B: (friends.PUBLIC, {400: "Portal", 620: "Portal 2"})},
                      broken=[SID_A])
    assert refresh_friends(store, flaky, "key", [SID_A, SID_B]) == [SID_A]

    libs = store.get_friend_libraries()
    assert libs[SID_A] == {440: "TF2"}  # last good fetch survives the failure
    assert libs[SID_B] == {400: "Portal", 620: "Portal 2"}
    assert "boom" in {f["steamid"]: f for f in store.get_friends()}[SID_A]["error"]


def test_going_private_clears_the_cached_library(store):
    store.add_friend(SID_A)
    summ = {SID_A: {"personaname": "Alice", "communityvisibilitystate": 3}}
    refresh_friends(store, FakeSteam(summ, {SID_A: (friends.PUBLIC, {440: "TF2"})}),
                    "key", [SID_A])
    refresh_friends(store, FakeSteam(summ, {SID_A: (friends.GAMES_PRIVATE, None)}),
                    "key", [SID_A])
    assert store.get_friend_libraries() == {SID_A: None}


def test_remove_friend_drops_library(store):
    store.add_friend(SID_A)
    store.update_friend(SID_A, {"personaname": "Alice"}, friends.PUBLIC, {440: "TF2"})
    assert store.remove_friend(SID_A) == 1
    assert store.get_friends() == [] and store.get_friend_libraries() == {}
