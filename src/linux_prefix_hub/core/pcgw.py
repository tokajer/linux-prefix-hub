# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Known storage locations from PCGamingWiki.

The diff in `core/snapshot.py` only learns where a game saves *after* it has
been played once. PCGamingWiki already knows, for thousands of games, so a
lookup answers the same question before the first launch -- and answers the
`type` (saves vs config) properly instead of guessing it from the path.

Four rules this module lives by:

1. **Optional.** It is a network dependency. Nothing here is required and
   nothing here raises: no network, no article, a redesigned wiki -- every
   entry point comes back with "nothing found" and the app carries on with
   the diff, which never needed help in the first place.
2. **Never on the launch path.** A lookup happens because the user asked for
   one (`--lookup`, the button in the window). The wrapper only ever reads
   the local cache (`cached_locations`); it does not go online while someone
   is trying to play.
3. **Cached on disk**, one small JSON per game: hits for a month, misses for
   a day. The wiki is a slow-moving source and we are a guest on someone
   else's server.
4. **It suggests, the user decides.** An article is written by people, may
   describe another edition, and what it says lands in the list the user then
   moves data around with -- so nothing from here reaches the DB until the
   user has seen it and said yes (`confirm`), and nothing reaches it at all
   while the folder it names is not on disk (`on_disk`). Both gates are asked
   again every time the answer is used, not once when it was given: a yes is
   permission, not proof, and a folder that was missing in March exists in
   April. A path that is not there is not written, not created and not
   redirected -- we would otherwise invent a storage location out of an
   article, and the first thing the user does with it is move data into it.

What we read is the `{{Game data/saves|...}}` and `{{Game data/config|...}}`
rows of the article's wikitext. That is wiki markup written by people, not an
API contract, so `parse_game_data` is deliberately forgiving: anything it does
not understand is dropped rather than guessed at.

Paths are written as `{{p|token}}\\rest`. Only the tokens that land inside one
of our two location spaces survive (`PATH_ROOTS`):

    {{p|userprofile\\documents}}  ->  Documents/...          (prefix)
    {{p|appdata}}                ->  AppData/Roaming/...    (prefix)
    {{p|localappdata}}           ->  AppData/Local/...      (prefix)
    {{p|game}}                   ->  ...                    (install folder)

Everything else is outside those two spaces and is dropped: `{{p|steam}}\\
userdata` (that is Steam Cloud, not the prefix), `{{p|programdata}}`,
`{{p|hkcu}}` (the registry), and the Linux/macOS rows -- their `{{p|game}}`
paths would otherwise pass for a Windows game's install folder.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .. import __version__
from . import db, paths
from .i18n import _
from .snapshot import WHERE_GAME, WHERE_PREFIX

API_URL = "https://www.pcgamingwiki.com/w/api.php"
PAGE_URL = "https://www.pcgamingwiki.com/wiki/{page}"
SITE_NAME = "PCGamingWiki"

# Provenance written into every location we produce.
DETECTED_BY = "pcgamingwiki"

TIMEOUT = 15                      # seconds -- a lookup is interactive
MAX_BYTES = 4 * 1024 * 1024       # a long article is ~300 KB
HIT_TTL = 30 * 24 * 3600          # articles move slowly
MISS_TTL = 24 * 3600              # "no article" may just mean "not yet"

# MediaWiki wants to know who is calling; an anonymous scraper gets blocked.
USER_AGENT = (f"{paths.APP_NAME}/{__version__} "
              "(+https://github.com/tokajer/linux-prefix-hub)")

# The AppImage bundles its own CPython, and its OpenSSL was built against a
# certificate store that does not exist on the host machine: `ssl` comes back
# with an empty trust store and *every* HTTPS call fails with
# CERTIFICATE_VERIFY_FAILED. It looks exactly like being offline, on a machine
# that is online. So we find the host's CA bundle ourselves. Distro paths, in
# the order the distros themselves try them.
CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",      # Debian, Ubuntu, Arch
    "/etc/pki/tls/certs/ca-bundle.crt",        # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",                  # openSUSE
    "/etc/pki/tls/cacert.pem",
    "/etc/ssl/cert.pem",                       # Alpine
)
CA_DIRS = ("/etc/ssl/certs", "/etc/pki/tls/certs")

