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
    draw: *mut XftDraw,
    fonts: [FontFace; 4],
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
    ) -> Result<Self, String> {
        let draw = unsafe { XftDrawCreate(display, window, visual, colormap) };
        if draw.is_null() {
            return Err("could not create the native Xft text surface".to_owned());
        }
        let specs = [
            (FontRole::Body, "Fira Mono:size=13"),
            (FontRole::Input, "Fira Mono:size=15"),
            (FontRole::Heading, "Fira Mono:bold:size=11"),
            (FontRole::Instrument, "Fira Mono:size=9"),
        ];
        let mut faces: Vec<FontFace> = Vec::with_capacity(specs.len());
        for (_, name) in specs {
            let font_name = CString::new(name).expect("static font name");
            let mut font = unsafe { XftFontOpenName(display, screen, font_name.as_ptr()) };
            if font.is_null() {
                let fallback_name = CString::new("monospace:size=11").expect("static font name");
                font = unsafe { XftFontOpenName(display, screen, fallback_name.as_ptr()) };
            }
            if font.is_null() {
                for face in faces {
                    unsafe { XftFontClose(display, face.font) };
                }
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
            faces.push(FontFace { font, metrics });
        }
        Ok(Self {
            display,
            draw,
            fonts: [faces[0], faces[1], faces[2], faces[3]],
            visual,
            colormap,
            colors: RefCell::new(HashMap::new()),
            widths: RefCell::new(HashMap::new()),
        })
    }

    pub(crate) fn metrics(&self) -> CellMetrics {
        self.metrics_for(FontRole::Body)
    }

    pub(crate) fn metrics_for(&self, role: FontRole) -> CellMetrics {
        self.face(role).metrics
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
