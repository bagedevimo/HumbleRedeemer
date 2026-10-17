"""Friends for the giveaway page: who you might give a spare to, and which
spares each of them doesn't already own.

Friends are typed in by hand (SteamID64, profile URL, or vanity name) and
their libraries are read with the same Web API key as your own. Steam only
answers for libraries whose "Game details" privacy is Public, so every
friend carries a visibility status and private ones are reported as
"can't check", never as "owns nothing".

Everything here is pure (no network, no database) so it can be tested
directly; steam_client does the fetching, store does the keeping.
"""
import re

from . import matching

# visibility states, most to least useful
PUBLIC = "public"                # game details visible, library cached
GAMES_PRIVATE = "games_private"  # profile visible, game details hidden
PROFILE_PRIVATE = "profile_private"
UNKNOWN = "unknown"              # never fetched, or the fetch failed

_STEAMID64 = re.compile(r"^7656\d{13}$")
_PROFILE_URL = re.compile(r"steamcommunity\.com/profiles/(\d{17})", re.I)
_VANITY_URL = re.compile(r"steamcommunity\.com/id/([^/?#\s]+)", re.I)
_VANITY = re.compile(r"^[A-Za-z0-9_-]{2,32}$")


def parse_friend_input(text):
    """Classify one hand-typed friend reference.

    Returns ('steamid', '7656…'), ('vanity', 'name') or (None, reason).
    A bare word is treated as a vanity name; persona (display) names can't
    be looked up through the API, so those only work via autocomplete,
    which hands us the SteamID64 directly."""
    s = (text or "").strip()
    if not s:
        return None, "Enter a SteamID64, profile link, or custom URL name."
    m = _PROFILE_URL.search(s)
    if m:
        return "steamid", m.group(1)
    m = _VANITY_URL.search(s)
    if m:
        return "vanity", m.group(1)
    if s.isdigit():
        if _STEAMID64.match(s):
            return "steamid", s
        return None, "That number isn't a SteamID64 (17 digits starting 7656)."
    if _VANITY.match(s):
        return "vanity", s
    return None, ("Couldn't read that. Use a SteamID64, a steamcommunity.com "
                  "profile link, or the custom URL name from one.")


def classify_owned_response(payload):
    """Read a GetOwnedGames JSON body for someone else's account.

    Returns (visibility, {appid: name} or None). Steam's shapes:
      {"response": {"game_count": N, "games": [...]}}  visible
      {"response": {"game_count": 0}}                  visible, owns nothing
      {"response": {}}                                 game details private"""
    resp = (payload or {}).get("response") or {}
    if "games" in resp:
        games = resp.get("games") or []
        return PUBLIC, {g["appid"]: g.get("name", "") for g in games if "appid" in g}
    if "game_count" in resp:
        return PUBLIC, {}
    return GAMES_PRIVATE, None


def annotate_spares(spares, libraries):
    """Stamp each spare with which friends lack it.

    libraries: {steamid: {appid: name}} for friends whose library is
    readable, or {steamid: None} for friends we can't check.
    Adds to each spare dict:
      friends_lacking  — steamids that don't own it
      friends_unknown  — steamids whose library is private / unfetched
      friends_by_name  — True when the call rests on a title match because
                         the key carries no appid (hand-entered keys)
    Only appid and exact normalized-name hits count as owning: the fuzzy
    tier would be spares x friends x library-size comparisons per request."""
    by_name = {}
    for sid, lib in libraries.items():
        if lib is not None:
            by_name[sid] = {matching.normalize(n) for n in lib.values() if n}
    for spare in spares:
        appid = spare.get("steam_app_id") or spare.get("match_appid")
        try:
            appid = int(appid) if appid else None
        except (TypeError, ValueError):
            appid = None
        names = {matching.normalize(n) for n in
                 (spare.get("human_name"), spare.get("match_name")) if n}
        names.discard("")
        lacking, unknown = [], []
        for sid, lib in libraries.items():
            if lib is None:
                unknown.append(sid)
                continue
            owns = (appid is not None and appid in lib) or bool(names & by_name[sid])
            if not owns:
                lacking.append(sid)
        spare["friends_lacking"] = lacking
        spare["friends_unknown"] = unknown
        spare["friends_by_name"] = appid is None
    return spares
