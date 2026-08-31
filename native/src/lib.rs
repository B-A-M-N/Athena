//! Native Athena terminal boundary.
//!
//! This crate is intentionally a small, compilable seam around the upstream
//! Alacritty terminal engine. It is not a second agent runtime: Python owns
//! the canonical service/event contracts while this frontend will eventually
//! consume their serialized projection.

use alacritty_terminal::event::VoidListener;
use alacritty_terminal::grid::Dimensions;
use alacritty_terminal::grid::Scroll;
use alacritty_terminal::index::{Column, Line};
use alacritty_terminal::term::RenderableContent;
use alacritty_terminal::term::{Config as TerminalConfig, TermMode};
use alacritty_terminal::{Term, vte};

/// Dimensions passed to the upstream terminal core.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TerminalSize {
    pub columns: usize,
    pub rows: usize,
}

impl TerminalSize {
    pub fn new(columns: usize, rows: usize) -> Self {
        Self {
            columns: columns.max(1),
            rows: rows.max(1),
        }
    }
}

impl Dimensions for TerminalSize {
    fn total_lines(&self) -> usize {
        self.rows
    }

    fn screen_lines(&self) -> usize {
        self.rows
    }

    fn columns(&self) -> usize {
        self.columns
    }
}

/// Small, directly testable wrapper around the pinned Alacritty terminal
/// engine. Windowing, PTY ownership, and Athena composition remain outside
/// this core so the native frontend can evolve without forking semantics.
pub struct NativeTerminalCore {
    term: Term<VoidListener>,
    parser: vte::ansi::Processor,
    size: TerminalSize,
}

const MAX_SCROLLBACK_LINES: usize = 4_000;

impl NativeTerminalCore {
    pub fn new(columns: usize, rows: usize) -> Self {
        let size = TerminalSize::new(columns, rows);
        let config = TerminalConfig {
            scrolling_history: MAX_SCROLLBACK_LINES,
            ..TerminalConfig::default()
        };
        let term = Term::new(config, &size, VoidListener);
        Self {
            term,
            parser: vte::ansi::Processor::new(),
            size,
        }
    }

    /// Feed terminal bytes through Alacritty's ANSI parser and grid.
    pub fn feed(&mut self, bytes: &[u8]) {
        self.parser.advance(&mut self.term, bytes);
    }

    /// Resize the upstream grid without touching Athena's outer apertures.
    pub fn resize(&mut self, columns: usize, rows: usize) {
        self.size = TerminalSize::new(columns, rows);
        self.term.resize(self.size);
    }

    pub fn size(&self) -> TerminalSize {
        self.size
    }

    /// Return a plain snapshot for the future glyph renderer and tests.
    pub fn snapshot(&self) -> Vec<String> {
        (0..self.size.rows)
            .map(|row| {
                (0..self.size.columns)
                    .map(|column| self.term.grid()[Line(row as i32)][Column(column)].c)
                    .collect()
            })
            .collect()
    }

    /// Extract a normalized inclusive/exclusive cell selection for the
    /// native clipboard bridge. Selection is a terminal concern: it reads
    /// the Alacritty grid and never changes Athena projection state.
    pub fn selection_text(&self, anchor: (usize, usize), extent: (usize, usize)) -> String {
        let rows = self.snapshot();
        if rows.is_empty() {
            return String::new();
        }
        // Cells are stored as (column, row), so tuple ordering is not the
        // visual reading order. Normalize by row first, then column.
        let (start, end) = if (anchor.1, anchor.0) <= (extent.1, extent.0) {
            (anchor, extent)
        } else {
            (extent, anchor)
        };
        let start_row = start.1.min(rows.len() - 1);
        let end_row = end.1.min(rows.len() - 1);
        let mut selected = Vec::new();
        for (row, row_text) in rows
            .iter()
            .enumerate()
            .skip(start_row)
            .take(end_row - start_row + 1)
        {
            let chars: Vec<char> = row_text.chars().collect();
            let first = if row == start_row {
                start.0.min(chars.len())
            } else {
                0
            };
            let last = if row == end_row {
                end.0.min(chars.len())
            } else {
                chars.len()
            };
            selected.push(
                chars[first..last]
                    .iter()
                    .collect::<String>()
                    .trim_end()
                    .to_owned(),
            );
        }
        selected.join("\n").trim_end().to_owned()
    }

