//! X11 window, GLX context, and presentation-surface acquisition.
//!
//! The event loop consumes this setup record but does not own the platform
//! resource acquisition or the symmetric teardown paths.  Window semantics
//! remain in the event loop and window-manager modules.

use std::ffi::CString;
use std::ptr;

use super::window_manager::{WindowMoveStrategy, select_window_move_strategy};
use super::*;

pub(crate) struct WindowSetup {
    pub(crate) display: *mut Display,
    pub(crate) screen: c_int,
    pub(crate) window: Window,
    pub(crate) visual: *mut XVisualInfo,
    pub(crate) colormap: Colormap,
    pub(crate) protocols_atom: Atom,
    pub(crate) delete_atom: Atom,
    pub(crate) initial_width: i32,
    pub(crate) initial_height: i32,
    pub(crate) stencil_available: bool,
    pub(crate) window_move_strategy: WindowMoveStrategy,
    pub(crate) context: GLXContext,
    pub(crate) presentation_surface: crate::platform::PresentationSurface,
}

impl WindowSetup {
    pub(crate) fn create(display: *mut Display, projection: &Projection) -> Result<Self, String> {
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
            visual = unsafe {
                glXChooseVisual(display, screen, fallback_attributes.as_ptr() as *mut c_int)
            };
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

        let title = match CString::new(projection.title.as_str()) {
            Ok(title) => title,
            Err(_) => {
                unsafe { XDestroyWindow(display, window) };
                return Err("invalid window title".to_owned());
            }
        };
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

        let mut presentation_surface = match crate::platform::PresentationSurface::new(
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
        if unsafe { glXMakeCurrent(display, presentation_surface.glx_pixmap(), context) } == 0 {
            unsafe {
                glXDestroyContext(display, context);
                presentation_surface.destroy();
                XDestroyWindow(display, window);
            }
            return Err("could not make the native presentation surface current".to_owned());
        }

        Ok(Self {
            display,
            screen,
            window,
            visual,
            colormap,
            protocols_atom,
            delete_atom,
            initial_width,
            initial_height,
            stencil_available,
            window_move_strategy,
            context,
            presentation_surface,
        })
    }

    pub(crate) fn destroy(mut self, window_destroyed: bool) {
        unsafe {
            glXMakeCurrent(self.display, 0, ptr::null_mut());
            glXDestroyContext(self.display, self.context);
            if !window_destroyed {
                XDestroyWindow(self.display, self.window);
            }
        }
        self.presentation_surface.destroy();
    }
}
