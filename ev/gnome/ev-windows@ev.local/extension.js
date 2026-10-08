/* E.V. Windows - lets E.V. see and manage windows on GNOME Wayland.
 *
 * Under Wayland no ordinary program may list, focus or close another
 * program's windows; only the compositor can. This extension runs inside
 * GNOME Shell and offers exactly that on the session bus as org.ev.Windows:
 * List, Activate, Close, Minimize, Maximize, plus a kill-switch hotkey grab
 * (GrabKill / ReleaseKill / KillCount). Nothing else - no Eval, no input,
 * no screenshots.
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
    <method name="GrabKill">
      <arg type="s" direction="in" name="accelerator"/>
      <arg type="u" direction="out" name="action"/>
    </method>
    <method name="ReleaseKill">
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="KillCount">
      <arg type="u" direction="out" name="count"/>
    </method>
  </interface>
</node>`;

class WindowService {
    constructor() {
        this._killAction = 0;
        this._killCount = 0;
        this._killSignal = global.display.connect('accelerator-activated',
            (_display, action) => {
                if (this._killAction && action === this._killAction)
                    this._killCount += 1;
            });
    }

    destroy() {
        this.ReleaseKill();
        if (this._killSignal) {
            global.display.disconnect(this._killSignal);
            this._killSignal = 0;
        }
    }

    // E.V.'s kill switch while it drives the screen. Wayland gives no client
    // a global hotkey, so the compositor holds it; E.V. polls KillCount.
    // Allowed in every action mode, so it works over full screen apps too.
    GrabKill(accelerator) {
        this.ReleaseKill();
        const action = global.display.grab_accelerator(
            accelerator, Meta.KeyBindingFlags.NONE);
        if (action === Meta.KeyBindingAction.NONE)
            return 0;
        Main.wm.allowKeybinding(
            Meta.external_binding_name_for_action(action), Shell.ActionMode.ALL);
        this._killAction = action;
        return action;
    }

    ReleaseKill() {
        if (!this._killAction)
            return false;
        Main.wm.allowKeybinding(
            Meta.external_binding_name_for_action(this._killAction),
            Shell.ActionMode.NONE);
        global.display.ungrab_accelerator(this._killAction);
        this._killAction = 0;
        return true;
    }

    KillCount() {
        return this._killCount;
    }

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
        this._impl = new WindowService();
        this._service = Gio.DBusExportedObject.wrapJSObject(IFACE, this._impl);
        this._service.export(Gio.DBus.session, '/org/ev/Windows');
        this._owner = Gio.bus_own_name(
            Gio.BusType.SESSION, 'org.ev.Windows', Gio.BusNameOwnerFlags.NONE, null, null, null);
    }

    disable() {
        if (this._impl) {
            this._impl.destroy();
            this._impl = null;
        }
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
