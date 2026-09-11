use super::{
    Display, EWMH_GESTURE_GRACE, ResizeZone, Window, WindowDragKind, XButtonEvent,
    window_parent_position, window_root_position,
};
use std::time::Instant;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct WindowDrag {
    pub(super) kind: WindowDragKind,
    pub(super) start_root_x: i32,
    pub(super) start_root_y: i32,
    pub(super) start_window_x: i32,
    pub(super) start_window_y: i32,
    pub(super) start_parent_x: i32,
    pub(super) start_parent_y: i32,
    pub(super) start_width: i32,
    pub(super) start_height: i32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct PendingEwmhGesture {
    pub(super) drag: WindowDrag,
    pub(super) configure_events_at_start: u64,
    pub(super) deadline: Instant,
}

impl PendingEwmhGesture {
    pub(super) fn new(drag: WindowDrag, configure_events: u64) -> Self {
        Self {
            drag,
            configure_events_at_start: configure_events,
            deadline: Instant::now() + EWMH_GESTURE_GRACE,
        }
    }

    pub(super) fn geometry_changed(self, x: i32, y: i32, width: i32, height: i32) -> bool {
        x != self.drag.start_window_x
            || y != self.drag.start_window_y
            || width != self.drag.start_width
            || height != self.drag.start_height
    }
}

impl WindowDrag {
    pub(super) fn new(
        display: *mut Display,
        window: Window,
        button: &XButtonEvent,
        width: i32,
        height: i32,
        kind: WindowDragKind,
    ) -> Self {
        let (start_window_x, start_window_y) = window_root_position(display, window);
        let (start_parent_x, start_parent_y) = window_parent_position(display, window);
        Self {
            kind,
            start_root_x: button.x_root,
            start_root_y: button.y_root,
            start_window_x,
            start_window_y,
            start_parent_x,
            start_parent_y,
            start_width: width,
            start_height: height,
        }
    }

    pub(super) fn geometry(self, root_x: i32, root_y: i32) -> (i32, i32, i32, i32) {
        let dx = root_x.saturating_sub(self.start_root_x);
        let dy = root_y.saturating_sub(self.start_root_y);
        match self.kind {
            WindowDragKind::Move => (
                self.start_window_x.saturating_add(dx),
                self.start_window_y.saturating_add(dy),
                self.start_width,
                self.start_height,
            ),
            WindowDragKind::Resize(zone) => {
                let left = matches!(
                    zone,
                    ResizeZone::TopLeft | ResizeZone::BottomLeft | ResizeZone::Left
                );
                let right = matches!(
                    zone,
                    ResizeZone::TopRight | ResizeZone::Right | ResizeZone::BottomRight
                );
                let top = matches!(
                    zone,
                    ResizeZone::TopLeft | ResizeZone::Top | ResizeZone::TopRight
                );
                let bottom = matches!(
                    zone,
                    ResizeZone::BottomLeft | ResizeZone::Bottom | ResizeZone::BottomRight
                );
                let width = if left {
                    self.start_width.saturating_sub(dx)
                } else if right {
                    self.start_width.saturating_add(dx)
                } else {
                    self.start_width
                }
                .max(900);
                let height = if top {
                    self.start_height.saturating_sub(dy)
                } else if bottom {
                    self.start_height.saturating_add(dy)
                } else {
                    self.start_height
                }
                .max(620);
                let x = if left {
                    self.start_window_x
                        .saturating_add(self.start_width.saturating_sub(width))
                } else {
                    self.start_window_x
                };
                let y = if top {
                    self.start_window_y
                        .saturating_add(self.start_height.saturating_sub(height))
                } else {
                    self.start_window_y
                };
                (x, y, width, height)
            }
        }
    }
}