    pub fn term(&self) -> &Term<VoidListener> {
        &self.term
    }

    /// Return the terminal application's current input modes. The native
    /// prompt owns editing in normal mode, while alternate-screen programs
    /// receive protocol-correct key and paste sequences.
    pub fn mode(&self) -> TermMode {
        *self.term.mode()
    }

    /// Scroll the visible grid without changing PTY or service state.
    pub fn scroll_display(&mut self, scroll: Scroll) {
        self.term.scroll_display(scroll);
    }

    /// Return Alacritty's renderer-facing view of the terminal.
    ///
    /// The native compositor must consume this instead of flattening the
    /// grid to strings: it carries cell attributes, cursor shape, selection,
    /// scroll offset, and terminal modes together.
    pub fn renderable_content(&self) -> RenderableContent<'_> {
        self.term.renderable_content()
    }
}

/// Font-derived metrics shared by layout, PTY sizing, input hit testing, and
/// glyph placement. The X11 frontend fills this from the selected Xft font.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CellMetrics {
    pub width: f32,
    pub height: f32,
    pub ascent: f32,
    pub descent: f32,
    pub baseline: f32,
}

impl CellMetrics {
    pub const fn fallback() -> Self {
        Self {
            width: 9.0,
            height: 18.0,
            ascent: 14.0,
            descent: 4.0,
            baseline: 14.0,
        }
    }

    pub fn new(width: f32, height: f32, ascent: f32, descent: f32) -> Self {
        let width = width.max(1.0);
        let ascent = ascent.max(1.0);
        let descent = descent.max(1.0);
        Self {
            width,
            height: height.max(ascent + descent),
            ascent,
            descent,
            baseline: ascent,
        }
    }
}

/// A physical-pixel rectangle in the native Athena compositor.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct PixelRect {
    pub x: f32,
    pub y: f32,
    pub width: f32,
    pub height: f32,
}

impl PixelRect {
    pub fn right(self) -> f32 {
        self.x + self.width
    }

    pub fn bottom(self) -> f32 {
        self.y + self.height
    }

    pub fn contains(self, x: i32, y: i32) -> bool {
        let x = x as f32;
        let y = y as f32;
        x >= self.x && x < self.right() && y >= self.y && y < self.bottom()
    }
}

/// The one authoritative native pixel layout.
///
/// Every native surface and PTY resize derives from this value. The layout
/// deliberately owns a compact mode instead of allowing minimum rectangles to
/// extend outside a small window.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct NativePixelLayout {
    pub width: i32,
    pub height: i32,
    pub compact: bool,
    pub left_x: f32,
    pub cell_width: f32,
    pub cell_height: f32,
    pub prompt_padding_x: f32,
    pub chassis: PixelRect,
    pub header: PixelRect,
    pub operator_outer: PixelRect,
    pub oi_outer: PixelRect,
    pub operator_inner: PixelRect,
    pub oi_inner: PixelRect,
    pub controls: PixelRect,
    pub prompt: PixelRect,
}

impl NativePixelLayout {
    pub fn new(width: i32, height: i32) -> Self {
        Self::for_window(width, height, CellMetrics::fallback())
    }