# `{{p|<token>}}` -> the space it lives in and the path prefix inside it.
# Tokens are the ones Template:Path actually defines; it lowercases its
# argument, so the wiki spells them in whatever case it likes.
PATH_ROOTS: dict[str, tuple[str, str]] = {
    "userprofile": (WHERE_PREFIX, ""),
    "userprofile\\documents": (WHERE_PREFIX, "Documents"),
    "userprofile\\appdata\\locallow": (WHERE_PREFIX, "AppData/LocalLow"),
    "appdata": (WHERE_PREFIX, "AppData/Roaming"),
    "localappdata": (WHERE_PREFIX, "AppData/Local"),
    "game": (WHERE_GAME, ""),
}

# Rows for another operating system. Their paths are shaped like ours
# (`{{p|game}}/...`) but describe a build we are not running.
SKIP_SYSTEMS = ("linux", "os x", "osx", "macos", "mac os", "mac os x",
                "dos", "ios", "android")

# `{{p|...}}` / `{{path|...}}` at the very start of a path parameter.
_PATH_TOKEN = re.compile(r"\s*\{\{\s*(?:p|path)\s*\|([^}|]*)\}\}", re.I)
_REF = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.I | re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAG = re.compile(r"<[^>]+>")
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,6}$")


class _Unreachable(Exception):
    """The wiki did not answer. Not something the user has to fix."""


# --- Settings ------------------------------------------------------------
def enabled() -> bool:
    """May we go online at all? Default yes, `online_lookup` can say no.

    A lookup is never automatic, so this is not what makes the feature
    optional -- it is for the machine that is meant to stay offline and
    would rather say so once than remember not to press the button.
    """
    return bool(db.load_config().get("online_lookup", True))


# --- Wikitext parsing (pure, no network) ---------------------------------
def _clean(text: str) -> str:
    """Drop the markup that decorates a path: refs, comments, tags, bold."""
    text = _REF.sub("", text)
    text = _COMMENT.sub("", text)
    text = _TAG.sub("", text)
    return text.replace("'''", "").replace("''", "").strip()


def _balanced(text: str, start: int) -> str | None:
    """The content of the `{{...}}` starting at `start`, braces balanced."""
    depth = 0
    i = start
    while i < len(text) - 1:
        pair = text[i:i + 2]
        if pair == "{{":
            depth += 1
        elif pair == "}}":
            depth -= 1
            if depth == 0:
                return text[start + 2:i]
        else:
            i += 1
            continue
        i += 2
    return None


