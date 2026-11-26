//! Window property/hint mechanics for the X11 compositor.

//! Size hints, Motif decoration suppression, and EWMH process identity
//! (`_NET_WM_PID`) are all window-manager-facing property writes owned here.

use crate::platform::*;
use crate::x11::window_manager::intern_atom;

pub(crate) fn set_window_hints(display: *mut Display, window: Window) {
    let mut hints = XSizeHints {
        flags: P_MIN_SIZE | P_BASE_SIZE,
        x: 0,
        y: 0,
        width: 1280,
        height: 800,
        min_width: 900,
        min_height: 620,
        max_width: 0,
        max_height: 0,
        width_inc: 1,
        height_inc: 1,
        min_aspect_x: 0,
        min_aspect_y: 0,
        max_aspect_x: 0,
        max_aspect_y: 0,
        base_width: 900,
        base_height: 620,
        win_gravity: 0,
    };
    unsafe { XSetWMNormalHints(display, window, &mut hints) };

    // `_MOTIF_WM_HINTS` is the broadly supported X11 decoration switch. It
    // leaves the surface managed and movable while removing the OS titlebar
    // that would otherwise break the physical AthenaBox illusion.
    let atom = intern_atom(display, "_MOTIF_WM_HINTS");
    let decorations: [c_long; 5] = [2, 0, 0, 0, 0];
    unsafe {
        XChangeProperty(
            display,
            window,
            atom,
            atom,
            32,
            PROP_MODE_REPLACE,
            decorations.as_ptr().cast(),
            decorations.len() as c_int,
        );
    }
}

pub(crate) fn set_window_pid(display: *mut Display, window: Window) {
    // EWMH window identity lets desktop tooling bind an X11 surface to the
    // process that owns it. This matters for acceptance harnesses and for
    // window managers that expose process-aware focus/restore behavior.
    let pid_atom = intern_atom(display, "_NET_WM_PID");
    let cardinal_atom = intern_atom(display, "CARDINAL");
    let pid = [std::process::id()];
    unsafe {
        XChangeProperty(
            display,
            window,
            pid_atom,
            cardinal_atom,
            32,
            PROP_MODE_REPLACE,
            pid.as_ptr().cast(),
            1,
        );
    }
}
