//! X11 offscreen presentation surface lifecycle.
//!
//! The compositor owns semantic rendering; this module owns the mechanical
//! pixmap/GLX drawable used to present a completed frame without exposing a
//! half-composed surface to the window.

use super::ffi::{
    CUint, Display, GC, GLXPixmap, Pixmap, Window, XCopyArea, XCreateGC, XCreatePixmap, XFreeGC,
    XFreePixmap, XVisualInfo, glFinish, glXCreateGLXPixmap, glXDestroyGLXPixmap, glXWaitGL,
};
use athena_terminal::PixelRect;
use std::ffi::c_int;
use std::ptr;

/// Compose the complete cabinet into one X11 pixmap before copying it to the
/// visible window. OpenGL and Xft both target this drawable, so the visible
/// surface never receives a half-GL/half-text frame while a redraw is in
/// flight.
pub(crate) struct PresentationSurface {
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
    pub(crate) fn glx_pixmap(&self) -> GLXPixmap {
        self.glx_pixmap
    }

    pub(crate) fn new(
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

    pub(crate) fn resize(&mut self, width: CUint, height: CUint) -> Result<(), String> {
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

    pub(crate) fn reap_retired(&mut self) {
        for (pixmap, glx_pixmap) in self.retired.drain(..) {
            unsafe {
                glXDestroyGLXPixmap(self.display, glx_pixmap);
                XFreePixmap(self.display, pixmap);
            }
        }
    }

    pub(crate) fn present_regions(&self, regions: &[PixelRect]) {
        unsafe {
            // Finish the GL command stream before issuing XCopyArea requests.
            // Each request is limited to the domain that changed. In
            // particular, an animated OI frame must never copy over the
            // window-owned Xft transcript on the opposite CRT.
            glFinish();
            glXWaitGL();
            for region in regions {
                let x = region.x.max(0.0).round() as c_int;
                let y = region.y.max(0.0).round() as c_int;
                let right = region.right().min(self.width as f32).round() as c_int;
                let bottom = region.bottom().min(self.height as f32).round() as c_int;
                let copy_width = right.saturating_sub(x) as CUint;
                let copy_height = bottom.saturating_sub(y) as CUint;
                if copy_width == 0 || copy_height == 0 {
                    continue;
                }
                XCopyArea(
                    self.display,
                    self.pixmap,
                    self.window,
                    self.gc,
                    x,
                    y,
                    copy_width,
                    copy_height,
                    x,
                    y,
                );
            }
        }
    }

    pub(crate) fn destroy(&mut self) {
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
