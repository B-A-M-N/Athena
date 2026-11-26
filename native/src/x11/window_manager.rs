//! Window-manager interaction mechanics for the X11 compositor.

//! EWMH moveresize strategy/telemetry, focused focus checks, pointer grabs,
//! resize cursors, and `_NET_WM_MOVERESIZE` protocol construction live here.
//! The `x11` entrypoint owns the event loop and presentation cadence.

use std::env;
use std::ffi::CString;
use std::io::Write;
use std::ptr;

use athena_terminal::NativePixelLayout;

use crate::platform::*;
use crate::window_management::{ResizeZone, WindowDrag, WindowDragKind};

pub(crate) fn initial_window_size(display: *mut Display, screen: c_int) -> (i32, i32) {
    let screen_width = unsafe { XDisplayWidth(display, screen) }.max(640);
    let screen_height = unsafe { XDisplayHeight(display, screen) }.max(480);
    // Leave a small amount of desktop context while opening large enough to
    // expose the actual cabinet proportions immediately.
    (
        ((screen_width as f32 * 0.88).round() as i32).clamp(640, screen_width),
        ((screen_height as f32 * 0.88).round() as i32).clamp(480, screen_height),
    )
}

pub(crate) fn set_input_focus_if_mapped(
    display: *mut Display,
    window: Window,
    mapped: bool,
) -> bool {
    if !mapped || window == 0 {
        return false;
    }
    let mut attributes: XWindowAttributes = unsafe { std::mem::zeroed() };
    let viewable = unsafe {
        XGetWindowAttributes(display, window, &mut attributes) != 0
            && attributes.map_state == IS_VIEWABLE
    };
    if !viewable {
        return false;
    }
    // Xlib reports BadMatch asynchronously for an unmapped/minimized window.
    // Querying map_state closes the known lifecycle race before requesting
    // focus; the process-wide nonfatal X error boundary remains a last line of
    // defense for a hostile window manager.
    unsafe {
        XSetInputFocus(display, window, 1, CURRENT_TIME);
        XSync(display, 0);
    }
    true
}

pub(crate) type FrameGeometry = NativePixelLayout;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum WindowMoveStrategy {
    Ewmh,
    ClientManagedFallback,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct WindowMoveTelemetry {
    pub(crate) ewmh_advertised: bool,
    pub(crate) ewmh_attempted: bool,
    pub(crate) ewmh_confirmed: bool,
    pub(crate) active_strategy: &'static str,
    pub(crate) fallback_reason: Option<&'static str>,
}

impl WindowMoveTelemetry {
    pub(crate) fn for_strategy(strategy: WindowMoveStrategy) -> Self {
        Self {
            ewmh_advertised: strategy.uses_ewmh(),
            ewmh_attempted: false,
            ewmh_confirmed: false,
            active_strategy: if strategy.uses_ewmh() {
                "ewmh_preferred"
            } else {
                "client_managed"
            },
            fallback_reason: None,
        }
    }

    pub(crate) fn begin_ewmh(&mut self) {
        self.ewmh_attempted = true;
        self.active_strategy = "ewmh_preferred";
    }

    pub(crate) fn confirm_ewmh(&mut self) {
        if !self.fallback_used() {
            self.ewmh_confirmed = true;
            self.active_strategy = "ewmh_confirmed";
        }
    }

    pub(crate) fn activate_fallback(&mut self, reason: &'static str) {
        if !self.ewmh_confirmed {
            self.active_strategy = "client_managed";
            self.fallback_reason = Some(reason);
        }
    }

    pub(crate) fn fallback_used(self) -> bool {
        self.fallback_reason.is_some()
    }
}

impl WindowMoveStrategy {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Ewmh => "ewmh",
            Self::ClientManagedFallback => "client_managed_fallback",
        }
    }

    pub(crate) fn uses_ewmh(self) -> bool {
        matches!(self, Self::Ewmh)
    }
}

pub(crate) fn select_window_move_strategy(
    display: *mut Display,
    screen: c_int,
) -> WindowMoveStrategy {
    if net_supported_contains(display, screen, "_NET_WM_MOVERESIZE") {
        WindowMoveStrategy::Ewmh
    } else {
        WindowMoveStrategy::ClientManagedFallback
    }
}

