use crate::platform::*;
use crate::x11::PixelRect;
use athena_terminal::CellMetrics;
use std::cell::RefCell;
use std::ffi::CString;
use std::ptr;

use super::color_cache::XftColorCache;
use super::fonts::FontCatalog;
pub(crate) use super::fonts::FontRole;
use super::text_cache::TextWidthCache;

pub(crate) struct TextRenderer {
    display: *mut Display,
    draw: *mut XftDraw,
    fonts: FontCatalog,
    colors: XftColorCache,
    widths: RefCell<TextWidthCache>,
}

impl TextRenderer {
    pub(crate) fn new(
        display: *mut Display,
        screen: c_int,
        window: Window,
        visual: *mut c_void,
        colormap: Colormap,
        scale: f32,
    ) -> Result<Self, String> {
        let draw = unsafe { XftDrawCreate(display, window, visual, colormap) };
        if draw.is_null() {
            return Err("could not create the native Xft text surface".to_owned());
        }
        let fonts = match FontCatalog::new(display, screen, scale) {
            Ok(fonts) => fonts,
            Err(error) => {
                unsafe { XftDrawDestroy(draw) };
                return Err(error);
            }
        };
        Ok(Self {
            display,
            draw,
            fonts,
            colors: XftColorCache::new(display, visual, colormap),
            widths: RefCell::new(TextWidthCache::new()),
        })
    }

    /// Reopen Xft faces only when quantized pixel sizes change. The returned
    /// boolean tells the window loop whether the PTY/layout metrics changed.
    pub(crate) fn reconfigure_for_scale(&mut self, scale: f32) -> Result<bool, String> {
        let changed = self.fonts.reconfigure_for_scale(scale)?;
        if changed {
            self.widths.borrow_mut().clear();
        }
        Ok(changed)
    }

    pub(crate) fn metrics(&self) -> CellMetrics {
        self.fonts.metrics_for(FontRole::Body)
    }

    pub(crate) fn metrics_for(&self, role: FontRole) -> CellMetrics {
        self.fonts.metrics_for(role)
    }

    pub(crate) fn font_pixel_sizes(&self) -> [i32; 4] {
        self.fonts.pixel_sizes()
    }

    pub(crate) fn text_width_in(&self, role: FontRole, text: &str) -> i32 {
        let sanitized = text.replace('\0', "");
        if let Some(width) = self.widths.borrow().get(role, &sanitized) {
            return width;
        }
        let Ok(value) = CString::new(sanitized.as_str()) else {
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
                self.fonts.font(role),
                value.as_ptr().cast(),
                value.as_bytes().len().min(c_int::MAX as usize) as c_int,
                &mut extents,
            );
        }
        let width = i32::from(extents.x_off.max(0));
        self.widths
            .borrow_mut()
            .insert_if_bounded(role, sanitized, width);
        width
    }

    pub(crate) fn draw(&self, x: c_int, y: c_int, text: &str, color: (u8, u8, u8)) {
        self.draw_in(FontRole::Body, x, y, text, color);
    }

    pub(crate) fn draw_in(
        &self,
        role: FontRole,
        x: c_int,
        y: c_int,
        text: &str,
        color: (u8, u8, u8),
    ) {
        let sanitized = text.replace('\0', "");
        if sanitized.is_empty() {
            return;
        }
        let Ok(value) = CString::new(sanitized) else {
            return;
        };
        if let Some((color, cached)) = self.colors.get_or_allocate(color) {
            unsafe {
                XftDrawStringUtf8(
                    self.draw,
                    &color,
                    self.fonts.font(role),
                    x,
                    y,
                    value.as_ptr().cast(),
                    value.as_bytes().len().min(c_int::MAX as usize) as c_int,
                );
                if !cached {
                    let mut color = color;
                    self.colors.free_uncached(&mut color);
                }
            }
        }
    }

    /// Clip Xft glyphs to a physical-pixel rectangle.
    ///
    /// OpenGL scissoring cannot constrain Xft drawing: the glyphs are emitted
    /// through an XRender picture owned by `XftDraw`. Every full-resolution
    /// text surface therefore needs this independent clip when it is drawn
    /// inside a recessed instrument or terminal aperture.
    pub(crate) fn with_clip(&self, rect: PixelRect, draw: impl FnOnce()) {
        let mut clip = XRectangle {
            x: rect.x.round().clamp(i16::MIN as f32, i16::MAX as f32) as c_short,
            y: rect.y.round().clamp(i16::MIN as f32, i16::MAX as f32) as c_short,
            width: rect.width.round().clamp(0.0, u16::MAX as f32) as c_ushort,
            height: rect.height.round().clamp(0.0, u16::MAX as f32) as c_ushort,
        };
        unsafe {
            XftDrawSetClipRectangles(self.draw, 0, 0, &mut clip, 1);
        }
        draw();
        unsafe {
            XftDrawSetClipRectangles(self.draw, 0, 0, ptr::null_mut(), 0);
        }
    }
}

impl Drop for TextRenderer {
    fn drop(&mut self) {
        unsafe { XftDrawDestroy(self.draw) };
    }
}
