//! Small Linux/X11 compositor for the native vertical slice.
//!
//! The production renderer will replace the immediate-mode drawing here with
//! the upstream Alacritty glyph/GPU renderer plus Athena shader passes.  This
//! slice is intentionally dependency-light so it can be built and tested in a
//! minimal development environment while proving window, PTY, input, resize,
//! and projection ownership.

#![allow(clippy::too_many_arguments)]

use std::ffi::{CString, c_char, c_int, c_long, c_ulong, c_void};
use std::io::Write;
use std::ptr;
use std::sync::mpsc::Receiver;
use std::thread;
use std::time::Duration;

use alacritty_terminal::event::{OnResize, WindowSize};
use alacritty_terminal::tty::{self, ChildEvent, EventedPty};

use athena_terminal::NativeTerminalCore;

use crate::{Projection, ProjectionEntity, ProjectionFrame, apply_available};

type Display = c_void;
type Window = c_ulong;
type Atom = c_ulong;
type Colormap = c_ulong;
type GLXContext = *mut c_void;

const KEY_PRESS: c_int = 2;
const DESTROY_NOTIFY: c_int = 17;
const CONFIGURE_NOTIFY: c_int = 22;
const CLIENT_MESSAGE: c_int = 33;
const KEY_PRESS_MASK: c_long = 1;
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

pub fn run(
    mut core: NativeTerminalCore,
    mut pty: tty::Pty,
    output_rx: Receiver<Vec<u8>>,
    bridge_rx: Option<Receiver<ProjectionFrame>>,
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
    bridge_rx: Option<Receiver<ProjectionFrame>>,
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
        event_mask: KEY_PRESS_MASK | STRUCTURE_NOTIFY_MASK | EXPOSURE_MASK,
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
    let mut width = 1000_i32;
    let mut height = 700_i32;
    let mut running = true;
    let mut child_exited = false;
    while running {
        apply_available(core, &output_rx, bridge_rx.as_ref(), projection);
        while unsafe { XPending(display) } > 0 {
            let mut event = XEvent {
                type_: 0,
                pad: [0; 24],
            };
            unsafe { XNextEvent(display, &mut event) };
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
                    if keysym == 0xff1b {
                        running = false;
                    } else if count > 0 {
                        let bytes = unsafe {
                            std::slice::from_raw_parts(buffer.as_ptr() as *const u8, count as usize)
                        };
                        let _ = writer.write_all(bytes);
                        let _ = writer.flush();
                    }
                }
                CONFIGURE_NOTIFY => {
                    let configure =
                        unsafe { &*(&event as *const XEvent as *const XConfigureEvent) };
                    width = configure.width.max(1);
                    height = configure.height.max(1);
                    resize_terminal(core, pty, width, height);
                }
                DESTROY_NOTIFY | CLIENT_MESSAGE => running = false,
                _ => {}
            }
        }
        if matches!(pty.next_child_event(), Some(ChildEvent::Exited(_))) {
            child_exited = true;
        }
        draw_frame(display, window, gc, width, height, core, projection);
        if child_exited {
            // Keep one final frame visible long enough for a caller or bridge
            // to observe it, then restore/destroy the native surface.
            thread::sleep(Duration::from_millis(80));
            running = false;
        } else {
            thread::sleep(Duration::from_millis(16));
        }
    }

    unsafe {
        XFreeGC(display, gc);
        glXMakeCurrent(display, 0, ptr::null_mut());
        glXDestroyContext(display, context);
        XDestroyWindow(display, window);
    }
    Ok(())
}

fn resize_terminal(core: &mut NativeTerminalCore, pty: &mut tty::Pty, width: i32, height: i32) {
    let columns = (width / 9).max(1) as usize;
    let rows = (height / 18).max(1) as usize;
    core.resize(columns, rows);
    pty.on_resize(WindowSize {
        num_cols: columns.min(u16::MAX as usize) as u16,
        num_lines: rows.min(u16::MAX as usize) as u16,
        cell_width: 9,
        cell_height: 18,
    });
}

fn draw_frame(
    display: *mut Display,
    window: Window,
    gc: *mut c_void,
    width: i32,
    height: i32,
    core: &NativeTerminalCore,
    projection: &Projection,
) {
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
    let margin = 18.0_f32;
    let gap = 18.0_f32;
    let header = 48.0_f32;
    let rail = 52.0_f32;
    let body_y = header + 12.0;
    let body_h = (height as f32 - body_y - rail).max(80.0);
    let aperture_w = ((width as f32 - margin * 2.0 - gap) / 2.0).max(80.0);
    let left_x = margin;
    let right_x = left_x + aperture_w + gap;
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
    unsafe { XSetForeground(display, gc, rgb(183, 210, 236)) };
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
    unsafe { XSetForeground(display, gc, rgb(145, 181, 216)) };
    for (index, entity) in projection.entities.iter().take(8).enumerate() {
        let y = body_y as c_int + 60 + index as c_int * 30;
        let label = if entity.label.is_empty() {
            &entity.id
        } else {
            &entity.label
        };
        draw_text(display, window, gc, right_x as c_int + 28, y, label);
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

    let runtime: Vec<&ProjectionEntity> = projection
        .entities
        .iter()
        .filter(|entity| {
            matches!(
                entity.kind.as_str(),
                "operation" | "workflow" | "verification" | "child_task" | "generated_tool"
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

    let (buddy_x, buddy_y) = match projection.status.to_ascii_uppercase().as_str() {
        "READING" | "SEARCHING" => (x + width * 0.18, y + height * 0.28),
        "EXECUTING" | "TOOLS" => (x + width * 0.76, y + height * 0.26),
        "FAILURE" | "BLOCKED" => (x + width * 0.78, y + height * 0.76),
        "APPROVAL" => (x + width * 0.72, y + height * 0.84),
        _ => (x + width * 0.50, y + height * 0.58),
    };
    draw_buddy(buddy_x, buddy_y, projection.status.as_str());
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

fn draw_buddy(x: f32, y: f32, status: &str) {
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

fn rgb(red: u8, green: u8, blue: u8) -> c_ulong {
    ((red as c_ulong) << 16) | ((green as c_ulong) << 8) | blue as c_ulong
}