pub(crate) fn net_supported_contains(display: *mut Display, screen: c_int, wanted: &str) -> bool {
    let root = unsafe { XRootWindow(display, screen) };
    let supported_atom = intern_atom(display, "_NET_SUPPORTED");
    let wanted_atom = intern_atom(display, wanted);
    if root == 0 || supported_atom == 0 || wanted_atom == 0 {
        return false;
    }
    let mut actual_type = 0;
    let mut actual_format = 0;
    let mut item_count = 0;
    let mut bytes_after = 0;
    let mut data: *mut u8 = ptr::null_mut();
    let status = unsafe {
        XGetWindowProperty(
            display,
            root,
            supported_atom,
            0,
            4096,
            0,
            0,
            &mut actual_type,
            &mut actual_format,
            &mut item_count,
            &mut bytes_after,
            &mut data,
        )
    };
    let found = if status == 0 && actual_format == 32 && !data.is_null() {
        let atoms =
            unsafe { std::slice::from_raw_parts(data.cast::<c_ulong>(), item_count as usize) };
        atoms.contains(&wanted_atom)
    } else {
        false
    };
    if !data.is_null() {
        unsafe { XFree(data.cast()) };
    }
    found
}

pub(crate) fn window_management_diagnostics(
    display: *mut Display,
    screen: c_int,
    strategy: WindowMoveStrategy,
    telemetry: WindowMoveTelemetry,
) -> serde_json::Value {
    serde_json::json!({
        "moveresize_supported": strategy.uses_ewmh(),
        "selected_strategy": strategy.name(),
        "strategy": telemetry.active_strategy,
        "adaptive": true,
        "ewmh_advertised": telemetry.ewmh_advertised,
        "ewmh_attempted": telemetry.ewmh_attempted,
        "ewmh_confirmed": telemetry.ewmh_confirmed,
        "active_strategy": telemetry.active_strategy,
        "fallback_reason": telemetry.fallback_reason,
        "client_managed_fallback": true,
        "protocol": "_NET_WM_MOVERESIZE",
        "source_indication": 1,
        "display": env::var("DISPLAY").unwrap_or_else(|_| "unknown".to_owned()),
        "session_type": env::var("XDG_SESSION_TYPE").unwrap_or_else(|_| "unknown".to_owned()),
        "wm_support_probe": net_supported_contains(display, screen, "_NET_WM_MOVERESIZE"),
    })
}

pub(crate) fn pointer_root_position(display: *mut Display) -> Option<(i32, i32)> {
    let root = unsafe { XRootWindow(display, XDefaultScreen(display)) };
    let mut root_return = 0;
    let mut child_return = 0;
    let mut root_x = 0;
    let mut root_y = 0;
    let mut window_x = 0;
    let mut window_y = 0;
    let mut mask = 0;
    let status = unsafe {
        XQueryPointer(
            display,
            root,
            &mut root_return,
            &mut child_return,
            &mut root_x,
            &mut root_y,
            &mut window_x,
            &mut window_y,
            &mut mask,
        )
    };
    (status != 0).then_some((root_x, root_y))
}

pub(crate) fn apply_window_drag(
    display: *mut Display,
    window: Window,
    drag: WindowDrag,
    root_x: i32,
    root_y: i32,
) {
    let (root_target_x, root_target_y, width, height) = drag.geometry(root_x, root_y);
    let x = drag
        .start_parent_x
        .saturating_add(root_target_x.saturating_sub(drag.start_window_x));
    let y = drag
        .start_parent_y
        .saturating_add(root_target_y.saturating_sub(drag.start_window_y));
    unsafe {
        match drag.kind {
            WindowDragKind::Move => {
                XMoveWindow(display, window, x, y);
            }
            WindowDragKind::Resize(_) => {
                XMoveResizeWindow(display, window, x, y, width as CUint, height as CUint);
            }
        }
        glFlush();
        XFlush(display);
    }
}

pub(crate) fn grab_window_pointer(display: *mut Display, window: Window) {
    unsafe {
        let status = XGrabPointer(
            display,
            window,
            0,
            (POINTER_MOTION_MASK | BUTTON_RELEASE_MASK | BUTTON_PRESS_MASK) as c_ulong,
            GRAB_MODE_ASYNC,
            GRAB_MODE_ASYNC,
            0,
            0,
            CURRENT_TIME,
        );
        if status != 0 {
            eprintln!("native pointer grab failed with X11 status {status}");
        }
        XFlush(display);
        // Complete the grab request before a fast pointer motion can leave
        // the surface; otherwise a remote/XTest desktop can race the next
        // client and drop an edge/corner motion event.
        XSync(display, 0);
    }
}

pub(crate) struct ResizeCursors {
    display: *mut Display,
    cursors: [Cursor; 8],
    active: Option<ResizeZone>,
}

