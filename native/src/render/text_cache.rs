//! Bounded text-width cache used by the Xft renderer.
//!
//! Width lookup is presentation state, not font acquisition or glyph drawing.
//! Keeping its bounded lifetime separate makes scale reconfiguration and cache
//! policy explicit without widening the renderer's platform surface.

use std::collections::HashMap;

use crate::platform::MAX_CACHED_TEXT_WIDTHS;

use super::fonts::FontRole;

pub(crate) struct TextWidthCache {
    widths: HashMap<(FontRole, String), i32>,
}

impl TextWidthCache {
    pub(crate) fn new() -> Self {
        Self {
            widths: HashMap::new(),
        }
    }

    pub(crate) fn get(&self, role: FontRole, text: &str) -> Option<i32> {
        self.widths.get(&(role, text.to_owned())).copied()
    }

    pub(crate) fn insert_if_bounded(&mut self, role: FontRole, text: String, width: i32) {
        if self.widths.len() < MAX_CACHED_TEXT_WIDTHS {
            self.widths.insert((role, text), width);
        }
    }

    pub(crate) fn clear(&mut self) {
        self.widths.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::TextWidthCache;
    use crate::platform::MAX_CACHED_TEXT_WIDTHS;
    use crate::render::fonts::FontRole;

    #[test]
    fn width_cache_has_a_hard_entry_bound_and_clears_on_reconfigure() {
        let mut cache = TextWidthCache::new();
        for index in 0..MAX_CACHED_TEXT_WIDTHS {
            cache.insert_if_bounded(FontRole::Body, format!("entry-{index}"), index as i32);
        }
        cache.insert_if_bounded(FontRole::Body, "overflow".to_owned(), 999);
        assert_eq!(cache.get(FontRole::Body, "entry-0"), Some(0));
        assert_eq!(cache.get(FontRole::Body, "overflow"), None);

        cache.clear();
        assert_eq!(cache.get(FontRole::Body, "entry-0"), None);
    }
}
