//! Small Linux/X11 compositor for the native vertical slice.
//!
//! The production renderer will replace the immediate-mode drawing here with
//! the upstream Alacritty glyph/GPU renderer plus Athena shader passes.  This
//! slice is intentionally dependency-light so it can be built and tested in a
//! minimal development environment while proving window, PTY, input, resize,
//! and projection ownership.

#![allow(clippy::too_many_arguments)]

use std::env;
use std::ffi::{CString, c_char, c_int, c_long, c_ulong, c_void};
use std::io::Write;
use std::ptr;
use std::sync::mpsc::Receiver;
use std::thread;
use std::time::{Duration, Instant};

use alacritty_terminal::event::{OnResize, WindowSize};
use alacritty_terminal::tty::{self, ChildEvent, EventedPty};

use athena_terminal::NativeTerminalCore;

use crate::{
    LatestProjection, Projection, ProjectionCodeView, ProjectionEntity, ProjectionOperation,
    ProjectionTreeNode, apply_available,
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
const KEY_PRESS_MASK: c_long = 1;
const BUTTON_PRESS_MASK: c_long = 1 << 2;
const BUTTON_RELEASE_MASK: c_long = 1 << 3;
const POINTER_MOTION_MASK: c_long = 1 << 6;
const STRUCTURE_NOTIFY_MASK: c_long = 1 << 17;
const EXPOSURE_MASK: c_long = 1 << 15;
const CW_EVENT_MASK: c_ulong = 1 << 11;
const CW_COLORMAP: c_ulong = 1 << 13;
const INPUT_OUTPUT: c_int = 1;
const GLX_RGBA: c_int = 4;
const GLX_DOUBLEBUFFER: c_int = 5;
const GLX_RED_SIZE: c_int = 8;
const GLX_GREEN_SIZE: c_int = 9;
const GLX_BLUE_SIZE: c_int = 10;
const GLX_DEPTH_SIZE: c_int = 12;
const GL_COLOR_BUFFER_BIT: u32 = 0x0000_4000;
const GL_QUADS: u32 = 0x0007;
const GL_LINES: u32 = 0x0001;
const GL_LINE_LOOP: u32 = 0x0002;
const GL_PROJECTION: u32 = 0x1701;
const GL_MODELVIEW: u32 = 0x1700;
const SHIFT_MASK: CUint = 1;
const CONTROL_MASK: CUint = 1 << 2;
const BUTTON1_MASK: CUint = 1 << 8;
const CURRENT_TIME: c_ulong = 0;
const PROP_MODE_REPLACE: c_int = 0;
const CELL_WIDTH: i32 = 9;
const CELL_HEIGHT: i32 = 18;
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

#[repr(C)]
struct XComposeStatus {
    compose_ptr: *mut c_char,
    chars_matched: c_int,
}

#[link(name = "X11")]
unsafe extern "C" {
    fn XOpenDisplay(name: *const c_char) -> *mut Display;
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
    fn XCreateGC(
        display: *mut Display,
        drawable: Window,
        valuemask: c_ulong,
        values: *mut c_void,
    ) -> *mut c_void;
    fn XSetForeground(display: *mut Display, gc: *mut c_void, foreground: c_ulong);
    fn XDrawString(
        display: *mut Display,
        drawable: Window,
        gc: *mut c_void,
        x: c_int,
        y: c_int,
        string: *const c_char,
        length: c_int,
    );
    fn XFlush(display: *mut Display) -> c_int;
    fn XFreeGC(display: *mut Display, gc: *mut c_void) -> c_int;
    fn XDestroyWindow(display: *mut Display, window: Window) -> c_int;
    fn XCloseDisplay(display: *mut Display) -> c_int;
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
    fn glXSwapBuffers(display: *mut Display, drawable: Window);
    fn glXDestroyContext(display: *mut Display, context: GLXContext);
    fn glClearColor(red: f32, green: f32, blue: f32, alpha: f32);
    fn glClear(mask: u32);
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

#[derive(Clone, Copy, Debug)]
struct FrameGeometry {
    width: i32,
    height: i32,
    left_x: f32,
    body_y: f32,
    body_height: f32,
    aperture_width: f32,
}

impl FrameGeometry {
    fn new(width: i32, height: i32) -> Self {
        let margin = 18.0_f32;
        let gap = 18.0_f32;
        let header = 48.0_f32;
        let rail = 52.0_f32;
        let body_y = header + 12.0;
        let body_height = (height as f32 - body_y - rail).max(80.0);
        let aperture_width = ((width as f32 - margin * 2.0 - gap) / 2.0).max(80.0);
        Self {
            width,
            height,
            left_x: margin,
            body_y,
            body_height,
            aperture_width,
        }
    }

    fn right_x(&self) -> f32 {
        self.left_x + self.aperture_width + 18.0
    }

    fn operator_origin(&self) -> (i32, i32) {
        (self.left_x as i32 + 12, self.body_y as i32 + 36)
    }

    fn cell_at(&self, x: i32, y: i32) -> Option<(usize, usize)> {
        let (content_x, content_y) = self.operator_origin();
        let max_x = self.left_x as i32 + self.aperture_width as i32 - 12;
        let max_y = self.body_y as i32 + self.body_height as i32 - 12;
        if x < content_x || x >= max_x || y < content_y || y >= max_y {
            return None;
        }
        Some((
            ((x - content_x) / CELL_WIDTH) as usize,
            ((y - content_y) / CELL_HEIGHT) as usize,
        ))
    }

    fn terminal_size(&self) -> (usize, usize) {
        (
            (self.width / CELL_WIDTH).max(1) as usize,
            (self.height / CELL_HEIGHT).max(1) as usize,
        )
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

pub fn run(
    mut core: NativeTerminalCore,
    mut pty: tty::Pty,
    output_rx: Receiver<Vec<u8>>,
    bridge_rx: Option<LatestProjection>,
    mut projection: Projection,
) -> Result<(), String> {
    let display = unsafe { XOpenDisplay(ptr::null()) };
    if display.is_null() {
        return Err("could not open an X11 display; use --headless for CI".to_owned());
    }
    let result = run_window(
        display,
        &mut core,
        &mut pty,
        output_rx,
        bridge_rx,
        &mut projection,
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
) -> Result<(), String> {
    let screen = unsafe { XDefaultScreen(display) };
    let attributes = [
        GLX_RGBA,
        GLX_DOUBLEBUFFER,
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
            | EXPOSURE_MASK,
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
            1000,
            700,
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
    unsafe { XSetWMProtocols(display, window, &delete_atom as *const Atom as *mut Atom, 1) };
    unsafe { XMapWindow(display, window) };

    let context = unsafe { glXCreateContext(display, visual, ptr::null_mut(), 1) };
    if context.is_null() {
        unsafe { XDestroyWindow(display, window) };
        return Err("could not create the Athena OpenGL context".to_owned());
    }
    unsafe { glXMakeCurrent(display, window, context) };
    let gc = unsafe { XCreateGC(display, window, 0, ptr::null_mut()) };
    if gc.is_null() {
        unsafe {
            glXDestroyContext(display, context);
            XDestroyWindow(display, window);
        }
        return Err("could not create the native text drawing context".to_owned());
    }
    let mut writer = pty
        .file()
        .try_clone()
        .map_err(|error| format!("could not clone PTY writer: {error}"))?;
    let mut clipboard = Clipboard::new(display);
    let mut selection: Option<((usize, usize), (usize, usize))> = None;
    let mut width = 1000_i32;
    let mut height = 700_i32;
    let mut running = true;
    let mut child_exited = false;
    let mut dirty = true;
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
        if apply_available(core, &output_rx, bridge_rx.as_ref(), projection) {
            dirty = true;
            activity_dirty = true;
        }
        while unsafe { XPending(display) } > 0 {
            let mut event = XEvent {
                type_: 0,
                pad: [0; 24],
            };
            unsafe { XNextEvent(display, &mut event) };
            if let Some(bytes) = clipboard.handle_event(display, window, &mut event) {
                let _ = writer.write_all(&bytes);
                let _ = writer.flush();
                continue;
            }
            match event.type_ {
                KEY_PRESS => {
                    let key_event = unsafe { &mut *(&mut event as *mut XEvent as *mut XKeyEvent) };
                    let mut buffer = [0_i8; 32];
                    let mut keysym = 0 as c_ulong;
                    let mut compose = XComposeStatus {
                        compose_ptr: ptr::null_mut(),
                        chars_matched: 0,
                    };
                    let count = unsafe {
                        XLookupString(
                            key_event,
                            buffer.as_mut_ptr(),
                            buffer.len() as c_int,
                            &mut keysym,
                            &mut compose,
                        )
                    };
                    if key_event.state & (CONTROL_MASK | SHIFT_MASK) == (CONTROL_MASK | SHIFT_MASK)
                        && keysym == 'c' as c_ulong
                    {
                        if let Some((anchor, extent)) = selection {
                            let (start, end) = selection_bounds(anchor, extent);
                            let end = (end.0.saturating_add(1), end.1);
                            clipboard.own(display, window, core.selection_text(start, end));
                        }
                    } else if key_event.state & (CONTROL_MASK | SHIFT_MASK)
                        == (CONTROL_MASK | SHIFT_MASK)
                        && keysym == 'v' as c_ulong
                    {
                        clipboard.request(display, window);
                    } else if keysym == 0xff1b {
                        running = false;
                    } else if count > 0 {
                        let bytes = unsafe {
                            std::slice::from_raw_parts(buffer.as_ptr() as *const u8, count as usize)
                        };
                        let _ = writer.write_all(bytes);
                        let _ = writer.flush();
                        dirty = true;
                        activity_dirty = true;
                    }
                }
                BUTTON_PRESS => {
                    let button = unsafe { &*((&event as *const XEvent).cast::<XButtonEvent>()) };
                    if button.button == 1 {
                        if let Some(cell) =
                            FrameGeometry::new(width, height).cell_at(button.x, button.y)
                        {
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
                            FrameGeometry::new(width, height).cell_at(motion.x, motion.y),
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
                            FrameGeometry::new(width, height).cell_at(button.x, button.y),
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
                    resize_terminal(core, pty, width, height);
                    dirty = true;
                    activity_dirty = true;
                }
                EXPOSE => {
                    dirty = true;
                    activity_dirty = true;
                }
                DESTROY_NOTIFY | CLIENT_MESSAGE => running = false,
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
        let draw_ready = last_draw
            .map(|last| now.duration_since(last) >= ACTIVE_FRAME_INTERVAL)
            .unwrap_or(true);
        if dirty && draw_ready {
            draw_frame(
                display, window, gc, width, height, core, projection, selection,
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
            activity_dirty = false;
        }
        if !dirty && !child_exited && steady_started.is_none() {
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

    unsafe {
        XFreeGC(display, gc);
        glXMakeCurrent(display, 0, ptr::null_mut());
        glXDestroyContext(display, context);
        XDestroyWindow(display, window);
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

fn resize_terminal(core: &mut NativeTerminalCore, pty: &mut tty::Pty, width: i32, height: i32) {
    let (columns, rows) = FrameGeometry::new(width, height).terminal_size();
    core.resize(columns, rows);
    pty.on_resize(WindowSize {
        num_cols: columns.min(u16::MAX as usize) as u16,
        num_lines: rows.min(u16::MAX as usize) as u16,
        cell_width: CELL_WIDTH as u16,
        cell_height: CELL_HEIGHT as u16,
    });
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
    window: Window,
    gc: *mut c_void,
    width: i32,
    height: i32,
    core: &NativeTerminalCore,
    projection: &Projection,
    selection: Option<((usize, usize), (usize, usize))>,
) {
    let geometry = FrameGeometry::new(width, height);
    // ProjectionView.mode is the canonical view selector. semantic_state is
    // retained as a compatibility fallback for older bridge frames, but the
    // native renderer must not silently maintain a second mode machine.
    let semantic_mode = if projection.view.mode.is_empty() {
        projection.semantic_state.as_str()
    } else {
        projection.view.mode.as_str()
    };
    unsafe {
        glViewport(0, 0, width, height);
        glMatrixMode(GL_PROJECTION);
        glLoadIdentity();
        glOrtho(0.0, width as f64, height as f64, 0.0, -1.0, 1.0);
        glMatrixMode(GL_MODELVIEW);
        glLoadIdentity();
        glClearColor(0.025, 0.03, 0.07, 1.0);
        glClear(GL_COLOR_BUFFER_BIT);
    }
    let margin = geometry.left_x;
    let left_x = geometry.left_x;
    let right_x = geometry.right_x();
    let body_y = geometry.body_y;
    let body_h = geometry.body_height;
    let aperture_w = geometry.aperture_width;
    let rail = 52.0_f32;
    draw_rect(0.0, 0.0, width as f32, height as f32, (0.025, 0.03, 0.07));
    draw_rect(
        margin,
        8.0,
        width as f32 - margin * 2.0,
        30.0,
        (0.07, 0.10, 0.18),
    );
    draw_rect(left_x, body_y, aperture_w, body_h, (0.045, 0.055, 0.10));
    draw_rect(right_x, body_y, aperture_w, body_h, (0.035, 0.07, 0.12));
    draw_rect(
        margin,
        body_y + body_h + 12.0,
        width as f32 - margin * 2.0,
        rail,
        (0.06, 0.07, 0.13),
    );
    draw_rect(
        right_x + 8.0,
        body_y + 8.0,
        aperture_w - 16.0,
        body_h - 16.0,
        (0.025, 0.055, 0.10),
    );
    draw_selection(left_x + 12.0, body_y + 36.0, selection);
    let oi_inner = (
        right_x + 16.0,
        body_y + 34.0,
        (aperture_w - 32.0).max(40.0),
        (body_h - 50.0).max(40.0),
    );
    draw_oi_scene(oi_inner.0, oi_inner.1, oi_inner.2, oi_inner.3, projection);
    unsafe { glXSwapBuffers(display, window) };

    unsafe { XSetForeground(display, gc, rgb(180, 198, 226)) };
    draw_text(display, window, gc, 28, 31, &projection.title);
    unsafe { XSetForeground(display, gc, rgb(130, 160, 198)) };
    draw_text(
        display,
        window,
        gc,
        left_x as c_int + 12,
        body_y as c_int + 22,
        "OPERATOR WELL",
    );
    draw_text(
        display,
        window,
        gc,
        right_x as c_int + 12,
        body_y as c_int + 22,
        &projection.status,
    );
    if let Some(operation) = projection.active_operation.as_ref() {
        unsafe { XSetForeground(display, gc, rgb(101, 183, 206)) };
        let progress = operation_progress(operation);
        draw_text(
            display,
            window,
            gc,
            right_x as c_int + 12,
            body_y as c_int + 40,
            &format!(
                "{} {} ({}) {} [{}] {}{}",
                operation.action_kind.to_ascii_uppercase(),
                operation.label,
                operation.id,
                operation.target,
                operation.state,
                operation.mutation_state,
                progress,
            ),
        );
    }
    unsafe { XSetForeground(display, gc, rgb(193, 214, 239)) };
    let line_height = 18_i32;
    let max_lines = ((body_h as i32 - 42) / line_height).max(1) as usize;
    for (row, line) in core.snapshot().into_iter().take(max_lines).enumerate() {
        draw_text(
            display,
            window,
            gc,
            left_x as c_int + 12,
            body_y as c_int + 44 + row as c_int * line_height,
            line.trim_end(),
        );
    }
    let mut workspace_tree_entities = Vec::new();
    if !projection.workspace_tree.is_empty() {
        flatten_tree(
            &projection.workspace_tree,
            None,
            &mut workspace_tree_entities,
        );
    }
    let workspace_source = if workspace_tree_entities.is_empty() {
        &projection.workspace_entities
    } else {
        &workspace_tree_entities
    };
    if !workspace_source.is_empty() {
        unsafe { XSetForeground(display, gc, rgb(145, 181, 216)) };
        let files = workspace_source
            .iter()
            .take(3)
            .map(projection_entity_label)
            .collect::<Vec<_>>()
            .join(", ");
        draw_text(
            display,
            window,
            gc,
            right_x as c_int + 16,
            body_y as c_int + 58,
            &format!("files: {files}"),
        );
    }
    unsafe { XSetForeground(display, gc, rgb(183, 210, 236)) };
    if semantic_mode.eq_ignore_ascii_case("code") {
        if let Some(code) = projection.code_view.as_ref() {
            draw_code_view(
                display,
                window,
                gc,
                right_x as c_int + 16,
                body_y as c_int + 44,
                max_lines,
                code,
            );
        }
    } else if semantic_mode.eq_ignore_ascii_case("failure") && !projection.diagnostics.is_empty() {
        for (row, diagnostic) in projection.diagnostics.iter().take(max_lines).enumerate() {
            unsafe { XSetForeground(display, gc, rgb(224, 119, 126)) };
            let message = if diagnostic.message.is_empty() {
                &diagnostic.detail
            } else {
                &diagnostic.message
            };
            draw_text(
                display,
                window,
                gc,
                right_x as c_int + 16,
                body_y as c_int + 44 + row as c_int * line_height,
                &diagnostic_text(diagnostic, message),
            );
        }
    } else if semantic_mode.eq_ignore_ascii_case("verify") {
        unsafe { XSetForeground(display, gc, rgb(222, 176, 108)) };
        draw_text(
            display,
            window,
            gc,
            right_x as c_int + 16,
            body_y as c_int + 44,
            &format!(
                "verification: {} ({} checks)",
                projection.verification.status,
                projection.verification.checks.len(),
            ),
        );
        for (row, check) in projection
            .verification
            .checks
            .iter()
            .take(max_lines.saturating_sub(1))
            .enumerate()
        {
            unsafe { XSetForeground(display, gc, rgb(177, 196, 225)) };
            draw_text(
                display,
                window,
                gc,
                right_x as c_int + 16,
                body_y as c_int + 62 + row as c_int * line_height,
                &check.to_string(),
            );
        }
    } else if semantic_mode.eq_ignore_ascii_case("test") {
        unsafe { XSetForeground(display, gc, rgb(101, 183, 206)) };
        draw_text(
            display,
            window,
            gc,
            right_x as c_int + 16,
            body_y as c_int + 44,
            &format!(
                "test progress {}",
                projection
                    .progress
                    .as_ref()
                    .map_or("active".to_owned(), ToString::to_string),
            ),
        );
        for (row, line) in projection
            .oi
            .iter()
            .take(max_lines.saturating_sub(1))
            .enumerate()
        {
            draw_text(
                display,
                window,
                gc,
                right_x as c_int + 16,
                body_y as c_int + 62 + row as c_int * line_height,
                line,
            );
        }
    } else if semantic_mode.eq_ignore_ascii_case("search") {
        unsafe { XSetForeground(display, gc, rgb(101, 183, 206)) };
        draw_text(
            display,
            window,
            gc,
            right_x as c_int + 16,
            body_y as c_int + 44,
            "SEARCHING // SYMBOL GRAPH",
        );
        for (row, entity) in projection
            .entities
            .iter()
            .take(max_lines.saturating_sub(1))
            .enumerate()
        {
            draw_text(
                display,
                window,
                gc,
                right_x as c_int + 16,
                body_y as c_int + 62 + row as c_int * line_height,
                &format!("· {}", projection_entity_label(entity)),
            );
        }
    } else if semantic_mode.eq_ignore_ascii_case("approval") {
        unsafe { XSetForeground(display, gc, rgb(222, 176, 108)) };
        draw_text(
            display,
            window,
            gc,
            right_x as c_int + 16,
            body_y as c_int + 44,
            "APPROVAL // OPERATION SCOPE",
        );
        draw_text(
            display,
            window,
            gc,
            right_x as c_int + 16,
            body_y as c_int + 62,
            "? choose a permitted scope",
        );
        if let Some(progress) = projection.progress.as_ref() {
            draw_text(
                display,
                window,
                gc,
                right_x as c_int + 16,
                body_y as c_int + 80,
                &progress.to_string(),
            );
        }
    } else if semantic_mode.eq_ignore_ascii_case("recover") {
        unsafe { XSetForeground(display, gc, rgb(222, 176, 108)) };
        draw_text(
            display,
            window,
            gc,
            right_x as c_int + 16,
            body_y as c_int + 44,
            "RECOVERING // RETAINED EVIDENCE",
        );
        draw_text(
            display,
            window,
            gc,
            right_x as c_int + 16,
            body_y as c_int + 62,
            &projection.status,
        );
    } else if semantic_mode.eq_ignore_ascii_case("generate") {
        unsafe { XSetForeground(display, gc, rgb(101, 183, 206)) };
        draw_text(
            display,
            window,
            gc,
            right_x as c_int + 16,
            body_y as c_int + 44,
            "GENERATING // CAPABILITY",
        );
        if let Some(operation) = projection.active_operation.as_ref() {
            draw_text(
                display,
                window,
                gc,
                right_x as c_int + 16,
                body_y as c_int + 62,
                &format!("{} [{}]", operation.label, operation.state),
            );
        }
    } else {
        for (row, line) in projection.oi.iter().take(max_lines).enumerate() {
            draw_text(
                display,
                window,
                gc,
                right_x as c_int + 16,
                body_y as c_int + 44 + row as c_int * line_height,
                line,
            );
        }
    }
    unsafe { XSetForeground(display, gc, rgb(145, 181, 216)) };
    for (index, entity) in projection.entities.iter().take(8).enumerate() {
        let y = body_y as c_int + 60 + index as c_int * 30;
        let label = projection_entity_label(entity);
        draw_text(display, window, gc, right_x as c_int + 28, y, &label);
    }
    if let Some(alert) = projection.alerts.first() {
        unsafe { XSetForeground(display, gc, rgb(224, 158, 118)) };
        draw_text(
            display,
            window,
            gc,
            right_x as c_int + 16,
            (body_y + body_h - 18.0) as c_int,
            alert,
        );
    }
    unsafe { XFlush(display) };
}

fn draw_selection(x: f32, y: f32, selection: Option<((usize, usize), (usize, usize))>) {
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
            x + first as f32 * 9.0,
            y + row as f32 * 18.0,
            (last.saturating_sub(first)) as f32 * 9.0,
            18.0,
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

fn draw_oi_scene(x: f32, y: f32, width: f32, height: f32, projection: &Projection) {
    // The native scene is deliberately sparse: a faint CRT grid, runtime
    // links/nodes, and one Buddy. Python supplies semantic entities; these
    // primitives are presentation only and cannot invent task progress.
    unsafe {
        glColor3f(0.18, 0.34, 0.50);
        glLineWidth(1.0);
        glBegin(GL_LINES);
        for index in 1..7 {
            let gy = y + height * index as f32 / 7.0;
            glVertex2f(x, gy);
            glVertex2f(x + width, gy);
        }
        for index in 1..8 {
            let gx = x + width * index as f32 / 8.0;
            glVertex2f(gx, y);
            glVertex2f(gx, y + height);
        }
        glEnd();
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
            )
        })
        .take(6)
        .collect();
    let mut positions: Vec<(String, f32, f32)> = Vec::with_capacity(runtime.len());
    for (index, entity) in runtime.iter().enumerate() {
        let column = index % 2;
        let row = index / 2;
        let nx = x + width * (0.30 + column as f32 * 0.40);
        let ny = y + height * (0.22 + row as f32 * 0.28);
        positions.push((entity.id.clone(), nx, ny));
    }
    unsafe {
        glColor3f(0.29, 0.63, 0.72);
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
            (0.35, 0.72, 0.82)
        };
        draw_node(*nx, *ny, 18.0, color);
    }
    let semantic_mode = if projection.view.mode.is_empty() {
        projection.semantic_state.as_str()
    } else {
        projection.view.mode.as_str()
    };
    let action = if let Some(buddy) = projection.buddy.as_ref() {
        if !buddy.state.is_empty() {
            buddy.state.as_str()
        } else if buddy.anchor.is_empty() {
            semantic_mode
        } else {
            buddy.anchor.as_str()
        }
    } else if projection.semantic_state.is_empty() {
        projection.status.as_str()
    } else {
        semantic_mode
    };
    let (buddy_x, buddy_y) = match action.to_ascii_uppercase().as_str() {
        "READ" | "INSPECT" | "SEARCH" | "READING" | "SEARCHING" => {
            (x + width * 0.18, y + height * 0.28)
        }
        "CODE" | "TEST" | "VERIFY" | "EXECUTE" | "EXECUTING" | "TOOLS" => {
            (x + width * 0.76, y + height * 0.26)
        }
        "FAILURE" | "BLOCKED" => (x + width * 0.78, y + height * 0.76),
        "APPROVAL" => (x + width * 0.72, y + height * 0.84),
        _ => (x + width * 0.50, y + height * 0.58),
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
        .unwrap_or("owl");
    draw_buddy(buddy_x, buddy_y, buddy_status, buddy_character);
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

fn draw_code_view(
    display: *mut Display,
    window: Window,
    gc: *mut c_void,
    x: c_int,
    y: c_int,
    max_lines: usize,
    code: &ProjectionCodeView,
) {
    unsafe { XSetForeground(display, gc, rgb(101, 183, 206)) };
    draw_text(
        display,
        window,
        gc,
        x,
        y,
        &format!(
            "{}  {}  {}",
            code.path,
            code.language.to_ascii_uppercase(),
            code.mutation_state.to_ascii_uppercase()
        ),
    );
    let fallback_lines: Vec<String> = code.text.lines().map(str::to_owned).collect();
    let lines = if code.diff.is_empty() && code.lines.is_empty() {
        &fallback_lines
    } else if code.diff.is_empty() {
        &code.lines
    } else {
        &code.diff
    };
    for (row, line) in lines.iter().take(max_lines.saturating_sub(2)).enumerate() {
        let color = if line.starts_with('+') {
            rgb(101, 183, 206)
        } else if line.starts_with('-') {
            rgb(224, 119, 126)
        } else if line.starts_with('@') {
            rgb(222, 176, 108)
        } else {
            rgb(177, 196, 225)
        };
        unsafe { XSetForeground(display, gc, color) };
        draw_text(display, window, gc, x, y + 18 + row as c_int * 18, line);
    }
    if code.preview_truncated {
        unsafe { XSetForeground(display, gc, rgb(222, 176, 108)) };
        draw_text(
            display,
            window,
            gc,
            x,
            y + max_lines as c_int * 18,
            "... preview bounded for display",
        );
    }
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

fn draw_buddy(x: f32, y: f32, status: &str, character: &str) {
    let color = match status.to_ascii_uppercase().as_str() {
        "FAILURE" | "BLOCKED" => (0.86, 0.36, 0.40),
        "APPROVAL" => (0.88, 0.67, 0.36),
        _ => (0.46, 0.78, 0.88),
    };
    draw_rect(x - 22.0, y - 14.0, 44.0, 28.0, (0.035, 0.10, 0.18));
    unsafe {
        glColor3f(color.0, color.1, color.2);
        glLineWidth(2.0);
        glBegin(GL_LINE_LOOP);
        glVertex2f(x - 22.0, y - 14.0);
        glVertex2f(x + 22.0, y - 14.0);
        glVertex2f(x + 22.0, y + 14.0);
        glVertex2f(x - 22.0, y + 14.0);
        glEnd();
        glBegin(GL_LINES);
        glVertex2f(x - 9.0, y - 2.0);
        glVertex2f(x - 3.0, y - 2.0);
        glVertex2f(x + 3.0, y - 2.0);
        glVertex2f(x + 9.0, y - 2.0);
        glVertex2f(x, y - 14.0);
        glVertex2f(x + 8.0, y - 28.0);
        glEnd();
        match character.to_ascii_lowercase().as_str() {
            "cat" => {
                glBegin(GL_LINES);
                glVertex2f(x - 18.0, y - 14.0);
                glVertex2f(x - 10.0, y - 24.0);
                glVertex2f(x + 18.0, y - 14.0);
                glVertex2f(x + 10.0, y - 24.0);
                glEnd();
            }
            "owl" => {
                glBegin(GL_LINES);
                glVertex2f(x - 18.0, y - 14.0);
                glVertex2f(x - 10.0, y - 22.0);
                glVertex2f(x + 18.0, y - 14.0);
                glVertex2f(x + 10.0, y - 22.0);
                glEnd();
            }
            _ => {}
        }
    }
}

fn draw_text(
    display: *mut Display,
    window: Window,
    gc: *mut c_void,
    x: c_int,
    y: c_int,
    text: &str,
) {
    let sanitized = text.replace('\0', "");
    let Ok(value) = CString::new(sanitized) else {
        return;
    };
    unsafe {
        XDrawString(
            display,
            window,
            gc,
            x,
            y,
            value.as_ptr(),
            value.as_bytes().len().min(c_int::MAX as usize) as c_int,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::{CELL_HEIGHT, CELL_WIDTH, FrameGeometry};

    #[test]
    fn frame_geometry_keeps_apertures_equal() {
        let geometry = FrameGeometry::new(1000, 700);
        let left_end = geometry.left_x + geometry.aperture_width;
        let right_start = left_end + 18.0;

        assert_eq!(
            left_end - geometry.left_x,
            right_start + geometry.aperture_width - right_start
        );
        assert_eq!(geometry.aperture_width, 473.0);
    }

    #[test]
    fn frame_geometry_maps_operator_cells_within_bounds() {
        let geometry = FrameGeometry::new(1000, 700);
        let (origin_x, origin_y) = geometry.operator_origin();

        assert_eq!(geometry.cell_at(origin_x, origin_y), Some((0, 0)));
        assert_eq!(
            geometry.cell_at(origin_x + CELL_WIDTH, origin_y + CELL_HEIGHT),
            Some((1, 1))
        );
        assert_eq!(geometry.cell_at(origin_x - 1, origin_y), None);
        assert_eq!(geometry.cell_at(origin_x, origin_y - 1), None);
    }

    #[test]
    fn frame_geometry_preserves_minimum_resize() {
        assert_eq!(FrameGeometry::new(1, 1).terminal_size(), (1, 1));
        assert_eq!(FrameGeometry::new(1000, 700).terminal_size(), (111, 38));
    }
}

fn rgb(red: u8, green: u8, blue: u8) -> c_ulong {
    ((red as c_ulong) << 16) | ((green as c_ulong) << 8) | blue as c_ulong
}