    pub fn for_window(width: i32, height: i32, metrics: CellMetrics) -> Self {
        let width = width.max(1);
        let height = height.max(1);
        let compact = width < 900 || height < 620;
        let width_f = width as f32;
        let height_f = height as f32;
        let margin_ratio = if compact { 0.022 } else { 0.028 };
        let margin_limit = (width_f / 3.0).max(0.0);
        let margin = (width_f * margin_ratio).min(margin_limit);
        let gap_ratio = if compact { 0.018 } else { 0.022 };
        let gap = (width_f * gap_ratio).min((width_f - margin * 2.0).max(0.0) / 3.0);
        let available_width = (width_f - margin * 2.0 - gap).max(0.0);
        let aperture_width = (available_width / 2.0).max(0.0);
        let left_x = margin;
        let right_x = left_x + aperture_width + gap;
        let header_y = (if compact { 16.0_f32 } else { 24.0_f32 }).min(height_f * 0.08);
        let header_height = (if compact { 44.0_f32 } else { 52.0_f32 }).min(height_f * 0.14);
        let body_y = (if compact { 88.0_f32 } else { 112.0_f32 }).min(height_f * 0.30);
        let rail_gap = (if compact { 10.0_f32 } else { 16.0_f32 }).min(height_f * 0.04);
        let controls_height = (if compact { 42.0_f32 } else { 88.0_f32 })
            .min(height_f * if compact { 0.12 } else { 0.15 });
        let bottom = (if compact { 12.0_f32 } else { 24.0_f32 }).min(height_f * 0.04);
        let body_height = (height_f - body_y - rail_gap - controls_height - bottom).max(0.0);
        let operator_outer = PixelRect {
            x: left_x,
            y: body_y,
            width: aperture_width,
            height: body_height,
        };
        let oi_outer = PixelRect {
            x: right_x,
            y: body_y,
            width: aperture_width,
            height: body_height,
        };
        // The reference console uses a deep bezel around both displays. Keep
        // the logical apertures equal, but leave enough metal around them to
        // make the two screens read as embedded hardware rather than cards.
        let desired_inset_x: f32 = if compact { 16.0 } else { 44.0 };
        let inset_x = desired_inset_x.min(aperture_width / 4.0);
        let desired_top: f32 = if compact { 30.0 } else { 48.0 };
        let desired_bottom: f32 = if compact { 30.0 } else { 46.0 };
        let inset_top = desired_top.min(body_height / 3.0);
        let inset_bottom = desired_bottom.min((body_height - inset_top).max(0.0) / 2.0);
        let inner_width = (aperture_width - inset_x * 2.0).max(0.0);
        let inner_height = (body_height - inset_top - inset_bottom).max(0.0);
        let operator_inner = PixelRect {
            x: operator_outer.x + inset_x,
            y: operator_outer.y + inset_top,
            width: inner_width,
            height: inner_height,
        };
        let oi_inner = PixelRect {
            x: oi_outer.x + inset_x,
            y: oi_outer.y + inset_top,
            width: inner_width,
            height: inner_height,
        };
        let rail_y = operator_outer.bottom() + rail_gap;
        let controls = PixelRect {
            x: margin,
            y: rail_y,
            width: (width_f - margin * 2.0).max(0.0),
            height: controls_height,
        };
        let prompt_height = (if compact { 28.0 } else { 62.0_f32 }).min(controls_height);
        let speaker_width = if compact {
            controls.width.min(72.0)
        } else {
            144.0
        };
        let right_control_width = if compact {
            controls.width.min(88.0)
        } else {
            360.0
        };
        let prompt_gap = if compact { 4.0 } else { 12.0 };
        let prompt_reserve = if compact { prompt_gap } else { 100.0 };
        let prompt_x = (controls.x + speaker_width + prompt_gap).min(controls.right());
        let prompt_right = (controls.right() - right_control_width - prompt_reserve).max(prompt_x);
        let prompt = PixelRect {
            x: prompt_x,
            y: controls.y + (controls.height - prompt_height) / 2.0,
            width: (prompt_right - prompt_x).max(0.0),
            height: prompt_height,
        };
        let layout = Self {
            width,
            height,
            compact,
            left_x,
            cell_width: metrics.width.max(1.0),
            cell_height: metrics.height.max(1.0),
            prompt_padding_x: if compact { 12.0 } else { 20.0 },
            chassis: PixelRect {
                x: 0.0,
                y: 0.0,
                width: width as f32,
                height: height as f32,
            },
            header: PixelRect {
                x: margin,
                y: header_y,
                width: (width_f - margin * 2.0).max(0.0),
                height: header_height,
            },
            operator_outer,
            oi_outer,
            operator_inner,
            oi_inner,
            controls,
            prompt,
        };
        for rect in [
            layout.header,
            layout.operator_outer,
            layout.oi_outer,
            layout.operator_inner,
            layout.oi_inner,
            layout.controls,
            layout.prompt,
        ] {
            debug_assert!(rect.x >= -0.01);
            debug_assert!(rect.y >= -0.01);
            debug_assert!(rect.right() <= width_f + 0.01);
            debug_assert!(rect.bottom() <= height_f + 0.01);
        }
        layout
    }

