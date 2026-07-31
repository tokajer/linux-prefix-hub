# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Status tray icon, spoken straight onto the session bus.

Why there is no library here. GTK4 removed `Gtk.StatusIcon` and put nothing
in its place, and the usual answer -- AppIndicator, Ayatana's or Canonical's
-- is linked against **GTK3**: loading its typelib inside a GTK4 process
aborts with "Using GTK 2/3 and GTK 4 in the same process is not supported".
So it is not an optional dependency we chose not to take, it is one we cannot
take at all.

What every desktop that has a tray actually speaks underneath those libraries
is **StatusNotifierItem** plus **com.canonical.dbusmenu**, two D-Bus
interfaces. Gio can export both, and Gio is GTK-version-agnostic. That is all
this module is: the two interfaces, a small menu model, and no GTK at all.

Degrading is the normal case, not the error case:

  * No `gi` (this module is imported by tests too), no session bus, or no
    `org.kde.StatusNotifierWatcher` on it -- plain GNOME without the
    AppIndicator extension is exactly that -- and `live` stays False.
  * The caller then leaves its window alone. **Nothing may close into a tray
    that is not there**: an app you cannot see and cannot quit is a worse
    outcome than one that exits when you close it, and it is the one bug this
    whole module could plausibly cause.

`live` is re-read, never cached by the caller: a desktop shell restart takes
the watcher away and brings it back, and `_watch_for_host` follows it.

Verified against KDE's own `StatusNotifierWatcher`: the item registers, every
property and the dbusmenu layout read back correctly, and the window hides and
comes back.