def _split_params(body: str) -> list[str]:
    """Split a template body on its top-level `|`.

    Nested templates and links carry their own pipes (`{{p|game}}`), so a
    plain `split("|")` would tear every path apart.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    i = 0
    while i < len(body):
        pair = body[i:i + 2]
        if pair in ("{{", "[["):
            depth += 1
            current.append(pair)
            i += 2
        elif pair in ("}}", "]]"):
            depth = max(0, depth - 1)
            current.append(pair)
            i += 2
        elif body[i] == "|" and depth == 0:
            parts.append("".join(current))
            current = []
            i += 1
        else:
            current.append(body[i])
            i += 1
    parts.append("".join(current))
    return parts


def _templates(text: str, name: str) -> list[list[str]]:
    """Every `{{name|...}}` in `text`, each split into its parameters."""
    found: list[list[str]] = []
    needle = "{{" + name.lower()
    low = text.lower()
    start = low.find(needle)
    while start != -1:
        after = start + len(needle)
        if after < len(text) and text[after] in "|}":
            body = _balanced(text, start)
            if body is not None:
                found.append(_split_params(body))
        start = low.find(needle, start + 2)
    return found


def _looks_like_a_file(segment: str) -> bool:
    """`config.json`, `*.dat`, `*` -- not a folder we could point at."""
    return ("*" in segment or "?" in segment
            or bool(_EXTENSION.search(segment)))


def expand_path(raw: str) -> tuple[str, str] | None:
    """`{{p|appdata}}\\Foo\\x.ini` -> ("prefix", "AppData/Roaming/Foo").

    Returns None for everything that does not land in one of our two
    spaces -- the registry, Steam Cloud, ProgramData, another OS' home.
    The trailing file name is dropped: a storage location is a folder.
    """
    text = _clean(raw)
    match = _PATH_TOKEN.match(text)
    if not match:
        return None
    token = match.group(1).strip().lower().replace("/", "\\")
    root = PATH_ROOTS.get(token)
    if root is None:
        return None
    where, base = root

    # A `{{p|uid}}` further along names something we cannot resolve (a
    # profile id); keep the shape of the path and mark the hole.
    rest = _TEMPLATE.sub("*", text[match.end():]).replace("\\", "/")
    parts = [seg.strip() for seg in f"{base}/{rest}".split("/")
             if seg.strip()]
    if parts and _looks_like_a_file(parts[-1]):
        parts.pop()
    if not parts:
        return None
    return where, "/".join(parts)


def parse_game_data(wikitext: str) -> list[dict[str, Any]]:
    """The storage locations an article lists, in our own shape.

    Saves are read before config so that a path listed as both -- which
    happens when a game keeps one folder for everything -- is remembered as
    the more interesting of the two.
    """
    locations: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for kind, template in (("saves", "game data/saves"),
                           ("config", "game data/config")):
        for params in _templates(wikitext, template):
            if len(params) < 3:
                continue
            if _clean(params[1]).lower() in SKIP_SYSTEMS:
                continue
            for raw in params[2:]:
                mapped = expand_path(raw)
                if mapped is None or mapped in seen:
                    continue
                seen.add(mapped)
                where, win_path = mapped
                locations.append({
                    "type": kind,
                    "win_path": win_path,
                    "where": where,
                    "file_count": 0,
                    "detected_by": DETECTED_BY,
                    "redirected": False,
                })
    return locations


# --- Is that folder actually there? --------------------------------------
def space_root(game: dict[str, Any], where: str) -> Path | None:
    """The folder a location's `win_path` is counted from, or None.

    None means the space itself is not there: a game nobody has started has
    no prefix, a hand-made setup has no known install folder. Nothing in it
    can exist, which is the honest answer rather than a missing one.
    """
    if where == WHERE_GAME:
        game_dir = game.get("game_dir")
        return Path(str(game_dir)) if game_dir else None
    prefix, user_dir = game.get("prefix_path"), game.get("user_dir")
    if not prefix or not user_dir:
        return None
    return Path(str(prefix)) / "drive_c" / "users" / str(user_dir)


def _child(parent: Path, segment: str) -> Path | None:
    """The subfolder `segment` names in `parent`, however it is spelled."""
    try:
        entries = sorted(p for p in parent.iterdir() if p.is_dir())
    except OSError:
        return None
    wanted = segment.lower()
    for path in entries:
        if path.name.lower() == wanted:
            return path
    if any(char in segment for char in "*?["):
        for path in entries:
            if fnmatch.fnmatch(path.name.lower(), wanted):
                return path
    return None


def resolve_dir(root: Path, win_path: str) -> Path | None:
    """`win_path` under `root`, spelled the way the disk spells it.

    Two things stand between the wiki's string and a real folder. Windows
    paths are case-insensitive and an article spells them however its editor
    felt like ("documents\\My Games"), while the filesystem underneath is not
    and does not. And a `*` is standing in for something we could not resolve
    (`expand_path`: a profile id), so it has to be matched against what is
    there rather than looked up.

    Returns None when no such folder exists -- which is the whole point of
    asking.
    """
    if not root.is_dir():
        return None
    current = root
    for segment in win_path.replace("\\", "/").split("/"):
        if not segment.strip():
            continue
        found = _child(current, segment.strip())
        if found is None:
            return None
        current = found
    return current


def on_disk(game: dict[str, Any], locations: list[dict[str, Any]]
            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split suggestions into (there, not there).

    The ones that are there come back spelled as the disk spells them, so
    that a folder the diff also finds stays *one* entry and not two
    (`db.location_key`). The ones that are not are handed back too rather
    than dropped: "the wiki says this and your copy has not written it yet"
    is worth showing, and it is the difference between a game that keeps its
    saves elsewhere and one that has simply never been played.
    """
    there: list[dict[str, Any]] = []
    not_there: list[dict[str, Any]] = []
    for loc in locations:
        where = str(loc.get("where") or WHERE_PREFIX)
        root = space_root(game, where)
        found = (resolve_dir(root, str(loc.get("win_path", "")))
                 if root is not None else None)
        if found is None:
            not_there.append(dict(loc))
            continue
        entry = dict(loc)
        entry["win_path"] = str(found.relative_to(root))
        there.append(entry)
    return there, not_there


