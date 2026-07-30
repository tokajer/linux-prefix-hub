"""GTK4 / libadwaita front-end.

Sits on the same logic the CLI uses -- `base.iter_games()`, `adapter.connect()`
and `core.redirect` -- and adds nothing of its own beyond presentation. If a
behaviour is missing here, it belongs in `core/` or `adapters/`, not in a
widget callback.

Vocabulary rule (CLAUDE.md #6): the user reads "game folder", "saves",
"connect", "moved to". Never prefix, Wine or Proton.

Structure:
  LphApplication   -- Adw.Application, one window
  MainWindow       -- header + game list, everything else is a dialog
  GameRow          -- one expander per game: connect switch + save locations

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

    def __init__(self, window: MainWindow, game: dict[str, Any]) -> None:
        super().__init__()
        self._window = window
        self._game = game
        self._syncing = False
        # Raw, unescaped -- get_title() would hand back the escaped form and
        # escaping that again shows "&amp;" to the user.
        self._name = str(game.get("game_name", "?"))

        self.set_title(esc(self._name))
        self.set_subtitle(esc(self._subtitle()))

        self._lookup = Gtk.Button(icon_name="system-search-symbolic",
                                  valign=Gtk.Align.CENTER)
        self._lookup.add_css_class("flat")
        self._lookup.set_tooltip_text(_("Look up where this game saves"))
        self._lookup.connect("clicked", self._on_lookup)
        self.add_suffix(self._lookup)

        self._switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._switch.set_tooltip_text(
            _("Let this game report where it saves"))
        self._sync_switch(bool(game.get("managed")))
        self._switch.connect("state-set", self._on_toggled)
        self.add_suffix(self._switch)

        self._fill_locations()

    # --- presentation ----------------------------------------------------
    def _subtitle(self) -> str:
        from ..adapters import base
        source = base.source_label(str(self._game.get("source", "")))
        if not self._game.get("installed"):
            return _("{source} - not fully installed yet", source=source)
        if not self._game.get("prefix_path"):
            return _("{source} - not started yet", source=source)
        return _("{source} - ready", source=source)

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

        found = self._entry()
        locations = found[1].get("storage_locations", []) if found else []
        if not locations:
            hint = Adw.ActionRow(
                title=esc(_("No save locations known yet")),
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

        self.set_title(esc(_("Saves in {folder}",
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

        self.set_title(esc(_("Saves in {folder}", folder=root)))
        self.set_subtitle(esc(self._where(loc)))

        self.add_suffix(open_button(window, entry, loc))

        self._switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._switch.set_tooltip_text(_("Keep these saves in your home "
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
        return True


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
            title=_("Where saves are kept"),
            description=_("When you move a game's saves out of the game "
                          "folder, they land here, one folder per game. "
                          "Folders you already moved stay where they are."))

        self._row = Adw.ActionRow(title=esc(_("Save folder")))
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
        self._dialog.add(page)

    def _online_group(self) -> Adw.PreferencesGroup:
        """The one switch that keeps the app entirely offline."""
        from ..core import pcgw
        group = Adw.PreferencesGroup(
            title=_("Finding save locations"),
            description=_("Looking a game up asks PCGamingWiki where it "
                          "keeps its saves, so you do not have to play it "
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
        self._window.toast(_("Saves will be kept in {path}.",
                             path=str(db.redirect_root())))

    def _on_reset(self, *_a: Any) -> None:
        self._apply(None)

    def _on_choose(self, *_a: Any) -> None:
        if not hasattr(Gtk, "FileDialog"):        # GTK < 4.10
            self._window.toast(_("Set it with: {cmd}",
                                 cmd=f"{paths.APP_NAME} --set-save-folder "
                                     f"PATH"))
            return
        dialog = Gtk.FileDialog(title=_("Choose the save folder"))
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
        self._group = Adw.PreferencesGroup(
            title=_("Your games"),
            description=_("Turn a game on so it can tell us where it saves."))

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
        view.set_content(self._toasts)
        self.set_content(view)

        self._rows: list[GameRow] = []
        self.reload()

    # --- chrome ----------------------------------------------------------
    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text(_("Look for games again"))
        refresh.connect("clicked", lambda *_a: self.reload())
        header.pack_start(refresh)

        menu = Gio.Menu()
        menu.append(_("Settings"), "app.settings")
        menu.append(_("Check for updates"), "app.check-update")
        menu.append(_("Repair setup"), "app.integrate")
        menu.append(_("About"), "app.about")
        button = Gtk.MenuButton(icon_name="open-menu-symbolic",
                                menu_model=menu)
        button.set_tooltip_text(_("Main menu"))
        header.pack_end(button)
        return header

    def _build_list(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=620, margin_top=18, margin_bottom=18,
                          margin_start=12, margin_end=12)
        clamp.set_child(self._group)
        scroller.set_child(clamp)
        return scroller

    def _build_empty(self) -> Gtk.Widget:
        page = Adw.StatusPage(
            icon_name="applications-games-symbolic",
            title=_("No games found"),
            description=_("We look for games from Steam, Lutris and Heroic, "
                          "and for game folders you set up yourself. Install "
                          "one, then press refresh."))
        return page

    # --- data ------------------------------------------------------------
    def reload(self) -> None:
        self._stack.set_visible_child_name("loading")

        def work() -> list[dict[str, Any]]:
            from ..adapters import base
            return sorted(base.iter_games(),
                          key=lambda g: str(g.get("game_name", "")).lower())

        def done(games: Any, error: Exception | None) -> None:
            if error is not None:
                self._stack.set_visible_child_name("empty")
                self.toast(_("Something went wrong: {error}",
                             error=str(error)))
                return
            self._show(games or [])

        tasks.run(work, done)

    def _show(self, games: list[dict[str, Any]]) -> None:
        for row in self._rows:
            self._group.remove(row)
        self._rows = []

        for game in games:
            row = GameRow(self, game)
            self._group.add(row)
            self._rows.append(row)

        self._stack.set_visible_child_name("list" if games else "empty")

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
        for name, handler in (("settings", self._on_settings),
                              ("check-update", self._on_check_update),
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
        self._window.present()
        if not db.load_config().get("setup_done"):
            self._first_run()

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
                window.toast(_("Version {version} is available.",
                               version=str(state.get("version"))))
            else:
                window.toast(_("You are up to date."))

        tasks.run(work, done)

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
            comments=_("Find out where your games store their saves -- and "
                       "keep those saves in your home folder."),
            website="https://github.com/tokajer/linux-prefix-hub",
            license_type=Gtk.License.MIT_X11)
        about.present(self._window)


def main(argv: list[str] | None = None) -> int:
    """Entry point used by `--gui` and by the desktop entry."""
    Adw.init()
    return LphApplication().run(argv or [])
