//! Linux/X11 AthenaBOX compositor.
//!
//! The terminal engine remains Alacritty.  This module owns the native window,
//! the physical chassis layout, and a single composed frame.  Text is rendered
//! with Fontconfig/Xft (including UTF-8) rather than the X11 core-font path;
//! Athena chrome and the OI scene are rendered in the same OpenGL surface.

#![allow(clippy::too_many_arguments)]

use std::cell::RefCell;
use std::collections::HashMap;
use std::env;
use std::ffi::{CString, c_char, c_int, c_long, c_ulong, c_void};
use std::io::Write;
use std::ptr;
use std::sync::mpsc::Receiver;
use std::thread;
use std::time::{Duration, Instant};

use alacritty_terminal::event::{OnResize, WindowSize};
use alacritty_terminal::grid::Scroll;
use alacritty_terminal::term::TermMode;
use alacritty_terminal::term::cell::Flags;
use alacritty_terminal::term::color::Colors;
use alacritty_terminal::tty::{self, ChildEvent, EventedPty};
use alacritty_terminal::vte::ansi::{Color as TermColor, NamedColor};

use athena_terminal::{CellMetrics, NativePixelLayout, NativeTerminalCore, PixelRect};

use crate::input::InputBuffer;
use crate::{
    LatestProjection, Projection, ProjectionEntity, ProjectionOperation, ProjectionTreeNode,
    VisualMode, apply_available,
};

type Display = c_void;
type Window = c_ulong;
type Atom = c_ulong;
type Colormap = c_ulong;
type GLXContext = *mut c_void;

const KEY_PRESS: c_int = 2;
const BUTTON_PRESS: c_int = 4;
const BUTTON_RELEASE: c_int = 5;
const MOTION_NOTIFY: c_int = 6;
const SELECTION_CLEAR: c_int = 29;
const SELECTION_REQUEST: c_int = 30;
const SELECTION_NOTIFY: c_int = 31;
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
const GL_COLOR_BUFFER_BIT: u32 = 0x0000_4000;
const GL_QUADS: u32 = 0x0007;
const GL_LINES: u32 = 0x0001;
const GL_LINE_LOOP: u32 = 0x0002;
const GL_POLYGON: u32 = 0x0009;
const GL_PROJECTION: u32 = 0x1701;
const GL_MODELVIEW: u32 = 0x1700;
const GL_SCISSOR_TEST: u32 = 0x0c11;
const SHIFT_MASK: CUint = 1;
const CONTROL_MASK: CUint = 1 << 2;
const BUTTON1_MASK: CUint = 1 << 8;
const CURRENT_TIME: c_ulong = 0;
const PROP_MODE_REPLACE: c_int = 0;
const X_BUFFER_OVERFLOW: c_int = -1;
const MAX_XIM_BUFFER: usize = 16 * 1024;
const MAX_CACHED_XFT_COLORS: usize = 256;
const MAX_CACHED_TEXT_WIDTHS: usize = 512;
const ACTIVE_FRAME_INTERVAL: Duration = Duration::from_millis(40);
const IDLE_POLL_INTERVAL: Duration = Duration::from_millis(50);

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
    fn XInternAtom(display: *mut Display, name: *const c_char, only_if_exists: c_int) -> Atom;
    fn XSetWMProtocols(
        display: *mut Display,
        window: Window,
        protocols: *mut Atom,
        count: c_int,
    ) -> c_int;
    fn XMapWindow(display: *mut Display, window: Window) -> c_int;
    fn XSetInputFocus(
        display: *mut Display,
        focus: Window,
        revert_to: c_int,
        time: c_ulong,
    ) -> c_int;
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
    fn glXWaitGL();
    fn glXWaitX();
    fn glClearColor(red: f32, green: f32, blue: f32, alpha: f32);
    fn glClear(mask: u32);
    fn glFinish();
    fn glEnable(cap: u32);
    fn glDisable(cap: u32);
    fn glScissor(x: c_int, y: c_int, width: c_int, height: c_int);
    fn glFlush();
    fn glViewport(x: c_int, y: c_int, width: c_int, height: c_int);
    fn glMatrixMode(mode: u32);
    fn glLoadIdentity();
    fn glOrtho(left: f64, right: f64, bottom: f64, top: f64, near: f64, far: f64);
    fn glBegin(mode: u32);
    fn glEnd();
    fn glColor3f(red: f32, green: f32, blue: f32);
    fn glLineWidth(width: f32);
    fn glVertex2f(x: f32, y: f32);
}

struct Clipboard {
    clipboard: Atom,
    primary: Atom,
    utf8_string: Atom,
    string: Atom,
    targets: Atom,
    atom: Atom,
    property: Atom,
    text: String,
}

type PanelRect = PixelRect;
type FrameGeometry = NativePixelLayout;

struct TextRenderer {
    display: *mut Display,
    draw: *mut XftDraw,
    font: *mut XftFont,
    visual: *mut c_void,
    colormap: Colormap,
    colors: RefCell<HashMap<(u8, u8, u8), XftColor>>,
    widths: RefCell<HashMap<String, i32>>,
    metrics: CellMetrics,
}

impl TextRenderer {
    fn new(
        display: *mut Display,
        screen: c_int,
        window: Window,
        visual: *mut c_void,
        colormap: Colormap,
    ) -> Result<Self, String> {
        let draw = unsafe { XftDrawCreate(display, window, visual, colormap) };
        if draw.is_null() {
            return Err("could not create the native Xft text surface".to_owned());
        }
        let font_name = CString::new("Fira Mono:size=13").expect("static font name");
        let mut font = unsafe { XftFontOpenName(display, screen, font_name.as_ptr()) };
        if font.is_null() {
            let fallback = CString::new("monospace:size=13").expect("static font name");
            font = unsafe { XftFontOpenName(display, screen, fallback.as_ptr()) };
        }
        if font.is_null() {
            unsafe { XftDrawDestroy(draw) };
            return Err("could not open a Fontconfig monospace font".to_owned());
        }
        let sample = CString::new("M").expect("static glyph sample");
        let mut extents = XGlyphInfo {
            width: 0,
            height: 0,
            x: 0,
            y: 0,
            x_off: 0,
            y_off: 0,
        };
        unsafe {
            XftTextExtentsUtf8(display, font, sample.as_ptr().cast(), 1, &mut extents);
        }
        let metrics = unsafe {
            CellMetrics::new(
                (*font)
                    .max_advance_width
                    .max(i32::from(extents.x_off))
                    .max(1) as f32,
                (*font).height as f32,
                (*font).ascent as f32,
                (*font).descent as f32,
            )
        };
        Ok(Self {
            display,
            draw,
            font,
            visual,
            colormap,
            colors: RefCell::new(HashMap::new()),
            widths: RefCell::new(HashMap::new()),
            metrics,
        })
    }

    fn metrics(&self) -> CellMetrics {
        self.metrics
    }

    fn text_width(&self, text: &str) -> i32 {
        let sanitized = text.replace('\0', "");
        if let Some(width) = self.widths.borrow().get(&sanitized).copied() {
            return width;
        }
        let Ok(value) = CString::new(sanitized) else {
            return 0;
        };
        let mut extents = XGlyphInfo {
            width: 0,
            height: 0,
            x: 0,
            y: 0,
            x_off: 0,
            y_off: 0,
        };
        unsafe {
            XftTextExtentsUtf8(
                self.display,
                self.font,
                value.as_ptr().cast(),
                value.as_bytes().len().min(c_int::MAX as usize) as c_int,
                &mut extents,
            );
        }
        let width = i32::from(extents.x_off.max(0));
        let mut widths = self.widths.borrow_mut();
        if widths.len() < MAX_CACHED_TEXT_WIDTHS {
            widths.insert(value.to_string_lossy().into_owned(), width);
        }
        width
    }

    fn draw(&self, x: c_int, y: c_int, text: &str, color: (u8, u8, u8)) {
        let sanitized = text.replace('\0', "");
        if sanitized.is_empty() {
            return;
        }
        let Ok(value) = CString::new(sanitized) else {
            return;
        };
        if let Some((color, cached)) = self.cached_color(color) {
            unsafe {
                XftDrawStringUtf8(
                    self.draw,
                    &color,
                    self.font,
                    x,
                    y,
                    value.as_ptr().cast(),
                    value.as_bytes().len().min(c_int::MAX as usize) as c_int,
                );
                if !cached {
                    let mut color = color;
                    XftColorFree(self.display, self.visual, self.colormap, &mut color);
                }
            }
        }
    }

    fn cached_color(&self, color: (u8, u8, u8)) -> Option<(XftColor, bool)> {
        if let Some(cached) = self.colors.borrow().get(&color).copied() {
            return Some((cached, true));
        }
        let allocated = self.allocate_color(color)?;
        let mut colors = self.colors.borrow_mut();
        if colors.len() < MAX_CACHED_XFT_COLORS {
            let cached = *colors.entry(color).or_insert(allocated);
            if cached.pixel != allocated.pixel {
                unsafe {
                    let mut allocated = allocated;
                    XftColorFree(self.display, self.visual, self.colormap, &mut allocated);
                }
            }
            Some((cached, true))
        } else {
            Some((allocated, false))
        }
    }