# --- Talking to the wiki -------------------------------------------------
def ssl_context() -> Any:
    """A verifying TLS context, with the host's CA store if ours is empty.

    Verification is never switched off. If no trust store can be found at
    all, the call fails the same way being offline does -- an answer nobody
    vouched for is not worth having, least of all one we act on.
    """
    import os
    import ssl

    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca"):
        return context                 # this interpreter has its own store
    for bundle in CA_BUNDLES:
        if not os.path.isfile(bundle):
            continue
        try:
            context.load_verify_locations(cafile=bundle)
            return context
        except (OSError, ssl.SSLError):
            continue
    for directory in CA_DIRS:
        if not os.path.isdir(directory):
            continue
        try:
            context.load_verify_locations(capath=directory)
            return context
        except (OSError, ssl.SSLError):
            continue
    return context


def _get(params: dict[str, str]) -> Any:
    """One API call. Raises `_Unreachable` instead of returning junk."""
    import urllib.parse
    import urllib.request

    url = API_URL + "?" + urllib.parse.urlencode(
        {**params, "format": "json", "formatversion": "2"})
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT,
                                    context=ssl_context()) as response:
            raw = response.read(MAX_BYTES)
    except Exception as exc:
        raise _Unreachable(str(exc)) from exc
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise _Unreachable("unreadable answer") from exc


