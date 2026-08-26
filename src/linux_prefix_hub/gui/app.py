# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""GTK4 / libadwaita front-end.

Sits on the same logic the CLI uses -- `base.iter_games()`, `adapter.connect()`
and `core.redirect` -- and adds nothing of its own beyond presentation. If a
behaviour is missing here, it belongs in `core/` or `adapters/`, not in a
widget callback.

Vocabulary rule (CLAUDE.md #6): the user reads "game folder", "game data",
"connect", "moved to". Never prefix, Wine or Proton -- and never "saves" for
the whole of it, because half of what we find is settings and logs.

Structure:
  LphApplication   -- Adw.Application, one window, optionally a tray icon
  MainWindow       -- header + game list, everything else is a dialog
  GameRow          -- one expander per game: connect switch + storage locations

The list is cut by launcher (`base.group_by_source`), one group per source in
the adapters' own order.

Anything that touches a disk goes through `tasks.run` so the window stays
responsive; widgets are only ever touched from the main loop.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from ..core import (  # noqa: E402
    db,
    desktop,
    newprefix,
    paths,
    redirect,
    registry,
)
from ..core.i18n import _  # noqa: E402
from . import tasks  # noqa: E402

APP_ID = paths.APP_ID


def esc(text: Any) -> str:
    """Escape text for a widget that parses Pango markup.

    Titles, subtitles, toasts and dialog headings all go through the markup
    parser. Real game names contain ampersands ("Command & Conquer"), and an
    unescaped one makes GTK reject the whole string -- the row then renders
    with no title at all. Escaping is unconditional on purpose: it needs no
    libadwaita version checks and cannot regress into an empty label.
    """
    return GLib.markup_escape_text(str(text))


def _copy(text: str) -> None:
    Gdk.Display.get_default().get_clipboard().set(text)


class GameRow(Adw.ExpanderRow):
    """One game: connect switch, plus a row per known save location."""

    def __init__(self, window: MainWindow, game: dict[str, Any],
                 hidden: bool = False) -> None:
        super().__init__()
        self._window = window
        self._game = game
        self._hidden = hidden
        self._syncing = False
        # Raw, unescaped -- get_title() would hand back the escaped form and
        # escaping that again shows "&amp;" to the user.
        self._name = str(game.get("game_name", "?"))

        self.set_title(esc(self._name))
        self.set_subtitle(esc(self._subtitle()))

        self._lookup = Gtk.Button(icon_name="system-search-symbolic",
                                  valign=Gtk.Align.CENTER)
        self._lookup.add_css_class("flat")
        self._lookup.set_tooltip_text(
            _("Look up where this game stores its data"))
        self._lookup.connect("clicked", self._on_lookup)
        self.add_suffix(self._lookup)

        self._hide = Gtk.Button(
            icon_name=("view-reveal-symbolic" if hidden
                       else "view-conceal-symbolic"),
            valign=Gtk.Align.CENTER)
        self._hide.add_css_class("flat")
        self._hide.set_tooltip_text(
            _("Show this game in the list again") if hidden
            else _("Hide this game from the list"))
        self._hide.connect("clicked", self._on_hide)
        self.add_suffix(self._hide)

        # Only for a folder this app made: everything else in this list is
        # somebody else's, and a launcher's game is theirs to remove.
        self._own = newprefix.owned(game.get("prefix_path"))
        if self._own is not None:
            delete = Gtk.Button(icon_name="user-trash-symbolic",
                                valign=Gtk.Align.CENTER)
            delete.add_css_class("flat")
            delete.set_tooltip_text(_("Delete this game folder"))
            delete.connect("clicked", self._on_delete)
            self.add_suffix(delete)

        self._switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._switch.set_tooltip_text(
            _("Let this game report where it stores its data"))
        self._sync_switch(bool(game.get("managed")))
        self._switch.connect("state-set", self._on_toggled)
        self.add_suffix(self._switch)

        self._fill_locations()

    # --- presentation ----------------------------------------------------
    def _subtitle(self) -> str:
        # No source here: the group this row sits in is already named after
        # it (`MainWindow._render`), and repeating it in every subtitle only
        # crowds out the part that differs.
        if not self._game.get("installed"):
            state = _("not fully installed yet")
        elif not self._game.get("prefix_path"):
            state = _("not started yet")
        else:
            state = _("ready")
        # Only ever drawn while "show hidden" is on, and then saying why this
        # row is here at all is the whole point.
        return _("hidden -- {state}", state=state) if self._hidden else state

    def _sync_switch(self, active: bool) -> None:
        self._syncing = True
        self._switch.set_active(active)
        self._switch.set_state(active)
        self._syncing = False

    def _entry(self) -> tuple[str, dict[str, Any]] | None:
        return db.find_prefix(str(self._game.get("source")),
                              str(self._game.get("app_id")))

    def _fill_locations(self) -> None:
        for row in list(self._rows()):
            self.remove(row)

        folder = self._game.get("prefix_path")
        own = self._own
        if own is not None:
            # One we made ourselves: the game gets installed *next to* the
            # Windows part, so that folder is the one worth showing, and
            # putting something in it is what this row is here for.
            self.add_row(GameFolderRow(self._window, str(own)))
            self.add_row(PlayRow(self._window, own))
            self.add_row(WindowsRow(self._window, own))
            self.add_row(WatchRow(self._window, own))
            self.add_row(ShortcutRow(self._window, own))
        elif folder:
            self.add_row(GameFolderRow(self._window, str(folder)))
        else:
            # Never started: there is nothing to move yet, only something to
            # ask for. See `PendingRow`.
            self.add_row(PendingRow(self._window, self._game))

        # How the game runs, before where it saves: it is a property of the
        # game itself and does not wait on anything being learned. Steam
        # games get it because Steam can be pointed at a build; every other
        # game folder gets it because it can have a build of its own, and a
        # folder we made because we start that one ourselves.
        other = newprefix.foreign(self._game)
        if str(self._game.get("source")) == "steam" or other:
            self.add_row(OptionsRow(self._window, self._game))
        if other:
            self.add_row(EngineRow(self._window, self._game))
            self.add_row(PrivateBuildRow(self._window, self._game))

        found = self._entry()
        locations = found[1].get("storage_locations", []) if found else []
        if not locations:
            hint = Adw.ActionRow(
                title=esc(_("No storage locations known yet")),
                subtitle=esc(_("Connect the game and play it once -- or use "
                               "the search button to look it up.")))
            hint.set_activatable(False)
            self.add_row(hint)
            return

        fingerprint, entry = found            # type: ignore[misc]
        seen: set[str] = set()
        for loc in locations:
            # Only a shell folder can be moved (the registry has no key for
            # anything else). The rest is shown read-only rather than hidden:
            # knowing *where* a game saves is the point, even when we cannot
            # touch it.
            root = (registry.shell_folder_root(str(loc.get("win_path", "")))
                    if loc.get("where") != "game_folder" else None)
            if root is None:
                self.add_row(FixedLocationRow(self._window, entry, loc))
                continue
            if root in seen:
                continue
            seen.add(root)
            self.add_row(LocationRow(self._window, fingerprint, entry,
                                     root, loc))

    def _rows(self) -> list[Gtk.Widget]:
        # Adw.ExpanderRow has no "get children"; track what we added.
        return getattr(self, "_added_rows", [])

    def add_row(self, row: Gtk.Widget) -> None:      # type: ignore[override]
        super().add_row(row)
        self._added_rows = self._rows() + [row]

    def remove(self, row: Gtk.Widget) -> None:       # type: ignore[override]
        super().remove(row)
        self._added_rows = [r for r in self._rows() if r is not row]

    # --- actions ---------------------------------------------------------
    def _on_toggled(self, _switch: Gtk.Switch, wanted: bool) -> bool:
        if self._syncing:
            return False
        self._switch.set_sensitive(False)
        source = str(self._game.get("source"))
        app_id = str(self._game.get("app_id"))

        def work() -> Any:
            from ..adapters import base
            adapter = base.get_adapter(source)
            action = adapter.connect if wanted else adapter.disconnect
            result = action(app_id)
            found = db.find_prefix(source, app_id)
            if found and result.ok:
                db.set_managed(found[0], wanted)
            return result

        def done(result: Any, error: Exception | None) -> None:
            self._switch.set_sensitive(True)
            if error is not None:
                self._sync_switch(not wanted)
                self._window.toast(_("Something went wrong: {error}",
                                     error=str(error)))
                return
            self._sync_switch(wanted if result.ok else not wanted)
            if result.manual:
                self._window.show_manual_step(self._name, result)
            else:
                self._window.toast(result.message)

        tasks.run(work, done)
        # We drive the visual state ourselves once the work is finished.
        return True

    def _on_hide(self, *_args: Any) -> None:
        """Take this game out of the list, or put it back.

        One small config key, no disk walk -- so it runs here rather than
        through `tasks.run`. The redraw is deferred to the next idle turn
        because it removes the group this very button sits in, and a widget
        that destroys itself from inside its own signal handler is a crash
        waiting for the wrong GTK version.
        """
        source = str(self._game.get("source"))
        app_id = str(self._game.get("app_id"))
        if self._hidden:
            db.unhide_game(source, app_id)
            self._window.toast(_("{game} is back in the list.",
                                 game=self._name))
        else:
            db.hide_game(source, app_id)
            self._window.toast(
                _("{game} is hidden. The eye in the header shows hidden "
                  "games.", game=self._name))
        GLib.idle_add(lambda: (self._window.refilter(), False)[1])

    def _on_delete(self, *_args: Any) -> None:
        """Ask first, and say what goes and what stays before asking.

        A game folder holds the install and everything saved inside it. What
        the user moved into their home folder is *not* in there and survives
        -- which is exactly the part they would worry about, so it is said
        here rather than found out afterwards.
        """
        folder = self._own
        if folder is None:
            return
        body = _("This deletes {path} and everything in it: the game, its "
                 "settings and anything saved inside it.", path=str(folder))
        for path in newprefix.moved_out(str(self._game.get("prefix_path"))):
            body += "\n\n" + _("Your moved game data stays in {path}.",
                                path=path)

        dialog = Adw.AlertDialog(
            heading=esc(_("Delete {name}?", name=self._name)),
            body=esc(body))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d: Any, response: str) -> None:
            if response == "delete":
                self._delete()

        dialog.connect("response", on_response)
        dialog.present(self._window)

    def _delete(self) -> None:
        folder = str(self._own)

        def work() -> Any:
            return newprefix.delete(folder)

        def done(result: Any, error: Exception | None) -> None:
            if error is not None:
                self._window.toast(_("Something went wrong: {error}",
                                     error=str(error)))
                return
            self._window.toast(result.message)
            self._window.reload()

        tasks.run(work, done)

    def _on_lookup(self, *_args: Any) -> None:
        """Ask PCGamingWiki where this game saves. Network, so off the loop."""
        self._lookup.set_sensitive(False)
        game = dict(self._game)

        def work() -> Any:
            from ..core import pcgw
            result = pcgw.lookup(game)
            # Checking the disk is disk work too, so it happens here rather
            # than in `done`, which runs on the main loop.
            result["here"], result["waiting"] = pcgw.on_disk(
                game, result["locations"])
            return result

        def done(result: Any, error: Exception | None) -> None:
            self._lookup.set_sensitive(True)
            if error is not None:
                self._window.toast(_("Something went wrong: {error}",
                                     error=str(error)))
                return
            if not result["locations"]:
                self._window.toast(str(result["message"]))
                return
            self._propose(result)

        tasks.run(work, done)

    def _propose(self, result: dict[str, Any]) -> None:
        """Show what the wiki suggests and ask before keeping any of it.

        A lookup is a proposal, not a finding: the article is written by
        people, it may describe a different edition, and what it says ends up
        in the very list the user then moves data around with. So it is shown
        first and stored on "Add" -- never on the way past.

        Folders that are not there are listed too, marked rather than hidden.
        "The wiki says this and your copy has not written it yet" is worth
        knowing, and it explains why fewer rows appear afterwards than lines
        were shown.
        """
        from ..core import pcgw
        lines = [_("{site} suggests these storage locations:",
                   site=pcgw.SITE_NAME), ""]
        lines += [str(loc.get("win_path")) for loc in result["here"]]
        lines += [_("{path} -- not there yet", path=str(loc.get("win_path")))
                  for loc in result["waiting"]]
        if result.get("url"):
            lines += ["", str(result["url"])]

        dialog = Adw.AlertDialog(heading=esc(self._name),
                                 body=esc("\n".join(lines)))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("add", _("Add"))
        dialog.set_response_appearance("add",
                                       Adw.ResponseAppearance.SUGGESTED)
        # Cancel is the default and the close response: a dialog dismissed
        # with Escape has not agreed to anything.
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d: Any, response: str) -> None:
            if response == "add":
                self._accept(list(result["locations"]))

        dialog.connect("response", on_response)
        dialog.present(self._window)

    def _accept(self, locations: list[dict[str, Any]]) -> None:
        """Keep what was proposed: config write plus DB write, off the loop."""
        game = dict(self._game)

        def work() -> Any:
            from ..core import pcgw
            return pcgw.confirm(game, locations)

        def done(outcome: Any, error: Exception | None) -> None:
            if error is not None:
                self._window.toast(_("Something went wrong: {error}",
                                     error=str(error)))
                return
            if outcome["stored"]:
                self._fill_locations()
                message = _("Added {n} storage location(s).",
                            n=len(outcome["added"]))
            else:
                # Nothing to key the DB on yet, or nothing that exists to
                # write into it -- the yes is kept either way.
                message = _("Start the game once -- then they show up here.")
            if outcome["waiting"]:
                message += " " + _("{n} of them do not exist yet.",
                                   n=len(outcome["waiting"]))
            self._window.toast(message)

        tasks.run(work, done)


