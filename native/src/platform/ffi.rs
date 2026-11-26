//! Shared X11/Xft/GL FFI declarations for the native platform host.
//!
//! This module contains ABI shapes and constants only. Window lifecycle,
//! input policy, and presentation orchestration remain in `x11.rs`.

pub(crate) use std::ffi::{c_char, c_int, c_long, c_short, c_ulong, c_ushort, c_void};
use std::time::Duration;

pub(crate) type Display = c_void;
pub(crate) type Window = c_ulong;
pub(crate) type Atom = c_ulong;
pub(crate) type Colormap = c_ulong;
pub(crate) type Cursor = c_ulong;
pub(crate) type Pixmap = c_ulong;
pub(crate) type GLXPixmap = c_ulong;
pub(crate) type GC = *mut c_void;
pub(crate) type GLXContext = *mut c_void;

pub(crate) const KEY_PRESS: c_int = 2;
pub(crate) const BUTTON_PRESS: c_int = 4;
pub(crate) const BUTTON_RELEASE: c_int = 5;
pub(crate) const MOTION_NOTIFY: c_int = 6;
pub(crate) const SELECTION_CLEAR: c_int = 29;
pub(crate) const SELECTION_REQUEST: c_int = 30;
pub(crate) const SELECTION_NOTIFY: c_int = 31;
pub(crate) const UNMAP_NOTIFY: c_int = 18;
pub(crate) const MAP_NOTIFY: c_int = 19;
pub(crate) const DESTROY_NOTIFY: c_int = 17;
pub(crate) const CONFIGURE_NOTIFY: c_int = 22;
pub(crate) const EXPOSE: c_int = 12;
pub(crate) const CLIENT_MESSAGE: c_int = 33;
pub(crate) const FOCUS_IN: c_int = 9;
pub(crate) const FOCUS_OUT: c_int = 10;
pub(crate) const KEY_PRESS_MASK: c_long = 1;
pub(crate) const BUTTON_PRESS_MASK: c_long = 1 << 2;
pub(crate) const BUTTON_RELEASE_MASK: c_long = 1 << 3;
pub(crate) const POINTER_MOTION_MASK: c_long = 1 << 6;
pub(crate) const STRUCTURE_NOTIFY_MASK: c_long = 1 << 17;
pub(crate) const EXPOSURE_MASK: c_long = 1 << 15;
pub(crate) const FOCUS_CHANGE_MASK: c_long = 1 << 21;
pub(crate) const CW_EVENT_MASK: c_ulong = 1 << 11;
pub(crate) const CW_COLORMAP: c_ulong = 1 << 13;
pub(crate) const INPUT_OUTPUT: c_int = 1;
pub(crate) const GLX_RGBA: c_int = 4;
pub(crate) const GLX_RED_SIZE: c_int = 8;
pub(crate) const GLX_GREEN_SIZE: c_int = 9;
pub(crate) const GLX_BLUE_SIZE: c_int = 10;
pub(crate) const GLX_DEPTH_SIZE: c_int = 12;
pub(crate) const GLX_STENCIL_SIZE: c_int = 13;
pub(crate) const GL_COLOR_BUFFER_BIT: u32 = 0x0000_4000;
pub(crate) const GL_STENCIL_BUFFER_BIT: u32 = 0x0000_0400;
pub(crate) const GL_QUADS: u32 = 0x0007;
pub(crate) const GL_LINE_LOOP: u32 = 0x0002;
pub(crate) const GL_LINES: u32 = 0x0001;
pub(crate) const GL_POLYGON: u32 = 0x0009;
pub(crate) const GL_PROJECTION: u32 = 0x1701;
pub(crate) const GL_MODELVIEW: u32 = 0x1700;
pub(crate) const GL_SCISSOR_TEST: u32 = 0x0c11;
pub(crate) const GL_STENCIL_TEST: u32 = 0x0b90;
pub(crate) const GL_BLEND: u32 = 0x0be2;
pub(crate) const GL_SRC_ALPHA: u32 = 0x0302;
pub(crate) const GL_ONE_MINUS_SRC_ALPHA: u32 = 0x0303;
pub(crate) const GL_ALWAYS: u32 = 0x0207;
pub(crate) const GL_EQUAL: u32 = 0x0202;
pub(crate) const GL_KEEP: u32 = 0x1e00;
pub(crate) const GL_REPLACE: u32 = 0x1e01;
pub(crate) const GL_FRAMEBUFFER: u32 = 0x8d40;
pub(crate) const GL_COLOR_ATTACHMENT0: u32 = 0x8ce0;
pub(crate) const GL_FRAMEBUFFER_COMPLETE: u32 = 0x8cd5;
pub(crate) const GL_TEXTURE_2D: u32 = 0x0de1;
pub(crate) const GL_RGBA: u32 = 0x1908;
pub(crate) const GL_RGB: u32 = 0x1907;
pub(crate) const GL_UNSIGNED_BYTE: u32 = 0x1401;
pub(crate) const GL_TEXTURE_MIN_FILTER: u32 = 0x2801;
pub(crate) const GL_TEXTURE_MAG_FILTER: u32 = 0x2800;
pub(crate) const GL_TEXTURE_WRAP_S: u32 = 0x2802;
pub(crate) const GL_TEXTURE_WRAP_T: u32 = 0x2803;
pub(crate) const GL_NEAREST: c_int = 0x2600;
pub(crate) const GL_LINEAR: c_int = 0x2601;
pub(crate) const GL_REPEAT: c_int = 0x2901;
pub(crate) const GL_CLAMP_TO_EDGE: c_int = 0x812f;
pub(crate) const SHIFT_MASK: CUint = 1;
pub(crate) const CONTROL_MASK: CUint = 1 << 2;
pub(crate) const BUTTON1_MASK: CUint = 1 << 8;
pub(crate) const CURRENT_TIME: c_ulong = 0;
pub(crate) const IS_VIEWABLE: c_int = 2;
pub(crate) const PROP_MODE_REPLACE: c_int = 0;
pub(crate) const P_MIN_SIZE: c_long = 1 << 4;
pub(crate) const P_BASE_SIZE: c_long = 1 << 8;
pub(crate) const SUBSTRUCTURE_NOTIFY_MASK: c_long = 1 << 19;
pub(crate) const SUBSTRUCTURE_REDIRECT_MASK: c_long = 1 << 20;
pub(crate) const X_BUFFER_OVERFLOW: c_int = -1;
pub(crate) const MAX_XIM_BUFFER: usize = 16 * 1024;
pub(crate) const MAX_CACHED_XFT_COLORS: usize = 256;
pub(crate) const MAX_CACHED_TEXT_WIDTHS: usize = 512;
pub(crate) const ACTIVE_FRAME_INTERVAL: Duration = Duration::from_millis(100);

