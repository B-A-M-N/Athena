//! Mutable state and time-based coordination for the X11 event loop.

use std::time::{Duration, Instant};

use crate::platform::*;
use crate::window_management::{PendingEwmhGesture, WindowDrag};

use super::window_manager::{
    WindowMoveTelemetry, apply_window_drag, grab_window_pointer, pointer_root_position,
};

/// State that survives individual X11 events for pointer-drag fallback.
///
/// The parent compositor still owns rendering and lifecycle decisions; this
/// seam owns only the mutable interaction state needed when a WM does not
/// complete an EWMH moveresize gesture.
#[derive(Default)]
pub(crate) struct WindowSession {
    pub(crate) window_drag: Option<WindowDrag>,
    pub(crate) pending_ewmh_gesture: Option<PendingEwmhGesture>,
    pub(crate) client_fallback_until: Option<Instant>,
}

impl WindowSession {
    /// Advance WM-grab fallback independently of the incoming event stream.
    pub(crate) fn advance_fallback(
        &mut self,
        display: *mut Display,
        window: Window,
        telemetry: &mut WindowMoveTelemetry,
    ) {
        if let Some(pending) = self.pending_ewmh_gesture {
            if Instant::now() >= pending.deadline {
                self.pending_ewmh_gesture = None;
                telemetry.activate_fallback("ewmh_no_configure_before_grace");
                self.window_drag = Some(pending.drag);
                self.client_fallback_until = Some(Instant::now() + Duration::from_millis(500));
                grab_window_pointer(display, window);
                if let Some((root_x, root_y)) = pointer_root_position(display) {
                    apply_window_drag(display, window, pending.drag, root_x, root_y);
                }
            }
        }
        if let Some(until) = self.client_fallback_until {
            if Instant::now() < until {
                if let (Some(drag), Some((root_x, root_y))) =
                    (self.window_drag, pointer_root_position(display))
                {
                    apply_window_drag(display, window, drag, root_x, root_y);
                }
            } else {
                self.client_fallback_until = None;
                if self.window_drag.is_some() {
                    self.window_drag = None;
                    unsafe { XUngrabPointer(display, CURRENT_TIME) };
                }
            }
        }
    }
}
