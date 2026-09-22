use crate::platform::*;
use athena_terminal::CellMetrics;
use std::ffi::CString;

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

/// Owns the Xft faces and their font-derived metrics.
pub(crate) struct FontCatalog {
    display: *mut Display,
    screen: c_int,
    faces: [FontFace; 4],
    pixel_sizes: [i32; 4],
}

impl FontCatalog {
    pub(crate) fn new(display: *mut Display, screen: c_int, scale: f32) -> Result<Self, String> {
        let (faces, pixel_sizes) = Self::open_faces(display, screen, scale)?;
        Ok(Self {
            display,
            screen,
            faces,
            pixel_sizes,
        })
    }

    fn open_faces(
        display: *mut Display,
        screen: c_int,
        scale: f32,
    ) -> Result<([FontFace; 4], [i32; 4]), String> {
        let pixel_sizes = Self::sizes_for_scale(scale);
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

    fn sizes_for_scale(scale: f32) -> [i32; 4] {
        let scale = scale.max(0.1);
        [
            (20.0 * scale).round().max(16.0) as i32,
            (20.0 * scale).round().max(16.0) as i32,
            (18.0 * scale).round().max(15.0) as i32,
            (13.0 * scale).round().max(12.0) as i32,
        ]
    }

    /// Reopen faces only when quantized pixel sizes change.
    pub(crate) fn reconfigure_for_scale(&mut self, scale: f32) -> Result<bool, String> {
        let desired = Self::sizes_for_scale(scale);
        if desired == self.pixel_sizes {
            return Ok(false);
        }
        let (faces, pixel_sizes) = Self::open_faces(self.display, self.screen, scale)?;
        unsafe {
            for face in self.faces {
                XftFontClose(self.display, face.font);
            }
        }
        self.faces = faces;
        self.pixel_sizes = pixel_sizes;
        Ok(true)
    }

    pub(crate) fn metrics_for(&self, role: FontRole) -> CellMetrics {
        self.face(role).metrics
    }

    pub(crate) fn pixel_sizes(&self) -> [i32; 4] {
        self.pixel_sizes
    }

    pub(crate) fn font(&self, role: FontRole) -> *mut XftFont {
        self.face(role).font
    }

    fn face(&self, role: FontRole) -> FontFace {
        match role {
            FontRole::Body => self.faces[0],
            FontRole::Input => self.faces[1],
            FontRole::Heading => self.faces[2],
            FontRole::Instrument => self.faces[3],
        }
    }
}

impl Drop for FontCatalog {
    fn drop(&mut self) {
        unsafe {
            for face in self.faces {
                XftFontClose(self.display, face.font);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::FontCatalog;

    #[test]
    fn native_text_sizes_keep_legibility_floor_when_cabinet_shrinks() {
        assert_eq!(FontCatalog::sizes_for_scale(0.7655), [16, 16, 15, 12]);
        assert_eq!(FontCatalog::sizes_for_scale(1.0), [20, 20, 18, 13]);
        assert_eq!(FontCatalog::sizes_for_scale(1.10), [22, 22, 20, 14]);
        assert_eq!(FontCatalog::sizes_for_scale(1.25), [25, 25, 23, 16]);
    }
}
