//! Bounded Xft color allocation owned by the text presentation surface.
//!
//! Color handles are Xft resources tied to the renderer's visual and
//! colormap. Keeping allocation and teardown together prevents the text
//! renderer from becoming the owner of an unrelated cache policy.

use std::cell::RefCell;
use std::collections::HashMap;

use crate::platform::*;

pub(crate) struct XftColorCache {
    display: *mut Display,
    visual: *mut c_void,
    colormap: Colormap,
    colors: RefCell<HashMap<(u8, u8, u8), XftColor>>,
}

impl XftColorCache {
    pub(crate) fn new(display: *mut Display, visual: *mut c_void, colormap: Colormap) -> Self {
        Self {
            display,
            visual,
            colormap,
            colors: RefCell::new(HashMap::new()),
        }
    }

    pub(crate) fn get_or_allocate(&self, color: (u8, u8, u8)) -> Option<(XftColor, bool)> {
        if let Some(cached) = self.colors.borrow().get(&color).copied() {
            return Some((cached, true));
        }
        let allocated = self.allocate(color)?;
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

    pub(crate) fn free_uncached(&self, color: &mut XftColor) {
        unsafe { XftColorFree(self.display, self.visual, self.colormap, color) };
    }

    fn allocate(&self, color: (u8, u8, u8)) -> Option<XftColor> {
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

impl Drop for XftColorCache {
    fn drop(&mut self) {
        unsafe {
            for color in self.colors.get_mut().values_mut() {
                XftColorFree(self.display, self.visual, self.colormap, color);
            }
        }
    }
}
