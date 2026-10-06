/* E.V. Windows - lets E.V. see and manage windows on GNOME Wayland.
 *
 * Under Wayland no ordinary program may list, focus or close another
 * program's windows; only the compositor can. This extension runs inside
 * GNOME Shell and offers exactly that on the session bus as org.ev.Windows:
 * List, Activate, Close, Minimize, Maximize. Nothing else - no Eval, no
 * input, no screenshots.
 *
 * Close is Meta.Window.delete(): the same request as the title bar's X, so
 * an editor with unsaved work asks its own "Save changes?" question.
 */
import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const IFACE = `
<node>
  <interface name="org.ev.Windows">
    <method name="List">
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="Activate">
      <arg type="t" direction="in" name="id"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="Close">
      <arg type="t" direction="in" name="id"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="Minimize">
      <arg type="t" direction="in" name="id"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="Maximize">
      <arg type="t" direction="in" name="id"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
  </interface>
</node>`;

class WindowService {
    _windows() {
        // Most-recently-used first, all workspaces: the order Alt+Tab uses,
        // which is also the order "the window I was just in" lives in.
        return global.display.get_tab_list(Meta.TabList.NORMAL_ALL, null);
    }

    _find(id) {
        const wanted = Number(id);
        return this._windows().find(win => Number(win.get_id()) === wanted) ?? null;
    }

    List() {
        const focus = global.display.focus_window;
        const tracker = Shell.WindowTracker.get_default();
        const out = this._windows().map(win => {
            const rect = win.get_frame_rect();
            const app = tracker.get_window_app(win);
            return {
                id: Number(win.get_id()),
                title: win.get_title() ?? '',
                wm_class: win.get_wm_class() ?? '',
                app: app ? app.get_name() : '',
                app_id: app ? app.get_id() : '',
                pid: win.get_pid(),
                x: rect.x, y: rect.y, width: rect.width, height: rect.height,
                focused: win === focus,
                minimized: Boolean(win.minimized),
            };
        });
        return JSON.stringify(out);
    }

    Activate(id) {
        const win = this._find(id);
        if (!win)
            return false;
        // Main.activateWindow also switches workspace and unminimises.
        Main.activateWindow(win);
        return true;
    }

    Close(id) {
        const win = this._find(id);
        if (!win)
            return false;
        win.delete(global.get_current_time());
        return true;
    }

    Minimize(id) {
        const win = this._find(id);
        if (!win)
            return false;
        win.minimize();
        return true;
    }

    Maximize(id) {
        const win = this._find(id);
        if (!win)
            return false;
        // GNOME 49 removed the flags argument from maximize().
        if (win.maximize.length === 0)
            win.maximize();
        else
            win.maximize(Meta.MaximizeFlags.BOTH);
        return true;
    }
}

export default class EvWindowsExtension extends Extension {
    enable() {
        this._service = Gio.DBusExportedObject.wrapJSObject(IFACE, new WindowService());
        this._service.export(Gio.DBus.session, '/org/ev/Windows');
        this._owner = Gio.bus_own_name(
            Gio.BusType.SESSION, 'org.ev.Windows', Gio.BusNameOwnerFlags.NONE, null, null, null);
    }

    disable() {
        if (this._service) {
            this._service.unexport();
            this._service = null;
        }
        if (this._owner) {
            Gio.bus_unown_name(this._owner);
            this._owner = 0;
        }
    }
}
