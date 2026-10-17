"""Unit tests for the Attention report's per-entry fields.

Every entry built from a key carries the key's order gamekey, so the UI
can link it to that order's page on humblebundle.com. Fake store only.
"""
from redeemer import attention


def make_key(id, service="Steam", gamekey="ORDERKEY0000AAAA", **fields):
    key = {"id": id, "human_name": f"Game {id}", "service": service,
           "gamekey": gamekey, "redeemed_key_val": "", "revealed": 0,
           "expires": "", "redeem_status": "", "last_result_code": None,
           "last_result_label": "", "given_away": 0, "is_expired": 0}
    key.update(fields)
    return key


class FakeStore:
    def __init__(self, keys, events=()):
        self.keys = keys
        self.events = list(events)

    def get_keys(self):
        return self.keys

    def get_events(self, limit=2000):
        return self.events


def test_every_key_entry_carries_its_order_gamekey():
    keys = [
        make_key(1, is_expired=1, gamekey="EXPIRED00000AAAA"),
        make_key(2, service="EA/Origin", gamekey="EAORDER00000AAAA"),
        make_key(3, redeem_status="errored", last_result_code=15, gamekey="DEADKEY00000AAAA"),
        make_key(4, redeem_status="gift_link", redeemed_key_val="https://example/gift",
                 gamekey="GIFTKEY00000AAAA"),
    ]
    report = attention.build_report(FakeStore(keys))
    by_id = {e["id"]: e for bucket in report.values() for e in bucket if e["id"]}
    assert {i: e["gamekey"] for i, e in by_id.items()} == {
        1: "EXPIRED00000AAAA", 2: "EAORDER00000AAAA",
        3: "DEADKEY00000AAAA", 4: "GIFTKEY00000AAAA"}


def test_manual_choice_entries_keep_their_choice_page_link():
    events = [{"action": "choice_claim", "result_label": "manual_needed",
               "name": "Some Game", "gamekey": "MONTHKEY0000",
               "detail": "October 2024 Humble Choice: needs the site"}]
    report = attention.build_report(FakeStore([], events))
    (entry,) = report["manual_choice"]
    assert entry["url"] == "https://www.humblebundle.com/subscription/october-2024"
