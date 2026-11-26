use crate::platform::{
    CUint, Display, RESIZE_EDGE, Window, XButtonEvent, XDefaultScreen, XGetWindowAttributes,
    XRootWindow, XTranslateCoordinates, XWindowAttributes, c_long,
};
use std::time::{Duration, Instant};

pub(crate) const EWMH_GESTURE_GRACE: Duration = Duration::from_millis(80);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ResizeZone {
    TopLeft,
    Top,
    TopRight,
    Right,
    BottomRight,
    Bottom,
    BottomLeft,
    Left,
}

impl ResizeZone {
    pub(crate) fn direction(self) -> c_long {
        match self {
            Self::TopLeft => 0,
            Self::Top => 1,
            Self::TopRight => 2,
            Self::Right => 3,
            Self::BottomRight => 4,
            Self::Bottom => 5,
            Self::BottomLeft => 6,
            Self::Left => 7,
        }
    }

    pub(crate) fn cursor_shape(self) -> CUint {
        match self {
            Self::TopLeft => 134,
            Self::Top => 138,
            Self::TopRight => 136,
            Self::Right => 96,
            Self::BottomRight => 14,
            Self::Bottom => 16,
            Self::BottomLeft => 12,
            Self::Left => 70,
        }
    }
}

pub(crate) fn resize_zone(x: i32, y: i32, width: i32, height: i32) -> Option<ResizeZone> {
    let left = x <= RESIZE_EDGE;
    let right = x >= width.saturating_sub(RESIZE_EDGE + 1);
    let top = y <= RESIZE_EDGE;
    let bottom = y >= height.saturating_sub(RESIZE_EDGE + 1);
    match (left, top, right, bottom) {
        (true, true, _, _) => Some(ResizeZone::TopLeft),
        (_, true, true, _) => Some(ResizeZone::TopRight),
        (true, _, _, true) => Some(ResizeZone::BottomLeft),
        (_, _, true, true) => Some(ResizeZone::BottomRight),
        (_, true, _, _) => Some(ResizeZone::Top),
        (_, _, _, true) => Some(ResizeZone::Bottom),
        (true, _, _, _) => Some(ResizeZone::Left),
        (_, _, true, _) => Some(ResizeZone::Right),
        _ => None,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum WindowDragKind {
    Move,
    Resize(ResizeZone),
}

pub(crate) fn window_root_position(display: *mut Display, window: Window) -> (i32, i32) {
    let root = unsafe { XRootWindow(display, XDefaultScreen(display)) };
    let mut x = 0;
    let mut y = 0;
    let mut child = 0;
    let translated =
        unsafe { XTranslateCoordinates(display, window, root, 0, 0, &mut x, &mut y, &mut child) };
    if translated == 0 { (0, 0) } else { (x, y) }
}

pub(crate) fn window_parent_position(display: *mut Display, window: Window) -> (i32, i32) {
    let mut attributes = unsafe { std::mem::zeroed::<XWindowAttributes>() };
    if unsafe { XGetWindowAttributes(display, window, &mut attributes) } != 0 {
        (attributes.x, attributes.y)
    } else {
        window_root_position(display, window)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct WindowDrag {
    pub(crate) kind: WindowDragKind,
    pub(crate) start_root_x: i32,
    pub(crate) start_root_y: i32,
    pub(crate) start_window_x: i32,
    pub(crate) start_window_y: i32,
    pub(crate) start_parent_x: i32,
    pub(crate) start_parent_y: i32,
    pub(crate) start_width: i32,
    pub(crate) start_height: i32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct PendingEwmhGesture {
    pub(crate) drag: WindowDrag,
    pub(crate) configure_events_at_start: u64,
    pub(crate) deadline: Instant,
}

impl PendingEwmhGesture {
    pub(crate) fn new(drag: WindowDrag, configure_events: u64) -> Self {
        Self {
            drag,
            configure_events_at_start: configure_events,
            deadline: Instant::now() + EWMH_GESTURE_GRACE,
        }
    }

    pub(crate) fn geometry_changed(self, x: i32, y: i32, width: i32, height: i32) -> bool {
        x != self.drag.start_window_x
            || y != self.drag.start_window_y
            || width != self.drag.start_width
            || height != self.drag.start_height
    }
}

impl WindowDrag {
    pub(crate) fn new(
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

    pub(crate) fn geometry(self, root_x: i32, root_y: i32) -> (i32, i32, i32, i32) {
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
