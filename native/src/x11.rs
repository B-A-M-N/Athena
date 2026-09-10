//! Linux/X11 AthenaBOX compositor.
//!
//! The terminal engine remains Alacritty.  This module owns the native window,
//! the physical chassis layout, and a single composed frame.  Text is rendered
//! with Fontconfig/Xft (including UTF-8) rather than the X11 core-font path;
//! Athena chrome and the OI scene are rendered in the same OpenGL surface.

#![allow(clippy::too_many_arguments)]

use std::env;
use std::ffi::{CString, c_char, c_int, c_long, c_short, c_ulong, c_ushort, c_void};
use std::io::Write;
use std::ptr;
use std::sync::mpsc::Receiver;
use std::thread;
use std::time::{Duration, Instant};

use alacritty_terminal::event::{OnResize, WindowSize};
use alacritty_terminal::grid::Scroll;
use alacritty_terminal::term::TermMode;
use alacritty_terminal::tty::{self, ChildEvent, EventedPty};

use athena_terminal::{
    NativePixelLayout, NativeTerminalCore, PixelRect, PromptLayout, UiFontMetrics,
};

use crate::input::InputBuffer;
use crate::{LatestProjection, Projection, VisualMode, apply_available};

#[path = "platform/clipboard.rs"]
mod clipboard;
#[path = "platform/input_method.rs"]
mod input_method;
#[path = "render/mod.rs"]
mod render;
use clipboard::Clipboard;
use input_method::{InputMethod, lookup_key, terminal_key_bytes};
use render::chassis::{PresentationControl, PresentationSettings};
use render::text::{FontRole, TextRenderer};

type Display = c_void;
type Window = c_ulong;
type Atom = c_ulong;
type Colormap = c_ulong;
type Cursor = c_ulong;
type Pixmap = c_ulong;
type GLXPixmap = c_ulong;
type GC = *mut c_void;
type GLXContext = *mut c_void;

const KEY_PRESS: c_int = 2;
const BUTTON_PRESS: c_int = 4;
const BUTTON_RELEASE: c_int = 5;
const MOTION_NOTIFY: c_int = 6;
const SELECTION_CLEAR: c_int = 29;
const SELECTION_REQUEST: c_int = 30;
const SELECTION_NOTIFY: c_int = 31;
const UNMAP_NOTIFY: c_int = 18;
const MAP_NOTIFY: c_int = 19;
const DESTROY_NOTIFY: c_int = 17;
const CONFIGURE_NOTIFY: c_int = 22;
const EXPOSE: c_int = 12;
const CLIENT_MESSAGE: c_int = 33;
const FOCUS_IN: c_int = 9;
const FOCUS_OUT: c_int = 10;
const KEY_PRESS_MASK: c_long = 1;
const BUTTON_PRESS_MASK: c_long = 1 << 2;
const BUTTON_RELEASE_MASK: c_long = 1 << 3;
const POINTER_MOTION_MASK: c_long = 1 << 6;
const STRUCTURE_NOTIFY_MASK: c_long = 1 << 17;
const EXPOSURE_MASK: c_long = 1 << 15;
const FOCUS_CHANGE_MASK: c_long = 1 << 21;
const CW_EVENT_MASK: c_ulong = 1 << 11;
const CW_COLORMAP: c_ulong = 1 << 13;
const INPUT_OUTPUT: c_int = 1;
const GLX_RGBA: c_int = 4;
const GLX_RED_SIZE: c_int = 8;
const GLX_GREEN_SIZE: c_int = 9;
const GLX_BLUE_SIZE: c_int = 10;
const GLX_DEPTH_SIZE: c_int = 12;
const GLX_STENCIL_SIZE: c_int = 13;
const GL_COLOR_BUFFER_BIT: u32 = 0x0000_4000;
const GL_STENCIL_BUFFER_BIT: u32 = 0x0000_0400;
const GL_QUADS: u32 = 0x0007;
const GL_LINE_LOOP: u32 = 0x0002;
const GL_LINES: u32 = 0x0001;
const GL_POLYGON: u32 = 0x0009;
const GL_PROJECTION: u32 = 0x1701;
const GL_MODELVIEW: u32 = 0x1700;
const GL_SCISSOR_TEST: u32 = 0x0c11;
const GL_STENCIL_TEST: u32 = 0x0b90;
const GL_BLEND: u32 = 0x0be2;
const GL_SRC_ALPHA: u32 = 0x0302;
const GL_ONE_MINUS_SRC_ALPHA: u32 = 0x0303;
const GL_ALWAYS: u32 = 0x0207;
const GL_EQUAL: u32 = 0x0202;
const GL_KEEP: u32 = 0x1e00;
const GL_REPLACE: u32 = 0x1e01;
const GL_FRAMEBUFFER: u32 = 0x8d40;
const GL_COLOR_ATTACHMENT0: u32 = 0x8ce0;
const GL_FRAMEBUFFER_COMPLETE: u32 = 0x8cd5;
const GL_TEXTURE_2D: u32 = 0x0de1;
const GL_RGBA: u32 = 0x1908;
const GL_RGB: u32 = 0x1907;
const GL_UNSIGNED_BYTE: u32 = 0x1401;
const GL_TEXTURE_MIN_FILTER: u32 = 0x2801;
const GL_TEXTURE_MAG_FILTER: u32 = 0x2800;
const GL_TEXTURE_WRAP_S: u32 = 0x2802;
const GL_TEXTURE_WRAP_T: u32 = 0x2803;
const GL_NEAREST: c_int = 0x2600;
const GL_LINEAR: c_int = 0x2601;
const GL_REPEAT: c_int = 0x2901;
const GL_CLAMP_TO_EDGE: c_int = 0x812f;
const SHIFT_MASK: CUint = 1;
const CONTROL_MASK: CUint = 1 << 2;
const BUTTON1_MASK: CUint = 1 << 8;
const CURRENT_TIME: c_ulong = 0;
const IS_VIEWABLE: c_int = 2;
const PROP_MODE_REPLACE: c_int = 0;
const P_MIN_SIZE: c_long = 1 << 4;
const P_BASE_SIZE: c_long = 1 << 8;
const SUBSTRUCTURE_NOTIFY_MASK: c_long = 1 << 19;
const SUBSTRUCTURE_REDIRECT_MASK: c_long = 1 << 20;
const X_BUFFER_OVERFLOW: c_int = -1;
const MAX_XIM_BUFFER: usize = 16 * 1024;
const MAX_CACHED_XFT_COLORS: usize = 256;
const MAX_CACHED_TEXT_WIDTHS: usize = 512;
const ACTIVE_FRAME_INTERVAL: Duration = Duration::from_millis(100);
const IDLE_POLL_INTERVAL: Duration = Duration::from_millis(50);
const RESIZE_EDGE: i32 = 12;
const GRAB_MODE_ASYNC: c_int = 1;

fn initial_window_size(display: *mut Display, screen: c_int) -> (i32, i32) {
    let screen_width = unsafe { XDisplayWidth(display, screen) }.max(640);
    let screen_height = unsafe { XDisplayHeight(display, screen) }.max(480);
    // Leave a small amount of desktop context while opening large enough to
    // expose the actual cabinet proportions immediately.
    (
        ((screen_width as f32 * 0.88).round() as i32).clamp(640, screen_width),
        ((screen_height as f32 * 0.88).round() as i32).clamp(480, screen_height),
    )
}

/// Supplies the phase used by the composed presentation.
///
/// Production rendering follows wall time.  ``ATHENA_PRESENTATION_CLOCK``
/// enables a deterministic frame clock for temporal release evidence:
/// ``fixed`` advances by the normal 100 ms frame interval and
/// ``fixed:<seconds>`` accepts an explicit positive step.  The clock only
/// controls presentation phase; X11 polling and process lifecycle remain
/// unchanged.
#[derive(Debug, Clone, Copy)]
struct PresentationClock {
    started_at: Instant,
    fixed_step_seconds: Option<f32>,
}

impl PresentationClock {
    fn from_environment() -> Result<Self, String> {
        let fixed_step_seconds = match env::var("ATHENA_PRESENTATION_CLOCK") {
            Ok(value) if value == "fixed" => Some(ACTIVE_FRAME_INTERVAL.as_secs_f32()),
            Ok(value) => {
                let raw = value.strip_prefix("fixed:").ok_or_else(|| {
                    "ATHENA_PRESENTATION_CLOCK must be fixed or fixed:<seconds>".to_owned()
                })?;
                let seconds = raw
                    .parse::<f32>()
                    .map_err(|_| "fixed presentation clock step must be a number".to_owned())?;
                if !seconds.is_finite() || seconds <= 0.0 {
                    return Err("fixed presentation clock step must be positive".to_owned());
                }
                Some(seconds)
            }
            Err(env::VarError::NotPresent) => None,
            Err(error) => return Err(format!("could not read presentation clock: {error}")),
        };
        Ok(Self {
            started_at: Instant::now(),
            fixed_step_seconds,
        })
    }

    #[cfg(test)]
    fn fixed(step_seconds: f32) -> Self {
        Self {
            started_at: Instant::now(),
            fixed_step_seconds: Some(step_seconds),
        }
    }

    fn seconds_at_frame(&self, frame_sequence: u64) -> f32 {
        self.fixed_step_seconds
            .map(|step| step * frame_sequence as f32)
            .unwrap_or_else(|| self.started_at.elapsed().as_secs_f32())
    }
}

#[repr(C)]
struct XVisualInfo {
    visual: *mut c_void,
    visualid: c_ulong,
    screen: c_int,
    depth: c_int,
    class: c_int,
    red_mask: c_ulong,
    green_mask: c_ulong,
    blue_mask: c_ulong,
    colormap_size: c_int,
    bits_per_rgb: c_int,
}

#[repr(C)]
struct XErrorEvent {
    type_: c_int,
    display: *mut Display,
    resourceid: c_ulong,
    serial: c_ulong,
    error_code: u8,
    request_code: u8,
    minor_code: u8,
}

#[repr(C)]
struct XSetWindowAttributes {
    background_pixmap: c_ulong,
    background_pixel: c_ulong,
    border_pixmap: c_ulong,
    border_pixel: c_ulong,
    bit_gravity: c_int,
    win_gravity: c_int,
    backing_store: c_int,
    backing_planes: c_ulong,
    backing_pixel: c_ulong,
    save_under: c_int,
    event_mask: c_long,
    do_not_propagate_mask: c_long,
    override_redirect: c_int,
    colormap: Colormap,
    cursor: c_ulong,
}

#[repr(C)]
struct XWindowAttributes {
    x: c_int,
    y: c_int,
    width: c_int,
    height: c_int,
    border_width: c_int,
    depth: c_int,
    visual: *mut c_void,
    root: Window,
    class: c_int,
    bit_gravity: c_int,
    win_gravity: c_int,
    backing_store: c_int,
    backing_planes: c_ulong,
    backing_pixel: c_ulong,
    save_under: c_int,
    colormap: Colormap,
    map_installed: c_int,
    map_state: c_int,
    all_event_masks: c_long,
    your_event_mask: c_long,
    do_not_propagate_mask: c_long,
    override_redirect: c_int,
    screen: *mut c_void,
}

#[repr(C)]
struct XEvent {
    type_: c_int,
    pad: [c_long; 24],
}

#[repr(C)]
struct XConfigureEvent {
    type_: c_int,
    serial: c_ulong,
    send_event: c_int,
    display: *mut Display,
    event: Window,
    window: Window,
    x: c_int,
    y: c_int,
    width: c_int,
    height: c_int,
    border_width: c_int,
    above: Window,
    override_redirect: c_int,
}

#[repr(C)]
struct XKeyEvent {
    type_: c_int,
    serial: c_ulong,
    send_event: c_int,
    display: *mut Display,
    window: Window,
    root: Window,
    subwindow: Window,
    time: c_ulong,
    x: c_int,
    y: c_int,
    x_root: c_int,
    y_root: c_int,
    state: CUint,
    keycode: CUint,
    same_screen: c_int,
}