    pub fn terminal_size(&self) -> TerminalSize {
        TerminalSize::new(
            (self.operator_inner.width / self.cell_width)
                .floor()
                .max(1.0) as usize,
            (self.operator_inner.height / self.cell_height)
                .floor()
                .max(1.0) as usize,
        )
    }

    pub fn operator_origin(&self) -> (i32, i32) {
        (
            self.operator_inner.x.round() as i32,
            self.operator_inner.y.round() as i32,
        )
    }

    pub fn cell_at(&self, x: i32, y: i32) -> Option<(usize, usize)> {
        if !self.operator_inner.contains(x, y) {
            return None;
        }
        Some((
            ((x as f32 - self.operator_inner.x) / self.cell_width) as usize,
            ((y as f32 - self.operator_inner.y) / self.cell_height) as usize,
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::{CellMetrics, NativePixelLayout, NativeTerminalCore, TerminalSize};
    use alacritty_terminal::term::TermMode;

    #[test]
    fn equal_surface_geometry_is_content_independent() {
        let layout = NativePixelLayout::for_window(1280, 800, CellMetrics::fallback());
        assert_eq!(layout.operator_outer.width, layout.oi_outer.width);
        assert_eq!(layout.operator_inner.width, layout.oi_inner.width);
        assert_eq!(layout.operator_inner.height, layout.oi_inner.height);
    }

    #[test]
    fn narrow_terminal_keeps_both_apertures_symmetric() {
        let layout = NativePixelLayout::for_window(320, 240, CellMetrics::fallback());
        assert_eq!(layout.operator_outer.width, layout.oi_outer.width);
        assert!(layout.oi_outer.right() <= 320.0);
    }

    #[test]
    fn layout_fits_supported_small_windows() {
        for (width, height) in [(1, 1), (80, 40), (320, 240), (640, 480), (1280, 800)] {
            let layout = NativePixelLayout::for_window(width, height, CellMetrics::fallback());
            for rect in [
                layout.header,
                layout.operator_outer,
                layout.oi_outer,
                layout.operator_inner,
                layout.oi_inner,
                layout.controls,
                layout.prompt,
            ] {
                assert!(rect.x >= 0.0 && rect.y >= 0.0);
                assert!(rect.right() <= width as f32);
                assert!(rect.bottom() <= height as f32);
            }
        }
    }

    #[test]
    fn pinned_alacritty_core_parses_and_resizes_terminal_content() {
        let mut terminal = NativeTerminalCore::new(12, 3);
        terminal.feed(b"ATHENA\r\nOI");

        assert!(terminal.snapshot()[0].starts_with("ATHENA"));
        assert!(terminal.snapshot()[1].starts_with("OI"));

        terminal.resize(8, 2);
        assert_eq!(terminal.size(), TerminalSize::new(8, 2));
        assert_eq!(terminal.snapshot().len(), 2);
        assert_eq!(terminal.snapshot()[0].len(), 8);
    }

    #[test]
    fn grid_selection_is_normalized_and_clipped() {
        let mut terminal = super::NativeTerminalCore::new(8, 3);
        terminal.feed(b"one\r\ntwo\r\nthree");

        assert_eq!(terminal.selection_text((5, 2), (1, 0)), "ne\ntwo\nthree");
    }

    #[test]
    fn terminal_mode_parser_tracks_application_cursor_and_bracketed_paste() {
        let mut terminal = NativeTerminalCore::new(8, 3);
        terminal.feed(b"\x1b[?1h\x1b[?2004h");
        assert!(terminal.mode().contains(TermMode::APP_CURSOR));
        assert!(terminal.mode().contains(TermMode::BRACKETED_PASTE));

        terminal.feed(b"\x1b[?1l\x1b[?2004l");
        assert!(!terminal.mode().contains(TermMode::APP_CURSOR));
        assert!(!terminal.mode().contains(TermMode::BRACKETED_PASTE));
    }
}