impl ResizeCursors {
    pub(crate) fn new(display: *mut Display) -> Self {
        let zones = [
            ResizeZone::TopLeft,
            ResizeZone::Top,
            ResizeZone::TopRight,
            ResizeZone::Right,
            ResizeZone::BottomRight,
            ResizeZone::Bottom,
            ResizeZone::BottomLeft,
            ResizeZone::Left,
        ];
        let cursors = zones.map(|zone| unsafe { XCreateFontCursor(display, zone.cursor_shape()) });
        Self {
            display,
            cursors,
            active: None,
        }
    }

    pub(crate) fn set(&mut self, window: Window, zone: Option<ResizeZone>) {
        if self.active == zone {
            return;
        }
        unsafe {
            if let Some(zone) = zone {
                let cursor = self.cursors[cursor_index(zone)];
                if cursor != 0 {
                    XDefineCursor(self.display, window, cursor);
                }
            } else {
                XUndefineCursor(self.display, window);
            }
        }
        self.active = zone;
    }
}

impl Drop for ResizeCursors {
    fn drop(&mut self) {
        unsafe {
            for cursor in self.cursors {
                if cursor != 0 {
                    XFreeCursor(self.display, cursor);
                }
            }
        }
    }
}

pub(crate) fn cursor_index(zone: ResizeZone) -> usize {
    match zone {
        ResizeZone::TopLeft => 0,
        ResizeZone::Top => 1,
        ResizeZone::TopRight => 2,
        ResizeZone::Right => 3,
        ResizeZone::BottomRight => 4,
        ResizeZone::Bottom => 5,
        ResizeZone::BottomLeft => 6,
        ResizeZone::Left => 7,
    }
}

pub(crate) fn intern_atom(display: *mut Display, name: &str) -> Atom {
    let Ok(value) = CString::new(name) else {
        return 0;
    };
    unsafe { XInternAtom(display, value.as_ptr(), 0) }
}

pub(crate) fn is_wm_delete_message(
    message_type: Atom,
    format: c_int,
    first_data: c_long,
    protocols_atom: Atom,
    delete_atom: Atom,
) -> bool {
    message_type == protocols_atom && format == 32 && first_data as c_ulong == delete_atom
}

pub(crate) fn begin_window_move(display: *mut Display, window: Window, button: &XButtonEvent) {
    begin_window_moveresize(display, window, button, 8);
}

pub(crate) fn write_attention_action(
    writer: &mut impl Write,
    action: &crate::render::oi::AttentionAction,
) -> bool {
    let command = match action {
        crate::render::oi::AttentionAction::Approve { approval_id, scope } => {
            if !is_command_token(approval_id) || !is_command_token(scope) {
                return false;
            }
            format!("/approve {approval_id} {scope}\n")
        }
        crate::render::oi::AttentionAction::Deny { approval_id } => {
            if !is_command_token(approval_id) {
                return false;
            }
            format!("/deny {approval_id}\n")
        }
    };
    writer.write_all(command.as_bytes()).is_ok() && writer.flush().is_ok()
}

pub(crate) fn is_command_token(value: &str) -> bool {
    !value.is_empty()
        && value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || "_-:.".contains(character))
}

pub(crate) fn begin_window_resize(
    display: *mut Display,
    window: Window,
    button: &XButtonEvent,
    zone: ResizeZone,
) {
    begin_window_moveresize(display, window, button, zone.direction());
}

pub(crate) fn begin_window_moveresize(
    display: *mut Display,
    window: Window,
    button: &XButtonEvent,
    direction: c_long,
) {
    let root = unsafe { XRootWindow(display, XDefaultScreen(display)) };
    let message_type = intern_atom(display, "_NET_WM_MOVERESIZE");
    let mut event = XEvent {
        type_: CLIENT_MESSAGE,
        pad: [0; 24],
    };
    let message = unsafe { &mut *(&mut event as *mut XEvent as *mut XClientMessageEvent) };
    message.display = display;
    message.window = window;
    message.message_type = message_type;
    message.format = 32;
    message.data = moveresize_message_data(button.x_root, button.y_root, direction, button.button);
    unsafe {
        // EWMH hands pointer ownership to the window manager. Release any
        // stale client grab before sending the request; the EWMH path never
        // creates a local grab in the first place.
        XUngrabPointer(display, CURRENT_TIME);
        XSendEvent(
            display,
            root,
            0,
            SUBSTRUCTURE_NOTIFY_MASK | SUBSTRUCTURE_REDIRECT_MASK,
            &mut event,
        );
        XFlush(display);
    }
}

pub(crate) fn moveresize_message_data(
    root_x: i32,
    root_y: i32,
    direction: c_long,
    button: u32,
) -> [c_long; 5] {
    [
        root_x as c_long,
        root_y as c_long,
        direction,
        button as c_long,
        // EWMH source indication: 1 means a normal application request.
        1,
    ]
}