#[repr(C)]
struct XButtonEvent {
    type_: c_int,
    serial: c_ulong,
    send_event: c_int,
    display: *mut Display,
    window: Window,
    root: Window,
    subwindow: Window,
    time: c_ulong,
    x: c_int,
    y: c_int,
    x_root: c_int,
    y_root: c_int,
    state: CUint,
    button: CUint,
    same_screen: c_int,
}

#[repr(C)]
struct XMotionEvent {
    type_: c_int,
    serial: c_ulong,
    send_event: c_int,
    display: *mut Display,
    window: Window,
    root: Window,
    subwindow: Window,
    time: c_ulong,
    x: c_int,
    y: c_int,
    x_root: c_int,
    y_root: c_int,
    state: CUint,
    is_hint: c_char,
    same_screen: c_int,
}

#[repr(C)]
struct XClientMessageEvent {
    type_: c_int,
    serial: c_ulong,
    send_event: c_int,
    display: *mut Display,
    window: Window,
    message_type: Atom,
    format: c_int,
    data: [c_long; 5],
}

#[repr(C)]
struct XSizeHints {
    flags: c_long,
    x: c_int,
    y: c_int,
    width: c_int,
    height: c_int,
    min_width: c_int,
    min_height: c_int,
    max_width: c_int,
    max_height: c_int,
    width_inc: c_int,
    height_inc: c_int,
    min_aspect_x: c_int,
    min_aspect_y: c_int,
    max_aspect_x: c_int,
    max_aspect_y: c_int,
    base_width: c_int,
    base_height: c_int,
    win_gravity: c_int,
}

#[repr(C)]
struct XSelectionRequestEvent {
    type_: c_int,
    serial: c_ulong,
    send_event: c_int,
    display: *mut Display,
    owner: Window,
    requestor: Window,
    selection: Atom,
    target: Atom,
    property: Atom,
    time: c_ulong,
}

#[repr(C)]
struct XSelectionEvent {
    type_: c_int,
    serial: c_ulong,
    send_event: c_int,
    display: *mut Display,
    requestor: Window,
    selection: Atom,
    target: Atom,
    property: Atom,
    time: c_ulong,
}

type CUint = u32;

const XK_BACKSPACE: c_ulong = 0xff08;
const XK_TAB: c_ulong = 0xff09;
const XK_RETURN: c_ulong = 0xff0d;
const XK_ESCAPE: c_ulong = 0xff1b;
const XK_HOME: c_ulong = 0xff50;
const XK_LEFT: c_ulong = 0xff51;
const XK_UP: c_ulong = 0xff52;
const XK_RIGHT: c_ulong = 0xff53;
const XK_DOWN: c_ulong = 0xff54;
const XK_PAGE_UP: c_ulong = 0xff55;
const XK_PAGE_DOWN: c_ulong = 0xff56;
const XK_END: c_ulong = 0xff57;
const XK_DELETE: c_ulong = 0xffff;
const XK_F1: c_ulong = 0xffbe;
const XK_F2: c_ulong = 0xffbf;
const XK_F3: c_ulong = 0xffc0;
const XK_F4: c_ulong = 0xffc1;
const XK_F5: c_ulong = 0xffc2;

