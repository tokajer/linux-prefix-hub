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

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..core import db, desktop, paths, redirect, registry  # noqa: E402
from ..core.i18n import _  # noqa: E402
from . import tasks  # noqa: E402

APP_ID = "io.github.tokajer.LinuxPrefixHub"


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
        if folder:
            self.add_row(GameFolderRow(self._window, str(folder)))
        else:
            # Never started: there is nothing to move yet, only something to
            # ask for. See `PendingRow`.
            self.add_row(PendingRow(self._window, self._game))

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

    def _on_lookup(self, *_args: Any) -> None:
        """Ask PCGamingWiki where this game saves. Network, so off the loop."""
        self._lookup.set_sensitive(False)
        game = dict(self._game)

        def work() -> Any:
            from ..core import pcgw
            return pcgw.lookup_and_store(game)

        def done(result: Any, error: Exception | None) -> None:
            self._lookup.set_sensitive(True)
            if error is not None:
                self._window.toast(_("Something went wrong: {error}",
                                     error=str(error)))
                return
            message = str(result["message"])
            if result["locations"] and not result.get("stored"):
                # Known, but nowhere to keep it yet: the DB is keyed by the
                # game folder, which only exists once the game has run.
                message += " " + _("Start the game once -- then they show "
                                   "up here.")
            self._window.toast(message)
            if result.get("stored"):
                self._fill_locations()

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


class SettingsDialog:
    """Where moved saves are kept. Built as a dialog, presented on the window.

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
        page.add(group)
        page.add(self._online_group())
        page.add(self._tray_group())
        self._dialog.add(page)

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
        dialog = Gtk.FileDialog(title=_("Choose the data folder"))
        dialog.set_initial_folder(Gio.File.new_for_path(
            str(db.redirect_root().parent)))

        def on_done(source: Any, result: Any) -> None:
            try:
                folder = source.select_folder_finish(result)
            except GLib.Error:
                return                            # cancelled -- not an error
            if folder and folder.get_path():
                self._apply(folder.get_path())

        dialog.select_folder(self._window, None, on_done)


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

    # --- feedback --------------------------------------------------------
    def toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=esc(message), timeout=4))

    def show_manual_step(self, game: str, result: Any) -> None:
        """One step only the user can take, with the text to copy.

        Two cases: Steam's launch options (they cannot be written while Steam
        runs) and a hand-installed game (there is no launcher config at all).
        Each adapter names its detail after what the string *is*, so look for
        both rather than inventing a shared name for two different things.
        """
        detail = result["detail"]
        options = next((str(detail[key]) for key in ("launch_options",
                                                     "command")
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
        """Download and apply. Velopack restarts the app, so on success this
        never comes back -- everything below `done` is an error path."""
        window = self._window
        if window is not None:
            window.toast(_("Downloading the update..."))

        def work() -> Any:
            from ..core import updater
            return updater.update()

        def done(result: Any, error: Exception | None) -> None:
            if window is None:
                return
            if error is not None:
                window.toast(_("Update failed: {error}", error=str(error)))
                return
            if not result.get("ok"):
                window.toast(str(result.get("message", "")))
                window.offer_update(None)      # let them try again
                return
            # We are still the old code: the window cannot show the new
            # version, so say so and offer the restart instead of leaving
            # the user to work it out.
            window.offer_update(None)
            if result.get("skipped"):           # GearLever handles it
                window.toast(str(result.get("message", "")))
            else:
                self._offer_restart(window)

        tasks.run(work, done)

    def _offer_restart(self, window: MainWindow) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Update installed"),
            body=_("The window is still running the old version. Restart to "
                   "use the new one."))
        dialog.add_response("later", _("Later"))
        dialog.add_response("restart", _("Restart now"))
        dialog.set_response_appearance("restart",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("restart")

        def on_response(_dialog: Any, response: str) -> None:
            if response != "restart":
                return
            from ..core import updater
            if updater.restart_app():
                self.quit()
            else:
                window.toast(_("Could not restart. Please start {app} again.",
                               app=paths.APP_TITLE))

        dialog.connect("response", on_response)
        dialog.present(window)

    def _on_integrate(self, *_args: Any) -> None:
        window = self._window
        if window is None:
            return
        self._run_setup(window)

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
    """Entry point used by `--gui` and by the desktop entry."""
    Adw.init()
    return LphApplication().run(argv or [])