def _normalise(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


def _same_game(title: str, name: str) -> bool:
    """Is `title` the article for `name`, or a decorated edition of it?

    Guard for the search fallback: "Portal" must not answer for "Portal 2",
    while "Cyberpunk 2077" should answer for "Cyberpunk 2077 Ultimate
    Edition". So a shorter title only wins when what follows it in the name
    is not a number -- numbers are how sequels are told apart.
    """
    short, full = _normalise(title), _normalise(name)
    if len(short) < 3 or not full:
        return False
    if short == full:
        return True
    return full.startswith(short) and not full[len(short):len(short) + 1]\
        .isdigit()


def _page_by_steam_appid(app_id: str) -> str | None:
    """The article that claims this Steam appid (their Cargo table)."""
    data = _get({"action": "cargoquery", "tables": "Infobox_game",
                 "fields": "Infobox_game._pageName=Page",
                 "where": f'Infobox_game.Steam_AppID HOLDS "{app_id}"',
                 "limit": "1"})
    rows = data.get("cargoquery") if isinstance(data, dict) else None
    for row in rows or []:
        page = (row or {}).get("title", {}).get("Page")
        if page:
            return str(page)
    return None


def _page_by_title(name: str) -> str | None:
    """An article of exactly this name (redirects followed)."""
    data = _get({"action": "query", "titles": name, "redirects": "1"})
    pages = (data.get("query", {}).get("pages")
             if isinstance(data, dict) else None)
    for page in pages or []:
        if not page.get("missing") and page.get("title"):
            return str(page["title"])
    return None


def _page_by_search(name: str) -> str | None:
    """Search, then refuse anything that is not clearly the same game."""
    data = _get({"action": "query", "list": "search", "srsearch": name,
                 "srlimit": "5"})
    hits = (data.get("query", {}).get("search")
            if isinstance(data, dict) else None)
    for hit in hits or []:
        title = str((hit or {}).get("title", ""))
        if title and _same_game(title, name):
            return title
    return None


def resolve_page(game: dict[str, Any]) -> str | None:
    """Which article describes this game?

    A Steam appid is an exact key and is asked first. Everything else goes
    by name, which is why the search step is guarded (`_same_game`) -- a
    wrong article would hand the user another game's save folders.
    """
    app_id = str(game.get("app_id", ""))
    if game.get("source") == "steam" and app_id.isdigit():
        page = _page_by_steam_appid(app_id)
        if page:
            return page
    name = str(game.get("game_name", "")).strip()
    if not name:
        return None
    return _page_by_title(name) or _page_by_search(name)


def fetch_wikitext(page: str) -> str:
    """The raw article source. Empty string when there is none."""
    data = _get({"action": "parse", "page": page, "prop": "wikitext",
                 "redirects": "1"})
    if not isinstance(data, dict):
        return ""
    return str(data.get("parse", {}).get("wikitext") or "")


def page_url(page: str) -> str:
    import urllib.parse
    return PAGE_URL.format(page=urllib.parse.quote(page.replace(" ", "_")))


# --- Cache ---------------------------------------------------------------
def cache_key(source: str, app_id: str) -> str:
    """One file per game. Hashed because a game id can be a whole path."""
    raw = f"{source}\0{app_id}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def load_cached(source: str, app_id: str,
                fresh_only: bool = True) -> dict[str, Any] | None:
    """The stored answer for this game, or None.

    `fresh_only=False` also hands back an expired one: for the launch path,
    where a month-old answer beats going online (which it must not do).
    """
    path = paths.pcgw_cache_file(cache_key(source, app_id))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    locations = [loc for loc in data.get("locations", [])
                 if isinstance(loc, dict)]
    if fresh_only:
        age = time.time() - float(data.get("at") or 0)
        if age > (HIT_TTL if locations else MISS_TTL):
            return None
    return {"ok": bool(locations), "page": data.get("page"),
            "url": data.get("url"), "locations": locations}


def store_cached(source: str, app_id: str, result: dict[str, Any]) -> None:
    """Remember an answer -- including "the wiki has nothing on this"."""
    path = paths.pcgw_cache_file(cache_key(source, app_id))
    payload = {"at": time.time(), "page": result.get("page"),
               "url": result.get("url"),
               "locations": result.get("locations", [])}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass          # a cache we cannot write is a slow app, not a broken one


def cached_locations(game: dict[str, Any]) -> list[dict[str, Any]]:
    """What we may act on for this game, from the cache alone.

    The entry point the launch hook and `redirect._register` use. It never
    goes online and never expires anything -- the alternative to a stale
    answer there is no answer at all -- but it does ask both gates of rule 4
    again, every time:

    * **Confirmed.** Only what the user accepted (`db.confirmed_locations`).
      An article nobody has looked at stays in the cache and goes nowhere.
    * **There.** Only folders that exist right now, spelled as on disk. This
      is what makes a yes given before the first launch useful without being
      a promise: the accepted locations are folded in on the first launch
      that actually creates them, and one the game never creates is never
      invented.
    """
    source = str(game.get("source", ""))
    app_id = str(game.get("app_id", ""))
    accepted = db.confirmed_locations(source, app_id)
    if not accepted:
        return []
    cached = load_cached(source, app_id, fresh_only=False)
    wanted = [dict(loc) for loc in (cached["locations"] if cached else [])
              if db.confirm_key(loc) in accepted]
    there, _not_there = on_disk(game, wanted)
    return there