#[repr(C)]
struct XComposeStatus {
    compose_ptr: *mut c_char,
    chars_matched: c_int,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct XRenderColor {
    red: u16,
    green: u16,
    blue: u16,
    alpha: u16,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct XftColor {
    pixel: c_ulong,
    color: XRenderColor,
}

#[repr(C)]
struct XGlyphInfo {
    width: u16,
    height: u16,
    x: i16,
    y: i16,
    x_off: i16,
    y_off: i16,
}

#[repr(C)]
struct XRectangle {
    x: c_short,
    y: c_short,
    width: c_ushort,
    height: c_ushort,
}

type XftDraw = c_void;
#[repr(C)]
struct XftFont {
    ascent: c_int,
    descent: c_int,
    height: c_int,
    max_advance_width: c_int,
    charset: *mut c_void,
    pattern: *mut c_void,
}
type Xim = c_void;
type Xic = c_void;

/// Native renderer switches that are intentionally presentation-only.
#[derive(Clone, Debug)]
pub struct RendererOptions {
    pub mascot: String,
    pub animations: bool,
    pub reduced_motion: bool,
    pub text_scale: f32,
    pub cabinet_only: bool,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct DirtyDomains {
    full: bool,
    terminal: bool,
    oi_motion: bool,
}

impl Default for RendererOptions {
    fn default() -> Self {
        Self {
            mascot: "owl".to_owned(),
            animations: true,
            reduced_motion: false,
            text_scale: 1.0,
            cabinet_only: false,
        }
    }
}

#[link(name = "X11")]
unsafe extern "C" {
    fn XOpenDisplay(name: *const c_char) -> *mut Display;
    fn XSetErrorHandler(
        handler: Option<unsafe extern "C" fn(*mut Display, *mut XErrorEvent) -> c_int>,
    ) -> Option<unsafe extern "C" fn(*mut Display, *mut XErrorEvent) -> c_int>;
    fn XDefaultScreen(display: *mut Display) -> c_int;
    fn XRootWindow(display: *mut Display, screen: c_int) -> Window;
    fn XTranslateCoordinates(
        display: *mut Display,
        src_w: Window,
        dest_w: Window,
        src_x: c_int,
        src_y: c_int,
        dest_x: *mut c_int,
        dest_y: *mut c_int,
        child: *mut Window,
    ) -> c_int;
    fn XMoveWindow(display: *mut Display, window: Window, x: c_int, y: c_int) -> c_int;
    fn XMoveResizeWindow(
        display: *mut Display,
        window: Window,
        x: c_int,
        y: c_int,
        width: CUint,
        height: CUint,
    ) -> c_int;
    fn XDefaultVisual(display: *mut Display, screen: c_int) -> *mut c_void;
    fn XDisplayWidth(display: *mut Display, screen: c_int) -> c_int;
    fn XDisplayHeight(display: *mut Display, screen: c_int) -> c_int;
    fn XDefaultDepth(display: *mut Display, screen: c_int) -> c_int;
    fn XDefaultColormap(display: *mut Display, screen: c_int) -> Colormap;
    fn XCreateColormap(
        display: *mut Display,
        window: Window,
        visual: *mut c_void,
        alloc: c_int,
    ) -> Colormap;
    fn XCreateWindow(
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
    fn XStoreName(display: *mut Display, window: Window, name: *const c_char) -> c_int;
    fn XSetWMNormalHints(display: *mut Display, window: Window, hints: *mut XSizeHints);
    fn XInternAtom(display: *mut Display, name: *const c_char, only_if_exists: c_int) -> Atom;
    fn XSetWMProtocols(
        display: *mut Display,
        window: Window,
        protocols: *mut Atom,
        count: c_int,
    ) -> c_int;
    fn XMapWindow(display: *mut Display, window: Window) -> c_int;
    fn XCreatePixmap(
        display: *mut Display,
        drawable: Window,
        width: CUint,
        height: CUint,
        depth: CUint,
    ) -> Pixmap;
    fn XFreePixmap(display: *mut Display, pixmap: Pixmap) -> c_int;
    fn XCreateGC(
        display: *mut Display,
        drawable: Window,
        valuemask: c_ulong,
        values: *mut c_void,
    ) -> GC;
    fn XFreeGC(display: *mut Display, gc: GC) -> c_int;
    fn XCopyArea(
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
    fn XSetInputFocus(
        display: *mut Display,
        focus: Window,
        revert_to: c_int,
        time: c_ulong,
    ) -> c_int;
    fn XGetWindowAttributes(
        display: *mut Display,
        window: Window,
        attributes: *mut XWindowAttributes,
    ) -> c_int;
    fn XGrabPointer(
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
    fn XUngrabPointer(display: *mut Display, time: c_ulong) -> c_int;
    fn XCreateFontCursor(display: *mut Display, shape: CUint) -> Cursor;
    fn XDefineCursor(display: *mut Display, window: Window, cursor: Cursor) -> c_int;
    fn XUndefineCursor(display: *mut Display, window: Window) -> c_int;
    fn XFreeCursor(display: *mut Display, cursor: Cursor) -> c_int;
    fn XSetSelectionOwner(display: *mut Display, selection: Atom, owner: Window, time: c_ulong);
    fn XConvertSelection(
        display: *mut Display,
        selection: Atom,
        target: Atom,
        property: Atom,
        requestor: Window,
        time: c_ulong,
    );
    fn XChangeProperty(
        display: *mut Display,
        window: Window,
        property: Atom,
        type_: Atom,
        format: c_int,
        mode: c_int,
        data: *const u8,
        nelements: c_int,
    );
    fn XGetWindowProperty(
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
    fn XDeleteProperty(display: *mut Display, window: Window, property: Atom);
    fn XSendEvent(
        display: *mut Display,
        window: Window,
        propagate: c_int,
        event_mask: c_long,
        event: *mut XEvent,
    ) -> c_int;
    fn XFree(data: *mut c_void) -> c_int;
    fn XPending(display: *mut Display) -> c_int;
    fn XNextEvent(display: *mut Display, event: *mut XEvent) -> c_int;
    fn XLookupString(
        event: *mut XKeyEvent,
        buffer: *mut c_char,
        length: c_int,
        keysym: *mut c_ulong,
        status: *mut XComposeStatus,
    ) -> c_int;
    fn XOpenIM(
        display: *mut Display,
        db: *mut c_void,
        res_name: *mut c_char,
        res_class: *mut c_char,
    ) -> *mut Xim;
    fn XCloseIM(im: *mut Xim) -> c_int;
    fn XCreateIC(im: *mut Xim, ...) -> *mut Xic;
    fn XDestroyIC(ic: *mut Xic);
    fn XSetICFocus(ic: *mut Xic);
    fn XUnsetICFocus(ic: *mut Xic);
    fn Xutf8LookupString(
        ic: *mut Xic,
        event: *mut XKeyEvent,
        buffer: *mut c_char,
        length: c_int,
        keysym: *mut c_ulong,
        status: *mut c_int,
    ) -> c_int;
    fn XFlush(display: *mut Display) -> c_int;
    fn XSync(display: *mut Display, discard: c_int) -> c_int;
    fn XDestroyWindow(display: *mut Display, window: Window) -> c_int;
    fn XCloseDisplay(display: *mut Display) -> c_int;
}

unsafe extern "C" fn ignore_shutdown_x_error(
    _display: *mut Display,
    _error: *mut XErrorEvent,
) -> c_int {
    0
}

fn set_input_focus_if_mapped(display: *mut Display, window: Window, mapped: bool) -> bool {
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

/// Compose the complete cabinet into one X11 pixmap before copying it to the
/// visible window. OpenGL and Xft both target this drawable, so the visible
/// surface never receives a half-GL/half-text frame while a redraw is in
/// flight.
struct PresentationSurface {
    display: *mut Display,
    window: Window,
    visual: *mut XVisualInfo,
    depth: CUint,
    pixmap: Pixmap,
    glx_pixmap: GLXPixmap,
    gc: GC,
    retired: Vec<(Pixmap, GLXPixmap)>,
    width: CUint,
    height: CUint,
}

impl PresentationSurface {
    fn new(
        display: *mut Display,
        window: Window,
        visual: *mut XVisualInfo,
        depth: CUint,
        width: CUint,
        height: CUint,
    ) -> Result<Self, String> {
        let pixmap = unsafe { XCreatePixmap(display, window, width, height, depth) };
        if pixmap == 0 {
            return Err("could not create the native offscreen presentation pixmap".to_owned());
        }
        let glx_pixmap = unsafe { glXCreateGLXPixmap(display, visual, pixmap) };
        if glx_pixmap == 0 {
            unsafe { XFreePixmap(display, pixmap) };
            return Err("could not create the native GLX presentation pixmap".to_owned());
        }
        let gc = unsafe { XCreateGC(display, window, 0, ptr::null_mut()) };
        if gc.is_null() {
            unsafe {
                glXDestroyGLXPixmap(display, glx_pixmap);
                XFreePixmap(display, pixmap);
            }
            return Err("could not create the native presentation copy context".to_owned());
        }
        Ok(Self {
            display,
            window,
            visual,
            depth,
            pixmap,
            glx_pixmap,
            gc,
            retired: Vec::new(),
            width,
            height,
        })
    }

    fn resize(&mut self, width: CUint, height: CUint) -> Result<(), String> {
        if self.width == width && self.height == height {
            return Ok(());
        }
        let pixmap = unsafe { XCreatePixmap(self.display, self.window, width, height, self.depth) };
        if pixmap == 0 {
            return Err("could not resize the native offscreen presentation pixmap".to_owned());
        }
        let glx_pixmap = unsafe { glXCreateGLXPixmap(self.display, self.visual, pixmap) };
        if glx_pixmap == 0 {
            unsafe { XFreePixmap(self.display, pixmap) };
            return Err("could not resize the native GLX presentation pixmap".to_owned());
        }
        self.retired.push((self.pixmap, self.glx_pixmap));
        self.pixmap = pixmap;
        self.glx_pixmap = glx_pixmap;
        self.width = width;
        self.height = height;
        Ok(())
    }

    fn reap_retired(&mut self) {
        for (pixmap, glx_pixmap) in self.retired.drain(..) {
            unsafe {
                glXDestroyGLXPixmap(self.display, glx_pixmap);
                XFreePixmap(self.display, pixmap);
            }
        }
    }

    fn present(&self, width: CUint, height: CUint) {
        unsafe {
            // Finish the GL command stream, wait for it before issuing the
            // XCopyArea request, then copy the composed pixmap in one server
            // request.
            glFinish();
            glXWaitGL();
            XCopyArea(
                self.display,
                self.pixmap,
                self.window,
                self.gc,
                0,
                0,
                width.min(self.width),
                height.min(self.height),
                0,
                0,
            );
            XFlush(self.display);
        }
    }

    fn destroy(&mut self) {
        unsafe {
            if self.glx_pixmap != 0 {
                glXDestroyGLXPixmap(self.display, self.glx_pixmap);
                self.glx_pixmap = 0;
            }
            if self.pixmap != 0 {
                XFreePixmap(self.display, self.pixmap);
                self.pixmap = 0;
            }
            for (pixmap, glx_pixmap) in self.retired.drain(..) {
                glXDestroyGLXPixmap(self.display, glx_pixmap);
                XFreePixmap(self.display, pixmap);
            }
            if !self.gc.is_null() {
                XFreeGC(self.display, self.gc);
                self.gc = ptr::null_mut();
            }
        }
    }
}

#[link(name = "Xft")]
unsafe extern "C" {
    fn XftDrawCreate(
        display: *mut Display,
        drawable: Window,
        visual: *mut c_void,
        colormap: Colormap,
    ) -> *mut XftDraw;
    fn XftDrawDestroy(draw: *mut XftDraw);
    fn XftFontOpenName(display: *mut Display, screen: c_int, name: *const c_char) -> *mut XftFont;
    fn XftFontClose(display: *mut Display, font: *mut XftFont);
    fn XftColorAllocValue(
        display: *mut Display,
        visual: *mut c_void,
        colormap: Colormap,
        color: *const XRenderColor,
        result: *mut XftColor,
    ) -> c_int;
    fn XftColorFree(
        display: *mut Display,
        visual: *mut c_void,
        colormap: Colormap,
        color: *mut XftColor,
    );
    fn XftTextExtentsUtf8(
        display: *mut Display,
        font: *mut XftFont,
        string: *const u8,
        length: c_int,
        extents: *mut XGlyphInfo,
    );
    fn XftDrawStringUtf8(
        draw: *mut XftDraw,
        color: *const XftColor,
        font: *mut XftFont,
        x: c_int,
        y: c_int,
        string: *const u8,
        length: c_int,
    );
    fn XftDrawSetClipRectangles(
        draw: *mut XftDraw,
        x_origin: c_int,
        y_origin: c_int,
        rectangles: *mut XRectangle,
        n_rectangles: c_int,
    );
}

#[link(name = "GL")]
unsafe extern "C" {
    fn glXChooseVisual(
        display: *mut Display,
        screen: c_int,
        attributes: *mut c_int,
    ) -> *mut XVisualInfo;
    fn glXCreateContext(
        display: *mut Display,
        visual: *mut XVisualInfo,
        share: GLXContext,
        direct: c_int,
    ) -> GLXContext;
    fn glXMakeCurrent(display: *mut Display, drawable: Window, context: GLXContext) -> c_int;
    fn glXDestroyContext(display: *mut Display, context: GLXContext);
    fn glXCreateGLXPixmap(
        display: *mut Display,
        visual: *mut XVisualInfo,
        pixmap: Pixmap,
    ) -> GLXPixmap;
    fn glXDestroyGLXPixmap(display: *mut Display, pixmap: GLXPixmap);
    fn glXWaitGL();
    fn glXWaitX();
    fn glClearColor(red: f32, green: f32, blue: f32, alpha: f32);
    fn glClear(mask: u32);
    fn glClearStencil(value: c_int);
    fn glFinish();
    fn glEnable(cap: u32);
    fn glDisable(cap: u32);
    fn glColorMask(red: u8, green: u8, blue: u8, alpha: u8);
    fn glColor4f(red: f32, green: f32, blue: f32, alpha: f32);
    fn glBlendFunc(source: u32, destination: u32);
    fn glStencilMask(mask: u32);
    fn glStencilFunc(function: u32, reference: c_int, mask: u32);
    fn glStencilOp(sfail: u32, dpfail: u32, dppass: u32);
    fn glGenFramebuffers(count: c_int, framebuffers: *mut u32);
    fn glDeleteFramebuffers(count: c_int, framebuffers: *const u32);
    fn glBindFramebuffer(target: u32, framebuffer: u32);
    fn glCheckFramebufferStatus(target: u32) -> u32;
    fn glFramebufferTexture2D(
        target: u32,
        attachment: u32,
        textarget: u32,
        texture: u32,
        level: c_int,
    );
    fn glGenTextures(count: c_int, textures: *mut u32);
    fn glDeleteTextures(count: c_int, textures: *const u32);
    fn glBindTexture(target: u32, texture: u32);
    fn glTexParameteri(target: u32, pname: u32, parameter: c_int);
    fn glTexImage2D(
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
    fn glReadPixels(
        x: c_int,
        y: c_int,
        width: c_int,
        height: c_int,
        format: u32,
        pixel_type: u32,
        pixels: *mut c_void,
    );
    fn glTexCoord2f(s: f32, t: f32);
    fn glScissor(x: c_int, y: c_int, width: c_int, height: c_int);
    fn glFlush();
    fn glViewport(x: c_int, y: c_int, width: c_int, height: c_int);
    fn glMatrixMode(mode: u32);
    fn glLoadIdentity();
    fn glPushMatrix();
    fn glPopMatrix();
    fn glTranslatef(x: f32, y: f32, z: f32);
    fn glScalef(x: f32, y: f32, z: f32);
    fn glOrtho(left: f64, right: f64, bottom: f64, top: f64, near: f64, far: f64);
    fn glBegin(mode: u32);
    fn glEnd();
    fn glColor3f(red: f32, green: f32, blue: f32);
    fn glLineWidth(width: f32);
    fn glVertex2f(x: f32, y: f32);
}

type FrameGeometry = NativePixelLayout;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ResizeZone {
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
    fn direction(self) -> c_long {
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

    fn cursor_shape(self) -> CUint {
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

fn resize_zone(x: i32, y: i32, width: i32, height: i32) -> Option<ResizeZone> {
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
enum WindowDragKind {
    Move,
    Resize(ResizeZone),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum WindowMoveStrategy {
    Ewmh,
    ClientManagedFallback,
}

const EWMH_GESTURE_GRACE: Duration = Duration::from_millis(80);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct WindowMoveTelemetry {
    ewmh_advertised: bool,
    ewmh_attempted: bool,
    ewmh_confirmed: bool,
    active_strategy: &'static str,
    fallback_reason: Option<&'static str>,
}

impl WindowMoveTelemetry {
    fn for_strategy(strategy: WindowMoveStrategy) -> Self {
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

    fn begin_ewmh(&mut self) {
        self.ewmh_attempted = true;
        self.active_strategy = "ewmh_preferred";
    }

    fn confirm_ewmh(&mut self) {
        if !self.fallback_used() {
            self.ewmh_confirmed = true;
            self.active_strategy = "ewmh_confirmed";
        }
    }

    fn activate_fallback(&mut self, reason: &'static str) {
        if !self.ewmh_confirmed {
            self.active_strategy = "client_managed";
            self.fallback_reason = Some(reason);
        }
    }

    fn fallback_used(self) -> bool {
        self.fallback_reason.is_some()
    }
}

impl WindowMoveStrategy {
    fn name(self) -> &'static str {
        match self {
            Self::Ewmh => "ewmh",
            Self::ClientManagedFallback => "client_managed_fallback",
        }
    }

    fn uses_ewmh(self) -> bool {
        matches!(self, Self::Ewmh)
    }
}

fn select_window_move_strategy(display: *mut Display, screen: c_int) -> WindowMoveStrategy {
    if net_supported_contains(display, screen, "_NET_WM_MOVERESIZE") {
        WindowMoveStrategy::Ewmh
    } else {
        WindowMoveStrategy::ClientManagedFallback
    }
}

fn net_supported_contains(display: *mut Display, screen: c_int, wanted: &str) -> bool {
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
        atoms.iter().any(|atom| *atom == wanted_atom)
    } else {
        false
    };
    if !data.is_null() {
        unsafe { XFree(data.cast()) };
    }
    found
}

fn window_management_diagnostics(
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct WindowDrag {
    kind: WindowDragKind,
    start_root_x: i32,
    start_root_y: i32,
    start_window_x: i32,
    start_window_y: i32,
    start_width: i32,
    start_height: i32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct PendingEwmhGesture {
    drag: WindowDrag,
    configure_events_at_start: u64,
    deadline: Instant,
}

impl PendingEwmhGesture {
    fn new(drag: WindowDrag, configure_events: u64) -> Self {
        Self {
            drag,
            configure_events_at_start: configure_events,
            deadline: Instant::now() + EWMH_GESTURE_GRACE,
        }
    }

    fn geometry_changed(self, x: i32, y: i32, width: i32, height: i32) -> bool {
        x != self.drag.start_window_x
            || y != self.drag.start_window_y
            || width != self.drag.start_width
            || height != self.drag.start_height
    }
}

impl WindowDrag {
    fn new(
        display: *mut Display,
        window: Window,
        button: &XButtonEvent,
        width: i32,
        height: i32,
        kind: WindowDragKind,
    ) -> Self {
        let (start_window_x, start_window_y) = window_root_position(display, window);
        Self {
            kind,
            start_root_x: button.x_root,
            start_root_y: button.y_root,
            start_window_x,
            start_window_y,
            start_width: width,
            start_height: height,
        }
    }

    fn geometry(self, root_x: i32, root_y: i32) -> (i32, i32, i32, i32) {
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

fn window_root_position(display: *mut Display, window: Window) -> (i32, i32) {
    let root = unsafe { XRootWindow(display, XDefaultScreen(display)) };
    let mut x = 0;
    let mut y = 0;
    let mut child = 0;
    let translated =
        unsafe { XTranslateCoordinates(display, window, root, 0, 0, &mut x, &mut y, &mut child) };
    if translated == 0 { (0, 0) } else { (x, y) }
}

fn apply_window_drag(
    display: *mut Display,
    window: Window,
    drag: WindowDrag,
    root_x: i32,
    root_y: i32,
) {
    let (x, y, width, height) = drag.geometry(root_x, root_y);
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

fn grab_window_pointer(display: *mut Display, window: Window) {
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

struct ResizeCursors {
    display: *mut Display,
    cursors: [Cursor; 8],
    active: Option<ResizeZone>,
}

impl ResizeCursors {
    fn new(display: *mut Display) -> Self {
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

    fn set(&mut self, window: Window, zone: Option<ResizeZone>) {
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

fn cursor_index(zone: ResizeZone) -> usize {
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

fn intern_atom(display: *mut Display, name: &str) -> Atom {
    let Ok(value) = CString::new(name) else {
        return 0;
    };
    unsafe { XInternAtom(display, value.as_ptr(), 0) }
}

fn is_wm_delete_message(
    message_type: Atom,
    format: c_int,
    first_data: c_long,
    protocols_atom: Atom,
    delete_atom: Atom,
) -> bool {
    message_type == protocols_atom && format == 32 && first_data as c_ulong == delete_atom
}

pub fn run(
    mut core: NativeTerminalCore,
    mut pty: tty::Pty,
    output_rx: Receiver<Vec<u8>>,
    bridge_rx: Option<LatestProjection>,
    mut projection: Projection,
    options: RendererOptions,
) -> Result<(), String> {
    let display = unsafe { XOpenDisplay(ptr::null()) };
    if display.is_null() {
        return Err("could not open an X11 display; use --headless for CI".to_owned());
    }
    // Window managers may destroy a drawable asynchronously while a final
    // GL/Xft request is still queued. Keep that normal close race from
    // invoking Xlib's process-aborting default handler.
    unsafe { XSetErrorHandler(Some(ignore_shutdown_x_error)) };
    let result = run_window(
        display,
        &mut core,
        &mut pty,
        output_rx,
        bridge_rx,
        &mut projection,
        &options,
    );
    unsafe { XCloseDisplay(display) };
    result
}

/// Probe the same Xft font roles used by the live compositor and serialize
/// their metric-derived geometry without opening the native event loop.
///
/// CI machines without an X server deliberately fall back in `main.rs`, but
/// that output is tagged as static so a layout dump can never masquerade as a
/// live font measurement.
pub(crate) fn dump_live_layout_json(
    width: i32,
    height: i32,
    text_scale: f32,
) -> Result<serde_json::Value, String> {
    let display = unsafe { XOpenDisplay(ptr::null()) };
    if display.is_null() {
        return Err("could not open an X11 display for live layout metrics".to_owned());
    }
    let screen = unsafe { XDefaultScreen(display) };
    let visual = unsafe { XDefaultVisual(display, screen) };
    if visual.is_null() {
        unsafe { XCloseDisplay(display) };
        return Err("X11 display has no compatible visual for live layout metrics".to_owned());
    }
    let root = unsafe { XRootWindow(display, screen) };
    let window_move_strategy = select_window_move_strategy(display, screen);
    let window_move_telemetry = WindowMoveTelemetry::for_strategy(window_move_strategy);
    let colormap = unsafe { XDefaultColormap(display, screen) };
    let mut window_attributes = XSetWindowAttributes {
        background_pixmap: 0,
        background_pixel: 0,
        border_pixmap: 0,
        border_pixel: 0,
        bit_gravity: 0,
        win_gravity: 0,
        backing_store: 0,
        backing_planes: 0,
        backing_pixel: 0,
        save_under: 0,
        event_mask: 0,
        do_not_propagate_mask: 0,
        override_redirect: 0,
        colormap,
        cursor: 0,
    };
    let window = unsafe {
        XCreateWindow(
            display,
            root,
            0,
            0,
            width.max(1) as CUint,
            height.max(1) as CUint,
            0,
            XDefaultDepth(display, screen),
            INPUT_OUTPUT as CUint,
            visual,
            CW_COLORMAP,
            &mut window_attributes,
        )
    };
    if window == 0 {
        unsafe {
            XCloseDisplay(display);
        }
        return Err("could not create an X11 drawable for live layout metrics".to_owned());
    }
    let result = (|| {
        let text = TextRenderer::new(display, screen, window, visual, colormap, text_scale)?;
        let metrics = UiFontMetrics {
            body: text.metrics_for(FontRole::Body),
            input: text.metrics_for(FontRole::Input),
            heading: text.metrics_for(FontRole::Heading),
            instrument: text.metrics_for(FontRole::Instrument),
        };
        let layout = FrameGeometry::for_window(width, height, metrics);
        let prompt = PromptLayout::from_rect(
            layout.prompt,
            if layout.compact {
                metrics.instrument
            } else {
                metrics.input
            },
            metrics.instrument,
            layout.prompt_padding_y,
            layout.prompt_gap,
            layout.prompt_bottom_padding,
            !layout.compact,
        );
        let mut dump = serde_json::to_value(layout).map_err(|error| error.to_string())?;
        let object = dump
            .as_object_mut()
            .ok_or_else(|| "native layout did not serialize as an object".to_owned())?;
        object.insert("metrics_source".to_owned(), serde_json::json!("live_xft"));
        object.insert("text_scale".to_owned(), serde_json::json!(text_scale));
        object.insert("metrics".to_owned(), serde_json::to_value(metrics).unwrap());
        object.insert(
            "font_pixel_sizes".to_owned(),
            serde_json::to_value(text.font_pixel_sizes()).unwrap(),
        );
        object.insert(
            "terminal_size".to_owned(),
            serde_json::to_value(layout.terminal_size()).unwrap(),
        );
        object.insert(
            "prompt_layout".to_owned(),
            serde_json::to_value(prompt).unwrap(),
        );
        object.insert(
            "window_management".to_owned(),
            window_management_diagnostics(
                display,
                screen,
                window_move_strategy,
                window_move_telemetry,
            ),
        );
        Ok(dump)
    })();
    unsafe {
        XDestroyWindow(display, window);
        XCloseDisplay(display);
    }
    result
}

fn run_window(
    display: *mut Display,
    core: &mut NativeTerminalCore,
    pty: &mut tty::Pty,
    output_rx: Receiver<Vec<u8>>,
    bridge_rx: Option<LatestProjection>,
    projection: &mut Projection,
    options: &RendererOptions,
) -> Result<(), String> {
    let presentation_clock = PresentationClock::from_environment()?;
    let screen = unsafe { XDefaultScreen(display) };
    let stencil_attributes = [
        GLX_RGBA,
        GLX_RED_SIZE,
        8,
        GLX_GREEN_SIZE,
        8,
        GLX_BLUE_SIZE,
        8,
        GLX_DEPTH_SIZE,
        0,
        GLX_STENCIL_SIZE,
        8,
        0,
    ];
    let fallback_attributes = [
        GLX_RGBA,
        GLX_RED_SIZE,
        8,
        GLX_GREEN_SIZE,
        8,
        GLX_BLUE_SIZE,
        8,
        GLX_DEPTH_SIZE,
        0,
        0,
    ];
    let mut visual =
        unsafe { glXChooseVisual(display, screen, stencil_attributes.as_ptr() as *mut c_int) };
    let stencil_available = !visual.is_null();
    if visual.is_null() {
        visual =
            unsafe { glXChooseVisual(display, screen, fallback_attributes.as_ptr() as *mut c_int) };
    }
    if visual.is_null() {
        return Err("X11 display has no compatible OpenGL visual".to_owned());
    }
    let root = unsafe { XRootWindow(display, screen) };
    let window_move_strategy = select_window_move_strategy(display, screen);
    let colormap = unsafe { XCreateColormap(display, root, (*visual).visual, 0) };
    let mut window_attributes = XSetWindowAttributes {
        background_pixmap: 0,
        background_pixel: 0,
        border_pixmap: 0,
        border_pixel: 0,
        bit_gravity: 0,
        win_gravity: 0,
        backing_store: 0,
        backing_planes: 0,
        backing_pixel: 0,
        save_under: 0,
        event_mask: KEY_PRESS_MASK
            | BUTTON_PRESS_MASK
            | BUTTON_RELEASE_MASK
            | POINTER_MOTION_MASK
            | STRUCTURE_NOTIFY_MASK
            | EXPOSURE_MASK
            | FOCUS_CHANGE_MASK,
        do_not_propagate_mask: 0,
        override_redirect: 0,
        colormap,
        cursor: 0,
    };
    let (initial_width, initial_height) = initial_window_size(display, screen);
    let window = unsafe {
        XCreateWindow(
            display,
            root,
            0,
            0,
            initial_width as CUint,
            initial_height as CUint,
            0,
            (*visual).depth,
            INPUT_OUTPUT as CUint,
            (*visual).visual,
            CW_COLORMAP | CW_EVENT_MASK,
            &mut window_attributes,
        )
    };
    if window == 0 {
        return Err("could not create the Athena native window".to_owned());
    }
    let title = CString::new(projection.title.as_str()).map_err(|_| "invalid window title")?;
    unsafe { XStoreName(display, window, title.as_ptr()) };
    set_window_hints(display, window);
    set_window_pid(display, window);
    let delete_atom = unsafe {
        XInternAtom(
            display,
            CString::new("WM_DELETE_WINDOW").unwrap().as_ptr(),
            0,
        )
    };
    let protocols_atom = intern_atom(display, "WM_PROTOCOLS");
    unsafe { XSetWMProtocols(display, window, &delete_atom as *const Atom as *mut Atom, 1) };
    unsafe { XMapWindow(display, window) };
    unsafe { XSync(display, 0) };

    let mut presentation_surface = match PresentationSurface::new(
        display,
        window,
        visual,
        unsafe { (*visual).depth as CUint },
        initial_width as CUint,
        initial_height as CUint,
    ) {
        Ok(surface) => surface,
        Err(error) => {
            unsafe { XDestroyWindow(display, window) };
            return Err(error);
        }
    };

    let context = unsafe { glXCreateContext(display, visual, ptr::null_mut(), 1) };
    if context.is_null() {
        presentation_surface.destroy();
        unsafe { XDestroyWindow(display, window) };
        return Err("could not create the Athena OpenGL context".to_owned());
    }
    if unsafe { glXMakeCurrent(display, presentation_surface.glx_pixmap, context) } == 0 {
        unsafe {
            glXDestroyContext(display, context);
            presentation_surface.destroy();
            XDestroyWindow(display, window);
        }
        return Err("could not make the native presentation surface current".to_owned());
    }
    let visual_ptr = unsafe { (*visual).visual };
    let mut text_zoom = options.text_scale.clamp(0.75, 2.5);
    let mut resize_cursors = ResizeCursors::new(display);
    let mut text = match TextRenderer::new(
        display,
        screen,
        presentation_surface.pixmap,
        visual_ptr,
        colormap,
        text_zoom,
    ) {
        Ok(text) => text,
        Err(error) => {
            drop(resize_cursors);
            unsafe {
                glXMakeCurrent(display, 0, ptr::null_mut());
                glXDestroyContext(display, context);
                presentation_surface.destroy();
                XDestroyWindow(display, window);
            }
            return Err(error);
        }
    };
    let oi_target = render::oi::OiTarget::new();
    let chassis_material = render::chassis::ChassisMaterial::new();
    let mut input_method = InputMethod::new(display, window);
    let mut writer = pty
        .file()
        .try_clone()
        .map_err(|error| format!("could not clone PTY writer: {error}"))?;
    let mut clipboard = Clipboard::new(display);
    let mut input_buffer = InputBuffer::default();
    let mut presentation = PresentationSettings::default();
    let mut selection: Option<((usize, usize), (usize, usize))> = None;
    let mut width = initial_width;
    let mut height = initial_height;
    let mut metrics = UiFontMetrics {
        body: text.metrics_for(FontRole::Body),
        input: text.metrics_for(FontRole::Input),
        heading: text.metrics_for(FontRole::Heading),
        instrument: text.metrics_for(FontRole::Instrument),
    };
    let mut window_move_telemetry = WindowMoveTelemetry::for_strategy(window_move_strategy);
    resize_terminal(core, pty, width, height, metrics);
    write_runtime_layout_dump(
        display,
        screen,
        width,
        height,
        metrics,
        text.font_pixel_sizes(),
        text_zoom,
        0,
        window_move_strategy,
        window_move_telemetry,
    );
    resize_cursors.set(window, None);
    // XSetInputFocus is a BadMatch until the WM has made the mapped window
    // viewable.  Defer the first request to MapNotify and only request focus
    // again after a remap; this also keeps minimize/restore from racing X11.
    let mut mapped = false;
    let mut focus_pending = true;
    let mut focused = true;
    let mut running = true;
    let mut window_destroyed = false;
    let mut child_exited = false;
    let mut window_drag: Option<WindowDrag> = None;
    let mut pending_ewmh_gesture: Option<PendingEwmhGesture> = None;
    let mut dirty = true;
    let mut terminal_dirty = false;
    let mut oi_motion_dirty = false;
    let mut activity_dirty = false;
    let mut initial_redraws = 0_u64;
    let mut event_redraws = 0_u64;
    let mut idle_redraws = 0_u64;
    let mut redraws = 0_u64;
    let mut presented_frame_sequence = 0_u64;
    let mut configure_events = 0_u64;
    let mut oi_dumped = false;
    let render_started = Instant::now();
    let cpu_started = process_cpu_seconds();
    let mut last_draw: Option<Instant> = None;
    let mut steady_started: Option<Instant> = None;
    let mut steady_cpu_started: Option<f64> = None;
    let mut steady_elapsed_seconds = 0.0_f64;
    let mut steady_cpu_seconds = 0.0_f64;
    while running {
        let changes = apply_available(core, &output_rx, bridge_rx.as_ref(), projection);
        if changes.projection {
            dirty = true;
            activity_dirty = true;
        } else if changes.terminal {
            terminal_dirty = true;
            activity_dirty = true;
        }
        while unsafe { XPending(display) } > 0 {
            let mut event = XEvent {
                type_: 0,
                pad: [0; 24],
            };
            unsafe { XNextEvent(display, &mut event) };
            if let Some(bytes) = clipboard.handle_event(display, window, &mut event) {
                if core.mode().contains(TermMode::ALT_SCREEN) {
                    let filtered: Vec<u8> = bytes
                        .into_iter()
                        .filter(|byte| *byte != 0x1b && *byte != 0x03)
                        .collect();
                    let _ = writer.write_all(b"\x1b[200~");
                    let _ = writer.write_all(&filtered);
                    let _ = writer.write_all(b"\x1b[201~");
                    let _ = writer.flush();
                } else {
                    let text = String::from_utf8_lossy(&bytes);
                    if input_buffer.insert(&text) {
                        dirty = true;
                        activity_dirty = true;
                    }
                }
                continue;
            }
            match event.type_ {
                MAP_NOTIFY => {
                    mapped = true;
                    if focus_pending
                        && !window_destroyed
                        && set_input_focus_if_mapped(display, window, mapped)
                    {
                        if let Some(input_method) = input_method.as_ref() {
                            unsafe { XSetICFocus(input_method.ic) };
                        }
                        focus_pending = false;
                        focused = true;
                        dirty = true;
                        activity_dirty = true;
                    }
                }
                UNMAP_NOTIFY => {
                    mapped = false;
                    // A minimized window may later be remapped.  Do not
                    // issue a focus request against its unmapped drawable.
                    focus_pending = true;
                    focused = false;
                    dirty = true;
                    activity_dirty = true;
                }
                KEY_PRESS => {
                    let key_event = unsafe { &mut *(&mut event as *mut XEvent as *mut XKeyEvent) };
                    let lookup = lookup_key(input_method.as_mut(), key_event);
                    let keysym = lookup.keysym;
                    let control = key_event.state & CONTROL_MASK != 0;
                    let selecting = key_event.state & SHIFT_MASK != 0;
                    let terminal_app = core.mode().contains(TermMode::ALT_SCREEN);
                    if key_event.state & (CONTROL_MASK | SHIFT_MASK) == (CONTROL_MASK | SHIFT_MASK)
                        && matches!(keysym, k if k == 'c' as c_ulong || k == 'C' as c_ulong)
                    {
                        if let Some(selected) = input_buffer.selected_text() {
                            clipboard.own(display, window, selected.to_owned());
                        } else if let Some((anchor, extent)) = selection {
                            let (start, end) = selection_bounds(anchor, extent);
                            let end = (end.0.saturating_add(1), end.1);
                            clipboard.own(display, window, core.selection_text(start, end));
                        }
                    } else if key_event.state & (CONTROL_MASK | SHIFT_MASK)
                        == (CONTROL_MASK | SHIFT_MASK)
                        && matches!(keysym, k if k == 'v' as c_ulong || k == 'V' as c_ulong)
                    {
                        clipboard.request(display, window);
                    } else if keysym == XK_ESCAPE {
                        // Escape remains a terminal byte. It never closes the
                        // native window and does not mutate the prompt buffer.
                        let _ = writer.write_all(&[0x1b]);
                        let _ = writer.flush();
                        dirty = true;
                        activity_dirty = true;
                    } else if control
                        && matches!(keysym, k if k == 'c' as c_ulong || k == 'C' as c_ulong)
                    {
                        // The PTY line discipline turns this into SIGINT for
                        // the Python session; that session cancels its
                        // foreground task and keeps the window alive.
                        input_buffer.clear();
                        let _ = writer.write_all(&[0x03]);
                        let _ = writer.flush();
                        dirty = true;
                        activity_dirty = true;
                    } else if control
                        && matches!(
                            keysym,
                            k if k == '+' as c_ulong || k == '=' as c_ulong || k == '-' as c_ulong
                                || k == '_' as c_ulong || k == '0' as c_ulong
                        )
                    {
                        // Native zoom is presentation-only: it reopens the
                        // Xft faces and resizes the PTY from the same layout
                        // contract without changing projection/backend state.
                        text_zoom = match keysym {
                            k if k == '+' as c_ulong || k == '=' as c_ulong => {
                                (text_zoom * 1.10).min(2.5)
                            }
                            k if k == '-' as c_ulong || k == '_' as c_ulong => {
                                (text_zoom / 1.10).max(0.75)
                            }
                            _ => options.text_scale.clamp(0.75, 2.5),
                        };
                        match text.reconfigure_for_scale(text_zoom) {
                            Ok(_) => {
                                metrics = UiFontMetrics {
                                    body: text.metrics_for(FontRole::Body),
                                    input: text.metrics_for(FontRole::Input),
                                    heading: text.metrics_for(FontRole::Heading),
                                    instrument: text.metrics_for(FontRole::Instrument),
                                };
                                resize_terminal(core, pty, width, height, metrics);
                                configure_events = configure_events.saturating_add(1);
                                write_runtime_layout_dump(
                                    display,
                                    screen,
                                    width,
                                    height,
                                    metrics,
                                    text.font_pixel_sizes(),
                                    text_zoom,
                                    configure_events,
                                    window_move_strategy,
                                    window_move_telemetry,
                                );
                                dirty = true;
                                activity_dirty = true;
                            }
                            Err(error) => eprintln!("could not apply native text zoom: {error}"),
                        }
                    } else if terminal_app {
                        let bytes = terminal_key_bytes(keysym, core.mode(), &lookup.bytes);
                        if !bytes.is_empty() {
                            let _ = writer.write_all(&bytes);
                            let _ = writer.flush();
                        }
                    } else if matches!(keysym, XK_F1 | XK_F2 | XK_F3 | XK_F4 | XK_F5) {
                        let changed = match keysym {
                            XK_F1 => {
                                presentation.adjust(PresentationControl::Brightness, -0.05);
                                true
                            }
                            XK_F2 => {
                                presentation.adjust(PresentationControl::Brightness, 0.05);
                                true
                            }
                            XK_F3 => {
                                presentation.adjust(PresentationControl::Focus, -0.05);
                                true
                            }
                            XK_F4 => {
                                presentation.adjust(PresentationControl::Focus, 0.05);
                                true
                            }
                            XK_F5 => {
                                presentation.adjust(PresentationControl::Power, 0.0);
                                true
                            }
                            _ => false,
                        };
                        if changed {
                            dirty = true;
                            activity_dirty = true;
                        }
                    } else if control {
                        let key = (keysym as u8).to_ascii_lowercase();
                        let changed = match key {
                            b'a' if selecting => input_buffer.select_all(),
                            b'a' => input_buffer.home(false),
                            b'e' => input_buffer.end(selecting),
                            b'u' => input_buffer.clear_to_start(),
                            b'k' => input_buffer.clear_to_end(),
                            b'w' => input_buffer.move_word_left(),
                            _ => false,
                        };
                        if changed {
                            dirty = true;
                            activity_dirty = true;
                        }
                    } else {
                        let changed = match keysym {
                            XK_BACKSPACE => input_buffer.backspace(),
                            XK_DELETE => input_buffer.delete(),
                            XK_LEFT => input_buffer.move_left(selecting),
                            XK_RIGHT => input_buffer.move_right(selecting),
                            XK_HOME => input_buffer.home(selecting),
                            XK_END => input_buffer.end(selecting),
                            XK_UP => input_buffer.history_up(),
                            XK_DOWN => input_buffer.history_down(),
                            XK_PAGE_UP if selecting => {
                                core.scroll_display(Scroll::PageUp);
                                true
                            }
                            XK_PAGE_DOWN if selecting => {
                                core.scroll_display(Scroll::PageDown);
                                true
                            }
                            XK_RETURN => {
                                let mut line = input_buffer.take_line();
                                line.push('\n');
                                let _ = writer.write_all(line.as_bytes());
                                let _ = writer.flush();
                                true
                            }
                            XK_TAB => input_buffer.insert("\t"),
                            _ => {
                                let text = String::from_utf8_lossy(&lookup.bytes);
                                input_buffer.insert(&text)
                            }
                        };
                        if changed {
                            dirty = true;
                            activity_dirty = true;
                        }
                    }
                }
                BUTTON_PRESS => {
                    let button = unsafe { &*((&event as *const XEvent).cast::<XButtonEvent>()) };
                    if button.button == 4 {
                        let geometry = FrameGeometry::for_window(width, height, metrics);
                        let over_encoder =
                            geometry.rail.primary_encoder.contains(button.x, button.y);
                        let over_attention = render::oi::attention_page_contains(
                            projection,
                            button.x as f32,
                            button.y as f32,
                            geometry.oi_inner,
                        );
                        let changed = if over_encoder && projection.attention_items.len() > 1 {
                            render::oi::cycle_attention_page(projection, -1)
                        } else if over_encoder || geometry.oi_inner.contains(button.x, button.y) {
                            projection.cycle_oi_history(-1)
                        } else if over_attention {
                            render::oi::cycle_attention_page(projection, -1)
                        } else {
                            false
                        };
                        if !changed {
                            core.scroll_display(Scroll::Delta(3));
                        }
                        dirty = true;
                        activity_dirty = true;
                    } else if button.button == 5 {
                        let geometry = FrameGeometry::for_window(width, height, metrics);
                        let over_encoder =
                            geometry.rail.primary_encoder.contains(button.x, button.y);
                        let over_attention = render::oi::attention_page_contains(
                            projection,
                            button.x as f32,
                            button.y as f32,
                            geometry.oi_inner,
                        );
                        let changed = if over_encoder && projection.attention_items.len() > 1 {
                            render::oi::cycle_attention_page(projection, 1)
                        } else if over_encoder || geometry.oi_inner.contains(button.x, button.y) {
                            projection.cycle_oi_history(1)
                        } else if over_attention {
                            render::oi::cycle_attention_page(projection, 1)
                        } else {
                            false
                        };
                        if !changed {
                            core.scroll_display(Scroll::Delta(-3));
                        }
                        dirty = true;
                        activity_dirty = true;
                    } else if button.button == 1 {
                        if mapped && !window_destroyed {
                            set_input_focus_if_mapped(display, window, mapped);
                        }
                        focused = true;
                        if let Some(zone) = resize_zone(button.x, button.y, width, height) {
                            selection = None;
                            resize_cursors.set(window, Some(zone));
                            let drag = WindowDrag::new(
                                display,
                                window,
                                button,
                                width,
                                height,
                                WindowDragKind::Resize(zone),
                            );
                            if window_move_strategy.uses_ewmh() {
                                window_move_telemetry.begin_ewmh();
                                begin_window_resize(display, window, button, zone);
                                pending_ewmh_gesture =
                                    Some(PendingEwmhGesture::new(drag, configure_events));
                            } else {
                                window_drag = Some(drag);
                                grab_window_pointer(display, window);
                            }
                        } else {
                            let geometry = FrameGeometry::for_window(width, height, metrics);
                            let attention_action = render::oi::attention_hit_map(projection)
                                .hit_physical(button.x as f32, button.y as f32, geometry.oi_inner)
                                .cloned();
                            if let Some(action) = attention_action {
                                if write_attention_action(&mut writer, &action) {
                                    dirty = true;
                                    activity_dirty = true;
                                }
                            } else if geometry.header.contains(button.x, button.y) {
                                let drag = WindowDrag::new(
                                    display,
                                    window,
                                    button,
                                    width,
                                    height,
                                    WindowDragKind::Move,
                                );
                                if window_move_strategy.uses_ewmh() {
                                    window_move_telemetry.begin_ewmh();
                                    begin_window_move(display, window, button);
                                    pending_ewmh_gesture =
                                        Some(PendingEwmhGesture::new(drag, configure_events));
                                } else {
                                    window_drag = Some(drag);
                                    grab_window_pointer(display, window);
                                }
                            } else if geometry.rail.primary_encoder.contains(button.x, button.y) {
                                projection.return_to_live_oi();
                                dirty = true;
                                activity_dirty = true;
                            } else if let Some(control) =
                                PresentationSettings::control_at(&geometry, button.x, button.y)
                            {
                                presentation.activate(control, button.x, &geometry);
                                dirty = true;
                                activity_dirty = true;
                            } else if geometry.prompt.contains(button.x, button.y) {
                                let offset = ((button.x as f32
                                    - geometry.prompt.x
                                    - geometry.prompt_padding_x)
                                    / geometry.prompt_cell_width)
                                    .max(0.0) as usize;
                                input_buffer
                                    .set_cursor_chars(offset, button.state & SHIFT_MASK != 0);
                                dirty = true;
                                activity_dirty = true;
                            } else if let Some(cell) = geometry.cell_at(button.x, button.y) {
                                selection = Some((cell, cell));
                                dirty = true;
                                activity_dirty = true;
                            }
                        }
                    }
                }
                MOTION_NOTIFY => {
                    let motion = unsafe { &*((&event as *const XEvent).cast::<XMotionEvent>()) };
                    let zone = resize_zone(motion.x, motion.y, width, height);
                    if motion.state & BUTTON1_MASK == 0 {
                        resize_cursors.set(window, zone);
                    }
                    if let Some(pending) = pending_ewmh_gesture {
                        if Instant::now() >= pending.deadline {
                            pending_ewmh_gesture = None;
                            window_move_telemetry
                                .activate_fallback("ewmh_no_configure_before_grace");
                            window_drag = Some(pending.drag);
                            grab_window_pointer(display, window);
                        }
                    }
                    if let Some(drag) = window_drag {
                        // Keep using the button-press-owned drag state even
                        // when a synthetic XTest motion omits BUTTON1_MASK;
                        // real pointer motion reports it, while remote/XTest
                        // desktops commonly do not.
                        apply_window_drag(display, window, drag, motion.x_root, motion.y_root);
                        dirty = true;
                        activity_dirty = true;
                        continue;
                    }
                    if motion.state & BUTTON1_MASK != 0 && zone.is_none() {
                        let geometry = FrameGeometry::for_window(width, height, metrics);
                        if let Some(control) =
                            PresentationSettings::control_at(&geometry, motion.x, motion.y)
                        {
                            // Brightness/focus are continuous presentation
                            // controls: dragging updates only their owned
                            // display setting. Power remains a click/toggle
                            // control and is intentionally not retriggered
                            // during motion.
                            if matches!(
                                control,
                                render::chassis::PresentationControl::Brightness
                                    | render::chassis::PresentationControl::Focus
                            ) {
                                presentation.activate(control, motion.x, &geometry);
                                dirty = true;
                                activity_dirty = true;
                            }
                        } else if let (Some((anchor, _)), Some(cell)) =
                            (selection, geometry.cell_at(motion.x, motion.y))
                        {
                            selection = Some((anchor, cell));
                            dirty = true;
                            activity_dirty = true;
                        }
                    }
                }
                BUTTON_RELEASE => {
                    let button = unsafe { &*((&event as *const XEvent).cast::<XButtonEvent>()) };
                    if button.button == 1 {
                        if let Some(pending) = pending_ewmh_gesture.take() {
                            // A short synthetic drag can produce one motion
                            // and release before the grace timer expires.
                            // Preserve the gesture by applying the final
                            // release geometry through Athena's fallback.
                            window_move_telemetry
                                .activate_fallback("ewmh_no_configure_before_release");
                            apply_window_drag(
                                display,
                                window,
                                pending.drag,
                                button.x_root,
                                button.y_root,
                            );
                        }
                        let had_client_drag = window_drag.is_some();
                        window_drag = None;
                        if had_client_drag {
                            unsafe { XUngrabPointer(display, CURRENT_TIME) };
                        }
                        if let (Some((anchor, _)), Some(cell)) = (
                            selection,
                            FrameGeometry::for_window(width, height, metrics)
                                .cell_at(button.x, button.y),
                        ) {
                            selection = Some((anchor, cell));
                            dirty = true;
                            activity_dirty = true;
                        }
                        resize_cursors.set(window, resize_zone(button.x, button.y, width, height));
                    }
                }
                CONFIGURE_NOTIFY => {
                    let configure =
                        unsafe { &*(&event as *const XEvent as *const XConfigureEvent) };
                    width = configure.width.max(1);
                    height = configure.height.max(1);
                    if let Err(error) = (|| {
                        unsafe { glXMakeCurrent(display, 0, ptr::null_mut()) };
                        presentation_surface.resize(width as CUint, height as CUint)?;
                        text.rebind_drawable(presentation_surface.pixmap)?;
                        presentation_surface.reap_retired();
                        if unsafe {
                            glXMakeCurrent(display, presentation_surface.glx_pixmap, context)
                        } == 0
                        {
                            return Err(
                                "could not bind the resized presentation surface".to_owned()
                            );
                        }
                        Ok::<(), String>(())
                    })() {
                        eprintln!("could not resize native presentation surface: {error}");
                        running = false;
                        continue;
                    }
                    match text.reconfigure_for_scale(text_zoom) {
                        Ok(_) => {
                            metrics = UiFontMetrics {
                                body: text.metrics_for(FontRole::Body),
                                input: text.metrics_for(FontRole::Input),
                                heading: text.metrics_for(FontRole::Heading),
                                instrument: text.metrics_for(FontRole::Instrument),
                            };
                        }
                        Err(error) => {
                            eprintln!("could not reconfigure native fonts: {error}");
                        }
                    }
                    resize_terminal(core, pty, width, height, metrics);
                    configure_events = configure_events.saturating_add(1);
                    if let Some(pending) = pending_ewmh_gesture {
                        if configure_events > pending.configure_events_at_start
                            && pending.geometry_changed(configure.x, configure.y, width, height)
                        {
                            pending_ewmh_gesture = None;
                            window_move_telemetry.confirm_ewmh();
                        }
                    }
                    write_runtime_layout_dump(
                        display,
                        screen,
                        width,
                        height,
                        metrics,
                        text.font_pixel_sizes(),
                        text_zoom,
                        configure_events,
                        window_move_strategy,
                        window_move_telemetry,
                    );
                    dirty = true;
                    activity_dirty = true;
                }
                EXPOSE => {
                    dirty = true;
                    activity_dirty = true;
                }
                FOCUS_IN => {
                    mapped = true;
                    if set_input_focus_if_mapped(display, window, mapped) {
                        if let Some(input_method) = input_method.as_ref() {
                            unsafe { XSetICFocus(input_method.ic) };
                        }
                        focused = true;
                        dirty = true;
                        activity_dirty = true;
                    }
                }
                FOCUS_OUT => {
                    if let Some(input_method) = input_method.as_ref() {
                        unsafe { XUnsetICFocus(input_method.ic) };
                    }
                    focused = false;
                    dirty = true;
                    activity_dirty = true;
                }
                DESTROY_NOTIFY => {
                    // A window manager may destroy the surface in response to
                    // WM_DELETE_WINDOW before the event loop reaches cleanup.
                    // Xft/XIM resources reference that drawable, so they must
                    // not issue teardown requests after this notification.
                    window_destroyed = true;
                    running = false;
                }
                CLIENT_MESSAGE => {
                    let message =
                        unsafe { &*(&event as *const XEvent as *const XClientMessageEvent) };
                    if is_wm_delete_message(
                        message.message_type,
                        message.format,
                        message.data[0],
                        protocols_atom,
                        delete_atom,
                    ) {
                        // WM_DELETE_WINDOW is an external shutdown request.
                        // The window manager owns the drawable lifecycle from
                        // here; let the X connection reclaim Xft/XIM handles
                        // rather than racing it with a second teardown.
                        window_destroyed = true;
                        running = false;
                    }
                }
                _ => {}
            }
        }
        if focus_pending
            && mapped
            && !window_destroyed
            && set_input_focus_if_mapped(display, window, mapped)
        {
            if let Some(input_method) = input_method.as_ref() {
                unsafe { XSetICFocus(input_method.ic) };
            }
            focus_pending = false;
            focused = true;
            dirty = true;
            activity_dirty = true;
        }
        if matches!(pty.next_child_event(), Some(ChildEvent::Exited(_))) {
            child_exited = true;
            dirty = true;
            activity_dirty = true;
        }
        if activity_dirty {
            finish_steady_interval(
                &mut steady_started,
                &mut steady_cpu_started,
                &mut steady_elapsed_seconds,
                &mut steady_cpu_seconds,
            );
        }
        let now = Instant::now();
        if options.animations && !options.reduced_motion && projection_is_animated(projection) {
            oi_motion_dirty = true;
            activity_dirty = true;
        }
        let draw_ready = last_draw
            .map(|last| now.duration_since(last) >= ACTIVE_FRAME_INTERVAL)
            .unwrap_or(true);
        if (dirty || terminal_dirty || oi_motion_dirty) && draw_ready {
            if options.animations && !options.reduced_motion && projection_is_animated(projection) {
                projection.advance_animation(ACTIVE_FRAME_INTERVAL.as_secs_f32());
            }
            render::frame::draw_frame(
                display,
                width,
                height,
                core,
                projection,
                selection,
                &text,
                focused,
                &input_buffer,
                options,
                presentation,
                stencil_available,
                &oi_target,
                &chassis_material,
                DirtyDomains {
                    full: dirty,
                    terminal: terminal_dirty,
                    oi_motion: oi_motion_dirty,
                },
                if options.animations && !options.reduced_motion {
                    projection.animation_phase(
                        presentation_clock.seconds_at_frame(presented_frame_sequence),
                    )
                } else {
                    0.0
                },
            );
            // Xft targets the same offscreen pixmap as GL. Force the XRender
            // text requests to land before the pixmap is copied to the mapped
            // window; without this fence the cabinet can present a complete
            // GL frame while silently dropping the terminal glyph layer.
            unsafe { XSync(display, 0) };
            presentation_surface.present(width as CUint, height as CUint);
            presented_frame_sequence = presented_frame_sequence.saturating_add(1);
            write_presentation_sync(presented_frame_sequence, width, height, projection);
            if !oi_dumped {
                if let Ok(path) = env::var("ATHENA_NATIVE_OI_DUMP") {
                    if let Err(error) = render::oi::dump_framebuffer(&oi_target, &path) {
                        eprintln!("could not write OI framebuffer dump: {error}");
                    }
                    oi_dumped = true;
                }
            }
            redraws += 1;
            if last_draw.is_none() {
                initial_redraws += 1;
            } else if activity_dirty {
                event_redraws += 1;
            } else {
                idle_redraws += 1;
            }
            last_draw = Some(now);
            dirty = false;
            terminal_dirty = false;
            oi_motion_dirty = false;
            activity_dirty = false;
        }
        if !dirty && !terminal_dirty && !child_exited && steady_started.is_none() {
            steady_started = Some(Instant::now());
            steady_cpu_started = process_cpu_seconds();
        }
        if child_exited && dirty {
            if let Some(last) = last_draw {
                let remaining =
                    ACTIVE_FRAME_INTERVAL.saturating_sub(Instant::now().duration_since(last));
                if !remaining.is_zero() {
                    thread::sleep(remaining);
                    continue;
                }
            }
        }
        if child_exited {
            // Keep one final frame visible long enough for a caller or bridge
            // to observe it, then restore/destroy the native surface.
            thread::sleep(Duration::from_millis(80));
            running = false;
        } else {
            let sleep_for = if dirty {
                last_draw
                    .map(|last| {
                        ACTIVE_FRAME_INTERVAL
                            .saturating_sub(Instant::now().duration_since(last))
                            .min(IDLE_POLL_INTERVAL)
                    })
                    .unwrap_or(IDLE_POLL_INTERVAL)
            } else {
                IDLE_POLL_INTERVAL
            };
            thread::sleep(sleep_for);
        }
    }

    finish_steady_interval(
        &mut steady_started,
        &mut steady_cpu_started,
        &mut steady_elapsed_seconds,
        &mut steady_cpu_seconds,
    );
    write_render_stats(
        render_started,
        cpu_started,
        steady_elapsed_seconds,
        steady_cpu_seconds,
        redraws,
        initial_redraws,
        event_redraws,
        idle_redraws,
        presented_frame_sequence,
    );

    // Xft owns an XRender picture tied to the window. Tear it down before
    // destroying the drawable. If a window manager already destroyed the
    // window, leaking these process-local handles is safer than asking Xft/XIM
    // to destroy resources whose drawable no longer exists.
    if window_destroyed {
        drop(resize_cursors);
        std::mem::forget(input_method);
        std::mem::forget(oi_target);
        std::mem::forget(chassis_material);
        drop(text);
        unsafe {
            glXMakeCurrent(display, 0, ptr::null_mut());
            glXDestroyContext(display, context);
        }
        presentation_surface.destroy();
        return Ok(());
    } else {
        drop(resize_cursors);
        drop(input_method);
        drop(oi_target);
        drop(chassis_material);
        drop(text);
    }
    unsafe {
        glXMakeCurrent(display, 0, ptr::null_mut());
        glXDestroyContext(display, context);
        if !window_destroyed {
            XDestroyWindow(display, window);
        }
    }
    presentation_surface.destroy();
    Ok(())
}

fn set_window_hints(display: *mut Display, window: Window) {
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

fn set_window_pid(display: *mut Display, window: Window) {
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

fn write_runtime_layout_dump(
    display: *mut Display,
    screen: c_int,
    width: i32,
    height: i32,
    metrics: UiFontMetrics,
    font_pixel_sizes: [i32; 4],
    text_scale: f32,
    configure_events: u64,
    window_move_strategy: WindowMoveStrategy,
    window_move_telemetry: WindowMoveTelemetry,
) {
    let Ok(path) = env::var("ATHENA_NATIVE_LAYOUT_DUMP") else {
        return;
    };
    let layout = FrameGeometry::for_window(width, height, metrics);
    let prompt = PromptLayout::from_rect(
        layout.prompt,
        if layout.compact {
            metrics.instrument
        } else {
            metrics.input
        },
        metrics.instrument,
        layout.prompt_padding_y,
        layout.prompt_gap,
        layout.prompt_bottom_padding,
        !layout.compact,
    );
    let mut value = match serde_json::to_value(layout) {
        Ok(value) => value,
        Err(error) => {
            eprintln!("could not serialize native layout dump: {error}");
            return;
        }
    };
    let Some(object) = value.as_object_mut() else {
        eprintln!("could not serialize native layout dump as an object");
        return;
    };
    object.insert("metrics_source".to_owned(), serde_json::json!("live_xft"));
    object.insert(
        "metrics".to_owned(),
        serde_json::to_value(metrics).expect("font metrics serialize"),
    );
    object.insert(
        "font_pixel_sizes".to_owned(),
        serde_json::to_value(font_pixel_sizes).expect("font sizes serialize"),
    );
    object.insert("text_scale".to_owned(), serde_json::json!(text_scale));
    object.insert(
        "terminal_size".to_owned(),
        serde_json::to_value(layout.terminal_size()).expect("terminal size serialize"),
    );
    object.insert(
        "configure_events".to_owned(),
        serde_json::json!(configure_events),
    );
    object.insert(
        "prompt_layout".to_owned(),
        serde_json::to_value(prompt).expect("prompt layout serialize"),
    );
    object.insert(
        "window_management".to_owned(),
        window_management_diagnostics(display, screen, window_move_strategy, window_move_telemetry),
    );
    if let Err(error) = std::fs::write(path, value.to_string()) {
        eprintln!("could not write native layout dump: {error}");
    }
}

fn begin_window_move(display: *mut Display, window: Window, button: &XButtonEvent) {
    begin_window_moveresize(display, window, button, 8);
}

fn write_attention_action(writer: &mut impl Write, action: &render::oi::AttentionAction) -> bool {
    let command = match action {
        render::oi::AttentionAction::Approve { approval_id, scope } => {
            if !is_command_token(approval_id) || !is_command_token(scope) {
                return false;
            }
            format!("/approve {approval_id} {scope}\n")
        }
        render::oi::AttentionAction::Deny { approval_id } => {
            if !is_command_token(approval_id) {
                return false;
            }
            format!("/deny {approval_id}\n")
        }
    };
    writer.write_all(command.as_bytes()).is_ok() && writer.flush().is_ok()
}

fn is_command_token(value: &str) -> bool {
    !value.is_empty()
        && value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || "_-:.".contains(character))
}

fn begin_window_resize(
    display: *mut Display,
    window: Window,
    button: &XButtonEvent,
    zone: ResizeZone,
) {
    begin_window_moveresize(display, window, button, zone.direction());
}

fn begin_window_moveresize(
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

fn moveresize_message_data(
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

fn process_cpu_seconds() -> Option<f64> {
    let mut usage = unsafe { std::mem::zeroed::<libc::rusage>() };
    if unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut usage) } != 0 {
        return None;
    }
    Some(
        usage.ru_utime.tv_sec as f64
            + usage.ru_utime.tv_usec as f64 / 1_000_000.0
            + usage.ru_stime.tv_sec as f64
            + usage.ru_stime.tv_usec as f64 / 1_000_000.0,
    )
}

fn write_render_stats(
    started: Instant,
    cpu_started: Option<f64>,
    steady_elapsed_seconds: f64,
    steady_cpu_seconds: f64,
    redraws: u64,
    initial_redraws: u64,
    event_redraws: u64,
    idle_redraws: u64,
    presented_frame_sequence: u64,
) {
    let Ok(path) = env::var("ATHENA_NATIVE_RENDER_STATS") else {
        return;
    };
    let elapsed_seconds = started.elapsed().as_secs_f64();
    let cpu_seconds = process_cpu_seconds()
        .zip(cpu_started)
        .map(|(end, start)| end - start);
    let value = serde_json::json!({
        "elapsed_seconds": elapsed_seconds,
        "redraws": redraws,
        "initial_redraws": initial_redraws,
        "event_redraws": event_redraws,
        "idle_redraws": idle_redraws,
        "presented_frame_sequence": presented_frame_sequence,
        "cpu_seconds": cpu_seconds,
        "steady_elapsed_seconds": steady_elapsed_seconds,
        "steady_cpu_seconds": steady_cpu_seconds,
    });
    let _ = std::fs::write(path, value.to_string());
}

fn write_presentation_sync(sequence: u64, width: i32, height: i32, projection: &Projection) {
    let Ok(path) = env::var("ATHENA_NATIVE_PRESENTATION_SYNC") else {
        return;
    };
    let value = serde_json::json!({
        "presented_frame_sequence": sequence,
        "width": width,
        "height": height,
        "status": projection.status.as_str(),
        "semantic_state": projection.semantic_state.as_str(),
        "current_action_kind": projection.current_action.as_ref().map(|action| action.kind.as_str()),
        "bridge_status": projection.bridge_status.as_str(),
        "bridge_generation": projection.bridge_generation,
        "last_frame_sequence": projection.last_frame_sequence,
        "last_frame_age_ms": projection.last_frame_age_ms(),
        "stale": projection.stale,
    });
    let path = std::path::PathBuf::from(path);
    let Some(file_name) = path.file_name().and_then(|name| name.to_str()) else {
        return;
    };
    let temporary = path.with_file_name(format!(".{file_name}.tmp"));
    if let Err(error) = std::fs::write(&temporary, value.to_string()) {
        eprintln!("could not write native presentation sync: {error}");
        return;
    }
    if let Err(error) = std::fs::rename(&temporary, &path) {
        eprintln!("could not publish native presentation sync: {error}");
        let _ = std::fs::remove_file(temporary);
    }
}

fn finish_steady_interval(
    started: &mut Option<Instant>,
    cpu_started: &mut Option<f64>,
    elapsed_seconds: &mut f64,
    cpu_seconds: &mut f64,
) {
    let Some(start) = started.take() else {
        cpu_started.take();
        return;
    };
    *elapsed_seconds += start.elapsed().as_secs_f64();
    if let (Some(start_cpu), Some(end_cpu)) = (cpu_started.take(), process_cpu_seconds()) {
        *cpu_seconds += end_cpu - start_cpu;
    }
}

fn resize_terminal(
    core: &mut NativeTerminalCore,
    pty: &mut tty::Pty,
    width: i32,
    height: i32,
    metrics: UiFontMetrics,
) {
    let layout = FrameGeometry::for_window(width, height, metrics);
    let terminal_size = layout.terminal_size();
    let columns = terminal_size.columns;
    let rows = terminal_size.rows;
    core.resize(columns, rows);
    pty.on_resize(WindowSize {
        num_cols: columns.min(u16::MAX as usize) as u16,
        num_lines: rows.min(u16::MAX as usize) as u16,
        cell_width: metrics.body.width.round().max(1.0) as u16,
        cell_height: metrics.body.height.round().max(1.0) as u16,
    });
}

fn projection_is_animated(projection: &Projection) -> bool {
    VisualMode::from_projection(projection).is_animated(projection)
}
fn fit_text_in(text: &TextRenderer, role: FontRole, value: &str, available: i32) -> String {
    if text.text_width_in(role, value) <= available {
        return value.to_owned();
    }
    let mut result = String::new();
    for character in value.chars() {
        let candidate = format!("{result}{character}…");
        if text.text_width_in(role, &candidate) > available {
            break;
        }
        result.push(character);
    }
    if result.is_empty() {
        "…".to_owned()
    } else {
        format!("{result}…")
    }
}

fn fit_input_in(
    text: &TextRenderer,
    role: FontRole,
    value: &str,
    cursor: usize,
    available: i32,
) -> (String, usize) {
    if text.text_width_in(role, value) <= available {
        return (value.to_owned(), value[..cursor].chars().count());
    }
    let characters: Vec<char> = value.chars().collect();
    let cursor_chars = value[..cursor].chars().count().min(characters.len());
    let mut start = 0;
    let mut end = characters.len();
    loop {
        let prefix = if start > 0 { "…" } else { "" };
        let suffix = if end < characters.len() { "…" } else { "" };
        let middle: String = characters[start..end].iter().collect();
        let candidate = format!("{prefix}{middle}{suffix}");
        if text.text_width_in(role, &candidate) <= available {
            let display_cursor = usize::from(start > 0) + cursor_chars.saturating_sub(start);
            return (candidate, display_cursor);
        }
        if start < cursor_chars
            && (end == characters.len() || cursor_chars - start >= end - cursor_chars)
        {
            start += 1;
        } else if end > cursor_chars {
            end -= 1;
        } else if start < end {
            start += 1;
        } else {
            return ("…".to_owned(), 0);
        }
    }
}

fn rgb_f32(color: (u8, u8, u8)) -> (f32, f32, f32) {
    (
        color.0 as f32 / 255.0,
        color.1 as f32 / 255.0,
        color.2 as f32 / 255.0,
    )
}

fn mode_color(mode: &str) -> (u8, u8, u8) {
    if mode.eq_ignore_ascii_case("failure") || mode.eq_ignore_ascii_case("blocked") {
        (235, 112, 116)
    } else if mode.eq_ignore_ascii_case("approval") {
        (239, 194, 105)
    } else {
        (103, 202, 212)
    }
}

fn selection_bounds(
    anchor: (usize, usize),
    extent: (usize, usize),
) -> ((usize, usize), (usize, usize)) {
    if (anchor.1, anchor.0) <= (extent.1, extent.0) {
        (anchor, extent)
    } else {
        (extent, anchor)
    }
}

#[cfg(test)]
mod tests {
    use super::render::chassis::{PresentationControl, PresentationSettings};
    use super::render::oi::AttentionAction;
    use super::{
        FrameGeometry, PresentationClock, Projection, ResizeZone, VisualMode, WindowMoveStrategy,
        WindowMoveTelemetry, is_wm_delete_message, moveresize_message_data, resize_zone,
        write_attention_action,
    };
    use crate::ProjectionView;
    use alacritty_terminal::term::TermMode;

    #[test]
    fn frame_geometry_keeps_apertures_equal() {
        let geometry = FrameGeometry::new(1000, 700);
        let left_end = geometry.operator_outer.x + geometry.operator_outer.width;
        assert!(geometry.oi_outer.x > left_end);

        assert!((left_end - geometry.left_x - geometry.oi_outer.width).abs() < 0.01);
        assert!((geometry.operator_inner.width - geometry.oi_inner.width).abs() < 0.01);
        assert!((geometry.operator_inner.height - geometry.oi_inner.height).abs() < 0.01);
    }

    #[test]
    fn fixed_presentation_clock_is_reproducible_by_frame_sequence() {
        let clock = PresentationClock::fixed(0.1);
        assert_eq!(clock.seconds_at_frame(0), 0.0);
        assert!((clock.seconds_at_frame(3) - 0.3).abs() < f32::EPSILON);
        assert!((clock.seconds_at_frame(10) - 1.0).abs() < f32::EPSILON);
    }

    #[test]
    fn frame_geometry_maps_operator_cells_within_bounds() {
        let geometry = FrameGeometry::new(1000, 700);
        let (origin_x, origin_y) = geometry.operator_origin();

        assert_eq!(geometry.cell_at(origin_x, origin_y), Some((0, 0)));
        assert_eq!(
            geometry.cell_at(
                origin_x + geometry.cell_width as i32,
                origin_y + geometry.cell_height as i32,
            ),
            Some((1, 1))
        );
        assert_eq!(geometry.cell_at(origin_x - 1, origin_y), None);
        assert_eq!(geometry.cell_at(origin_x, origin_y - 1), None);
    }

    #[test]
    fn frame_geometry_preserves_minimum_resize() {
        let tiny = FrameGeometry::new(1, 1).terminal_size();
        assert!(tiny.columns >= 1 && tiny.rows >= 1);
        let geometry = FrameGeometry::new(1000, 700);
        let size = geometry.terminal_size();
        assert!(size.columns >= 1 && size.rows >= 1);
        assert!(size.columns > 1 && size.rows > 1);
    }

    #[test]
    fn presentation_controls_change_only_their_owned_setting() {
        let geometry = FrameGeometry::new(1280, 800);
        let y = geometry.controls.y as i32 + 20;
        let brightness = geometry.rail.brightness;
        let focus = geometry.rail.focus;
        let power = geometry.rail.power;
        assert_eq!(
            PresentationSettings::control_at(&geometry, brightness.x as i32 + 20, y),
            Some(PresentationControl::Brightness)
        );
        assert_eq!(
            PresentationSettings::control_at(&geometry, focus.x as i32 + 20, y),
            Some(PresentationControl::Focus)
        );
        assert_eq!(
            PresentationSettings::control_at(&geometry, power.x as i32 + 20, y),
            Some(PresentationControl::Power)
        );

        let mut settings = PresentationSettings::default();
        settings.activate(
            PresentationControl::Brightness,
            brightness.x as i32 + brightness.width as i32 / 2,
            &geometry,
        );
        assert!((settings.brightness - 0.5).abs() < 0.02);
        assert_eq!(settings.focus, PresentationSettings::default().focus);
        assert!(settings.display_enabled);
        settings.activate(PresentationControl::Power, power.x as i32 + 20, &geometry);
        assert!(!settings.display_enabled);
    }

    #[test]
    fn native_attention_actions_use_the_service_command_bridge() {
        let mut output = Vec::new();
        assert!(write_attention_action(
            &mut output,
            &AttentionAction::Approve {
                approval_id: "apr-1".to_owned(),
                scope: "task".to_owned(),
            },
        ));
        assert_eq!(output, b"/approve apr-1 task\n");

        output.clear();
        assert!(write_attention_action(
            &mut output,
            &AttentionAction::Deny {
                approval_id: "apr-1".to_owned(),
            },
        ));
        assert_eq!(output, b"/deny apr-1\n");

        output.clear();
        assert!(!write_attention_action(
            &mut output,
            &AttentionAction::Approve {
                approval_id: "apr 1".to_owned(),
                scope: "task".to_owned(),
            },
        ));
        assert!(output.is_empty());
    }

    #[test]
    fn prompt_state_is_explicit_and_projection_driven() {
        let mut projection = Projection {
            status: "Approval requested".to_owned(),
            ..Projection::default()
        };
        assert_eq!(
            VisualMode::from_projection(&projection).prompt_state(&projection),
            "APPROVAL"
        );
        projection.status = "Disconnected".to_owned();
        assert_eq!(
            VisualMode::from_projection(&projection).prompt_state(&projection),
            "DISCONNECTED"
        );
        projection.status = "Working".to_owned();
        projection.view = ProjectionView {
            mode: "search".to_owned(),
            ..ProjectionView::default()
        };
        assert_eq!(
            VisualMode::from_projection(&projection).prompt_state(&projection),
            "WORKING"
        );
    }

    #[test]
    fn only_wm_protocol_delete_client_messages_close_the_window() {
        assert!(is_wm_delete_message(10, 32, 20, 10, 20));
        assert!(!is_wm_delete_message(10, 32, 21, 10, 20));
        assert!(!is_wm_delete_message(11, 32, 20, 10, 20));
        assert!(!is_wm_delete_message(10, 16, 20, 10, 20));
    }

    #[test]
    fn resize_zones_cover_all_edges_and_corners() {
        let width = 1280;
        let height = 800;
        assert_eq!(resize_zone(0, 0, width, height), Some(ResizeZone::TopLeft));
        assert_eq!(
            resize_zone(width - 1, 0, width, height),
            Some(ResizeZone::TopRight)
        );
        assert_eq!(
            resize_zone(0, height - 1, width, height),
            Some(ResizeZone::BottomLeft)
        );
        assert_eq!(
            resize_zone(width - 1, height - 1, width, height),
            Some(ResizeZone::BottomRight)
        );
        assert_eq!(
            resize_zone(width / 2, 0, width, height),
            Some(ResizeZone::Top)
        );
        assert_eq!(
            resize_zone(width / 2, height - 1, width, height),
            Some(ResizeZone::Bottom)
        );
        assert_eq!(
            resize_zone(0, height / 2, width, height),
            Some(ResizeZone::Left)
        );
        assert_eq!(
            resize_zone(width - 1, height / 2, width, height),
            Some(ResizeZone::Right)
        );
        assert_eq!(resize_zone(width / 2, height / 2, width, height), None);
    }

    #[test]
    fn resize_zones_use_ewmh_moveresize_directions() {
        assert_eq!(ResizeZone::TopLeft.direction(), 0);
        assert_eq!(ResizeZone::Top.direction(), 1);
        assert_eq!(ResizeZone::TopRight.direction(), 2);
        assert_eq!(ResizeZone::Right.direction(), 3);
        assert_eq!(ResizeZone::BottomRight.direction(), 4);
        assert_eq!(ResizeZone::Bottom.direction(), 5);
        assert_eq!(ResizeZone::BottomLeft.direction(), 6);
        assert_eq!(ResizeZone::Left.direction(), 7);
    }

    #[test]
    fn moveresize_message_uses_normal_application_source() {
        assert_eq!(
            moveresize_message_data(40, 50, ResizeZone::BottomRight.direction(), 1),
            [40, 50, 4, 1, 1]
        );
    }

    #[test]
    fn window_move_strategy_is_exclusive() {
        assert!(WindowMoveStrategy::Ewmh.uses_ewmh());
        assert!(!WindowMoveStrategy::ClientManagedFallback.uses_ewmh());
        assert_ne!(
            WindowMoveStrategy::Ewmh.name(),
            WindowMoveStrategy::ClientManagedFallback.name()
        );
    }

    #[test]
    fn adaptive_window_move_telemetry_requires_a_real_confirmation() {
        let mut telemetry = WindowMoveTelemetry::for_strategy(WindowMoveStrategy::Ewmh);
        assert_eq!(telemetry.active_strategy, "ewmh_preferred");
        assert!(!telemetry.ewmh_attempted);
        assert!(!telemetry.ewmh_confirmed);

        telemetry.begin_ewmh();
        telemetry.confirm_ewmh();
        assert_eq!(telemetry.active_strategy, "ewmh_confirmed");
        assert!(telemetry.ewmh_confirmed);

        telemetry.activate_fallback("late_test_fallback");
        assert_eq!(telemetry.active_strategy, "ewmh_confirmed");
        assert_eq!(telemetry.fallback_reason, None);

        let mut fallback = WindowMoveTelemetry::for_strategy(WindowMoveStrategy::Ewmh);
        fallback.begin_ewmh();
        fallback.activate_fallback("no_configure");
        fallback.confirm_ewmh();
        assert_eq!(fallback.active_strategy, "client_managed");
        assert!(!fallback.ewmh_confirmed);
        assert_eq!(fallback.fallback_reason, Some("no_configure"));
    }

    #[test]
    fn terminal_modes_choose_application_cursor_sequences() {
        assert_eq!(
            super::terminal_key_bytes(super::XK_LEFT, TermMode::empty(), &[]),
            b"\x1b[D"
        );
        assert_eq!(
            super::terminal_key_bytes(super::XK_LEFT, TermMode::APP_CURSOR, &[]),
            b"\x1bOD"
        );
        assert_eq!(
            super::terminal_key_bytes(super::XK_PAGE_DOWN, TermMode::empty(), &[]),
            b"\x1b[6~"
        );
    }

    #[test]
    fn xim_lookup_lengths_are_never_allowed_to_escape_the_buffer() {
        assert_eq!(super::input_method::bounded_lookup_length(4, 8), Some(4));
        assert_eq!(super::input_method::bounded_lookup_length(8, 8), Some(8));
        assert_eq!(super::input_method::bounded_lookup_length(9, 8), None);
        assert_eq!(super::input_method::bounded_lookup_length(-1, 8), None);
    }
}