def path_button(window: MainWindow, path: Any) -> Gtk.Button:
    """A button that shows one folder in the file manager.

    Insensitive when the folder is not there (yet) -- a button that does
    nothing when clicked is worse than one that is visibly unavailable.
    """
    button = Gtk.Button(icon_name="folder-open-symbolic",
                        valign=Gtk.Align.CENTER)
    button.add_css_class("flat")
    if path is None or not Path(path).is_dir():
        button.set_sensitive(False)
        button.set_tooltip_text(_("This folder does not exist (yet)"))
        return button

    button.set_tooltip_text(_("Open in the file manager"))

    def on_click(*_a: Any) -> None:
        if not desktop.open_folder(path):
            window.toast(_("Could not open {path}", path=str(path)))

    button.connect("clicked", on_click)
    return button


def open_button(window: MainWindow, entry: dict[str, Any],
                loc: dict[str, Any]) -> Gtk.Button:
    """The same button, for a storage location."""
    return path_button(window, redirect.location_path(entry, loc))


class GameFolderRow(Adw.ActionRow):
    """The folder the game itself lives in.

    Worth a row of its own: it is the only folder that exists before anything
    has been learned, and "where is this thing actually installed" is a
    question people ask long before they ask about saves. The path is
    selectable where libadwaita can do that, so it can be copied out.
    """

    def __init__(self, window: MainWindow, path: str) -> None:
        super().__init__()
        self.set_title(esc(_("Game folder")))
        self.set_subtitle(esc(path))
        if hasattr(self, "set_subtitle_selectable"):   # libadwaita 1.3+
            self.set_subtitle_selectable(True)
        self.add_suffix(path_button(window, path))
        self.set_activatable(False)


class PlayRow(Adw.ActionRow):
    """Start the game -- the only moment this app can watch one of these.

    Everywhere else a launcher starts the game with our hook in its config.
    A folder made here has no launcher, so a game started from the user's own
    desktop file is invisible however long they play it. Started from this
    button it gets the same two snapshots a Steam game gets
    (`newprefix.launch`).
    """

    def __init__(self, window: MainWindow, directory: Path) -> None:
        super().__init__()
        self._window = window
        self._dir = directory
        self.set_title(esc(_("Start the game")))
        self.set_activatable(False)

        program = newprefix.program_of(directory)
        self.set_subtitle(esc(
            str(program.name) if program is not None
            else _("Choose the program that starts it, once.")))

        if program is not None:
            change = Gtk.Button(icon_name="document-open-symbolic",
                                valign=Gtk.Align.CENTER)
            change.add_css_class("flat")
            change.set_tooltip_text(_("Choose a different program"))
            change.connect("clicked", self._on_choose)
            self.add_suffix(change)

            play = Gtk.Button(label=_("Start"), valign=Gtk.Align.CENTER)
            play.add_css_class("suggested-action")
            play.connect("clicked", self._on_play, None)
            self.add_suffix(play)
        else:
            choose = Gtk.Button(label=_("Choose..."),
                                valign=Gtk.Align.CENTER)
            choose.connect("clicked", self._on_choose)
            self.add_suffix(choose)

    def _on_choose(self, *_a: Any) -> None:
        if not hasattr(Gtk, "FileDialog"):             # GTK < 4.10
            self._window.toast(_("Start it with: {cmd}",
                                 cmd=f"{paths.APP_NAME} --play "
                                     f"{self._dir} --program GAME.EXE"))
            return
        dialog = Gtk.FileDialog(title=_("Choose the program that starts the "
                                        "game"))
        dialog.set_initial_folder(Gio.File.new_for_path(str(self._dir)))

        def on_done(source: Any, result: Any) -> None:
            try:
                chosen = source.open_finish(result)
            except GLib.Error:
                return                          # cancelled -- not an error
            if chosen and chosen.get_path():
                self._on_play(None, chosen.get_path())

        dialog.open(self._window, None, on_done)

    def _on_play(self, _button: Any, program: str | None) -> None:
        directory = str(self._dir)
        self._window.toast(_("Starting the game. What it stores is picked up "
                             "when it ends."))

        def work() -> Any:
            return newprefix.launch(directory, program)

        def done(result: Any, error: Exception | None) -> None:
            if error is not None:
                self._window.toast(_("Something went wrong: {error}",
                                     error=str(error)))
                return
            self._window.toast(result.message)
            self._window.reload()

        tasks.run(work, done)


class EngineRow(Adw.ActionRow):
    """Which Windows version this game folder is started with.

    Changeable after the fact and with no game installed yet: the build is
    not part of the folder, it is what gets pointed at it, and which one that
    is is exactly the thing people try one after another. For a folder
    somebody else made the choice only takes effect through a build of its
    own -- the row below this one.
    """

    def __init__(self, window: MainWindow, game: dict[str, Any]) -> None:
        super().__init__()
        self._window = window
        self._game = game
        self.set_title(esc(_("Windows version")))
        self.set_activatable(False)

        self._ids = [str(e["id"]) for e in newprefix.engines()]
        current = newprefix.engine_for(game)
        if current and current not in self._ids:
            # The build it was made with is gone. Shown anyway, because it is
            # what the folder says, and picking it up silently would hide
            # that the next start uses something else (`find_engine`).
            self._ids.insert(0, current)
        labels = [newprefix.engine_label(name) for name in self._ids]

        self._combo = Gtk.DropDown(model=Gtk.StringList.new(labels),
                                   valign=Gtk.Align.CENTER)
        self._combo.set_selected(self._ids.index(current)
                                 if current in self._ids else 0)
        self._combo.connect("notify::selected", self._on_pick)
        self.add_suffix(self._combo)
        self._say_runtime(current)

    def _say_runtime(self, engine: str) -> None:
        """Which runtime this build needs, when that can bite.

        It is what a launcher of the game's own gets wrong: one that always
        wraps the build in the same runtime cannot start a build that asks
        for a newer one, and the failure lands in that launcher's log as a
        Python traceback.
        """
        _appid, runtime = newprefix.required_runtime(engine)
        warning = newprefix.runtime_warning(engine)
        if warning:
            self.set_subtitle(esc(warning))
        elif runtime:
            self.set_subtitle(esc(_("Needs {runtime}", runtime=runtime)))
        else:
            self.set_subtitle("")

    def _on_pick(self, *_a: Any) -> None:
        index = int(self._combo.get_selected())
        if not 0 <= index < len(self._ids):
            return
        wanted = self._ids[index]
        if wanted == newprefix.engine_for(self._game):
            return
        result = newprefix.set_engine_for(self._game, wanted)
        self._window.toast(result.message)
        self._say_runtime(wanted)