    fn allocate_color(&self, color: (u8, u8, u8)) -> Option<XftColor> {
        let mut allocated = XftColor {
            pixel: 0,
            color: XRenderColor {
                red: u16::from(color.0) * 257,
                green: u16::from(color.1) * 257,
                blue: u16::from(color.2) * 257,
                alpha: u16::MAX,
            },
        };
        unsafe {
            (XftColorAllocValue(
                self.display,
                self.visual,
                self.colormap,
                &allocated.color,
                &mut allocated,
            ) != 0)
                .then_some(allocated)
        }
    }
}

impl Drop for TextRenderer {
    fn drop(&mut self) {
        unsafe {
            for color in self.colors.get_mut().values_mut() {
                XftColorFree(self.display, self.visual, self.colormap, color);
            }
            XftFontClose(self.display, self.font);
            XftDrawDestroy(self.draw);
        }
    }
}

struct InputMethod {
    im: *mut Xim,
    ic: *mut Xic,
}

struct KeyLookup {
    keysym: c_ulong,
    bytes: Vec<u8>,
}

fn terminal_key_bytes(keysym: c_ulong, mode: TermMode, fallback: &[u8]) -> Vec<u8> {
    let arrow = |key: u8| {
        if mode.contains(TermMode::APP_CURSOR) {
            vec![0x1b, b'O', key]
        } else {
            vec![0x1b, b'[', key]
        }
    };
    match keysym {
        XK_LEFT => arrow(b'D'),
        XK_RIGHT => arrow(b'C'),
        XK_UP => arrow(b'A'),
        XK_DOWN => arrow(b'B'),
        XK_HOME => arrow(b'H'),
        XK_END => arrow(b'F'),
        XK_PAGE_UP => b"\x1b[5~".to_vec(),
        XK_PAGE_DOWN => b"\x1b[6~".to_vec(),
        XK_DELETE => b"\x1b[3~".to_vec(),
        XK_BACKSPACE => vec![0x7f],
        XK_TAB => vec![b'\t'],
        XK_RETURN => vec![b'\r'],
        _ => fallback.to_vec(),
    }
}

impl InputMethod {
    fn new(display: *mut Display, window: Window) -> Option<Self> {
        let im = unsafe { XOpenIM(display, ptr::null_mut(), ptr::null_mut(), ptr::null_mut()) };
        if im.is_null() {
            return None;
        }
        let input_style = CString::new("inputStyle").expect("static XIM attribute");
        let client_window = CString::new("clientWindow").expect("static XIM attribute");
        // XIMPreeditNothing | XIMStatusNothing. This lets the platform input
        // method compose Unicode/IME text without asking the renderer to own
        // composition state.
        let style: c_ulong = 0x0008 | 0x0400;
        let ic = unsafe {
            XCreateIC(
                im,
                input_style.as_ptr(),
                style,
                client_window.as_ptr(),
                window,
                ptr::null::<c_char>(),
            )
        };
        if ic.is_null() {
            unsafe { XCloseIM(im) };
            return None;
        }
        Some(Self { im, ic })
    }

    fn lookup(
        &mut self,
        event: *mut XKeyEvent,
        buffer: *mut c_char,
        length: c_int,
        keysym: *mut c_ulong,
        status: *mut c_int,
    ) -> c_int {
        unsafe { Xutf8LookupString(self.ic, event, buffer, length, keysym, status) }
    }
}

fn lookup_key(input_method: Option<&mut InputMethod>, event: &mut XKeyEvent) -> KeyLookup {
    if let Some(input_method) = input_method {
        let mut capacity = 128_usize;
        loop {
            let mut buffer = vec![0_i8; capacity];
            let mut keysym = 0 as c_ulong;
            let mut status = 0;
            let count = input_method.lookup(
                event,
                buffer.as_mut_ptr(),
                buffer.len().min(c_int::MAX as usize) as c_int,
                &mut keysym,
                &mut status,
            );
            if status == X_BUFFER_OVERFLOW {
                let required = usize::try_from(count).unwrap_or(capacity.saturating_mul(2));
                let next = required.max(capacity.saturating_mul(2));
                if next > MAX_XIM_BUFFER {
                    return KeyLookup {
                        keysym,
                        bytes: Vec::new(),
                    };
                }
                capacity = next;
                continue;
            }
            let length = bounded_lookup_length(count, buffer.len()).unwrap_or(0);
            return KeyLookup {
                keysym,
                bytes: buffer[..length].iter().map(|byte| *byte as u8).collect(),
            };
        }
    }

    let mut buffer = [0_i8; 128];
    let mut keysym = 0 as c_ulong;
    let mut compose = XComposeStatus {
        compose_ptr: ptr::null_mut(),
        chars_matched: 0,
    };
    let count = unsafe {
        XLookupString(
            event,
            buffer.as_mut_ptr(),
            buffer.len() as c_int,
            &mut keysym,
            &mut compose,
        )
    };
    let length = bounded_lookup_length(count, buffer.len()).unwrap_or(0);
    KeyLookup {
        keysym,
        bytes: buffer[..length].iter().map(|byte| *byte as u8).collect(),
    }
}

fn bounded_lookup_length(count: c_int, capacity: usize) -> Option<usize> {
    usize::try_from(count)
        .ok()
        .filter(|length| *length <= capacity)
}

impl Drop for InputMethod {
    fn drop(&mut self) {
        unsafe {
            XDestroyIC(self.ic);
            XCloseIM(self.im);
        }
    }
}

impl Clipboard {
    fn new(display: *mut Display) -> Self {
        Self {
            clipboard: intern_atom(display, "CLIPBOARD"),
            primary: intern_atom(display, "PRIMARY"),
            utf8_string: intern_atom(display, "UTF8_STRING"),
            string: intern_atom(display, "STRING"),
            targets: intern_atom(display, "TARGETS"),
            atom: intern_atom(display, "ATOM"),
            property: intern_atom(display, "ATHENA_SELECTION"),
            text: String::new(),
        }
    }

    fn own(&mut self, display: *mut Display, window: Window, text: String) {
        self.text = text;
        unsafe {
            XSetSelectionOwner(display, self.primary, window, CURRENT_TIME);
            XSetSelectionOwner(display, self.clipboard, window, CURRENT_TIME);
        }
    }

    fn request(&self, display: *mut Display, window: Window) {
        unsafe {
            XConvertSelection(
                display,
                self.clipboard,
                self.utf8_string,
                self.property,
                window,
                CURRENT_TIME,
            );
            XFlush(display);
        }
    }

    fn handle_event(
        &mut self,
        display: *mut Display,
        window: Window,
        event: &mut XEvent,
    ) -> Option<Vec<u8>> {
        match event.type_ {
            SELECTION_REQUEST => {
                let request =
                    unsafe { &*((&mut *event) as *mut XEvent as *const XSelectionRequestEvent) };
                self.respond(display, request);
                None
            }
            SELECTION_NOTIFY => {
                let notification =
                    unsafe { &*((&mut *event) as *mut XEvent as *const XSelectionEvent) };
                if notification.requestor != window || notification.property == 0 {
                    return None;
                }
                let mut actual_type = 0;
                let mut actual_format = 0;
                let mut nitems = 0;
                let mut bytes_after = 0;
                let mut data: *mut u8 = ptr::null_mut();
                let status = unsafe {
                    XGetWindowProperty(
                        display,
                        window,
                        notification.property,
                        0,
                        1_048_576,
                        1,
                        self.utf8_string,
                        &mut actual_type,
                        &mut actual_format,
                        &mut nitems,
                        &mut bytes_after,
                        &mut data,
                    )
                };
                if status != 0 || actual_format != 8 || data.is_null() {
                    if !data.is_null() {
                        unsafe { XFree(data.cast()) };
                    }
                    return None;
                }
                let bytes = unsafe { std::slice::from_raw_parts(data, nitems as usize).to_vec() };
                unsafe {
                    XFree(data.cast());
                    XDeleteProperty(display, window, notification.property);
                }
                Some(bytes)
            }
            SELECTION_CLEAR => {
                self.text.clear();
                None
            }
            _ => None,
        }
    }

