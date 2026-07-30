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

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..core import db, paths, redirect, registry  # noqa: E402
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

        self._switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._switch.set_tooltip_text(
            _("Let this game report where it saves"))
        self._sync_switch(bool(game.get("managed")))
        self._switch.connect("state-set", self._on_toggled)
        self.add_suffix(self._switch)

        self._fill_locations()

    # --- presentation ----------------------------------------------------
    def _subtitle(self) -> str:
        source = str(self._game.get("source", ""))
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

        found = self._entry()
        locations = found[1].get("storage_locations", []) if found else []
        if not locations:
            hint = Adw.ActionRow(
                title=esc(_("No save locations known yet")),
                subtitle=esc(_("Connect the game and play it once -- then "
                               "its save locations show up here.")))
            hint.set_activatable(False)
            self.add_row(hint)
            return

        fingerprint, entry = found            # type: ignore[misc]
        seen: set[str] = set()
        for loc in locations:
            root = registry.shell_folder_root(str(loc.get("win_path", "")))
            if not root or root in seen:
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
            description=_("We look for games from Steam, Lutris and Heroic. "
                          "Install one, then press refresh."))
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
        """Steam's launch options cannot be written while Steam runs."""
        options = str(result["detail"].get("options", ""))
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
        for name, handler in (("check-update", self._on_check_update),
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
