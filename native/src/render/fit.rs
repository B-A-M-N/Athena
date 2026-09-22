//! Pure text-fitting and color conversion helpers used by render surfaces.

use crate::render::text::{FontRole, TextRenderer};

pub(crate) fn fit_text_in(
    text: &TextRenderer,
    role: FontRole,
    value: &str,
    available: i32,
) -> String {
    if text.text_width_in(role, value) <= available {
        return value.to_owned();
    }
    let mut result = String::new();
    for character in value.chars() {
        let candidate = format!("{result}{character}…");
        if text.text_width_in(role, &candidate) > available {
            break;
        }
        result.push(character);
    }
    if result.is_empty() {
        "…".to_owned()
    } else {
        format!("{result}…")
    }
}

pub(crate) fn fit_input_in(
    text: &TextRenderer,
    role: FontRole,
    value: &str,
    cursor: usize,
    available: i32,
) -> (String, usize) {
    if text.text_width_in(role, value) <= available {
        return (value.to_owned(), value[..cursor].chars().count());
    }
    let characters: Vec<char> = value.chars().collect();
    let cursor_chars = value[..cursor].chars().count().min(characters.len());
    let mut start = 0;
    let mut end = characters.len();
    loop {
        let prefix = if start > 0 { "…" } else { "" };
        let suffix = if end < characters.len() { "…" } else { "" };
        let middle: String = characters[start..end].iter().collect();
        let candidate = format!("{prefix}{middle}{suffix}");
        if text.text_width_in(role, &candidate) <= available {
            let display_cursor = usize::from(start > 0) + cursor_chars.saturating_sub(start);
            return (candidate, display_cursor);
        }
        if start < cursor_chars
            && (end == characters.len() || cursor_chars - start >= end - cursor_chars)
        {
            start += 1;
        } else if end > cursor_chars {
            end -= 1;
        } else if start < end {
            start += 1;
        } else {
            return ("…".to_owned(), 0);
        }
    }
}

pub(crate) fn rgb_f32(color: (u8, u8, u8)) -> (f32, f32, f32) {
    (
        color.0 as f32 / 255.0,
        color.1 as f32 / 255.0,
        color.2 as f32 / 255.0,
    )
}

pub(crate) fn selection_bounds(
    anchor: (usize, usize),
    extent: (usize, usize),
) -> ((usize, usize), (usize, usize)) {
    if (anchor.1, anchor.0) <= (extent.1, extent.0) {
        (anchor, extent)
    } else {
        (extent, anchor)
    }
}