# --- The lookup itself ---------------------------------------------------
def _result(reason: str, game_name: str, page: str | None = None,
            locations: list[dict[str, Any]] | None = None,
            cached: bool = False) -> dict[str, Any]:
    locations = locations or []
    messages = {
        "": _("{site} knows {n} storage location(s) for {game}.",
              site=SITE_NAME, n=len(locations), game=game_name),
        "disabled": _("Looking things up online is switched off."),
        "offline": _("Could not reach {site}.", site=SITE_NAME),
        "not-found": _("{site} has nothing about {game}.",
                       site=SITE_NAME, game=game_name),
        "failed": _("Could not read what {site} says about {game}.",
                    site=SITE_NAME, game=game_name),
    }
    return {"ok": not reason, "reason": reason, "page": page,
            "url": page_url(page) if page else None,
            "locations": locations, "cached": cached,
            "message": messages.get(reason, messages["failed"])}


def lookup(game: dict[str, Any], refresh: bool = False) -> dict[str, Any]:
    """What the wiki knows about one game. Never raises.

    Returns {ok, reason, page, url, locations, cached, message}, where
    `message` is ready to show and `reason` is "" on success or one of
    disabled / offline / not-found / failed.
    """
    source = str(game.get("source", ""))
    app_id = str(game.get("app_id", ""))
    name = str(game.get("game_name", "")) or app_id

    if not refresh:
        cached = load_cached(source, app_id)
        if cached is not None:
            return _result("" if cached["locations"] else "not-found", name,
                           page=cached.get("page"),
                           locations=cached["locations"], cached=True)
    if not enabled():
        return _result("disabled", name)

    try:
        page = resolve_page(game)
        wikitext = fetch_wikitext(page) if page else ""
        locations = parse_game_data(wikitext) if wikitext else []
    except _Unreachable:
        # Offline is not an error and is not cached -- the answer would be
        # about our network, not about the game.
        return _result("offline", name)
    except Exception:
        return _result("failed", name)

    result = _result("" if locations else "not-found", name, page=page,
                     locations=locations)
    store_cached(source, app_id, result)
    return result


def store(game: dict[str, Any],
          locations: list[dict[str, Any]]) -> str | None:
    """Write the locations into the prefix DB. Fingerprint, or None.

    None when the game has no prefix yet: the DB is keyed by it, and a game
    that was never started has nothing to key on. The answer stays in the
    cache and the launch hook picks it up the first time the game runs.

    Not a public entry point on its own -- everything that lands here has
    been through `confirm`, which is where the user's yes and the existence
    check are.
    """
    if not locations or not game.get("prefix_path") or not game.get(
            "user_dir"):
        return None
    return db.upsert_prefix({
        "source": game.get("source", "unknown"),
        "app_id": game.get("app_id", ""),
        "game_name": game.get("game_name", ""),
        "prefix_path": game["prefix_path"],
        "user_dir": game["user_dir"],
        "game_dir": game.get("game_dir"),
        "storage_locations": [dict(loc) for loc in locations],
    })


def confirm(game: dict[str, Any],
            locations: list[dict[str, Any]]) -> dict[str, Any]:
    """The user accepted these suggestions. Returns what came of it.

    `{added, waiting, stored}` -- the locations written into the DB (spelled
    as they are on disk), the accepted ones whose folder does not exist yet,
    and whether the DB write happened at all.

    Two separate things, and the split is the point. The yes is remembered
    for the whole list, in the config, keyed by the game (`db.
    confirm_locations`) -- it outlives the cache entry it was given about and
    no rescan can undo it. The DB, on the other hand, only ever gets the
    folders that are there. The rest stay a permission without an object: the
    first launch that creates one folds it in (`cached_locations`), and a
    folder the game never creates is never written anywhere.
    """
    there, not_there = on_disk(game, locations)
    db.confirm_locations(str(game.get("source", "")),
                         str(game.get("app_id", "")), locations)
    return {"added": there, "waiting": not_there,
            "stored": store(game, there) is not None}