class WatchRow(Adw.ActionRow):
    """A second folder that belongs to the same game.

    The game does not always live where its Windows part does -- a launcher
    of the game's own keeps the install wherever it likes, and games write
    saves next to themselves often enough that not looking there is how "it
    never notices anything" happens. Nobody can guess that folder.
    """

    def __init__(self, window: MainWindow, directory: Path) -> None:
        super().__init__()
        self._window = window
        self._dir = directory
        self.set_title(esc(_("Also watch the game's own folder")))
        self.set_activatable(False)
        self._sync()

        clear = Gtk.Button(icon_name="edit-clear-symbolic",
                           valign=Gtk.Align.CENTER)
        clear.add_css_class("flat")
        clear.set_tooltip_text(_("Stop watching it"))
        clear.connect("clicked", self._on_clear)
        self.add_suffix(clear)

        choose = Gtk.Button(label=_("Choose..."), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose)
        self.add_suffix(choose)

    def _sync(self) -> None:
        named = newprefix.watch_dir(self._dir)
        self.set_subtitle(esc(
            str(named) if named is not None
            else _("Where the game itself is installed, if that is somewhere "
                   "else.")))

    def _apply(self, path: str | None) -> None:
        result = newprefix.set_watch_dir(self._dir, path)
        self._window.toast(result.message)
        self._sync()

    def _on_clear(self, *_a: Any) -> None:
        self._apply(None)

    def _on_choose(self, *_a: Any) -> None:
        if not hasattr(Gtk, "FileDialog"):             # GTK < 4.10
            self._window.toast(_("Set it with: {cmd}",
                                 cmd=f"{paths.APP_NAME} --watch-folder "
                                     f"{self._dir} --target PATH"))
            return
        start = newprefix.watch_dir(self._dir) or self._dir
        choose_folder(self._window, _("Choose the folder the game is "
                                      "installed in"), start, self._apply)


class ShortcutRow(Adw.ActionRow):
    """Start the game without going through this window.

    Starting from here keeps the window busy for the whole session and loses
    what was learned if it is closed. The entry runs `--play`, so a game
    started from the desktop is watched exactly the same way.
    """

    def __init__(self, window: MainWindow, directory: Path) -> None:
        super().__init__()
        self._window = window
        self._dir = directory
        self._syncing = False
        self.set_title(esc(_("In your application menu")))
        self.set_subtitle(esc(_("Starts the game from your desktop, and "
                                "still picks up what it stores.")))
        self.set_activatable(False)

        self._switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._sync_switch(newprefix.shortcut_file(directory).exists())
        self._switch.connect("state-set", self._on_toggled)
        self.add_suffix(self._switch)

    def _sync_switch(self, active: bool) -> None:
        self._syncing = True
        self._switch.set_active(active)
        self._switch.set_state(active)
        self._syncing = False

    def _on_toggled(self, _switch: Gtk.Switch, wanted: bool) -> bool:
        if self._syncing:
            return False
        result = (newprefix.make_shortcut(self._dir) if wanted
                  else newprefix.drop_shortcut(self._dir))
        self._window.toast(result.message)
        self._sync_switch(wanted if result.ok else not wanted)
        return True


class PrivateBuildRow(Adw.ActionRow):
    """A copy of the Windows version that belongs to this folder alone.

    Made of hardlinks, so it costs no disk space. Two things it buys: the
    version stops moving under the folder, and -- the reason it is offered
    at all -- the extra options reach the game even when something else
    starts it. A build reads its own settings file from inside the
    container; a launcher of the game's own does not ask us, but it can be
    pointed at this copy.
    """

    def __init__(self, window: MainWindow, game: dict[str, Any]) -> None:
        super().__init__()
        self._window = window
        self._game = game
        self._syncing = False
        self.set_title(esc(_("Own Windows version for this folder")))
        self.set_activatable(False)

        copy = newprefix.private_build_for(game)
        self.set_subtitle(esc(
            str(copy) if copy is not None
            else _("A copy of the version above, for this game only. Costs "
                   "no disk space.")))
        if hasattr(self, "set_subtitle_selectable"):
            self.set_subtitle_selectable(True)

        self._switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._sync_switch(copy is not None)
        self._switch.connect("state-set", self._on_toggled)
        self.add_suffix(self._switch)

    def _sync_switch(self, active: bool) -> None:
        self._syncing = True
        self._switch.set_active(active)
        self._switch.set_state(active)
        self._syncing = False

    def _on_toggled(self, _switch: Gtk.Switch, wanted: bool) -> bool:
        if self._syncing:
            return False
        self._switch.set_sensitive(False)
        game = dict(self._game)

        def work() -> Any:
            return (newprefix.make_private_for(game) if wanted
                    else newprefix.drop_private_for(game))

        def done(result: Any, error: Exception | None) -> None:
            self._switch.set_sensitive(True)
            if error is not None:
                self._sync_switch(not wanted)
                self._window.toast(_("Something went wrong: {error}",
                                     error=str(error)))
                return
            self._sync_switch(wanted if result.ok else not wanted)
            self._window.toast(result.message)
            self._window.reload()

        tasks.run(work, done)
        return True


class WindowsRow(Adw.ActionRow):
    """Put a program into a game folder we made ourselves.

    Only appears on those (`newprefix.owned`): for any other game folder we
    do not know which Windows version made it, and starting a program with
    the wrong one is how a working setup stops working.
    """

    def __init__(self, window: MainWindow, directory: Path) -> None:
        super().__init__()
        self._window = window
        self._dir = directory
        self.set_title(esc(_("Install a program")))
        self.set_subtitle(esc(_("An installer or a game you downloaded "
                                "yourself.")))
        self.set_activatable(False)

        config = Gtk.Button(icon_name="emblem-system-symbolic",
                            valign=Gtk.Align.CENTER)
        config.add_css_class("flat")
        config.set_tooltip_text(_("Windows settings for this folder"))
        config.connect("clicked", self._on_settings)
        self.add_suffix(config)

        choose = Gtk.Button(label=_("Choose..."), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose)
        self.add_suffix(choose)

    def _on_choose(self, *_a: Any) -> None:
        if not hasattr(Gtk, "FileDialog"):            # GTK < 4.10
            self._window.toast(_("Start it with: {cmd}",
                                 cmd=f"{paths.APP_NAME} --run-in "
                                     f"{self._dir} --program SETUP.EXE"))
            return
        dialog = Gtk.FileDialog(title=_("Choose the program to install"))
        dialog.set_initial_folder(Gio.File.new_for_path(str(Path.home())))

        def on_done(source: Any, result: Any) -> None:
            try:
                chosen = source.open_finish(result)
            except GLib.Error:
                return                        # cancelled -- not an error
            if chosen and chosen.get_path():
                self._run(chosen.get_path())

        dialog.open(self._window, None, on_done)

    def _run(self, program: str) -> None:
        directory = str(self._dir)
        self._window.toast(_("Starting {name}...",
                             name=Path(program).name))

        def work() -> Any:
            return newprefix.install(directory, program)

        def done(result: Any, error: Exception | None) -> None:
            if error is not None:
                self._window.toast(_("Something went wrong: {error}",
                                     error=str(error)))
                return
            self._window.toast(result.message)
            # An installer that put a game somewhere is a change to the
            # folder, and the row above it says what is in there.
            self._window.reload()

        tasks.run(work, done)

    def _on_settings(self, *_a: Any) -> None:
        directory = str(self._dir)

        def work() -> Any:
            return newprefix.settings(directory)

        def done(result: Any, error: Exception | None) -> None:
            if error is not None:
                self._window.toast(_("Something went wrong: {error}",
                                     error=str(error)))
                return
            if not result.ok:
                self._window.toast(result.message)

        tasks.run(work, done)


class PendingRow(Adw.ActionRow):
    """Ask for a move before there is anything to move.

    A game nobody has started has no folder yet: no registry to point
    somewhere else, no directory to replace with a link. The wish is the only
    thing that can exist this early -- so it is what gets stored, and the
    watcher carries it out once the game has run and let go again
    (`core/redirect.apply_pending`).

    Deliberately the same switch as `LocationRow`, in the same place, saying
    the same thing: from where the user sits this is one decision, and only
    the moment it can be acted on differs.
    """

    def __init__(self, window: MainWindow, game: dict[str, Any]) -> None:
        super().__init__()
        self._window = window
        self._game = game
        self._syncing = False

        self.set_title(esc(_("Keep this game's data in your home folder")))
        self.set_subtitle(esc(_("as soon as you have played it once")))

        self._switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._sync_switch(redirect.is_requested(game))
        self._switch.connect("state-set", self._on_toggled)
        self.add_suffix(self._switch)
        self.set_activatable(False)

    def _sync_switch(self, active: bool) -> None:
        self._syncing = True
        self._switch.set_active(active)
        self._switch.set_state(active)
        self._syncing = False

    def _on_toggled(self, _switch: Gtk.Switch, wanted: bool) -> bool:
        if self._syncing:
            return False
        # Writing one small JSON key -- no disk walk, nothing to move yet.
        if wanted:
            self._window.toast(redirect.request(self._game).message)
        else:
            redirect.cancel_request(self._game)
            self._window.toast(_("{game} will be left where it is.",
                                 game=self._game.get("game_name", "")))
        self._sync_switch(wanted)
        return True