/// Presentation cadence override (ATHENA_NATIVE_FRAME_INTERVAL_MS).  The
/// 100 ms default stands until a benchmarked lane justifies a faster cadence
/// (review item 24: measurement first, then change).
pub(crate) fn active_frame_interval() -> Duration {
    match std::env::var("ATHENA_NATIVE_FRAME_INTERVAL_MS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .filter(|ms| *ms >= 16 && *ms <= 1000)
    {
        Some(ms) => Duration::from_millis(ms),
        None => ACTIVE_FRAME_INTERVAL,
    }
}
pub(crate) const IDLE_POLL_INTERVAL: Duration = Duration::from_millis(50);
pub(crate) const RESIZE_EDGE: i32 = 12;
pub(crate) const GRAB_MODE_ASYNC: c_int = 1;

#[repr(C)]
pub(crate) struct XVisualInfo {
    pub(crate) visual: *mut c_void,
    pub(crate) visualid: c_ulong,
    pub(crate) screen: c_int,
    pub(crate) depth: c_int,
    pub(crate) class: c_int,
    pub(crate) red_mask: c_ulong,
    pub(crate) green_mask: c_ulong,
    pub(crate) blue_mask: c_ulong,
    pub(crate) colormap_size: c_int,
    pub(crate) bits_per_rgb: c_int,
}

#[repr(C)]
pub(crate) struct XErrorEvent {
    pub(crate) type_: c_int,
    pub(crate) display: *mut Display,
    pub(crate) resourceid: c_ulong,
    pub(crate) serial: c_ulong,
    pub(crate) error_code: u8,
    pub(crate) request_code: u8,
    pub(crate) minor_code: u8,
}

#[repr(C)]
pub(crate) struct XSetWindowAttributes {
    pub(crate) background_pixmap: c_ulong,
    pub(crate) background_pixel: c_ulong,
    pub(crate) border_pixmap: c_ulong,
    pub(crate) border_pixel: c_ulong,
    pub(crate) bit_gravity: c_int,
    pub(crate) win_gravity: c_int,
    pub(crate) backing_store: c_int,
    pub(crate) backing_planes: c_ulong,
    pub(crate) backing_pixel: c_ulong,
    pub(crate) save_under: c_int,
    pub(crate) event_mask: c_long,
    pub(crate) do_not_propagate_mask: c_long,
    pub(crate) override_redirect: c_int,
    pub(crate) colormap: Colormap,
    pub(crate) cursor: c_ulong,
}

#[repr(C)]
pub(crate) struct XWindowAttributes {
    pub(crate) x: c_int,
    pub(crate) y: c_int,
    pub(crate) width: c_int,
    pub(crate) height: c_int,
    pub(crate) border_width: c_int,
    pub(crate) depth: c_int,
    pub(crate) visual: *mut c_void,
    pub(crate) root: Window,
    pub(crate) class: c_int,
    pub(crate) bit_gravity: c_int,
    pub(crate) win_gravity: c_int,
    pub(crate) backing_store: c_int,
    pub(crate) backing_planes: c_ulong,
    pub(crate) backing_pixel: c_ulong,
    pub(crate) save_under: c_int,
    pub(crate) colormap: Colormap,
    pub(crate) map_installed: c_int,
    pub(crate) map_state: c_int,
    pub(crate) all_event_masks: c_long,
    pub(crate) your_event_mask: c_long,
    pub(crate) do_not_propagate_mask: c_long,
    pub(crate) override_redirect: c_int,
    pub(crate) screen: *mut c_void,
}

#[repr(C)]
pub(crate) struct XEvent {
    pub(crate) type_: c_int,
    pub(crate) pad: [c_long; 24],
}

#[repr(C)]
pub(crate) struct XConfigureEvent {
    pub(crate) type_: c_int,
    pub(crate) serial: c_ulong,
    pub(crate) send_event: c_int,
    pub(crate) display: *mut Display,
    pub(crate) event: Window,
    pub(crate) window: Window,
    pub(crate) x: c_int,
    pub(crate) y: c_int,
    pub(crate) width: c_int,
    pub(crate) height: c_int,
    pub(crate) border_width: c_int,
    pub(crate) above: Window,
    pub(crate) override_redirect: c_int,
}

#[repr(C)]
pub(crate) struct XKeyEvent {
    pub(crate) type_: c_int,
    pub(crate) serial: c_ulong,
    pub(crate) send_event: c_int,
    pub(crate) display: *mut Display,
    pub(crate) window: Window,
    pub(crate) root: Window,
    pub(crate) subwindow: Window,
    pub(crate) time: c_ulong,
    pub(crate) x: c_int,
    pub(crate) y: c_int,
    pub(crate) x_root: c_int,
    pub(crate) y_root: c_int,
    pub(crate) state: CUint,
    pub(crate) keycode: CUint,
    pub(crate) same_screen: c_int,
}

#[repr(C)]
pub(crate) struct XButtonEvent {
    pub(crate) type_: c_int,
    pub(crate) serial: c_ulong,
    pub(crate) send_event: c_int,
    pub(crate) display: *mut Display,
    pub(crate) window: Window,
    pub(crate) root: Window,
    pub(crate) subwindow: Window,
    pub(crate) time: c_ulong,
    pub(crate) x: c_int,
    pub(crate) y: c_int,
    pub(crate) x_root: c_int,
    pub(crate) y_root: c_int,
    pub(crate) state: CUint,
    pub(crate) button: CUint,
    pub(crate) same_screen: c_int,
}

#[repr(C)]
pub(crate) struct XMotionEvent {
    pub(crate) type_: c_int,
    pub(crate) serial: c_ulong,
    pub(crate) send_event: c_int,
    pub(crate) display: *mut Display,
    pub(crate) window: Window,
    pub(crate) root: Window,
    pub(crate) subwindow: Window,
    pub(crate) time: c_ulong,
    pub(crate) x: c_int,
    pub(crate) y: c_int,
    pub(crate) x_root: c_int,
    pub(crate) y_root: c_int,
    pub(crate) state: CUint,
    pub(crate) is_hint: c_char,
    pub(crate) same_screen: c_int,
}

#[repr(C)]
pub(crate) struct XClientMessageEvent {
    pub(crate) type_: c_int,
    pub(crate) serial: c_ulong,
    pub(crate) send_event: c_int,
    pub(crate) display: *mut Display,
    pub(crate) window: Window,
    pub(crate) message_type: Atom,
    pub(crate) format: c_int,
    pub(crate) data: [c_long; 5],
}

#[repr(C)]
pub(crate) struct XSizeHints {
    pub(crate) flags: c_long,
    pub(crate) x: c_int,
    pub(crate) y: c_int,
    pub(crate) width: c_int,
    pub(crate) height: c_int,
    pub(crate) min_width: c_int,
    pub(crate) min_height: c_int,
    pub(crate) max_width: c_int,
    pub(crate) max_height: c_int,
    pub(crate) width_inc: c_int,
    pub(crate) height_inc: c_int,
    pub(crate) min_aspect_x: c_int,
    pub(crate) min_aspect_y: c_int,
    pub(crate) max_aspect_x: c_int,
    pub(crate) max_aspect_y: c_int,
    pub(crate) base_width: c_int,
    pub(crate) base_height: c_int,
    pub(crate) win_gravity: c_int,
}

#[repr(C)]
pub(crate) struct XSelectionRequestEvent {
    pub(crate) type_: c_int,
    pub(crate) serial: c_ulong,
    pub(crate) send_event: c_int,
    pub(crate) display: *mut Display,
    pub(crate) owner: Window,
    pub(crate) requestor: Window,
    pub(crate) selection: Atom,
    pub(crate) target: Atom,
    pub(crate) property: Atom,
    pub(crate) time: c_ulong,
}

#[repr(C)]
pub(crate) struct XSelectionEvent {
    pub(crate) type_: c_int,
    pub(crate) serial: c_ulong,
    pub(crate) send_event: c_int,
    pub(crate) display: *mut Display,
    pub(crate) requestor: Window,
    pub(crate) selection: Atom,
    pub(crate) target: Atom,
    pub(crate) property: Atom,
    pub(crate) time: c_ulong,
}

pub(crate) type CUint = u32;

pub(crate) const XK_BACKSPACE: c_ulong = 0xff08;
pub(crate) const XK_TAB: c_ulong = 0xff09;
pub(crate) const XK_RETURN: c_ulong = 0xff0d;
pub(crate) const XK_ESCAPE: c_ulong = 0xff1b;
pub(crate) const XK_HOME: c_ulong = 0xff50;
pub(crate) const XK_LEFT: c_ulong = 0xff51;
pub(crate) const XK_UP: c_ulong = 0xff52;
pub(crate) const XK_RIGHT: c_ulong = 0xff53;
pub(crate) const XK_DOWN: c_ulong = 0xff54;
pub(crate) const XK_PAGE_UP: c_ulong = 0xff55;
pub(crate) const XK_PAGE_DOWN: c_ulong = 0xff56;
pub(crate) const XK_END: c_ulong = 0xff57;
pub(crate) const XK_DELETE: c_ulong = 0xffff;
pub(crate) const XK_F1: c_ulong = 0xffbe;
pub(crate) const XK_F2: c_ulong = 0xffbf;
pub(crate) const XK_F3: c_ulong = 0xffc0;
pub(crate) const XK_F4: c_ulong = 0xffc1;
pub(crate) const XK_F5: c_ulong = 0xffc2;

#[repr(C)]
pub(crate) struct XComposeStatus {
    pub(crate) compose_ptr: *mut c_char,
    pub(crate) chars_matched: c_int,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub(crate) struct XRenderColor {
    pub(crate) red: u16,
    pub(crate) green: u16,
    pub(crate) blue: u16,
    pub(crate) alpha: u16,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub(crate) struct XftColor {
    pub(crate) pixel: c_ulong,
    pub(crate) color: XRenderColor,
}

#[repr(C)]
pub(crate) struct XGlyphInfo {
    pub(crate) width: u16,
    pub(crate) height: u16,
    pub(crate) x: i16,
    pub(crate) y: i16,
    pub(crate) x_off: i16,
    pub(crate) y_off: i16,
}

#[repr(C)]
pub(crate) struct XRectangle {
    pub(crate) x: c_short,
    pub(crate) y: c_short,
    pub(crate) width: c_ushort,
    pub(crate) height: c_ushort,
}

pub(crate) type XftDraw = c_void;
#[repr(C)]
pub(crate) struct XftFont {
    pub(crate) ascent: c_int,
    pub(crate) descent: c_int,
    pub(crate) height: c_int,
    pub(crate) max_advance_width: c_int,
    pub(crate) charset: *mut c_void,
    pub(crate) pattern: *mut c_void,
}
pub(crate) type Xim = c_void;
pub(crate) type Xic = c_void;

#[link(name = "X11")]
unsafe extern "C" {
    pub(crate) fn XOpenDisplay(name: *const c_char) -> *mut Display;
    pub(crate) fn XSetErrorHandler(
        handler: Option<unsafe extern "C" fn(*mut Display, *mut XErrorEvent) -> c_int>,
    ) -> Option<unsafe extern "C" fn(*mut Display, *mut XErrorEvent) -> c_int>;
    pub(crate) fn XDefaultScreen(display: *mut Display) -> c_int;
    pub(crate) fn XRootWindow(display: *mut Display, screen: c_int) -> Window;
    pub(crate) fn XTranslateCoordinates(
        display: *mut Display,
        src_w: Window,
        dest_w: Window,
        src_x: c_int,
        src_y: c_int,
        dest_x: *mut c_int,
        dest_y: *mut c_int,
        child: *mut Window,
    ) -> c_int;
    pub(crate) fn XMoveWindow(display: *mut Display, window: Window, x: c_int, y: c_int) -> c_int;
    pub(crate) fn XMoveResizeWindow(
        display: *mut Display,
        window: Window,
        x: c_int,
        y: c_int,
        width: CUint,
        height: CUint,
    ) -> c_int;
    pub(crate) fn XDefaultVisual(display: *mut Display, screen: c_int) -> *mut c_void;
    pub(crate) fn XDisplayWidth(display: *mut Display, screen: c_int) -> c_int;
    pub(crate) fn XDisplayHeight(display: *mut Display, screen: c_int) -> c_int;
    pub(crate) fn XDefaultDepth(display: *mut Display, screen: c_int) -> c_int;
    pub(crate) fn XDefaultColormap(display: *mut Display, screen: c_int) -> Colormap;
    pub(crate) fn XCreateColormap(
        display: *mut Display,
        window: Window,
        visual: *mut c_void,
        alloc: c_int,
    ) -> Colormap;
    pub(crate) fn XCreateWindow(
        display: *mut Display,
        parent: Window,
        x: c_int,
        y: c_int,
        width: CUint,
        height: CUint,
        border_width: CUint,
        depth: c_int,
        class: CUint,
        visual: *mut c_void,
        valuemask: c_ulong,
        attributes: *mut XSetWindowAttributes,
    ) -> Window;
    pub(crate) fn XStoreName(display: *mut Display, window: Window, name: *const c_char) -> c_int;
    pub(crate) fn XSetWMNormalHints(display: *mut Display, window: Window, hints: *mut XSizeHints);
    pub(crate) fn XInternAtom(
        display: *mut Display,
        name: *const c_char,
        only_if_exists: c_int,
    ) -> Atom;
    pub(crate) fn XSetWMProtocols(
        display: *mut Display,
        window: Window,
        protocols: *mut Atom,
        count: c_int,
    ) -> c_int;
    pub(crate) fn XMapWindow(display: *mut Display, window: Window) -> c_int;
    pub(crate) fn XCreatePixmap(
        display: *mut Display,
        drawable: Window,
        width: CUint,
        height: CUint,
        depth: CUint,
    ) -> Pixmap;
    pub(crate) fn XFreePixmap(display: *mut Display, pixmap: Pixmap) -> c_int;
    pub(crate) fn XCreateGC(
        display: *mut Display,
        drawable: Window,
        valuemask: c_ulong,
        values: *mut c_void,
    ) -> GC;
    pub(crate) fn XFreeGC(display: *mut Display, gc: GC) -> c_int;
    pub(crate) fn XCopyArea(
        display: *mut Display,
        source: Pixmap,
        destination: Window,
        gc: GC,
        source_x: c_int,
        source_y: c_int,
        width: CUint,
        height: CUint,
        destination_x: c_int,
        destination_y: c_int,
    ) -> c_int;
    pub(crate) fn XSetInputFocus(
        display: *mut Display,
        focus: Window,
        revert_to: c_int,
        time: c_ulong,
    ) -> c_int;
    pub(crate) fn XGetWindowAttributes(
        display: *mut Display,
        window: Window,
        attributes: *mut XWindowAttributes,
    ) -> c_int;
    pub(crate) fn XGrabPointer(
        display: *mut Display,
        grab_window: Window,
        owner_events: c_int,
        event_mask: c_ulong,
        pointer_mode: c_int,
        keyboard_mode: c_int,
        confine_to: Window,
        cursor: Cursor,
        time: c_ulong,
    ) -> c_int;
    pub(crate) fn XUngrabPointer(display: *mut Display, time: c_ulong) -> c_int;
    pub(crate) fn XQueryPointer(
        display: *mut Display,
        window: Window,
        root_return: *mut Window,
        child_return: *mut Window,
        root_x_return: *mut c_int,
        root_y_return: *mut c_int,
        win_x_return: *mut c_int,
        win_y_return: *mut c_int,
        mask_return: *mut CUint,
    ) -> c_int;
    pub(crate) fn XCreateFontCursor(display: *mut Display, shape: CUint) -> Cursor;
    pub(crate) fn XDefineCursor(display: *mut Display, window: Window, cursor: Cursor) -> c_int;
    pub(crate) fn XUndefineCursor(display: *mut Display, window: Window) -> c_int;
    pub(crate) fn XFreeCursor(display: *mut Display, cursor: Cursor) -> c_int;
    pub(crate) fn XSetSelectionOwner(
        display: *mut Display,
        selection: Atom,
        owner: Window,
        time: c_ulong,
    );
    pub(crate) fn XConvertSelection(
        display: *mut Display,
        selection: Atom,
        target: Atom,
        property: Atom,
        requestor: Window,
        time: c_ulong,
    );
    pub(crate) fn XChangeProperty(
        display: *mut Display,
        window: Window,
        property: Atom,
        type_: Atom,
        format: c_int,
        mode: c_int,
        data: *const u8,
        nelements: c_int,
    );
    pub(crate) fn XGetWindowProperty(
        display: *mut Display,
        window: Window,
        property: Atom,
        long_offset: c_long,
        long_length: c_long,
        delete: c_int,
        req_type: Atom,
        actual_type: *mut Atom,
        actual_format: *mut c_int,
        nitems: *mut c_ulong,
        bytes_after: *mut c_ulong,
        prop: *mut *mut u8,
    ) -> c_int;
    pub(crate) fn XDeleteProperty(display: *mut Display, window: Window, property: Atom);
    pub(crate) fn XSendEvent(
        display: *mut Display,
        window: Window,
        propagate: c_int,
        event_mask: c_long,
        event: *mut XEvent,
    ) -> c_int;
    pub(crate) fn XFree(data: *mut c_void) -> c_int;
    pub(crate) fn XPending(display: *mut Display) -> c_int;
    pub(crate) fn XNextEvent(display: *mut Display, event: *mut XEvent) -> c_int;
    pub(crate) fn XLookupString(
        event: *mut XKeyEvent,
        buffer: *mut c_char,
        length: c_int,
        keysym: *mut c_ulong,
        status: *mut XComposeStatus,
    ) -> c_int;
    pub(crate) fn XOpenIM(
        display: *mut Display,
        db: *mut c_void,
        res_name: *mut c_char,
        res_class: *mut c_char,
    ) -> *mut Xim;
    pub(crate) fn XCloseIM(im: *mut Xim) -> c_int;
    pub(crate) fn XCreateIC(im: *mut Xim, ...) -> *mut Xic;
    pub(crate) fn XDestroyIC(ic: *mut Xic);
    pub(crate) fn XSetICFocus(ic: *mut Xic);
    pub(crate) fn XUnsetICFocus(ic: *mut Xic);
    pub(crate) fn Xutf8LookupString(
        ic: *mut Xic,
        event: *mut XKeyEvent,
        buffer: *mut c_char,
        length: c_int,
        keysym: *mut c_ulong,
        status: *mut c_int,
    ) -> c_int;
    pub(crate) fn XFlush(display: *mut Display) -> c_int;
    pub(crate) fn XSync(display: *mut Display, discard: c_int) -> c_int;
    pub(crate) fn XDestroyWindow(display: *mut Display, window: Window) -> c_int;
    pub(crate) fn XCloseDisplay(display: *mut Display) -> c_int;
}

#[link(name = "Xft")]
unsafe extern "C" {
    pub(crate) fn XftDrawCreate(
        display: *mut Display,
        drawable: Window,
        visual: *mut c_void,
        colormap: Colormap,
    ) -> *mut XftDraw;
    pub(crate) fn XftDrawDestroy(draw: *mut XftDraw);
    pub(crate) fn XftFontOpenName(
        display: *mut Display,
        screen: c_int,
        name: *const c_char,
    ) -> *mut XftFont;
    pub(crate) fn XftFontClose(display: *mut Display, font: *mut XftFont);
    pub(crate) fn XftColorAllocValue(
        display: *mut Display,
        visual: *mut c_void,
        colormap: Colormap,
        color: *const XRenderColor,
        result: *mut XftColor,
    ) -> c_int;
    pub(crate) fn XftColorFree(
        display: *mut Display,
        visual: *mut c_void,
        colormap: Colormap,
        color: *mut XftColor,
    );
    pub(crate) fn XftTextExtentsUtf8(
        display: *mut Display,
        font: *mut XftFont,
        string: *const u8,
        length: c_int,
        extents: *mut XGlyphInfo,
    );
    pub(crate) fn XftDrawStringUtf8(
        draw: *mut XftDraw,
        color: *const XftColor,
        font: *mut XftFont,
        x: c_int,
        y: c_int,
        string: *const u8,
        length: c_int,
    );
    pub(crate) fn XftDrawSetClipRectangles(
        draw: *mut XftDraw,
        x_origin: c_int,
        y_origin: c_int,
        rectangles: *mut XRectangle,
        n_rectangles: c_int,
    );
}

#[link(name = "GL")]
unsafe extern "C" {
    pub(crate) fn glXChooseVisual(
        display: *mut Display,
        screen: c_int,
        attributes: *mut c_int,
    ) -> *mut XVisualInfo;
    pub(crate) fn glXCreateContext(
        display: *mut Display,
        visual: *mut XVisualInfo,
        share: GLXContext,
        direct: c_int,
    ) -> GLXContext;
    pub(crate) fn glXMakeCurrent(
        display: *mut Display,
        drawable: Window,
        context: GLXContext,
    ) -> c_int;
    pub(crate) fn glXDestroyContext(display: *mut Display, context: GLXContext);
    pub(crate) fn glXCreateGLXPixmap(
        display: *mut Display,
        visual: *mut XVisualInfo,
        pixmap: Pixmap,
    ) -> GLXPixmap;
    pub(crate) fn glXDestroyGLXPixmap(display: *mut Display, pixmap: GLXPixmap);
    pub(crate) fn glXWaitGL();
    pub(crate) fn glXWaitX();
    pub(crate) fn glClearColor(red: f32, green: f32, blue: f32, alpha: f32);
    pub(crate) fn glClear(mask: u32);
    pub(crate) fn glClearStencil(value: c_int);
    pub(crate) fn glFinish();
    pub(crate) fn glEnable(cap: u32);
    pub(crate) fn glDisable(cap: u32);
    pub(crate) fn glColorMask(red: u8, green: u8, blue: u8, alpha: u8);
    pub(crate) fn glColor4f(red: f32, green: f32, blue: f32, alpha: f32);
    pub(crate) fn glBlendFunc(source: u32, destination: u32);
    pub(crate) fn glStencilMask(mask: u32);
    pub(crate) fn glStencilFunc(function: u32, reference: c_int, mask: u32);
    pub(crate) fn glStencilOp(sfail: u32, dpfail: u32, dppass: u32);
    pub(crate) fn glGenFramebuffers(count: c_int, framebuffers: *mut u32);
    pub(crate) fn glDeleteFramebuffers(count: c_int, framebuffers: *const u32);
    pub(crate) fn glBindFramebuffer(target: u32, framebuffer: u32);
    pub(crate) fn glCheckFramebufferStatus(target: u32) -> u32;
    pub(crate) fn glFramebufferTexture2D(
        target: u32,
        attachment: u32,
        textarget: u32,
        texture: u32,
        level: c_int,
    );
    pub(crate) fn glGenTextures(count: c_int, textures: *mut u32);
    pub(crate) fn glDeleteTextures(count: c_int, textures: *const u32);
    pub(crate) fn glBindTexture(target: u32, texture: u32);
    pub(crate) fn glTexParameteri(target: u32, pname: u32, parameter: c_int);
    pub(crate) fn glTexImage2D(
        target: u32,
        level: c_int,
        internal_format: c_int,
        width: c_int,
        height: c_int,
        border: c_int,
        format: u32,
        pixel_type: u32,
        pixels: *const c_void,
    );
    pub(crate) fn glReadPixels(
        x: c_int,
        y: c_int,
        width: c_int,
        height: c_int,
        format: u32,
        pixel_type: u32,
        pixels: *mut c_void,
    );
    pub(crate) fn glTexCoord2f(s: f32, t: f32);
    pub(crate) fn glScissor(x: c_int, y: c_int, width: c_int, height: c_int);
    pub(crate) fn glFlush();
    pub(crate) fn glViewport(x: c_int, y: c_int, width: c_int, height: c_int);
    pub(crate) fn glMatrixMode(mode: u32);
    pub(crate) fn glLoadIdentity();
    pub(crate) fn glPushMatrix();
    pub(crate) fn glPopMatrix();
    pub(crate) fn glTranslatef(x: f32, y: f32, z: f32);
    pub(crate) fn glScalef(x: f32, y: f32, z: f32);
    pub(crate) fn glOrtho(left: f64, right: f64, bottom: f64, top: f64, near: f64, far: f64);
    pub(crate) fn glBegin(mode: u32);
    pub(crate) fn glEnd();
    pub(crate) fn glColor3f(red: f32, green: f32, blue: f32);
    pub(crate) fn glLineWidth(width: f32);
    pub(crate) fn glVertex2f(x: f32, y: f32);
}
