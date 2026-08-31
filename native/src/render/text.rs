use super::super::*;
use athena_terminal::CellMetrics;
use std::cell::RefCell;
use std::collections::HashMap;
use std::ffi::CString;
use std::ptr;

/// Roles keep physical chrome, terminal cells, and the editable prompt from
/// competing for one compromise font size.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum FontRole {
    Body,
    Input,
    Heading,
    Instrument,
}

#[derive(Clone, Copy)]
struct FontFace {
    font: *mut XftFont,
    metrics: CellMetrics,
}

pub(crate) struct TextRenderer {
    display: *mut Display,
    screen: c_int,
    draw: *mut XftDraw,
    fonts: [FontFace; 4],
    font_pixel_sizes: [i32; 4],
    visual: *mut c_void,
    colormap: Colormap,
    colors: RefCell<HashMap<(u8, u8, u8), XftColor>>,
    widths: RefCell<HashMap<(FontRole, String), i32>>,
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
        let (fonts, font_pixel_sizes) = match Self::open_fonts(display, screen, scale) {
            Ok(fonts) => fonts,
            Err(error) => {
                unsafe { XftDrawDestroy(draw) };
                return Err(error);
            }
        };
        Ok(Self {
            display,
            screen,
            draw,
            fonts,
            font_pixel_sizes,
            visual,
            colormap,
            colors: RefCell::new(HashMap::new()),
            widths: RefCell::new(HashMap::new()),
        })
    }

    fn open_fonts(
        display: *mut Display,
        screen: c_int,
        scale: f32,
    ) -> Result<([FontFace; 4], [i32; 4]), String> {
        let pixel_sizes = Self::pixel_sizes(scale);
        let roles = [
            (FontRole::Body, false),
            (FontRole::Input, false),
            (FontRole::Heading, true),
            (FontRole::Instrument, false),
        ];
        let mut faces: Vec<FontFace> = Vec::with_capacity(roles.len());
        for ((_, bold), pixel_size) in roles.into_iter().zip(pixel_sizes) {
            let family = if bold {
                format!("Fira Mono:style=Bold:pixelsize={pixel_size}")
            } else {
                format!("Fira Mono:pixelsize={pixel_size}")
            };
            let font_name = CString::new(family).expect("dynamic font name");
            let mut font = unsafe { XftFontOpenName(display, screen, font_name.as_ptr()) };
            if font.is_null() {
                let fallback = if bold {
                    format!("monospace:style=Bold:pixelsize={pixel_size}")
                } else {
                    format!("monospace:pixelsize={pixel_size}")
                };
                let fallback_name = CString::new(fallback).expect("dynamic fallback font name");
                font = unsafe { XftFontOpenName(display, screen, fallback_name.as_ptr()) };
            }
            if font.is_null() {
                for face in faces {
                    unsafe { XftFontClose(display, face.font) };
                }
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
            faces.push(FontFace { font, metrics });
        }
        Ok(([faces[0], faces[1], faces[2], faces[3]], pixel_sizes))
    }

    fn pixel_sizes(scale: f32) -> [i32; 4] {
        let scale = scale.max(0.1);
        [
            (14.0 * scale).round().max(10.0) as i32,
            (15.0 * scale).round().max(10.0) as i32,
            (12.0 * scale).round().max(9.0) as i32,
            (9.0 * scale).round().max(7.0) as i32,
        ]
    }

    /// Reopen Xft faces only when quantized pixel sizes change. The returned
    /// boolean tells the window loop whether the PTY/layout metrics changed.
    pub(crate) fn reconfigure_for_scale(&mut self, scale: f32) -> Result<bool, String> {
        let desired = Self::pixel_sizes(scale);
        if desired == self.font_pixel_sizes {
            return Ok(false);
        }
        let (fonts, font_pixel_sizes) = Self::open_fonts(self.display, self.screen, scale)?;
        unsafe {
            for face in self.fonts {
                XftFontClose(self.display, face.font);
            }
        }
        self.fonts = fonts;
        self.font_pixel_sizes = font_pixel_sizes;
        self.widths.borrow_mut().clear();
        Ok(true)
    }

    pub(crate) fn metrics(&self) -> CellMetrics {
        self.metrics_for(FontRole::Body)
    }

    pub(crate) fn metrics_for(&self, role: FontRole) -> CellMetrics {
        self.face(role).metrics
    }

    pub(crate) fn font_pixel_sizes(&self) -> [i32; 4] {
        self.font_pixel_sizes
    }

    fn face(&self, role: FontRole) -> FontFace {
        match role {
            FontRole::Body => self.fonts[0],
            FontRole::Input => self.fonts[1],
            FontRole::Heading => self.fonts[2],
            FontRole::Instrument => self.fonts[3],
        }
    }

    pub(crate) fn text_width_in(&self, role: FontRole, text: &str) -> i32 {
        let sanitized = text.replace('\0', "");
        let cache_key = (role, sanitized.clone());
        if let Some(width) = self.widths.borrow().get(&cache_key).copied() {
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
                self.face(role).font,
                value.as_ptr().cast(),
                value.as_bytes().len().min(c_int::MAX as usize) as c_int,
                &mut extents,
            );
        }
        let width = i32::from(extents.x_off.max(0));
        let mut widths = self.widths.borrow_mut();
        if widths.len() < MAX_CACHED_TEXT_WIDTHS {
            widths.insert(cache_key, width);
        }
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
        if let Some((color, cached)) = self.cached_color(color) {
            unsafe {
                XftDrawStringUtf8(
                    self.draw,
                    &color,
                    self.face(role).font,
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
            for face in self.fonts {
                XftFontClose(self.display, face.font);
            }
            XftDrawDestroy(self.draw);
        }
    }
}