class FixedLocationRow(Adw.ActionRow):
    """A location we can show but not move.

    Two cases land here: the game writes into its own install folder (no
    registry key exists for that, and symlinking it fights the launcher's
    updater), or it writes somewhere in the prefix that is not a shell folder.
    Both are worth showing -- "where does this game save?" is the question the
    app exists to answer.
    """

    def __init__(self, window: MainWindow, entry: dict[str, Any],
                 loc: dict[str, Any]) -> None:
        super().__init__()
        win_path = str(loc.get("win_path", ""))
        in_game_folder = loc.get("where") == "game_folder"

        self.set_title(esc(_("Game data in {folder}",
                             folder=win_path.split("/")[-1] or win_path)))
        self.set_subtitle(esc(
            _("in the game's own folder -- cannot be moved") if in_game_folder
            else _("outside the folders we can move")))
        self.add_suffix(open_button(window, entry, loc))
        self.set_activatable(False)


class LocationRow(Adw.ActionRow):
    """One save location, with a switch that moves it into the home folder."""

    def __init__(self, window: MainWindow, fingerprint: str,
                 entry: dict[str, Any], root: str,
                 loc: dict[str, Any]) -> None:
        super().__init__()
        self._window = window
        self._fingerprint = fingerprint
        self._entry = entry
        self._root = root
        self._syncing = False

        self.set_title(esc(_("Game data in {folder}", folder=root)))
        self.set_subtitle(esc(self._where(loc)))

        self.add_suffix(open_button(window, entry, loc))

        self._switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._switch.set_tooltip_text(_("Keep this data in your home "
                                        "folder"))
        redirected = bool(loc.get("redirected"))
        self._sync_switch(redirected)
        self._switch.connect("state-set", self._on_toggled)
        self.add_suffix(self._switch)
        self.set_activatable(False)

    def _where(self, loc: dict[str, Any]) -> str:
        if loc.get("redirected") and loc.get("redirect_target"):
            return _("moved to {target}",
                     target=str(loc.get("redirect_target")))
        return _("in the game folder")

    def _sync_switch(self, active: bool) -> None:
        self._syncing = True
        self._switch.set_active(active)
        self._switch.set_state(active)
        self._syncing = False

    def _on_toggled(self, _switch: Gtk.Switch, wanted: bool) -> bool:
        if self._syncing:
            return False
        self._switch.set_sensitive(False)
        if not wanted:
            self._move(False)
            return True

        # Moving in: ask first whether anything else writes here. Reading a
        # launcher's cloud bookkeeping is a few small files, but it is still
        # disk, so it goes off the main loop like everything else.
        entry, root = self._entry, self._root

        def work() -> Any:
            return redirect.cloud_warning(entry, root)

        def done(warning: Any, error: Exception | None) -> None:
            if error is not None or not warning:
                self._move(True)
                return
            self._switch.set_sensitive(True)
            self._confirm(warning)

        tasks.run(work, done)
        return True

    def _confirm(self, warning: tuple[str, str]) -> None:
        """A second writer on this folder -- let the user decide, not us.

        Cancel is the default response: someone who is not sure should end up
        where they started, and where they started is the arrangement their
        launcher already knows how to keep.
        """
        dialog = Adw.AlertDialog(heading=esc(warning[0]),
                                 body=esc(warning[1]))
        dialog.add_response("cancel", _("Leave it"))
        dialog.add_response("move", _("Move it anyway"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d: Any, response: str) -> None:
            if response == "move":
                self._switch.set_sensitive(False)
                self._move(True)
            else:
                self._sync_switch(False)

        dialog.connect("response", on_response)
        dialog.present(self._window)

    def _move(self, wanted: bool) -> None:
        """Do it: into the home folder, or back into the game folder."""
        fingerprint, root = self._fingerprint, self._root

        def work() -> Any:
            if wanted:
                return redirect.redirect(fingerprint, root)
            return redirect.undo(fingerprint, root)

        def done(result: Any, error: Exception | None) -> None:
            self._switch.set_sensitive(True)
            if error is not None:
                self._sync_switch(not wanted)
                self._window.toast(_("Something went wrong: {error}",
                                     error=str(error)))
                return
            self._sync_switch(wanted if result.ok else not wanted)
            self._window.toast(result.message)
            if result.ok:
                self._window.reload()

        tasks.run(work, done)


def choose_folder(window: MainWindow, title: str, start: Any,
                  on_folder: Any) -> None:
    """Ask for a folder. Only ever called where `Gtk.FileDialog` exists."""
    dialog = Gtk.FileDialog(title=title)
    dialog.set_initial_folder(Gio.File.new_for_path(str(start)))

    def on_done(source: Any, result: Any) -> None:
        try:
            folder = source.select_folder_finish(result)
        except GLib.Error:
            return                                # cancelled -- not an error
        if folder and folder.get_path():
            on_folder(folder.get_path())

    dialog.select_folder(window, None, on_done)


class SettingsDialog:
    """Everything about the app itself: where moved game data is kept, the
    two switches, and removing it again. Presented on the window.

    `Adw.PreferencesDialog` is libadwaita 1.5+; older systems get the window
    flavour. The app runs on whatever GTK the host has (see
    `__main__._reexec_gui`), so neither can be assumed.
    """

    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self._dialog = (Adw.PreferencesDialog()
                        if hasattr(Adw, "PreferencesDialog")
                        else Adw.PreferencesWindow())

        page = Adw.PreferencesPage(title=_("Settings"))
        group = Adw.PreferencesGroup(
            title=_("Where game data is kept"),
            description=_("When you move a game's data out of the game "
                          "folder, they land here, one folder per game. "
                          "Folders you already moved stay where they are."))

        self._row = Adw.ActionRow(title=esc(_("Data folder")))
        self._row.set_subtitle(esc(str(db.redirect_root())))

        choose = Gtk.Button(label=_("Choose..."), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose)
        self._row.add_suffix(choose)

        reset = Gtk.Button(icon_name="edit-undo-symbolic",
                           valign=Gtk.Align.CENTER)
        reset.add_css_class("flat")
        reset.set_tooltip_text(_("Back to the default folder"))
        reset.connect("clicked", self._on_reset)
        self._row.add_suffix(reset)

        group.add(self._row)
        self._offer_old_data(group)
        page.add(group)
        page.add(self._new_folder_group())
        page.add(self._online_group())
        page.add(self._tray_group())
        page.add(self._remove_group())
        self._dialog.add(page)

    def _offer_old_data(self, group: Adw.PreferencesGroup) -> None:
        """Data an earlier version left in the folder we used to use.

        Shown where the folder it would move into is named, and only while
        there is something to move -- a permanent row for a one-off tidy-up
        is one more thing to explain. Nothing happens without the button:
        moving somebody's saves is not a side effect of opening Settings.
        """
        from ..core import redirect
        waiting = redirect.stale_targets()
        if not waiting:
            return
        self._old_row = Adw.ActionRow(
            title=esc(_("{n} game(s) still in the old folder",
                        n=len(waiting))),
            subtitle=esc(", ".join(sorted({str(item["game_name"])
                                           for item in waiting}))))
        button = Gtk.Button(label=_("Move them"), valign=Gtk.Align.CENTER)
        button.connect("clicked", self._on_move_old)
        self._old_row.add_suffix(button)
        self._old_row.set_activatable(False)
        group.add(self._old_row)

    def _on_move_old(self, button: Gtk.Button) -> None:
        button.set_sensitive(False)

        def work() -> Any:
            from ..core import redirect
            return redirect.move_stale()

        def done(results: Any, error: Exception | None) -> None:
            if error is not None:
                button.set_sensitive(True)
                self._window.toast(_("Something went wrong: {error}",
                                     error=str(error)))
                return
            failed = [r for r in results or [] if not r.ok]
            if failed:
                button.set_sensitive(True)
                self._window.toast(failed[0].message)
                return
            self._old_row.set_visible(False)
            self._window.toast(_("Moved into {path}.",
                                 path=str(db.redirect_root())))
            self._window.reload()

        tasks.run(work, done)

    def _new_folder_group(self) -> Adw.PreferencesGroup:
        """Where a game folder of your own is made, unless you say otherwise.

        A second folder setting next to the first one, and not the same one:
        moved game data is small and belongs near the user, a game folder
        holds the whole install and is the thing that has to go on the disk
        with room on it.
        """
        group = Adw.PreferencesGroup(
            title=_("Where new game folders are made"),
            description=_("A game folder you set up yourself is made here, "
                          "and the game is installed into it -- so this "
                          "wants to be a disk with room on it. Folders you "
                          "already made stay where they are."))
        self._root_row = Adw.ActionRow(title=esc(_("Games folder")))
        self._root_row.set_subtitle(esc(str(newprefix.root())))
        if hasattr(self._root_row, "set_subtitle_selectable"):
            self._root_row.set_subtitle_selectable(True)

        choose = Gtk.Button(label=_("Choose..."), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose_root)
        self._root_row.add_suffix(choose)

        reset = Gtk.Button(icon_name="edit-undo-symbolic",
                           valign=Gtk.Align.CENTER)
        reset.add_css_class("flat")
        reset.set_tooltip_text(_("Back to the default folder"))
        reset.connect("clicked", self._on_reset_root)
        self._root_row.add_suffix(reset)
        group.add(self._root_row)
        return group

    def _apply_root(self, path: str | None) -> None:
        where = newprefix.set_root(path)
        self._root_row.set_subtitle(esc(str(where)))
        self._window.toast(_("New game folders are made in {path}.",
                             path=str(where)))

    def _on_reset_root(self, *_a: Any) -> None:
        self._apply_root(None)

    def _on_choose_root(self, *_a: Any) -> None:
        if not hasattr(Gtk, "FileDialog"):        # GTK < 4.10
            self._window.toast(_("Set it with: {cmd}",
                                 cmd=f"{paths.APP_NAME} --set-game-root "
                                     f"PATH"))
            return
        choose_folder(self._window, _("Choose where new game folders are "
                                      "made"), newprefix.root(),
                      self._apply_root)

    def _tray_group(self) -> Adw.PreferencesGroup:
        """Whether closing the window ends the app or only hides it."""
        group = Adw.PreferencesGroup(
            title=_("When you close the window"),
            description=_("Keeps {app} in the system tray, so it stays one "
                          "click away and can tell you about new games and "
                          "updates. Takes effect the next time you start it.",
                          app=paths.APP_TITLE))
        row = Adw.ActionRow(title=esc(_("Keep running in the background")))
        switch = Gtk.Switch(valign=Gtk.Align.CENTER,
                            active=db.background_tray())
        switch.connect("state-set", self._on_tray)
        row.add_suffix(switch)
        row.set_activatable(False)
        group.add(row)
        return group

    def _on_tray(self, _switch: Gtk.Switch, wanted: bool) -> bool:
        db.set_config("background_tray", bool(wanted))
        return False              # let the switch draw the new state itself

    def _online_group(self) -> Adw.PreferencesGroup:
        """The one switch that keeps the app entirely offline."""
        from ..core import pcgw
        group = Adw.PreferencesGroup(
            title=_("Finding storage locations"),
            description=_("Looking a game up asks PCGamingWiki where it "
                          "keeps its data, so you do not have to play it "
                          "first. It only happens when you ask for it."))
        row = Adw.ActionRow(title=esc(_("Allow looking games up online")))
        switch = Gtk.Switch(valign=Gtk.Align.CENTER,
                            active=pcgw.enabled())
        switch.connect("state-set", self._on_online)
        row.add_suffix(switch)
        row.set_activatable(False)
        group.add(row)
        return group

    def _on_online(self, _switch: Gtk.Switch, wanted: bool) -> bool:
        db.set_config("online_lookup", bool(wanted))
        return False              # let the switch draw the new state itself

    def _remove_group(self) -> Adw.PreferencesGroup:
        """Taking the app back off the machine. Last on the page.

        Here rather than in the header menu: it is the one thing in this app
        that no switch flicks back, and this is the page a user opens when
        they are done with it. The description says what it does *before*
        the button, because the first dialog it opens is already the plan.
        """
        group = Adw.PreferencesGroup(
            title=_("Remove {app}", app=paths.APP_TITLE),
            description=_("Every folder that was moved goes back into its "
                          "game and every game is disconnected again, then "
                          "the app removes itself. You are shown what that "
                          "means before anything happens."))
        row = Adw.ActionRow(
            title=esc(_("Move everything back, then remove the app")))
        button = Gtk.Button(label=_("Remove..."), valign=Gtk.Align.CENTER)
        button.add_css_class("destructive-action")
        button.connect("clicked", self._on_remove)
        row.add_suffix(button)
        row.set_activatable(False)
        group.add(row)
        return group

    def _on_remove(self, *_a: Any) -> None:
        """Hand the question to the window and get out of its way.

        Everything after this belongs to `LphApplication._on_uninstall`,
        which asks `uninstall.plan()` first and then puts its dialogs on the
        window -- underneath this one, if it were still open.
        """
        app = self._window.get_application()
        self._dialog.close()
        if app is not None:
            app.activate_action("uninstall", None)

    def present(self) -> None:
        if hasattr(self._dialog, "present"):
            try:
                self._dialog.present(self._window)
                return
            except TypeError:            # PreferencesWindow.present() takes 0
                pass
        self._dialog.set_transient_for(self._window)
        self._dialog.set_modal(True)
        self._dialog.show()

    # --- actions ---------------------------------------------------------
    def _apply(self, path: str | None) -> None:
        """None resets to the default."""
        db.set_config("redirect_root", path)
        self._row.set_subtitle(esc(str(db.redirect_root())))
        self._window.toast(_("Game data will be kept in {path}.",
                             path=str(db.redirect_root())))

    def _on_reset(self, *_a: Any) -> None:
        self._apply(None)

    def _on_choose(self, *_a: Any) -> None:
        if not hasattr(Gtk, "FileDialog"):        # GTK < 4.10
            self._window.toast(_("Set it with: {cmd}",
                                 cmd=f"{paths.APP_NAME} --set-data-folder "
                                     f"PATH"))
            return
        choose_folder(self._window, _("Choose the data folder"),
                      db.redirect_root().parent, self._apply)


class OptionsRow(Adw.ActionRow):
    """How this game runs, as opposed to where it keeps things.

    Two kinds of game get this row, for opposite reasons. A Steam game gets
    a compatibility build of its own to carry the settings, because Steam
    starts it inside a container that filters the environment. A folder this
    app made needs none of that: we start that game ourselves, so the
    profile is simply the environment we start it with. Lutris and Heroic
    will set the same variables their own way and read the same profile.
    """

    def __init__(self, window: MainWindow, game: dict[str, Any]) -> None:
        super().__init__()
        self._window = window
        self._game = game
        self._syncing = False

        self.set_title(esc(_("Extra options")))

        settings = Gtk.Button(icon_name="emblem-system-symbolic",
                              valign=Gtk.Align.CENTER)
        settings.add_css_class("flat")
        settings.set_tooltip_text(_("Choose what to turn on"))
        settings.connect("clicked", self._on_settings)
        self.add_suffix(settings)

        self._switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._switch.set_tooltip_text(_("Use these options for this game"))
        self._switch.connect("state-set", self._on_toggled)
        self.add_suffix(self._switch)
        self.set_activatable(False)
        self.refresh()

    # --- presentation ----------------------------------------------------
    def refresh(self) -> None:
        """Read the profile back and draw what it now says."""
        from ..core import gameopts
        profile = gameopts.read(str(self._game.get("source")),
                                str(self._game.get("app_id")))
        self._sync_switch(bool(profile.get("enabled")))
        self.set_subtitle(esc(self._summary(profile)))

    def _summary(self, profile: dict[str, Any]) -> str:
        from ..core import gameopts
        if not profile.get("enabled"):
            return _("off")
        chosen = [gameopts.switch_label(name)
                  for name in profile.get("switches", [])]
        own = gameopts.parse_custom(str(profile.get("custom") or ""))
        if own:
            chosen.append(_("{n} of your own", n=len(own)))
        summary = ", ".join(chosen) if chosen else _("on, nothing chosen yet")
        # Said, never acted on: a newer build is an offer, and the one that
        # is running now goes on working either way.
        if gameopts.outdated(profile):
            summary += " -- " + _("a newer version is available")
        return summary

    def _sync_switch(self, active: bool) -> None:
        self._syncing = True
        self._switch.set_active(active)
        self._switch.set_state(active)
        self._syncing = False

    # --- actions ---------------------------------------------------------
    def _on_settings(self, *_a: Any) -> None:
        OptionsDialog(self._window, self._game, self).present()

    def _on_toggled(self, _switch: Gtk.Switch, wanted: bool) -> bool:
        if self._syncing:
            return False
        self._switch.set_sensitive(False)
        self.apply(wanted)
        return True

    def apply(self, wanted: bool) -> None:
        """Build it and point Steam at it, or take both back away.

        Copying a compatibility build and editing Steam's settings are both
        disk, so both go off the main loop like everything else here.
        """
        game = dict(self._game)
        name = str(self._game.get("game_name", ""))

        def work() -> Any:
            from ..core import gameopts
            return (gameopts.turn_on(game) if wanted
                    else gameopts.turn_off(game))

        def done(result: Any, error: Exception | None) -> None:
            self._switch.set_sensitive(True)
            if error is not None:
                self._window.toast(_("Something went wrong: {error}",
                                     error=str(error)))
            elif result.get("manual"):
                # Steam is open, so the last step is the user's. The window
                # already knows how to show one of those.
                self._window.show_manual_step(name, result)
            else:
                self._window.toast(result.message)
            self.refresh()

        tasks.run(work, done)


class OptionsDialog:
    """What the extra options for one game are.

    Built like `SettingsDialog`: every choice is stored the moment it is
    made, and there is no OK button for that. What the page does *not* do by
    itself is put the choices to work -- that is the Apply row at the end,
    because it copies a build and edits Steam's own settings, and neither
    should happen while somebody is still making up their mind.
    """

    def __init__(self, window: MainWindow, game: dict[str, Any],
                 row: OptionsRow) -> None:
        from ..core import gameopts
        self._window = window
        self._game = game
        self._row = row
        self._source = str(game.get("source"))
        self._app_id = str(game.get("app_id"))
        self._profile = gameopts.read(self._source, self._app_id)
        self._own = gameopts.own_folder(game)
        self._folder = newprefix.owned(game.get("prefix_path"))

        self._dialog = (Adw.PreferencesDialog()
                        if hasattr(Adw, "PreferencesDialog")
                        else Adw.PreferencesWindow())
        page = Adw.PreferencesPage(title=_("Extra options"))
        # Built in this order because `_store` reads the text box, and the
        # switches above it can be flicked as soon as the page is up.
        self._custom = self._custom_group()
        if self._own:
            page.add(self._name_group())
        page.add(self._switch_group())
        page.add(self._custom)
        # Which build to copy is a question only Steam's copy has. Every
        # other game folder picks its version in its own row (`EngineRow`),
        # and a second control for it would be a second answer.
        if self._source == "steam":
            page.add(self._version_group())
        page.add(self._apply_group())
        self._dialog.add(page)

    # --- the page --------------------------------------------------------
    def _switch_group(self) -> Adw.PreferencesGroup:
        from ..core import gameopts
        group = Adw.PreferencesGroup(
            title=esc(str(self._game.get("game_name", ""))),
            description=_("These apply to this one thing and nothing else."))
        chosen = set(self._profile.get("switches", []))
        for name in gameopts.SWITCHES:
            row = Adw.ActionRow(title=esc(gameopts.switch_label(name)),
                                subtitle=esc(gameopts.switch_hint(name)))
            switch = Gtk.Switch(valign=Gtk.Align.CENTER,
                                active=name in chosen)
            switch.connect("state-set", self._on_switch, name)
            row.add_suffix(switch)
            row.set_activatable(False)
            group.add(row)
        return group

    def _name_group(self) -> Adw.PreferencesGroup:
        """What we call it here, as opposed to what it is called on disk.

        Only the title is editable. The short name underneath it is in
        somebody else's configuration by now -- whatever launcher this
        environment was made for points straight at that folder -- so it is
        shown and not offered for editing.
        """
        from ..core import gameopts
        group = Adw.PreferencesGroup(
            title=_("Name"),
            description=_("The short name is what the folder is called on "
                          "disk, and it stays -- other programs point at it. "
                          "This is only what it is called here."))
        directory = gameopts.find_instance(self._source, self._app_id)
        self._name = Adw.EntryRow(title=esc(_("Name")))
        self._name.set_text(
            str(newprefix.display_name(self._game.get("prefix_path")))
            if self._folder is not None
            else str(self._profile.get("title") or self._app_id))
        self._name.connect("apply", self._on_rename)
        self._name.set_show_apply_button(True)
        group.add(self._name)

        if self._folder is not None:
            short_name = self._folder.name
        elif directory is not None:
            short_name = directory.name
        else:
            short_name = f"{gameopts.PREFIX}-{self._app_id}"
        short = Adw.ActionRow(title=esc(_("Short name")),
                              subtitle=esc(short_name))
        if hasattr(short, "set_subtitle_selectable"):
            short.set_subtitle_selectable(True)
        short.set_activatable(False)
        group.add(short)
        return group

    def _on_rename(self, entry: Adw.EntryRow) -> None:
        from ..core import gameopts
        if self._folder is not None:
            # Ours keeps its name in its own marker, where the scan reads it.
            result = newprefix.rename(self._folder, entry.get_text())
        else:
            result = gameopts.rename(self._source, self._app_id,
                                     entry.get_text())
            if result.ok:
                self._profile["title"] = str(result["title"])
        self._window.toast(result.message)
        self._row.refresh()
        self._window.reload()

    def _custom_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("Your own settings"),
            description=_("One NAME=value per line. Lines starting with # "
                          "are ignored. These win over the switches above."))
        frame = Gtk.Frame()
        self._text = Gtk.TextView(top_margin=6, bottom_margin=6,
                                  left_margin=6, right_margin=6)
        self._text.set_monospace(True)
        self._text.set_size_request(-1, 120)
        self._text.get_buffer().set_text(
            str(self._profile.get("custom") or ""))
        frame.set_child(self._text)
        group.add(frame)
        return group

    def _version_group(self) -> Adw.PreferencesGroup:
        """Follow the newest, or hold one still.

        `Adw.ComboRow` rather than a bare `Gtk.DropDown`: it is in
        libadwaita 1.0, and this app runs on whatever GTK the host has.
        """
        from ..core import gameopts
        group = Adw.PreferencesGroup(
            title=_("Which version to use"),
            description=_("Following the newest keeps up with updates by "
                          "itself. A fixed one never changes under you."))
        self._bases = [gameopts.DEFAULT_FAMILY] + gameopts.list_bases()
        labels = [_("Newest available")] + self._bases[1:]
        current = str(self._profile.get("base") or gameopts.DEFAULT_FAMILY)

        self._combo = Adw.ComboRow(title=esc(_("Version")),
                                   model=Gtk.StringList.new(labels))
        self._combo.set_selected(self._bases.index(current)
                                 if current in self._bases else 0)
        self._combo.connect("notify::selected", self._on_base)
        group.add(self._combo)
        return group

    def _apply_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            description=_("They apply the next time you start the game from "
                          "here.") if self._own
            else _("Steam has to be closed for this, and picks the "
                   "change up the next time it starts."))
        row = Adw.ActionRow(
            title=esc(_("Use these options for this game")))
        button = Gtk.Button(label=_("Apply"), valign=Gtk.Align.CENTER)
        button.add_css_class("suggested-action")
        button.connect("clicked", self._on_apply)
        row.add_suffix(button)
        row.set_activatable(False)
        group.add(row)
        return group

    # --- actions ---------------------------------------------------------
    def _store(self) -> None:
        """Keep what the page currently says. One small JSON key, no walk.

        The text box is read here rather than on every keystroke: storing a
        character at a time would rewrite the config file per key pressed.
        """
        from ..core import gameopts
        buffer = self._text.get_buffer()
        start, end = buffer.get_bounds()
        self._profile["custom"] = buffer.get_text(start, end, False)
        gameopts.write(self._source, self._app_id, self._profile)
        self._row.refresh()

    def _on_switch(self, _switch: Gtk.Switch, wanted: bool,
                   name: str) -> bool:
        chosen = [s for s in self._profile.get("switches", []) if s != name]
        if wanted:
            chosen.append(name)
        self._profile["switches"] = chosen
        self._store()
        return False              # let the switch draw the new state itself

    def _on_base(self, *_a: Any) -> None:
        index = int(self._combo.get_selected())
        if 0 <= index < len(self._bases):
            self._profile["base"] = self._bases[index]
            self._store()

    def _on_apply(self, *_a: Any) -> None:
        self._store()
        self._dialog.close()
        self._row.apply(True)

    def present(self) -> None:
        if hasattr(self._dialog, "present"):
            try:
                self._dialog.present(self._window)
                return
            except TypeError:            # PreferencesWindow.present() takes 0
                pass
        self._dialog.set_transient_for(self._window)
        self._dialog.set_modal(True)
        self._dialog.show()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.set_title(paths.APP_TITLE)
        self.set_default_size(680, 620)

        self._toasts = Adw.ToastOverlay()
        self._groups: list[Adw.PreferencesGroup] = []
        # The last scan, kept so that hiding a game redraws the list instead
        # of walking every library again.
        self._scanned: list[tuple[str, list[dict[str, Any]]]] = []
        self._show_hidden = False

        self._spinner = Adw.StatusPage(title=_("Looking for your games..."))
        self._spinner.set_child(Adw.Spinner() if hasattr(Adw, "Spinner")
                                else Gtk.Spinner(spinning=True))

        self._stack = Gtk.Stack()
        self._stack.add_named(self._spinner, "loading")
        self._stack.add_named(self._build_list(), "list")
        self._stack.add_named(self._build_empty(), "empty")
        self._toasts.set_child(self._stack)

        view = Adw.ToolbarView()
        view.add_top_bar(self._build_header())
        self._offer_a_known_update()
        view.set_content(self._toasts)
        self.set_content(view)

        self.reload()

    # --- chrome ----------------------------------------------------------
    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text(_("Look for games again"))
        refresh.connect("clicked", lambda *_a: self.reload())
        header.pack_start(refresh)

        # Appears the moment something is hidden, next to where it was
        # hidden. A permanent button for a list nobody has ever filtered is
        # one more thing to explain; a way back that only exists while it is
        # needed is not.
        new_folder = Gtk.Button(icon_name="folder-new-symbolic")
        new_folder.set_tooltip_text(_("Set up a new game folder"))
        new_folder.connect("clicked", self._on_new_folder)
        header.pack_start(new_folder)

        self._hidden_toggle = Gtk.ToggleButton(
            icon_name="view-conceal-symbolic")
        self._hidden_toggle.set_tooltip_text(_("Show hidden games"))
        self._hidden_toggle.set_visible(False)
        self._hidden_toggle.connect("toggled", self._on_show_hidden)
        header.pack_start(self._hidden_toggle)

        menu = Gio.Menu()
        menu.append(_("Settings"), "app.settings")
        menu.append(_("Check for updates"), "app.check-update")
        menu.append(_("Repair setup"), "app.integrate")
        menu.append(_("About"), "app.about")
        # Removing the app lives in the settings dialog (`SettingsDialog`),
        # not here: this menu is what you do *with* the app, and a menu item
        # one slip away from "About" is a poor place for the one entry that
        # ends it.
        # Remembered so the update entry can turn into "install" once a
        # check found something -- a GMenu item cannot be relabelled, only
        # replaced (see `offer_update`).
        self._menu = menu
        self._update_item = 1
        button = Gtk.MenuButton(icon_name="open-menu-symbolic",
                                menu_model=menu)
        button.set_tooltip_text(_("Main menu"))
        header.pack_end(button)
        return header

    def _offer_a_known_update(self) -> None:
        """An earlier check may already know about one.

        Cache only, never the network: opening the window must not wait on
        GitHub. `is_newer` guards against a cache written before the update
        was installed.
        """
        from ..core import updater
        cached = (db.load_config().get("update_check") or {}).get("result")
        version = str((cached or {}).get("version") or "")
        if version and updater.is_newer(version):
            self.offer_update(version)

    def offer_update(self, version: str | None) -> None:
        """Turn the menu's update entry into an install offer, or back.

        Finding an update and then leaving the user with no way to take it
        is the state this window was in: the check reported a new version and
        the only route to it was the command line.
        """
        label = (_("Install update {version}", version=version) if version
                 else _("Check for updates"))
        action = "app.install-update" if version else "app.check-update"
        self._menu.remove(self._update_item)
        self._menu.insert(self._update_item, label, action)

        # The tray carries the same offer: with the window closed it is the
        # only place the news can land (`LphApplication.update_offered`).
        app = self.get_application()
        if app is not None:
            app.update_offered(version, label)

    def _build_list(self) -> Gtk.Widget:
        """One column, filled with a group per launcher by `_show`."""
        scroller = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=620, margin_top=18, margin_bottom=18,
                          margin_start=12, margin_end=12)
        self._column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                               spacing=18)

        # Said once, above everything, instead of once per launcher heading.
        hint = Gtk.Label(label=_("Turn a game on so it can tell us where it "
                                 "stores its data."),
                         wrap=True, xalign=0.0)
        hint.add_css_class("dim-label")
        self._column.append(hint)

        clamp.set_child(self._column)
        scroller.set_child(clamp)
        return scroller

    def _build_empty(self) -> Gtk.Widget:
        self._empty = Adw.StatusPage(icon_name="applications-games-symbolic")
        self._empty_because_hidden(False)
        return self._empty

    def _empty_because_hidden(self, hidden: bool) -> None:
        """An empty list has two reasons; never give the wrong one.

        "No games found" in front of a library the user has just hidden reads
        as a broken scan, and the way back is the very button that sentence
        does not mention.
        """
        if hidden:
            self._empty.set_title(_("Every game is hidden"))
            self._empty.set_description(
                _("Use the eye in the header bar to show them again."))
        else:
            self._empty.set_title(_("No games found"))
            self._empty.set_description(
                _("We look for games from Steam, Lutris and Heroic, and for "
                  "game folders you set up yourself. Install one, then press "
                  "refresh."))

    # --- data ------------------------------------------------------------
    def reload(self) -> None:
        self._stack.set_visible_child_name("loading")

        def work() -> list[tuple[str, list[dict[str, Any]]]]:
            from ..adapters import base
            # Grouped off the main loop with the scan itself: the order is
            # the adapters' own and nothing here gets to invent a second one.
            return base.group_by_source(base.iter_games())

        def done(groups: Any, error: Exception | None) -> None:
            if error is not None:
                self._stack.set_visible_child_name("empty")
                self.toast(_("Something went wrong: {error}",
                             error=str(error)))
                return
            self._show(groups or [])

        tasks.run(work, done)

    def _show(self, groups: list[tuple[str, list[dict[str, Any]]]]) -> None:
        self._scanned = groups
        self._render()

    def refilter(self) -> None:
        """Redraw the list after a game was hidden or shown.

        The scan itself is not repeated: nothing on disk changed, only which
        of the games we already found belong on screen.
        """
        self._render()

    def _on_show_hidden(self, button: Gtk.ToggleButton) -> None:
        self._show_hidden = button.get_active()
        self._render()

    def _render(self) -> None:
        from ..adapters import base

        for group in self._groups:
            self._column.remove(group)
        self._groups = []

        hidden_keys = set(db.hidden_games())
        shown = 0
        for source, games in self._scanned:
            rows = [(game, base.game_key(game) in hidden_keys)
                    for game in games]
            if not self._show_hidden:
                rows = [(game, False) for game, hidden in rows if not hidden]
            if not rows:
                # A launcher whose games are all hidden loses its heading
                # too, rather than leaving an empty box behind.
                continue
            group = Adw.PreferencesGroup(title=esc(base.source_label(source)))
            for game, hidden in rows:
                group.add(GameRow(self, game, hidden=hidden))
            self._column.append(group)
            self._groups.append(group)
            shown += len(rows)

        # Kept while the toggle is on even once nothing is hidden any more:
        # pulling the button out from under the user's pointer mid-cleanup is
        # not a way back, and the toggle they turned off takes it away anyway.
        self._hidden_toggle.set_visible(bool(hidden_keys)
                                        or self._show_hidden)
        found = sum(len(games) for _s, games in self._scanned)
        self._empty_because_hidden(bool(found) and not shown)
        self._stack.set_visible_child_name("list" if shown else "empty")

    # --- a game folder of your own ---------------------------------------
    def _on_new_folder(self, *_args: Any) -> None:
        """A game folder of the user's own: a name, a short name, a version.

        The version is asked here and can be changed later in the game's own
        row; the short name is the folder on disk and does not move again.
        """
        engines = newprefix.engines()
        if not engines:
            self.toast(_("Nothing on this system can run Windows games yet. "
                         "Install a compatibility build in Steam, or Wine."))
            return

        dialog = Adw.AlertDialog(
            heading=esc(_("A game folder of your own")),
            body=esc(_("For a game no launcher knows about. Give it a name, "
                       "then install the game into it.")))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=6)
        entry = Gtk.Entry(activates_default=True)
        entry.set_placeholder_text(_("Name"))
        box.append(entry)

        # The short name its folder gets. Left empty it follows the name,
        # which is what most people want; typed once it never moves again,
        # because a path is what everything else ends up pointing at.
        alias = Gtk.Entry(activates_default=True)
        alias.set_placeholder_text(_("Short name for the folder (optional)"))
        box.append(alias)

        labels = [newprefix.engine_label(e["id"]) for e in engines]
        combo = Gtk.DropDown(model=Gtk.StringList.new(labels))
        chosen = newprefix.default_engine()
        ids = [e["id"] for e in engines]
        combo.set_selected(ids.index(chosen) if chosen in ids else 0)
        box.append(combo)

        # Where it goes, for this one folder. The remembered default is in
        # Settings; this is the game that does not fit on that disk.
        where = {"path": str(newprefix.root())}
        place = Gtk.Button(label=where["path"])
        place.set_tooltip_text(_("Choose where this folder is made"))
        # A path is one long word: without this the dialog grows to fit it.
        inner = place.get_child()
        if isinstance(inner, Gtk.Label):
            inner.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            inner.set_max_width_chars(34)
        place.connect("clicked", self._pick_place, where, place)
        box.append(place)
        dialog.set_extra_child(box)

        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("make", _("Create"))
        dialog.set_response_appearance("make",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("make")
        dialog.set_close_response("cancel")

        def on_response(_d: Any, response: str) -> None:
            if response != "make":
                return
            index = int(combo.get_selected())
            self._make_folder(entry.get_text().strip(),
                              ids[index] if 0 <= index < len(ids) else "",
                              where["path"], alias.get_text().strip())

        dialog.connect("response", on_response)
        dialog.present(self)

    def _pick_place(self, _button: Gtk.Button, where: dict[str, str],
                    label: Gtk.Button) -> None:
        if not hasattr(Gtk, "FileDialog"):            # GTK < 4.10
            self.toast(_("Set it with: {cmd}",
                         cmd=f"{paths.APP_NAME} --set-game-root PATH"))
            return

        def chosen(path: str) -> None:
            where["path"] = path
            label.set_label(path)

        choose_folder(self, _("Choose where new game folders are made"),
                      where["path"], chosen)

    def _make_folder(self, name: str, engine: str, target: str,
                     alias: str = "") -> None:
        if not name:
            self.toast(_("That name cannot be used."))
            return
        # Setting one up runs a whole Windows boot and takes a while, so say
        # that it started -- the window has nothing else to show until it is
        # done.
        self.toast(_("Setting {name} up...", name=name))

        def work() -> Any:
            return newprefix.create(name, engine, target, alias)

        def done(result: Any, error: Exception | None) -> None:
            if error is not None:
                self.toast(_("Something went wrong: {error}",
                             error=str(error)))
                return
            self.toast(result.message)
            if result.ok:
                self.reload()

        tasks.run(work, done)

    # --- feedback --------------------------------------------------------
    def toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=esc(message), timeout=4))

    def show_manual_step(self, game: str, result: Any) -> None:
        """One step only the user can take, with the text to copy.

        Three cases: Steam's launch options and the compatibility build a
        game's extra options need (neither can be written while Steam runs),
        and a hand-installed game (there is no launcher config at all). Each
        one names its detail after what the string *is*, so look for all of
        them rather than inventing a shared name for three different things.
        """
        detail = result["detail"]
        options = next((str(detail[key]) for key in ("launch_options",
                                                     "command", "tool_name")
                        if detail.get(key)), "")
        dialog = Adw.AlertDialog(heading=esc(game),
                                 body=esc(result.message))
        dialog.add_response("close", _("Close"))
        if options:
            dialog.add_response("copy", _("Copy"))
            dialog.set_response_appearance("copy",
                                           Adw.ResponseAppearance.SUGGESTED)

            def on_response(_d: Any, response: str) -> None:
                if response == "copy":
                    _copy(options)
                    self.toast(_("(copied to clipboard)"))

            dialog.connect("response", on_response)
        dialog.present(self)


class LphApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self._window: MainWindow | None = None
        self._tray: Any = None
        # The window checks its update cache while it is being built, which is
        # before the tray exists -- so the answer is kept here and the tray
        # picks it up when it starts (`_start_tray`).
        self._update_ready = False
        self._update_label = _("Check for updates")
        for name, handler in (("settings", self._on_settings),
                              ("check-update", self._on_check_update),
                              ("install-update", self._on_install_update),
                              ("integrate", self._on_integrate),
                              ("uninstall", self._on_uninstall),
                              ("about", self._on_about),
                              ("quit", lambda *_a: self.quit())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def do_activate(self) -> None:          # noqa: N802 -- GObject vfunc
        if self._window is None:
            self._window = MainWindow(application=self)
            self._start_tray()
        self._window.present()
        if not db.load_config().get("setup_done"):
            self._first_run()

    def do_shutdown(self) -> None:          # noqa: N802 -- GObject vfunc
        if self._tray is not None:
            self._tray.close()
        Adw.Application.do_shutdown(self)

    # --- tray -------------------------------------------------------------
    def _start_tray(self) -> None:
        """Put the app in the tray and let the window close into it.

        Only when there really is one. A window that closes into a tray that
        does not exist is an app the user can neither see nor quit -- so the
        close handler is connected *after* `tray.live` says yes, and asks
        again on every close in case the desktop shell went away since.

        `hold()` is what actually keeps us running: without it GTK ends the
        application with its last window, tray or no tray.
        """
        if not db.background_tray() or self._window is None:
            return
        from . import tray

        self._tray = tray.Tray(
            title=paths.APP_TITLE, icon=paths.APP_NAME,
            on_activate=self.activate,
            items=[tray.Item("show", _("Show window"), self.activate),
                   tray.Item("update", self._update_label,
                             self._on_tray_update),
                   tray.Item("quit", _("Quit"), self.quit)])
        if not self._tray.live:
            self._tray = None
            return
        self._tray.set_attention(self._update_ready)

        self.hold()
        self._window.connect("close-request", self._on_close)

    def _on_close(self, window: MainWindow) -> bool:
        """Hide instead of quit -- but only while the icon is still there."""
        if self._tray is None or not self._tray.live:
            return False                     # let the window close for real
        window.set_visible(False)
        return True                          # handled: do not destroy it

    def update_offered(self, version: str | None, label: str) -> None:
        """The window found (or lost) an update; keep the tray in step."""
        self._update_ready = bool(version)
        self._update_label = label
        if self._tray is None:
            return                    # not started yet, or no tray at all
        self._tray.set_label("update", label)
        self._tray.set_attention(self._update_ready)

    def _on_tray_update(self) -> None:
        """Whatever that entry currently offers: check, or install.

        One entry rather than two, because it reads as one thing to do about
        updates -- and its label already says which of the two it is.
        """
        self.activate_action(
            "install-update" if self._update_ready else "check-update", None)

    def _first_run(self) -> None:
        """One quiet confirmation, then set ourselves up."""
        window = self._window
        dialog = Adw.AlertDialog(
            heading=_("Welcome to {app}", app=paths.APP_TITLE),
            body=_("We add ourselves to your menu and watch for newly "
                   "installed games. Nothing is changed inside your games "
                   "until you turn one on."))
        dialog.add_response("later", _("Later"))
        dialog.add_response("go", _("Set up"))
        dialog.set_response_appearance("go",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("go")

        def on_response(_d: Any, response: str) -> None:
            if response != "go" or window is None:
                return
            self._run_setup(window)

        dialog.connect("response", on_response)
        dialog.present(window)

    def _run_setup(self, window: MainWindow) -> None:
        def work() -> Any:
            from ..core import integrate
            cfg = db.load_config()
            cfg["setup_done"] = True
            cfg["managed_by"] = ("gearlever" if integrate.detect_gearlever()
                                 else "self")
            db.save_config(cfg)
            return integrate.full_setup(enable_watcher=True)

        def done(_result: Any, error: Exception | None) -> None:
            if error is not None:
                window.toast(_("Something went wrong: {error}",
                               error=str(error)))
            else:
                window.toast(_("Ready. Turn on a game to get started."))

        tasks.run(work, done)

    def _on_settings(self, *_args: Any) -> None:
        if self._window is not None:
            SettingsDialog(self._window).present()

    def _on_check_update(self, *_args: Any) -> None:
        window = self._window

        def work() -> Any:
            from ..core import updater
            return updater.check(force=True)

        def done(state: Any, error: Exception | None) -> None:
            if window is None:
                return
            if error is not None:
                window.toast(_("Something went wrong: {error}",
                               error=str(error)))
            elif state and state.get("available"):
                version = str(state.get("version"))
                # The menu entry becomes the way to take it.
                window.offer_update(version)
                window.toast(_("Version {version} is available.",
                               version=version))
            elif state and state.get("reason"):
                # Never claim "up to date" for a check that did not happen.
                window.toast(_("Could not check for updates."))
            else:
                window.toast(_("You are up to date."))

        tasks.run(work, done)

    def _on_install_update(self, *_args: Any) -> None:
        """Download it. Putting it in place is a second, separate step.

        Only `ready` may lead to that step. The version this replaces asked
        the same question of `ok`, which is also what "you are up to date"
        and "GearLever handles this" answer -- so clicking the entry after
        an update had already been installed produced an "Update installed,
        restart now" dialog for an update that never existed.
        """
        window = self._window
        if window is not None:
            window.toast(_("Downloading the update..."))

        def work() -> Any:
            from ..core import updater
            return updater.download()

        def done(result: Any, error: Exception | None) -> None:
            if window is None:
                return
            if error is not None:
                window.toast(_("Update failed: {error}", error=str(error)))
                return
            window.offer_update(None)
            if not result.get("ready"):
                # Up to date, GearLever's business, or a failure. None of
                # the three is an update waiting to be installed.
                window.toast(str(result.get("message", "")))
                return
            self._finish_update(window, str(result.get("version") or ""))

        tasks.run(work, done)

    def _finish_update(self, window: MainWindow, version: str) -> None:
        """The half that can only happen while we are *not* running.

        Installing means replacing the file we are executing, so Velopack's
        helper waits for this process to end. That is why closing the app is
        the install step and not something that follows it -- and why this
        is a question rather than a progress bar.
        """
        dialog = Adw.AlertDialog(
            heading=_("Update ready"),
            body=_("Version {version} is downloaded. {app} has to close to "
                   "put it in place, and starts again by itself when it is "
                   "done.", version=version, app=paths.APP_TITLE))
        dialog.add_response("later", _("Later"))
        dialog.add_response("finish", _("Close and update"))
        dialog.set_response_appearance("finish",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("finish")

        def on_response(_dialog: Any, response: str) -> None:
            if response != "finish":
                # Nothing was handed over, so this app's next exit is an
                # ordinary one. The download stays where it is and the next
                # start picks it up (`updater.app_hook`).
                window.toast(_("The update is installed the next time you "
                               "start {app}.", app=paths.APP_TITLE))
                return
            from ..core import updater
            result = updater.finish(restart=True)
            if not result.get("ok"):
                window.toast(str(result.get("message", "")))
                return
            self.quit()

        dialog.connect("response", on_response)
        dialog.present(window)

    def _on_integrate(self, *_args: Any) -> None:
        window = self._window
        if window is None:
            return
        self._run_setup(window)

    # --- removing the app -------------------------------------------------
    def _on_uninstall(self, *_args: Any) -> None:
        """Ask first, and ask with the plan in hand.

        The plan walks every library and every prefix, so it goes off the
        main loop like any other scan. Showing a confirmation before knowing
        what there is to confirm would mean asking "remove everything?" and
        only then finding out that a game is running.
        """
        window = self._window
        if window is None:
            return
        window.toast(_("Checking what has to be moved back..."))

        def work() -> Any:
            from ..core import uninstall
            return uninstall.plan()

        def done(preview: Any, error: Exception | None) -> None:
            if window is None:
                return
            if error is not None:
                window.toast(_("Something went wrong: {error}",
                               error=str(error)))
                return
            if preview["blockers"]:
                self._uninstall_blocked(window, list(preview["blockers"]))
                return
            self._confirm_uninstall(window, preview)

        tasks.run(work, done)

    def _uninstall_blocked(self, window: MainWindow,
                           reasons: list[str]) -> None:
        """Say what is in the way -- and offer nothing else.

        A dialog with a "remove anyway" button next to this text would be a
        button that leaves a game's data in a folder the game no longer
        points at.
        """
        dialog = Adw.AlertDialog(
            heading=_("Not right now"),
            body="\n".join(reasons) + "\n\n"
                 + _("Nothing was changed."))
        dialog.add_response("ok", _("Close"))
        dialog.set_default_response("ok")
        dialog.present(window)

    def _confirm_uninstall(self, window: MainWindow,
                           preview: dict[str, Any]) -> None:
        lines = []
        if preview["games"]:
            lines.append(_("{n} game(s) get their data moved back into the "
                           "game folder.", n=len(preview["games"])))
        if preview["connected"]:
            lines.append(_("{n} game(s) are disconnected again.",
                           n=len(preview["connected"])))
        if preview["options"]:
            lines.append(_("{n} game(s) go back to Steam's own settings.",
                           n=len(preview["options"])))
        lines.append(_("{n} file(s) that {app} installed are deleted.",
                       n=len(preview["files"]), app=paths.APP_TITLE))
        if preview["gearlever"]:
            lines.append(_("GearLever placed the app file, so it stays -- "
                           "remove it in GearLever."))

        dialog = Adw.AlertDialog(heading=_("Remove {app}?",
                                           app=paths.APP_TITLE),
                                 body="\n".join(lines))
        keep = Gtk.CheckButton(
            label=_("Keep what {app} learned about your games",
                    app=paths.APP_TITLE),
            margin_top=6)
        dialog.set_extra_child(keep)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d: Any, response: str) -> None:
            if response != "remove":
                return
            self._run_uninstall(window, keep.get_active())

        dialog.connect("response", on_response)
        dialog.present(window)

    def _run_uninstall(self, window: MainWindow, keep_settings: bool) -> None:
        window.toast(_("Moving your game data back..."))

        def work() -> Any:
            from ..core import uninstall
            return uninstall.run(keep_settings=keep_settings)

        def done(result: Any, error: Exception | None) -> None:
            if window is None:
                return
            if error is not None:
                window.toast(_("Something went wrong: {error}",
                               error=str(error)))
                return
            if not result["ok"]:
                self._uninstall_blocked(
                    window, [str(result["message"])]
                    + list(result.get("failed", [])))
                return
            self._uninstall_done(window, result)

        tasks.run(work, done)

    def _uninstall_done(self, window: MainWindow,
                        result: dict[str, Any]) -> None:
        """The last thing this app ever shows. Then it closes for good."""
        lines = [_("{n} folder(s) are back in their game.",
                   n=len(result["reverted"]))]
        lines += [str(note) for note in result.get("notes", [])]
        if result.get("manual"):
            lines.append(_("You started these yourself -- take '{shim}' back "
                           "out of your own launch command: {games}",
                           shim=str(paths.WRAPPER_SHIM),
                           games=", ".join(result["manual"])))
        if result["kept_settings"]:
            lines.append(_("Settings and what was learned stay in {path}.",
                           path=str(paths.CONFIG_DIR)))

        dialog = Adw.AlertDialog(heading=str(result["message"]),
                                 body="\n".join(lines))
        dialog.add_response("quit", _("Close"))
        dialog.set_default_response("quit")
        dialog.connect("response", lambda *_a: self.quit())
        dialog.present(window)

    def _on_about(self, *_args: Any) -> None:
        from .. import __version__
        about = Adw.AboutDialog(
            application_name=paths.APP_TITLE,
            application_icon=paths.APP_NAME,
            version=__version__,
            developer_name="tokajer",
            comments=_("Find out where your games store their data -- and "
                       "keep that data in your home folder."),
            website="https://github.com/tokajer/linux-prefix-hub",
            copyright="© 2026 tokajer",
            license_type=Gtk.License.GPL_3_0)
        about.present(self._window)


def main(argv: list[str] | None = None) -> int:
    """Entry point used by `--gui` and by the desktop entry.

    The program name is set first and deliberately: GTK sends *that* to the
    compositor as the window's app id (X11: `WM_CLASS`), not the application
    id, and left alone it is whatever started the interpreter -- "python3",
    or "python3.12" from inside the AppImage. The task bar then looks for a
    desktop entry of that name, finds python's, and draws python's icon next
    to our window. `core/integrate.py` writes the entry this now matches.
    """
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(paths.APP_TITLE)
    Adw.init()
    return LphApplication().run(argv or [])
