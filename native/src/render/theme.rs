//! AthenaBOX presentation palette.
//!
//! Semantic state changes the accent, not the entire screen luminance. The
//! renderer modules consume these values instead of inventing neon variants.

pub(crate) const OPERATOR_BACKGROUND: (u8, u8, u8) = (10, 18, 28);
pub(crate) const GLASS_BACKGROUND: (f32, f32, f32) = (0.032, 0.072, 0.118);
pub(crate) const PRIMARY: (u8, u8, u8) = (220, 228, 232);
pub(crate) const SECONDARY: (u8, u8, u8) = (163, 181, 190);
pub(crate) const DIM: (u8, u8, u8) = (118, 136, 148);
pub(crate) const FAILURE: (u8, u8, u8) = (190, 105, 112);
pub(crate) const AMBER: (u8, u8, u8) = (190, 160, 104);
pub(crate) const SUCCESS: (u8, u8, u8) = (128, 177, 151);
pub(crate) const GRAPHITE: (f32, f32, f32) = (0.028, 0.030, 0.031);

pub(crate) fn mode_color(mode: &str) -> (u8, u8, u8) {
    if mode.eq_ignore_ascii_case("failure") || mode.eq_ignore_ascii_case("blocked") {
        FAILURE
    } else if mode.eq_ignore_ascii_case("approval") {
        AMBER
    } else if mode.eq_ignore_ascii_case("success") || mode.eq_ignore_ascii_case("verified") {
        SUCCESS
    } else {
        PRIMARY
    }
}

pub(crate) fn rgb(color: (u8, u8, u8)) -> (f32, f32, f32) {
    (
        color.0 as f32 / 255.0,
        color.1 as f32 / 255.0,
        color.2 as f32 / 255.0,
    )
}