    fn respond(&self, display: *mut Display, request: &XSelectionRequestEvent) {
        let property = if request.property == 0 {
            request.target
        } else {
            request.property
        };
        let mut accepted = property;
        unsafe {
            if request.target == self.targets {
                let targets = [self.utf8_string, self.string, self.targets];
                XChangeProperty(
                    display,
                    request.requestor,
                    property,
                    self.atom,
                    32,
                    PROP_MODE_REPLACE,
                    targets.as_ptr().cast(),
                    targets.len() as c_int,
                );
            } else if request.target == self.utf8_string || request.target == self.string {
                XChangeProperty(
                    display,
                    request.requestor,
                    property,
                    request.target,
                    8,
                    PROP_MODE_REPLACE,
                    self.text.as_bytes().as_ptr(),
                    self.text.len().min(c_int::MAX as usize) as c_int,
                );
            } else {
                accepted = 0;
            }
            let response = XSelectionEvent {
                type_: SELECTION_NOTIFY,
                serial: 0,
                send_event: 1,
                display,
                requestor: request.requestor,
                selection: request.selection,
                target: request.target,
                property: accepted,
                time: request.time,
            };
            let mut event = XEvent {
                type_: 0,
                pad: [0; 24],
            };
            ptr::write(
                (&mut event as *mut XEvent).cast::<XSelectionEvent>(),
                response,
            );
            XSendEvent(display, request.requestor, 0, 0, &mut event);
            XFlush(display);
        }
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

fn run_window(
    display: *mut Display,
    core: &mut NativeTerminalCore,
    pty: &mut tty::Pty,
    output_rx: Receiver<Vec<u8>>,
    bridge_rx: Option<LatestProjection>,
    projection: &mut Projection,
    options: &RendererOptions,
) -> Result<(), String> {
    let screen = unsafe { XDefaultScreen(display) };
    let attributes = [
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
    let visual = unsafe { glXChooseVisual(display, screen, attributes.as_ptr() as *mut c_int) };
    if visual.is_null() {
        return Err("X11 display has no compatible OpenGL visual".to_owned());
    }
    let root = unsafe { XRootWindow(display, screen) };
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
    let window = unsafe {
        XCreateWindow(
            display,
            root,
            0,
            0,
            1280,
            800,
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

    let context = unsafe { glXCreateContext(display, visual, ptr::null_mut(), 1) };
    if context.is_null() {
        unsafe { XDestroyWindow(display, window) };
        return Err("could not create the Athena OpenGL context".to_owned());
    }
    unsafe { glXMakeCurrent(display, window, context) };
    let visual_ptr = unsafe { (*visual).visual };
    let text = match TextRenderer::new(display, screen, window, visual_ptr, colormap) {
        Ok(text) => text,
        Err(error) => {
            unsafe {
                glXMakeCurrent(display, 0, ptr::null_mut());
                glXDestroyContext(display, context);
                XDestroyWindow(display, window);
            }
            return Err(error);
        }
    };
    let mut input_method = InputMethod::new(display, window);
    if let Some(input_method) = input_method.as_ref() {
        unsafe { XSetICFocus(input_method.ic) };
    }
    let mut writer = pty
        .file()
        .try_clone()
        .map_err(|error| format!("could not clone PTY writer: {error}"))?;
    let mut clipboard = Clipboard::new(display);
    let mut input_buffer = InputBuffer::default();
    let mut selection: Option<((usize, usize), (usize, usize))> = None;
    let mut width = 1280_i32;
    let mut height = 800_i32;
    let metrics = text.metrics();
    resize_terminal(core, pty, width, height, metrics);
    unsafe { XSetInputFocus(display, window, 1, CURRENT_TIME) };
    let mut focused = true;
    let mut running = true;
    let mut window_destroyed = false;
    let mut child_exited = false;
    let mut dirty = true;
    let mut terminal_dirty = false;
    let mut oi_motion_dirty = false;
    let mut activity_dirty = false;
    let mut initial_redraws = 0_u64;
    let mut event_redraws = 0_u64;
    let mut idle_redraws = 0_u64;
    let mut redraws = 0_u64;
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
                    } else if terminal_app {
                        let bytes = terminal_key_bytes(keysym, core.mode(), &lookup.bytes);
                        if !bytes.is_empty() {
                            let _ = writer.write_all(&bytes);
                            let _ = writer.flush();
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
                        core.scroll_display(Scroll::Delta(3));
                        dirty = true;
                        activity_dirty = true;
                    } else if button.button == 5 {
                        core.scroll_display(Scroll::Delta(-3));
                        dirty = true;
                        activity_dirty = true;
                    } else if button.button == 1 {
                        unsafe { XSetInputFocus(display, window, 1, CURRENT_TIME) };
                        focused = true;
                        let geometry = FrameGeometry::for_window(width, height, metrics);
                        if geometry.prompt.contains(button.x, button.y) {
                            let offset =
                                ((button.x as f32 - geometry.prompt.x - geometry.prompt_padding_x)
                                    / geometry.cell_width)
                                    .max(0.0) as usize;
                            input_buffer.set_cursor_chars(offset, button.state & SHIFT_MASK != 0);
                            dirty = true;
                            activity_dirty = true;
                        } else if let Some(cell) = geometry.cell_at(button.x, button.y) {
                            selection = Some((cell, cell));
                            dirty = true;
                            activity_dirty = true;
                        }
                    }
                }
                MOTION_NOTIFY => {
                    let motion = unsafe { &*((&event as *const XEvent).cast::<XMotionEvent>()) };
                    if motion.state & BUTTON1_MASK != 0 {
                        if let (Some((anchor, _)), Some(cell)) = (
                            selection,
                            FrameGeometry::for_window(width, height, metrics)
                                .cell_at(motion.x, motion.y),
                        ) {
                            selection = Some((anchor, cell));
                            dirty = true;
                            activity_dirty = true;
                        }
                    }
                }
                BUTTON_RELEASE => {
                    let button = unsafe { &*((&event as *const XEvent).cast::<XButtonEvent>()) };
                    if button.button == 1 {
                        if let (Some((anchor, _)), Some(cell)) = (
                            selection,
                            FrameGeometry::for_window(width, height, metrics)
                                .cell_at(button.x, button.y),
                        ) {
                            selection = Some((anchor, cell));
                            dirty = true;
                            activity_dirty = true;
                        }
                    }
                }
                CONFIGURE_NOTIFY => {
                    let configure =
                        unsafe { &*(&event as *const XEvent as *const XConfigureEvent) };
                    width = configure.width.max(1);
                    height = configure.height.max(1);
                    resize_terminal(core, pty, width, height, metrics);
                    dirty = true;
                    activity_dirty = true;
                }
                EXPOSE => {
                    dirty = true;
                    activity_dirty = true;
                }
                FOCUS_IN => {
                    if let Some(input_method) = input_method.as_ref() {
                        unsafe { XSetICFocus(input_method.ic) };
                    }
                    focused = true;
                    dirty = true;
                    activity_dirty = true;
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
            draw_frame(
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
                DirtyDomains {
                    full: dirty,
                    terminal: terminal_dirty,
                    oi_motion: oi_motion_dirty,
                },
                if options.animations && !options.reduced_motion {
                    render_started.elapsed().as_secs_f32()
                } else {
                    0.0
                },
            );
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
    );

    // Xft owns an XRender picture tied to the window. Tear it down before
    // destroying the drawable. If a window manager already destroyed the
    // window, leaking these process-local handles is safer than asking Xft/XIM
    // to destroy resources whose drawable no longer exists.
    if window_destroyed {
        std::mem::forget(input_method);
        std::mem::forget(text);
        // The display close in `run` will reclaim the context and any server
        // resources. Calling GLX unbind/destroy here can itself query the
        // drawable after a window manager has removed it.
        return Ok(());
    } else {
        drop(input_method);
        drop(text);
    }
    unsafe {
        glXMakeCurrent(display, 0, ptr::null_mut());
        glXDestroyContext(display, context);
        if !window_destroyed {
            XDestroyWindow(display, window);
        }
    }
    Ok(())
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
        "cpu_seconds": cpu_seconds,
        "steady_elapsed_seconds": steady_elapsed_seconds,
        "steady_cpu_seconds": steady_cpu_seconds,
    });
    let _ = std::fs::write(path, value.to_string());
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
    metrics: CellMetrics,
) {
    let layout = FrameGeometry::for_window(width, height, metrics);
    let terminal_size = layout.terminal_size();
    let columns = terminal_size.columns;
    let rows = terminal_size.rows;
    core.resize(columns, rows);
    pty.on_resize(WindowSize {
        num_cols: columns.min(u16::MAX as usize) as u16,
        num_lines: rows.min(u16::MAX as usize) as u16,
        cell_width: metrics.width.round().max(1.0) as u16,
        cell_height: metrics.height.round().max(1.0) as u16,
    });
}

fn projection_is_animated(projection: &Projection) -> bool {
    VisualMode::from_projection(projection).is_animated()
}
fn operation_progress(operation: &ProjectionOperation) -> String {
    if !operation.progress_determinate {
        return if operation.progress.is_empty() {
            String::new()
        } else {
            format!(" {}", operation.progress)
        };
    }
    let Some(value) = operation.progress_value else {
        return String::new();
    };
    let ratio = value.clamp(0.0, 1.0);
    if ratio.is_nan() {
        return String::new();
    }
    const BAR_WIDTH: usize = 12;
    let filled = (BAR_WIDTH as f64 * ratio).round() as usize;
    let filled = filled.min(BAR_WIDTH);
    format!(
        " [{}{}]{:.0}%",
        "\u{2588}".repeat(filled),
        "\u{2592}".repeat(BAR_WIDTH - filled),
        ratio * 100.0
    )
}

fn diagnostic_text(diagnostic: &crate::ProjectionDiagnostic, message: &str) -> String {
    let location = if diagnostic.path.is_empty() {
        diagnostic
            .line
            .map_or_else(String::new, |line| format!("line {line}"))
    } else {
        format!(
            "{}{}",
            diagnostic.path,
            diagnostic
                .line
                .map_or_else(String::new, |line| format!(":{line}"))
        )
    };
    let severity = if diagnostic.severity.is_empty() {
        String::new()
    } else {
        format!("[{}]", diagnostic.severity)
    };
    let mut detail = Vec::new();
    if !diagnostic.detail.is_empty() && diagnostic.detail != message {
        detail.push(diagnostic.detail.clone());
    }
    if let Some(expected) = diagnostic.expected.as_ref() {
        detail.push(format!("expected={expected}"));
    }
    if let Some(actual) = diagnostic.actual.as_ref() {
        detail.push(format!("actual={actual}"));
    }
    let mut fields = vec!["!".to_owned()];
    if !severity.is_empty() {
        fields.push(severity);
    }
    if !location.is_empty() {
        fields.push(location);
    }
    if !message.is_empty() {
        fields.push(message.to_owned());
    }
    fields.extend(detail);
    fields.join(" ")
}

fn draw_frame(
    display: *mut Display,
    width: i32,
    height: i32,
    core: &NativeTerminalCore,
    projection: &Projection,
    selection: Option<((usize, usize), (usize, usize))>,
    text: &TextRenderer,
    focused: bool,
    input_buffer: &InputBuffer,
    options: &RendererOptions,
    dirty: DirtyDomains,
    phase: f32,
) {
    let geometry = FrameGeometry::for_window(width, height, text.metrics());
    let semantic_mode = VisualMode::from_projection(projection).as_str();
    unsafe { glXWaitX() };
    unsafe {
        glViewport(0, 0, width, height);
        glMatrixMode(GL_PROJECTION);
        glLoadIdentity();
        glOrtho(0.0, width as f64, height as f64, 0.0, -1.0, 1.0);
        glMatrixMode(GL_MODELVIEW);
        glLoadIdentity();
    }

    if dirty.full {
        unsafe {
            glDisable(GL_SCISSOR_TEST);
            glClearColor(0.018, 0.024, 0.038, 1.0);
            glClear(GL_COLOR_BUFFER_BIT);
        }
        draw_chassis(&geometry, projection, focused, phase);
        with_scissor(height, geometry.operator_inner, || {
            draw_terminal_background(core, &geometry, selection);
        });
        with_scissor(height, geometry.oi_inner, || {
            draw_oi_scene(
                geometry.oi_inner.x,
                geometry.oi_inner.y,
                geometry.oi_inner.width,
                geometry.oi_inner.height,
                projection,
                phase,
                options,
            );
        });
    } else if dirty.terminal {
        with_scissor(height, geometry.operator_inner, || {
            draw_terminal_background(core, &geometry, selection);
        });
    } else if dirty.oi_motion {
        with_scissor(height, geometry.oi_inner, || {
            draw_rect(
                geometry.oi_inner.x,
                geometry.oi_inner.y,
                geometry.oi_inner.width,
                geometry.oi_inner.height,
                (0.010, 0.028, 0.037),
            );
            draw_oi_scene(
                geometry.oi_inner.x,
                geometry.oi_inner.y,
                geometry.oi_inner.width,
                geometry.oi_inner.height,
                projection,
                phase,
                options,
            );
        });
    }

    if dirty.full {
        // Complete the OpenGL writes before Xft submits text to the same
        // single-buffer drawable. GLX and Xlib have separate request paths;
        // glFlush alone does not establish the ordering Xft needs here.
        unsafe {
            glFinish();
            glXWaitGL();
        }
        // Xft renders UTF-8 after the OpenGL scene on the same single-buffer
        // X11 drawable. The frame is flushed only after both layers are done.
        let mode_label = if semantic_mode.is_empty() {
            "IDLE".to_owned()
        } else {
            semantic_mode.to_ascii_uppercase()
        };
        let header_baseline = geometry.header.y as c_int
            + ((geometry.header.height - text.metrics().height) / 2.0 + text.metrics().baseline)
                as c_int;
        text.draw(
            geometry.header.x as c_int + 22,
            header_baseline,
            &fit_text(text, "ATHENA", (geometry.header.width as c_int - 44).max(1)),
            (229, 239, 247),
        );
        text.draw(
            geometry.header.x as c_int + 145,
            header_baseline,
            &fit_text(
                text,
                "AUTONOMOUS OPERATIONS CONSOLE",
                (geometry.header.width as c_int - 190).max(1),
            ),
            (112, 145, 174),
        );
        let bridge_label = if projection.bridge_status.is_empty() {
            "WAITING"
        } else {
            projection.bridge_status.as_str()
        };
        let bridge_text = format!("BRIDGE / {bridge_label}");
        text.draw(
            (geometry.header.right() - text.text_width(&bridge_text) as f32 - 22.0) as c_int,
            header_baseline,
            &bridge_text,
            if bridge_label.starts_with("ERROR") {
                (235, 112, 116)
            } else if bridge_label == "CONNECTED" {
                (103, 198, 181)
            } else {
                (239, 194, 105)
            },
        );
        let panel_baseline =
            |panel: PixelRect| panel.y as c_int + text.metrics().baseline as c_int + 12;
        text.draw(
            geometry.operator_outer.x as c_int + 24,
            panel_baseline(geometry.operator_outer),
            &fit_text(
                text,
                &format!(
                    "OPERATOR // TERMINAL  ·  {}",
                    if focused { "FOCUS" } else { "UNFOCUSED" }
                ),
                (geometry.operator_outer.width as c_int - 48).max(1),
            ),
            (164, 189, 211),
        );
        text.draw(
            geometry.oi_outer.x as c_int + 24,
            panel_baseline(geometry.oi_outer),
            &fit_text(
                text,
                &format!("DAGOAL // {mode_label}"),
                (geometry.oi_outer.width as c_int - 48).max(1),
            ),
            mode_color(semantic_mode),
        );
        with_scissor(height, geometry.operator_inner, || {
            draw_terminal_text(text, core, &geometry);
        });
        draw_status_text(
            text,
            &geometry,
            projection,
            semantic_mode,
            focused,
            options,
            input_buffer,
        );
    } else if dirty.terminal {
        unsafe {
            glFinish();
            glXWaitGL();
        }
        with_scissor(height, geometry.operator_inner, || {
            draw_terminal_text(text, core, &geometry);
        });
    }
    unsafe {
        if !dirty.full {
            glFlush();
        }
        XFlush(display);
    }
}

fn draw_chassis(geometry: &FrameGeometry, projection: &Projection, focused: bool, _phase: f32) {
    let width = geometry.chassis.width;
    let height = geometry.chassis.height;
    draw_rect(
        geometry.chassis.x,
        geometry.chassis.y,
        width,
        height,
        (0.014, 0.019, 0.030),
    );
    draw_round_rect(
        geometry.left_x - 10.0,
        14.0,
        width - (geometry.left_x - 10.0) * 2.0,
        height - 28.0,
        20.0,
        (0.075, 0.090, 0.112),
    );
    draw_round_rect(
        geometry.left_x - 5.0,
        19.0,
        width - (geometry.left_x - 5.0) * 2.0,
        height - 38.0,
        16.0,
        (0.043, 0.055, 0.071),
    );
    draw_round_rect(
        geometry.header.x,
        geometry.header.y,
        geometry.header.width,
        geometry.header.height,
        12.0,
        (0.095, 0.112, 0.138),
    );
    for index in 0..9 {
        let x = geometry.header.x + geometry.header.width * 0.30 + index as f32 * 15.0;
        draw_round_rect(
            x,
            geometry.header.y + geometry.header.height * 0.29,
            9.0,
            4.0,
            2.0,
            (0.025, 0.034, 0.047),
        );
    }
    let indicator = if projection.status.to_ascii_lowercase().contains("fail") {
        (0.82, 0.24, 0.27)
    } else if projection.status.to_ascii_lowercase().contains("approval") {
        (0.88, 0.59, 0.20)
    } else {
        (0.27, 0.78, 0.68)
    };
    for (index, color) in [indicator, (0.28, 0.48, 0.62), (0.26, 0.34, 0.43)]
        .into_iter()
        .enumerate()
    {
        draw_round_rect(
            geometry.header.right() - 60.0 + index as f32 * 22.0,
            geometry.header.y + geometry.header.height * 0.35,
            9.0,
            9.0,
            4.5,
            color,
        );
    }
    draw_beveled_panel(
        geometry.operator_outer,
        (0.11, 0.125, 0.145),
        (0.025, 0.033, 0.045),
    );
    draw_beveled_panel(
        geometry.oi_outer,
        (0.13, 0.145, 0.16),
        (0.018, 0.047, 0.060),
    );
    draw_round_rect(
        geometry.operator_inner.x - 7.0,
        geometry.operator_inner.y - 7.0,
        geometry.operator_inner.width + 14.0,
        geometry.operator_inner.height + 14.0,
        8.0,
        if focused {
            (0.024, 0.048, 0.063)
        } else {
            (0.032, 0.038, 0.048)
        },
    );
    draw_round_rect(
        geometry.oi_inner.x - 11.0,
        geometry.oi_inner.y - 11.0,
        geometry.oi_inner.width + 22.0,
        geometry.oi_inner.height + 22.0,
        24.0,
        (0.010, 0.028, 0.037),
    );
    draw_round_rect(
        geometry.oi_inner.x - 4.0,
        geometry.oi_inner.y - 4.0,
        geometry.oi_inner.width + 8.0,
        geometry.oi_inner.height + 8.0,
        19.0,
        (0.014, 0.052, 0.065),
    );
    draw_beveled_panel(
        geometry.controls,
        (0.090, 0.105, 0.123),
        (0.032, 0.041, 0.052),
    );
    draw_beveled_panel(
        geometry.prompt,
        (0.073, 0.088, 0.106),
        (0.020, 0.028, 0.038),
    );
    for index in 0..18 {
        let x = geometry.controls.x + 26.0 + index as f32 * 11.0;
        draw_round_rect(
            x,
            geometry.controls.y + 18.0,
            6.0,
            15.0,
            2.0,
            (0.028, 0.037, 0.049),
        );
    }
    for index in 0..3 {
        let x = geometry.controls.x + geometry.controls.width - 160.0 + index as f32 * 42.0;
        draw_round_rect(
            x,
            geometry.controls.y + 11.0,
            24.0,
            24.0,
            12.0,
            (0.045, 0.058, 0.070),
        );
        draw_round_outline(
            x + 3.0,
            geometry.controls.y + 14.0,
            18.0,
            18.0,
            (0.24, 0.31, 0.36),
        );
        draw_rect(
            x + 11.0,
            geometry.controls.y + 6.0,
            2.0,
            8.0,
            (0.31, 0.46, 0.53),
        );
    }
}

fn draw_status_text(
    text: &TextRenderer,
    geometry: &FrameGeometry,
    projection: &Projection,
    semantic_mode: &str,
    focused: bool,
    options: &RendererOptions,
    input: &InputBuffer,
) {
    let input_state = input_state(projection);
    let detail = projection
        .active_operation
        .as_ref()
        .map(|operation| {
            format!(
                "{} {} ({}) -> {} [{}] {}{}",
                operation.action_kind.to_ascii_uppercase(),
                operation.label,
                operation.id,
                operation.target,
                operation.state.to_ascii_uppercase(),
                operation.mutation_state.to_ascii_uppercase(),
                operation_progress(operation)
            )
        })
        .unwrap_or_else(|| "NO ACTIVE OPERATION".to_owned());
    text.draw(
        geometry.oi_outer.x as c_int + 24,
        geometry.oi_outer.y as c_int + geometry.oi_outer.height as c_int - 12,
        &fit_text(
            text,
            &format!("{}  ·  {}", detail, options.mascot.to_ascii_uppercase()),
            (geometry.oi_outer.width as c_int - 48).max(1),
        ),
        mode_color(semantic_mode),
    );
    let prompt_color = if focused {
        (206, 220, 230)
    } else {
        (132, 145, 156)
    };
    let state_label = format!("  [{input_state}]");
    let prompt_x = geometry.prompt.x as c_int + geometry.prompt_padding_x as c_int;
    let prompt_y = geometry.prompt.y as c_int + geometry.prompt.height as c_int - 6;
    let available = (geometry.prompt.width as c_int
        - geometry.prompt_padding_x as c_int * 2
        - text.text_width(&state_label)
        - text.text_width("> "))
    .max(1);
    let input_value = format!("{}{}", input.text(), input.composition());
    let (displayed, display_cursor) = fit_input(text, &input_value, input.cursor(), available);
    let prompt_value = format!("> {displayed}{state_label}");
    text.draw(prompt_x, prompt_y, &prompt_value, prompt_color);
    if focused {
        let cursor_prefix: String = displayed.chars().take(display_cursor).collect();
        let cursor_text = format!("> {cursor_prefix}");
        let cursor_x = prompt_x + text.text_width(&cursor_text);
        draw_rect(
            cursor_x as f32,
            geometry.prompt.y + 5.0,
            2.0,
            geometry.prompt.height - 10.0,
            (0.36, 0.76, 0.72),
        );
    }
    let line_height = text.metrics().height.max(1.0);
    let mut annotation_y = geometry.oi_inner.y as c_int + text.metrics().baseline as c_int;
    let mut annotations: Vec<(String, (u8, u8, u8))> = Vec::new();
    if let Some(request) = projection.model_request.as_ref() {
        annotations.push((
            format!(
                "MODEL  {}/{}  {}",
                if request.provider.is_empty() {
                    "—"
                } else {
                    &request.provider
                },
                if request.model.is_empty() {
                    "—"
                } else {
                    &request.model
                },
                request.status.to_ascii_uppercase()
            ),
            (166, 191, 211),
        ));
    }
    if semantic_mode.eq_ignore_ascii_case("code") {
        if let Some(code) = projection.code_view.as_ref() {
            annotations.push((
                format!(
                    "CODE  {}  {}  {}",
                    code.path,
                    code.language.to_ascii_uppercase(),
                    code.mutation_state.to_ascii_uppercase()
                ),
                (103, 202, 212),
            ));
            let preview_lines: Vec<String> = if !code.diff.is_empty() {
                code.diff.iter().take(2).cloned().collect()
            } else if !code.lines.is_empty() {
                code.lines.iter().take(2).cloned().collect()
            } else {
                code.text.lines().take(2).map(str::to_owned).collect()
            };
            annotations.extend(preview_lines.into_iter().map(|line| {
                (
                    line.clone(),
                    if line.starts_with('+') {
                        (103, 202, 212)
                    } else if line.starts_with('-') {
                        (235, 112, 116)
                    } else {
                        (192, 208, 220)
                    },
                )
            }));
            if code.preview_truncated {
                annotations.push(("… PREVIEW BOUNDED".to_owned(), (239, 194, 105)));
            }
        }
    } else if semantic_mode.eq_ignore_ascii_case("failure") {
        annotations.extend(projection.diagnostics.iter().take(3).map(|diagnostic| {
            let message = if diagnostic.message.is_empty() {
                &diagnostic.detail
            } else {
                &diagnostic.message
            };
            (diagnostic_text(diagnostic, message), (235, 112, 116))
        }));
    } else if semantic_mode.eq_ignore_ascii_case("verify") {
        annotations.push((
            format!(
                "VERIFY  {}  {} CHECKS",
                projection.verification.status.to_ascii_uppercase(),
                projection.verification.checks.len()
            ),
            (239, 194, 105),
        ));
    } else {
        if let Some(action) = projection.current_action.as_ref() {
            let action_name = if action.kind.is_empty() {
                semantic_mode
            } else {
                action.kind.as_str()
            };
            annotations.push((
                format!("{}  {}", action_name.to_ascii_uppercase(), action.label),
                (176, 205, 220),
            ));
            if !action.query.is_empty() {
                annotations.push((format!("QUERY  {}", action.query), (192, 208, 220)));
            } else if !action.target.is_empty() {
                annotations.push((format!("TARGET  {}", action.target), (192, 208, 220)));
            }
            if !action.detail.is_empty()
                && action.detail != action.query
                && action.detail != action.target
            {
                annotations.push((action.detail.clone(), (176, 205, 220)));
            }
            if !action.progress.is_empty() {
                annotations.push((
                    if action.progress_determinate {
                        action.progress_value.map_or_else(
                            || format!("PROGRESS  {}", action.progress),
                            |value| format!("PROGRESS  {}  {:.0}%", action.progress, value * 100.0),
                        )
                    } else {
                        format!("PROGRESS  {}", action.progress)
                    },
                    (239, 194, 105),
                ));
            }
        }
        if annotations.is_empty() {
            annotations.extend(
                projection
                    .oi
                    .iter()
                    .take(3)
                    .map(|line| (line.clone(), (176, 205, 220))),
            );
        }
    }
    if let Some(entity) = projection.runtime_entities.first() {
        annotations.push((
            format!(
                "NODE  {}  [{}]",
                projection_entity_label(entity),
                entity.status
            ),
            (129, 176, 193),
        ));
    }
    for (line, color) in annotations.into_iter().take(4) {
        text.draw(
            geometry.oi_inner.x as c_int + 18,
            annotation_y,
            &fit_text(text, &line, (geometry.oi_inner.width as c_int - 36).max(1)),
            color,
        );
        annotation_y += line_height as c_int;
    }
    if let Some(alert) = projection.alerts.first() {
        text.draw(
            geometry.oi_inner.x as c_int + 18,
            (geometry.oi_inner.y + geometry.oi_inner.height - 24.0) as c_int,
            &fit_text(text, alert, (geometry.oi_inner.width as c_int - 36).max(1)),
            (235, 154, 105),
        );
    }
    let trace = projection
        .trace
        .first()
        .or_else(|| projection.stream_tail.first())
        .cloned()
        .unwrap_or_default();
    if !trace.is_empty() {
        text.draw(
            geometry.controls.x as c_int + 26,
            geometry.controls.y as c_int + 33,
            &fit_text(
                text,
                &trace,
                (geometry.controls.width as c_int - 220).max(1),
            ),
            (110, 150, 174),
        );
    } else {
        let status_line = if projection.bridge_status.starts_with("ERROR") {
            projection.bridge_status.as_str()
        } else if projection.status.is_empty() {
            "NO STREAM EVENTS"
        } else {
            projection.status.as_str()
        };
        text.draw(
            geometry.controls.x as c_int + 26,
            geometry.controls.y as c_int + 33,
            &fit_text(
                text,
                status_line,
                (geometry.controls.width as c_int - 220).max(1),
            ),
            (110, 150, 174),
        );
    }
}

fn input_state(projection: &Projection) -> &'static str {
    VisualMode::from_projection(projection).prompt_state(projection)
}

fn fit_text(text: &TextRenderer, value: &str, available: i32) -> String {
    if text.text_width(value) <= available {
        return value.to_owned();
    }
    let mut result = String::new();
    for character in value.chars() {
        let candidate = format!("{result}{character}…");
        if text.text_width(&candidate) > available {
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

fn fit_input(text: &TextRenderer, value: &str, cursor: usize, available: i32) -> (String, usize) {
    if text.text_width(value) <= available {
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
        if text.text_width(&candidate) <= available {
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

fn draw_terminal_background(
    core: &NativeTerminalCore,
    geometry: &FrameGeometry,
    selection: Option<((usize, usize), (usize, usize))>,
) {
    let content = core.renderable_content();
    let columns = core.size().columns.max(1);
    for (index, indexed) in content.display_iter.enumerate() {
        let cell = indexed.cell;
        let row = index / columns;
        let column = index % columns;
        let mut background = resolve_term_color(cell.bg, content.colors, false);
        if cell.flags.contains(Flags::INVERSE) {
            background = resolve_term_color(cell.fg, content.colors, true);
        }
        if background != (7, 12, 19) {
            draw_rect(
                geometry.operator_inner.x + column as f32 * geometry.cell_width,
                geometry.operator_inner.y + row as f32 * geometry.cell_height,
                geometry.cell_width,
                geometry.cell_height,
                rgb_f32(background),
            );
        }
    }
    draw_selection(
        geometry.operator_inner.x,
        geometry.operator_inner.y,
        selection,
        geometry.cell_width,
        geometry.cell_height,
    );
}

fn draw_terminal_text(text: &TextRenderer, core: &NativeTerminalCore, geometry: &FrameGeometry) {
    let content = core.renderable_content();
    let columns = core.size().columns.max(1);
    let mut run_text = String::new();
    let mut run_row = 0;
    let mut run_start_column = 0;
    let mut run_next_column = 0;
    let mut run_color = (0, 0, 0);
    let mut has_run = false;
    for (index, indexed) in content.display_iter.enumerate() {
        let cell = indexed.cell;
        if cell
            .flags
            .intersects(Flags::HIDDEN | Flags::WIDE_CHAR_SPACER)
            || cell.c == ' '
        {
            if has_run {
                draw_terminal_run(
                    text,
                    geometry,
                    run_row,
                    run_start_column,
                    &run_text,
                    run_color,
                );
                run_text.clear();
                has_run = false;
            }
            continue;
        }
        let row = index / columns;
        let column = index % columns;
        let mut foreground = resolve_term_color(cell.fg, content.colors, true);
        if cell.flags.contains(Flags::INVERSE) {
            foreground = resolve_term_color(cell.bg, content.colors, false);
        }
        if cell.flags.contains(Flags::DIM) {
            foreground = (
                foreground.0.saturating_mul(2) / 3,
                foreground.1.saturating_mul(2) / 3,
                foreground.2.saturating_mul(2) / 3,
            );
        }
        if !has_run || run_row != row || run_next_column != column || run_color != foreground {
            if has_run {
                draw_terminal_run(
                    text,
                    geometry,
                    run_row,
                    run_start_column,
                    &run_text,
                    run_color,
                );
                run_text.clear();
            }
            run_row = row;
            run_start_column = column;
            run_color = foreground;
            has_run = true;
        }
        run_text.push(cell.c);
        run_next_column = column + 1;
    }
    if has_run {
        draw_terminal_run(
            text,
            geometry,
            run_row,
            run_start_column,
            &run_text,
            run_color,
        );
    }
}

fn draw_terminal_run(
    text: &TextRenderer,
    geometry: &FrameGeometry,
    row: usize,
    column: usize,
    value: &str,
    color: (u8, u8, u8),
) {
    if value.is_empty() {
        return;
    }
    text.draw(
        geometry.operator_inner.x as c_int + column as c_int * geometry.cell_width as c_int,
        geometry.operator_inner.y as c_int
            + row as c_int * geometry.cell_height as c_int
            + text.metrics().baseline as c_int,
        value,
        color,
    );
}

fn resolve_term_color(color: TermColor, colors: &Colors, foreground: bool) -> (u8, u8, u8) {
    let fallback = if foreground {
        (211, 225, 234)
    } else {
        (7, 12, 19)
    };
    match color {
        TermColor::Spec(rgb) => (rgb.r, rgb.g, rgb.b),
        TermColor::Named(name) => colors[name]
            .map(|rgb| (rgb.r, rgb.g, rgb.b))
            .unwrap_or_else(|| named_color(name, fallback)),
        TermColor::Indexed(index) => colors[index as usize]
            .map(|rgb| (rgb.r, rgb.g, rgb.b))
            .unwrap_or_else(|| indexed_color(index, fallback)),
    }
}

fn named_color(name: NamedColor, fallback: (u8, u8, u8)) -> (u8, u8, u8) {
    match name {
        NamedColor::Black | NamedColor::DimBlack => (18, 24, 31),
        NamedColor::Red | NamedColor::DimRed => (231, 102, 111),
        NamedColor::Green | NamedColor::DimGreen => (100, 205, 159),
        NamedColor::Yellow | NamedColor::DimYellow => (235, 190, 106),
        NamedColor::Blue | NamedColor::DimBlue => (112, 165, 232),
        NamedColor::Magenta | NamedColor::DimMagenta => (205, 132, 221),
        NamedColor::Cyan | NamedColor::DimCyan => (92, 199, 216),
        NamedColor::White | NamedColor::DimWhite => (205, 218, 229),
        NamedColor::BrightBlack => (87, 105, 119),
        NamedColor::BrightRed => (255, 133, 140),
        NamedColor::BrightGreen => (130, 240, 186),
        NamedColor::BrightYellow => (255, 216, 125),
        NamedColor::BrightBlue => (145, 193, 255),
        NamedColor::BrightMagenta => (232, 165, 245),
        NamedColor::BrightCyan => (125, 232, 240),
        NamedColor::BrightWhite => (244, 248, 251),
        NamedColor::Background => (7, 12, 19),
        NamedColor::Cursor => (128, 220, 209),
        NamedColor::Foreground | NamedColor::DimForeground | NamedColor::BrightForeground => {
            fallback
        }
    }
}

fn indexed_color(index: u8, fallback: (u8, u8, u8)) -> (u8, u8, u8) {
    if index < 16 {
        return named_color(
            match index {
                0 => NamedColor::Black,
                1 => NamedColor::Red,
                2 => NamedColor::Green,
                3 => NamedColor::Yellow,
                4 => NamedColor::Blue,
                5 => NamedColor::Magenta,
                6 => NamedColor::Cyan,
                7 => NamedColor::White,
                8 => NamedColor::BrightBlack,
                9 => NamedColor::BrightRed,
                10 => NamedColor::BrightGreen,
                11 => NamedColor::BrightYellow,
                12 => NamedColor::BrightBlue,
                13 => NamedColor::BrightMagenta,
                14 => NamedColor::BrightCyan,
                _ => NamedColor::BrightWhite,
            },
            fallback,
        );
    }
    if (16..=231).contains(&index) {
        let value = index - 16;
        let r = value / 36;
        let g = (value % 36) / 6;
        let b = value % 6;
        let channel = |value: u8| if value == 0 { 0 } else { value * 40 + 55 };
        return (channel(r), channel(g), channel(b));
    }
    if (232..=255).contains(&index) {
        let value = 8 + (index - 232) * 10;
        return (value, value, value);
    }
    fallback
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

fn draw_selection(
    x: f32,
    y: f32,
    selection: Option<((usize, usize), (usize, usize))>,
    cell_width: f32,
    cell_height: f32,
) {
    let Some((anchor, extent)) = selection else {
        return;
    };
    let (start, end) = selection_bounds(anchor, extent);
    unsafe {
        glColor3f(0.42, 0.68, 0.80);
        glLineWidth(1.0);
    }
    for row in start.1..=end.1 {
        let first = if row == start.1 { start.0 } else { 0 };
        let last = if row == end.1 {
            end.0.saturating_add(1).max(first + 1)
        } else {
            // A middle row spans the visible terminal content width. The
            // viewport clips the outline naturally at the aperture edge.
            end.0.max(first + 1)
        };
        draw_outline_rect(
            x + first as f32 * cell_width,
            y + row as f32 * cell_height,
            (last.saturating_sub(first)) as f32 * cell_width,
            cell_height,
        );
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

fn draw_outline_rect(x: f32, y: f32, width: f32, height: f32) {
    unsafe {
        glBegin(GL_LINE_LOOP);
        glVertex2f(x, y);
        glVertex2f(x + width, y);
        glVertex2f(x + width, y + height);
        glVertex2f(x, y + height);
        glEnd();
    }
}

fn draw_round_rect(x: f32, y: f32, width: f32, height: f32, radius: f32, color: (f32, f32, f32)) {
    let radius = radius.min(width / 2.0).min(height / 2.0).max(0.0);
    unsafe {
        glColor3f(color.0, color.1, color.2);
        glBegin(GL_POLYGON);
        for corner in 0..4 {
            let center = match corner {
                0 => (x + radius, y + radius),
                1 => (x + width - radius, y + radius),
                2 => (x + width - radius, y + height - radius),
                _ => (x + radius, y + height - radius),
            };
            let start = corner as f32 * std::f32::consts::FRAC_PI_2 - std::f32::consts::PI;
            for step in 0..=5 {
                let angle = start + step as f32 * std::f32::consts::FRAC_PI_2 / 5.0;
                glVertex2f(
                    center.0 + radius * angle.cos(),
                    center.1 + radius * angle.sin(),
                );
            }
        }
        glEnd();
    }
}

fn draw_round_outline(x: f32, y: f32, width: f32, height: f32, color: (f32, f32, f32)) {
    let radius = width.min(height) / 2.0;
    unsafe {
        glColor3f(color.0, color.1, color.2);
        glLineWidth(1.0);
        glBegin(GL_LINE_LOOP);
        for corner in 0..4 {
            let center = match corner {
                0 => (x + radius, y + radius),
                1 => (x + width - radius, y + radius),
                2 => (x + width - radius, y + height - radius),
                _ => (x + radius, y + height - radius),
            };
            let start = corner as f32 * std::f32::consts::FRAC_PI_2 - std::f32::consts::PI;
            for step in 0..=5 {
                let angle = start + step as f32 * std::f32::consts::FRAC_PI_2 / 5.0;
                glVertex2f(
                    center.0 + radius * angle.cos(),
                    center.1 + radius * angle.sin(),
                );
            }
        }
        glEnd();
    }
}

fn draw_beveled_panel(rect: PanelRect, outer: (f32, f32, f32), inner: (f32, f32, f32)) {
    let width = rect.width.max(0.0);
    let height = rect.height.max(0.0);
    draw_round_rect(
        rect.x + 5.0,
        rect.y + 7.0,
        width,
        height,
        14.0,
        (0.008, 0.012, 0.018),
    );
    draw_round_rect(rect.x, rect.y, width, height, 14.0, outer);
    draw_round_outline(rect.x, rect.y, width, height, (0.18, 0.22, 0.26));
    draw_round_rect(
        rect.x + 9.0,
        rect.y + 9.0,
        (width - 18.0).max(0.0),
        (height - 18.0).max(0.0),
        9.0,
        inner,
    );
    draw_round_outline(
        rect.x + 10.0,
        rect.y + 10.0,
        (width - 20.0).max(0.0),
        (height - 20.0).max(0.0),
        (0.06, 0.11, 0.14),
    );
}

fn draw_rect(x: f32, y: f32, width: f32, height: f32, color: (f32, f32, f32)) {
    unsafe {
        glColor3f(color.0, color.1, color.2);
        glBegin(GL_QUADS);
        glVertex2f(x, y);
        glVertex2f(x + width, y);
        glVertex2f(x + width, y + height);
        glVertex2f(x, y + height);
        glEnd();
    }
}

fn with_scissor(draw_height: i32, rect: PixelRect, draw: impl FnOnce()) {
    let x = rect.x.max(0.0).round() as c_int;
    let width = rect.width.max(0.0).round() as c_int;
    let height = rect.height.max(0.0).round() as c_int;
    let y = (draw_height as f32 - rect.bottom()).max(0.0).round() as c_int;
    unsafe {
        glEnable(GL_SCISSOR_TEST);
        glScissor(x, y, width, height);
    }
    draw();
    unsafe { glDisable(GL_SCISSOR_TEST) };
}

fn draw_oi_scene(
    x: f32,
    y: f32,
    width: f32,
    height: f32,
    projection: &Projection,
    phase: f32,
    options: &RendererOptions,
) {
    let visual_mode = VisualMode::from_projection(projection);
    let semantic_mode = visual_mode.as_str();
    let scene_color = mode_color(semantic_mode);
    // The OI is a CRT-like instrument, not a second text pane: a horizon,
    // converging floor, sparse phosphor points, and semantic graph links give
    // it a readable depth field while keeping all state authoritative in the
    // projection frame.
    let horizon = y + height * 0.36;
    unsafe {
        glColor3f(
            scene_color.0 as f32 / 255.0,
            scene_color.1 as f32 / 255.0,
            scene_color.2 as f32 / 255.0,
        );
        glLineWidth(1.0);
        glBegin(GL_LINES);
        for index in 0..8 {
            let t = index as f32 / 8.0;
            let gy = horizon + (height - (horizon - y)) * t * t;
            glVertex2f(x, gy);
            glVertex2f(x + width, gy);
        }
        let vanishing_x = x + width * 0.5;
        for index in 0..11 {
            let bottom_x = x + width * index as f32 / 10.0;
            glVertex2f(vanishing_x, horizon);
            glVertex2f(bottom_x, y + height);
        }
        glEnd();
    }
    for index in 0..22 {
        let px = x + ((index * 37 % 97) as f32 / 97.0) * width;
        let py = y + ((index * 19 % 89) as f32 / 89.0) * height * 0.72;
        let twinkle = 0.55 + 0.35 * ((phase * 1.7 + index as f32).sin().abs());
        draw_rect(
            px,
            py,
            1.5,
            1.5,
            (0.18 * twinkle, 0.50 * twinkle, 0.57 * twinkle),
        );
    }
    let mut tree_entities = Vec::new();
    if !projection.runtime_tree.is_empty() {
        flatten_tree(&projection.runtime_tree, None, &mut tree_entities);
    }
    let runtime_source = if tree_entities.is_empty() {
        if projection.runtime_entities.is_empty() {
            &projection.entities
        } else {
            &projection.runtime_entities
        }
    } else {
        &tree_entities
    };
    let runtime: Vec<&ProjectionEntity> = runtime_source
        .iter()
        .filter(|entity| {
            matches!(
                entity.kind.as_str(),
                "operation"
                    | "workflow"
                    | "verification"
                    | "child_task"
                    | "task"
                    | "execution"
                    | "generated_tool"
                    | "research"
                    | "resource"
                    | "artifact"
            )
        })
        .take(6)
        .collect();
    let mut positions: Vec<(String, f32, f32)> = Vec::with_capacity(runtime.len());
    for (index, entity) in runtime.iter().enumerate() {
        let column = index % 2;
        let row = index / 2;
        let nx = x + width * (0.28 + column as f32 * 0.44);
        let ny = y + height * (0.30 + row as f32 * 0.23);
        positions.push((entity.id.clone(), nx, ny));
    }
    unsafe {
        glColor3f(
            scene_color.0 as f32 / 255.0,
            scene_color.1 as f32 / 255.0,
            scene_color.2 as f32 / 255.0,
        );
        glLineWidth(1.5);
        glBegin(GL_LINES);
        for entity in runtime.iter() {
            let Some(parent_id) = entity.parent_id.as_ref() else {
                continue;
            };
            let Some((_, px, py)) = positions.iter().find(|(id, _, _)| id == parent_id) else {
                continue;
            };
            let Some((_, cx, cy)) = positions.iter().find(|(id, _, _)| id == &entity.id) else {
                continue;
            };
            glVertex2f(*px, *py);
            glVertex2f(*cx, *cy);
        }
        glEnd();
    }
    for (index, entity) in runtime.iter().enumerate() {
        let (_, nx, ny) = &positions[index];
        let color = if entity.status.eq_ignore_ascii_case("failed")
            || entity.status.eq_ignore_ascii_case("failure")
        {
            (0.82, 0.33, 0.36)
        } else if entity.status.eq_ignore_ascii_case("approval") {
            (0.82, 0.62, 0.34)
        } else {
            rgb_f32(scene_color)
        };
        draw_node(*nx, *ny, 15.0, color);
        if options.animations && !options.reduced_motion && index % 2 == 0 {
            let pulse = ((phase * 2.0 + index as f32).sin() + 1.0) * 0.5;
            draw_round_outline(
                *nx - 19.0 - pulse * 3.0,
                *ny - 19.0 - pulse * 3.0,
                38.0 + pulse * 6.0,
                38.0 + pulse * 6.0,
                (color.0 * 0.34, color.1 * 0.34, color.2 * 0.34),
            );
        }
    }
    if positions.len() > 1 {
        for index in 0..positions.len() - 1 {
            let t = (phase * 0.36 + index as f32 * 0.27).fract();
            let sx = positions[index].1;
            let sy = positions[index].2;
            let ex = positions[index + 1].1;
            let ey = positions[index + 1].2;
            draw_round_rect(
                sx + (ex - sx) * t - 3.0,
                sy + (ey - sy) * t - 3.0,
                6.0,
                6.0,
                3.0,
                rgb_f32(scene_color),
            );
        }
    }
    let (buddy_x, buddy_y) = match visual_mode {
        VisualMode::Read | VisualMode::Inspect | VisualMode::Search => {
            (x + width * 0.18, y + height * 0.28)
        }
        VisualMode::Code | VisualMode::Test | VisualMode::Verify | VisualMode::Execute => {
            (x + width * 0.76, y + height * 0.26)
        }
        VisualMode::Failure => (x + width * 0.78, y + height * 0.76),
        VisualMode::Approval => (x + width * 0.72, y + height * 0.84),
        VisualMode::Think | VisualMode::Respond | VisualMode::Generate | VisualMode::Recover => {
            (x + width * 0.50, y + height * 0.42)
        }
        VisualMode::Idle => match projection
            .buddy
            .as_ref()
            .map(|buddy| buddy.anchor.to_ascii_lowercase())
            .as_deref()
        {
            Some("left") => (x + width * 0.20, y + height * 0.58),
            Some("right") => (x + width * 0.80, y + height * 0.58),
            _ => (x + width * 0.50, y + height * 0.58),
        },
    };
    let buddy_status = projection
        .buddy
        .as_ref()
        .filter(|buddy| !buddy.status.is_empty())
        .map(|buddy| buddy.status.as_str())
        .unwrap_or(projection.status.as_str());
    let buddy_character = projection
        .buddy
        .as_ref()
        .map(|buddy| buddy.character.as_str())
        .filter(|character| !character.is_empty())
        .unwrap_or(options.mascot.as_str());
    if !buddy_character.eq_ignore_ascii_case("off") {
        draw_buddy(buddy_x, buddy_y, buddy_status, buddy_character, phase);
    }
}

fn flatten_tree(
    nodes: &[ProjectionTreeNode],
    parent_id: Option<&str>,
    output: &mut Vec<ProjectionEntity>,
) {
    for node in nodes {
        let id = if node.id.is_empty() {
            format!("{}:{}", node.kind, node.label)
        } else {
            node.id.clone()
        };
        output.push(ProjectionEntity {
            id: id.clone(),
            kind: node.kind.clone(),
            label: node.label.clone(),
            status: node.status.clone(),
            parent_id: parent_id.map(str::to_owned),
            metadata: node.metadata.clone(),
        });
        flatten_tree(&node.children, Some(&id), output);
    }
}

fn projection_entity_label(entity: &ProjectionEntity) -> String {
    if !entity.label.is_empty() {
        return entity.label.clone();
    }
    for key in ["canonical_path", "path", "uri", "resource"] {
        if let Some(value) = entity.metadata.get(key).and_then(serde_json::Value::as_str) {
            if !value.is_empty() {
                return value.to_owned();
            }
        }
    }
    entity.id.clone()
}

fn draw_node(x: f32, y: f32, radius: f32, color: (f32, f32, f32)) {
    unsafe {
        glColor3f(color.0, color.1, color.2);
        glLineWidth(1.5);
        glBegin(GL_LINE_LOOP);
        for index in 0..8 {
            let angle = std::f32::consts::TAU * index as f32 / 8.0;
            glVertex2f(x + radius * angle.cos(), y + radius * angle.sin());
        }
        glEnd();
    }
}

fn draw_buddy(x: f32, y: f32, status: &str, character: &str, phase: f32) {
    let color = match status.to_ascii_uppercase().as_str() {
        "FAILURE" | "BLOCKED" => (0.92, 0.32, 0.36),
        "APPROVAL" => (0.93, 0.69, 0.25),
        _ => (0.36, 0.82, 0.78),
    };
    let scale = 5.0;
    let left = x - scale * 5.0;
    let top = y - scale * 4.0;
    if status.eq_ignore_ascii_case("failure") || status.eq_ignore_ascii_case("blocked") {
        draw_round_outline(
            left - 13.0,
            top - 13.0,
            scale * 10.0 + 26.0,
            scale * 8.0 + 26.0,
            color,
        );
    }
    draw_round_rect(
        left - 7.0,
        top + scale * 7.0,
        scale * 10.0 + 14.0,
        9.0,
        4.0,
        (0.015, 0.043, 0.054),
    );
    let body = [
        "  ### ###  ",
        " ######### ",
        "###########",
        "##  ###  ##",
        "###########",
        " ######### ",
        "  #######  ",
        "   #####   ",
    ];
    for (row, line) in body.iter().enumerate() {
        for (column, pixel) in line.chars().enumerate() {
            if pixel == '#' {
                let wobble = if row == 0 && character.eq_ignore_ascii_case("owl") {
                    (phase * 3.0).sin().round()
                } else {
                    0.0
                };
                draw_rect(
                    left + column as f32 * scale + wobble,
                    top + row as f32 * scale,
                    scale + 0.7,
                    scale + 0.7,
                    color,
                );
            }
        }
    }
    let eye = if character.eq_ignore_ascii_case("cat") {
        (0.98, 0.84, 0.42)
    } else {
        (0.90, 0.96, 0.83)
    };
    draw_rect(left + scale * 2.0, top + scale * 3.0, scale, scale, eye);
    draw_rect(left + scale * 7.0, top + scale * 3.0, scale, scale, eye);
    if character.eq_ignore_ascii_case("cat") {
        draw_rect(left - scale, top - scale, scale * 2.0, scale * 2.0, color);
        draw_rect(
            left + scale * 9.0,
            top - scale,
            scale * 2.0,
            scale * 2.0,
            color,
        );
    } else if character.eq_ignore_ascii_case("bot") {
        draw_rect(x - 1.0, top - 13.0, 2.0, 8.0, color);
        draw_round_rect(x - 3.0, top - 17.0, 6.0, 6.0, 3.0, color);
    }
}

#[cfg(test)]
mod tests {
    use super::{FrameGeometry, Projection, input_state, is_wm_delete_message};
    use crate::ProjectionView;
    use alacritty_terminal::term::TermMode;

    #[test]
    fn frame_geometry_keeps_apertures_equal() {
        let geometry = FrameGeometry::new(1000, 700);
        let left_end = geometry.operator_outer.x + geometry.operator_outer.width;
        assert!(geometry.oi_outer.x > left_end);

        assert_eq!(left_end - geometry.left_x, geometry.oi_outer.width);
        assert_eq!(geometry.operator_inner.width, geometry.oi_inner.width);
        assert_eq!(geometry.operator_inner.height, geometry.oi_inner.height);
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
    fn prompt_state_is_explicit_and_projection_driven() {
        let mut projection = Projection {
            status: "Approval requested".to_owned(),
            ..Projection::default()
        };
        assert_eq!(input_state(&projection), "APPROVAL");
        projection.status = "Disconnected".to_owned();
        assert_eq!(input_state(&projection), "DISCONNECTED");
        projection.status = "Working".to_owned();
        projection.view = ProjectionView {
            mode: "search".to_owned(),
            ..ProjectionView::default()
        };
        assert_eq!(input_state(&projection), "WORKING");
    }

    #[test]
    fn only_wm_protocol_delete_client_messages_close_the_window() {
        assert!(is_wm_delete_message(10, 32, 20, 10, 20));
        assert!(!is_wm_delete_message(10, 32, 21, 10, 20));
        assert!(!is_wm_delete_message(11, 32, 20, 10, 20));
        assert!(!is_wm_delete_message(10, 16, 20, 10, 20));
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
        assert_eq!(super::bounded_lookup_length(4, 8), Some(4));
        assert_eq!(super::bounded_lookup_length(8, 8), Some(8));
        assert_eq!(super::bounded_lookup_length(9, 8), None);
        assert_eq!(super::bounded_lookup_length(-1, 8), None);
    }
}