VERIFY-ON-DEVICE:
  - Every other host is a different implementation of the same two interfaces
    (GNOME's AppIndicator extension, the xembed bridges). Check the icon
    appears, that a left click raises the window and that the menu opens on
    right click.
  - `IconName` resolves through the icon theme, so it needs
    `integrate.install_icon` to have run -- otherwise the item registers but
    draws blank.
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from typing import Any

from ..core import paths

# Where the tray host looks for us, and what it calls the pieces.
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_INTERFACE = "org.kde.StatusNotifierItem"
ITEM_PATH = "/StatusNotifierItem"
MENU_INTERFACE = "com.canonical.dbusmenu"
MENU_PATH = "/StatusNotifierItem/Menu"

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewTitle"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg name="id" type="i"/>
      <arg name="timestamp" type="u"/>
    </signal>
  </interface>
</node>
"""


class Item:
    """One entry in the tray menu.

    `key` is ours and stays put; `label` is the user's and may change (the
    update entry becomes "Install update 1.2.3" once there is one). The menu
    numbers items from 1 because dbusmenu keeps 0 for the root.
    """

    def __init__(self, key: str, label: str,
                 action: Callable[[], None] | None = None) -> None:
        self.key = key
        self.label = label
        self.action = action


def _gio() -> tuple[Any, Any] | None:
    """(Gio, GLib), or None where there is no `gi` to import them from."""
    try:
        from gi.repository import Gio, GLib
    except (ImportError, ValueError):
        return None
    return Gio, GLib


class Tray:
    """The icon, its menu, and the D-Bus plumbing under both.

    Construction never raises and never blocks on a desktop that has no tray:
    check `live` afterwards and carry on either way.
    """

    def __init__(self, title: str, icon: str, items: list[Item],
                 on_activate: Callable[[], None] | None = None) -> None:
        self.title = title
        self.icon = icon
        self.items = list(items)
        self._on_activate = on_activate
        self._attention = False
        self._revision = 1

        self._bus: Any = None
        self._glib: Any = None
        self._gio: Any = None
        self._registrations: list[int] = []
        self._name = f"{ITEM_INTERFACE}-{os.getpid()}-1"
        self._name_id = 0
        self._watch_id = 0
        self._named = False
        self._host = False

        self._start()

    # --- what the caller asks --------------------------------------------
    @property
    def live(self) -> bool:
        """Is there really an icon out there right now?

        False whenever hiding a window into this would lose it: no bus, no
        exported object, or no tray host on the desktop at this moment.
        """
        return bool(self._bus and self._registrations and self._host)

    def set_label(self, key: str, label: str) -> None:
        """Rename one entry (the update offer is the reason this exists)."""
        for item in self.items:
            if item.key == key and item.label != label:
                item.label = label
                self._layout_changed()
                return

    def set_attention(self, on: bool) -> None:
        """Mark the icon as wanting the user -- a waiting update does.

        `NeedsAttention` is the one thing an icon can say on its own, without
        a notification the user has to dismiss.
        """
        if bool(on) == self._attention:
            return
        self._attention = bool(on)
        self._emit(ITEM_PATH, ITEM_INTERFACE, "NewStatus",
                   self._glib.Variant("(s)", (self._status(),))
                   if self._glib else None)

    def close(self) -> None:
        """Take the icon down. Safe to call twice, and when never started."""
        if self._bus is not None:
            for registration in self._registrations:
                with contextlib.suppress(Exception):
                    self._bus.unregister_object(registration)
        self._registrations = []
        for owner_id, unown in ((self._name_id, "bus_unown_name"),
                                (self._watch_id, "bus_unwatch_name")):
            if owner_id and self._gio is not None:
                with contextlib.suppress(Exception):
                    getattr(self._gio, unown)(owner_id)
        self._name_id = self._watch_id = 0
        self._host = False

    # --- setup ------------------------------------------------------------
    def _start(self) -> None:
        """Export both interfaces and offer ourselves to the tray host.

        Every step is allowed to fail into "no tray": a session bus that is
        not there, a name we cannot own, a desktop with no host. None of that
        is worth an error the user has to read -- they did not ask for a tray
        icon, they asked for the app.
        """
        loaded = _gio()
        if loaded is None:
            return
        self._gio, self._glib = loaded
        gio = self._gio

        try:
            self._bus = gio.bus_get_sync(gio.BusType.SESSION, None)
        except Exception:
            self._bus = None
            return

        try:
            self._export(ITEM_XML, ITEM_PATH, self._item_call,
                         self._item_property)
            self._export(MENU_XML, MENU_PATH, self._menu_call,
                         self._menu_property)
        except Exception:
            self.close()
            self._bus = None
            return

        # The name is the address we hand the host, and owning it is
        # asynchronous -- so registration waits for `_on_name_acquired`.
        self._name_id = gio.bus_own_name_on_connection(
            self._bus, self._name, gio.BusNameOwnerFlags.NONE,
            self._on_name_acquired, None)

        # Asked once, synchronously, *before* the watch below: the caller
        # decides whether its window may close into this the moment we
        # return, and the main loop has not run yet at that point. Getting
        # this the async way meant `live` was False for every caller that
        # ever asked, and the tray silently did nothing.
        self._host = self._watcher_present()
        self._watch_for_host()
        self._register_with_host()

    def _watcher_present(self) -> bool:
        """Is a tray host on the bus right now?"""
        try:
            reply = self._bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "NameHasOwner",
                self._glib.Variant("(s)", (WATCHER_NAME,)),
                self._glib.VariantType("(b)"),
                self._gio.DBusCallFlags.NONE, 1000, None)
            return bool(reply[0])
        except Exception:
            return False

    def _export(self, xml: str, path: str, on_call: Any,
                on_get: Any) -> None:
        info = self._gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
        self._registrations.append(
            self._bus.register_object(path, info, on_call, on_get, None))

    def _on_name_acquired(self, *_args: Any) -> None:
        self._named = True
        self._register_with_host()

    def _watch_for_host(self) -> None:
        """Follow the tray host as it comes and goes.

        A desktop shell restart (or enabling GNOME's AppIndicator extension
        after we started) takes the watcher away and brings it back.
        Re-registering on every appearance is what makes the icon survive
        that; dropping `_host` on every vanish is what keeps `live` honest in
        between.
        """
        gio = self._gio

        def appeared(*_args: Any) -> None:
            self._host = True
            self._register_with_host()

        def vanished(*_args: Any) -> None:
            self._host = False

        self._watch_id = gio.bus_watch_name_on_connection(
            self._bus, WATCHER_NAME, gio.BusNameWatcherFlags.NONE,
            appeared, vanished)

    def _register_with_host(self) -> None:
        """Offer ourselves to the host, once both halves are actually there.

        Two independent things have to have happened -- the host has to be on
        the bus and we have to own the name we are about to hand it -- and
        they arrive in either order. Both callers land here, so whichever is
        second is the one that registers.
        """
        if not (self._named and self._host):
            return
        with contextlib.suppress(Exception):
            self._bus.call(
                WATCHER_NAME, WATCHER_PATH, WATCHER_NAME,
                "RegisterStatusNotifierItem",
                self._glib.Variant("(s)", (self._name,)),
                None, self._gio.DBusCallFlags.NONE, 3000, None, None)

    # --- StatusNotifierItem ----------------------------------------------
    def _status(self) -> str:
        return "NeedsAttention" if self._attention else "Active"

    def _item_property(self, _conn: Any, _sender: str, _path: str,
                       _interface: str, name: str) -> Any:
        variant = self._glib.Variant
        values = {
            "Category": ("s", "ApplicationStatus"),
            "Id": ("s", paths.APP_NAME),
            "Title": ("s", self.title),
            "Status": ("s", self._status()),
            "IconName": ("s", self.icon),
            "IconThemePath": ("s", ""),
            "AttentionIconName": ("s", self.icon),
            "OverlayIconName": ("s", ""),
            "Menu": ("o", MENU_PATH),
            # False, so a left click reaches `Activate` instead of only ever
            # opening the menu. Raising the window is the common gesture.
            "ItemIsMenu": ("b", False),
        }
        if name == "ToolTip":
            return variant("(sa(iiay)ss)",
                           (self.icon, [], self.title, ""))
        found = values.get(name)
        return variant(*found) if found else None

    def _item_call(self, _conn: Any, _sender: str, _path: str,
                   _interface: str, method: str, _params: Any,
                   invocation: Any) -> None:
        if method in ("Activate", "SecondaryActivate") and self._on_activate:
            self._glib.idle_add(self._run, self._on_activate)
        invocation.return_value(None)

    # --- com.canonical.dbusmenu ------------------------------------------
    def _numbered(self) -> list[tuple[int, Item]]:
        """Menu ids. 0 is the root, so entries start at 1."""
        return list(enumerate(self.items, start=1))

    def _properties(self, item: Item) -> dict[str, Any]:
        variant = self._glib.Variant
        return {"label": variant("s", item.label),
                "enabled": variant("b", item.action is not None),
                "visible": variant("b", True)}

    def _menu_property(self, _conn: Any, _sender: str, _path: str,
                       _interface: str, name: str) -> Any:
        variant = self._glib.Variant
        values: dict[str, tuple[str, Any]] = {
            "Version": ("u", 3),
            "Status": ("s", "normal"),
            "TextDirection": ("s", "ltr"),
            "IconThemePath": ("as", []),
        }
        found = values.get(name)
        return variant(*found) if found else None

    def _menu_call(self, _conn: Any, _sender: str, _path: str,
                   _interface: str, method: str, params: Any,
                   invocation: Any) -> None:
        variant = self._glib.Variant
        if method == "GetLayout":
            children = [variant("(ia{sv}av)",
                                (number, self._properties(item), []))
                        for number, item in self._numbered()]
            root = (0, {"children-display": variant("s", "submenu")},
                    children)
            invocation.return_value(
                variant("(u(ia{sv}av))", (self._revision, root)))
        elif method == "GetGroupProperties":
            wanted = set(params[0])
            invocation.return_value(variant("(a(ia{sv}))", ([
                (number, self._properties(item))
                for number, item in self._numbered()
                if not wanted or number in wanted], )))
        elif method == "GetProperty":
            number, name = params[0], params[1]
            item = dict(self._numbered()).get(number)
            value = self._properties(item).get(name) if item else None
            invocation.return_value(
                variant("(v)", (value or variant("s", ""),)))
        elif method == "Event":
            self._event(params[0], params[1])
            invocation.return_value(None)
        elif method == "EventGroup":
            for event in params[0]:
                self._event(event[0], event[1])
            invocation.return_value(variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _event(self, number: int, event_id: str) -> None:
        if event_id != "clicked":
            return
        item = dict(self._numbered()).get(number)
        if item is not None and item.action is not None:
            # Off the D-Bus callback and onto the main loop: an action opens
            # dialogs and touches widgets, which belongs there and nowhere
            # else (`gui/tasks.py` makes the same promise for disk work).
            self._glib.idle_add(self._run, item.action)

    @staticmethod
    def _run(action: Callable[[], None]) -> bool:
        # A broken menu entry must not stop the main loop.
        with contextlib.suppress(Exception):
            action()
        return False      # GLib.SOURCE_REMOVE -- run once

    # --- signals ----------------------------------------------------------
    def _layout_changed(self) -> None:
        self._revision += 1
        self._emit(MENU_PATH, MENU_INTERFACE, "LayoutUpdated",
                   self._glib.Variant("(ui)", (self._revision, 0))
                   if self._glib else None)

    def _emit(self, path: str, interface: str, signal: str,
              params: Any) -> None:
        if not self._bus:
            return
        # The host went away mid-emit; `_watch_for_host` notices for us.
        with contextlib.suppress(Exception):
            self._bus.emit_signal(None, path, interface, signal, params)
