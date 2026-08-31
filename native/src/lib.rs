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
#[derive(Clone, Copy, Debug, PartialEq, serde::Serialize)]
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

/// The native compositor uses separate faces for terminal cells, editable
/// input, headings, and small instrument labels. Keeping all four metrics in
/// one value makes layout, clipping, and PTY sizing agree with the fonts that
/// will actually be drawn.
#[derive(Clone, Copy, Debug, PartialEq, serde::Serialize)]
pub struct UiFontMetrics {
    pub body: CellMetrics,
    pub input: CellMetrics,
    pub heading: CellMetrics,
    pub instrument: CellMetrics,
}

impl UiFontMetrics {
    pub fn fallback() -> Self {
        Self {
            body: CellMetrics::fallback(),
            input: CellMetrics::new(9.0, 20.0, 15.0, 5.0),
            heading: CellMetrics::new(7.0, 15.0, 12.0, 3.0),
            instrument: CellMetrics::new(6.0, 12.0, 9.0, 3.0),
        }
    }
}

/// A physical-pixel rectangle in the native Athena compositor.
#[derive(Clone, Copy, Debug, Default, PartialEq, serde::Serialize)]
pub struct PixelRect {
    pub x: f32,
    pub y: f32,
    pub width: f32,
    pub height: f32,
}

/// One metric-derived text row inside an instrument panel.
#[derive(Clone, Copy, Debug, Default, PartialEq, serde::Serialize)]
pub struct TextRow {
    pub top: f32,
    pub baseline: f32,
    pub height: f32,
}

impl TextRow {
    pub fn new(top: f32, metrics: CellMetrics) -> Self {
        Self {
            top,
            baseline: top + metrics.baseline,
            height: metrics.height,
        }
    }

    pub fn bottom(self) -> f32 {
        self.top + self.height
    }
}

/// The prompt is a three-row instrument, not a pile of offsets.
#[derive(Clone, Copy, Debug, Default, PartialEq, serde::Serialize)]
pub struct PromptLayout {
    pub rect: PixelRect,
    pub status_row: Option<TextRow>,
    pub input_row: TextRow,
    pub hint_row: Option<TextRow>,
}

/// Physical regions in the lower AthenaBOX instrument rail.
///
/// Keeping these regions typed prevents a control label, activity meter, or
/// prompt row from silently borrowing space from a neighboring device.
#[derive(Clone, Copy, Debug, Default, PartialEq, serde::Serialize)]
pub struct RailLayout {
    pub rail: PixelRect,
    pub speaker: PixelRect,
    pub operator_panel: PixelRect,
    pub operator_status: PixelRect,
    pub operator_input: PixelRect,
    pub operator_hint: Option<PixelRect>,
    pub system_status: PixelRect,
    pub primary_encoder: PixelRect,
    pub brightness: PixelRect,
    pub focus: PixelRect,
    pub power: PixelRect,
    pub identity_plate: PixelRect,
}

impl PromptLayout {
    pub fn required_height(
        input: CellMetrics,
        status: CellMetrics,
        hint: CellMetrics,
        padding: f32,
        gap: f32,
        bottom_padding: f32,
        with_hint: bool,
    ) -> f32 {
        let rows = status.height + gap + input.height;
        let rows = if with_hint {
            rows + gap + hint.height
        } else {
            rows
        };
        padding + rows + bottom_padding
    }

    pub fn from_rect(
        rect: PixelRect,
        input: CellMetrics,
        status: CellMetrics,
        padding: f32,
        gap: f32,
        bottom_padding: f32,
        with_hint: bool,
    ) -> Self {
        let safe_padding = padding.min((rect.height * 0.5).max(0.0));
        let available = (rect.height - safe_padding - bottom_padding.max(0.0)).max(0.0);
        let status_and_input = status.height + gap + input.height;
        let full = status_and_input + gap + status.height <= available;
        let status_fits = status_and_input <= available;
        let input_top = rect.y + safe_padding;
        let input_height = input.height.min((available).max(0.5));
        let (status_row, input_row, hint_row) = if full && with_hint {
            let status_row = TextRow::new(input_top, status);
            let input_row = TextRow::new(status_row.bottom() + gap, input);
            let hint_row = TextRow::new(input_row.bottom() + gap, status);
            (Some(status_row), input_row, Some(hint_row))
        } else if status_fits {
            let status_row = TextRow::new(input_top, status);
            let input_row = TextRow::new(status_row.bottom() + gap, input);
            (Some(status_row), input_row, None)
        } else {
            (
                None,
                TextRow {
                    top: input_top,
                    baseline: input_top + input.baseline.min(input_height),
                    height: input_height,
                },
                None,
            )
        };
        Self {
            rect,
            status_row,
            input_row,
            hint_row,
        }
    }

    pub fn content_bottom(self) -> f32 {
        self.hint_row
            .map_or(self.input_row.bottom(), TextRow::bottom)
    }

    pub fn rows_fit(self, bottom_padding: f32) -> bool {
        self.content_bottom() + bottom_padding <= self.rect.bottom() + 0.01
    }
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
#[derive(Clone, Copy, Debug, PartialEq, serde::Serialize)]
pub struct NativePixelLayout {
    pub width: i32,
    pub height: i32,
    pub compact: bool,
    pub left_x: f32,
    pub cell_width: f32,
    pub cell_height: f32,
    pub prompt_cell_width: f32,
    pub prompt_padding_x: f32,
    pub prompt_padding_y: f32,
    pub prompt_gap: f32,
    pub prompt_bottom_padding: f32,
    pub scale: f32,
    pub canvas: PixelRect,
    pub chassis: PixelRect,
    pub header: PixelRect,
    pub operator_outer: PixelRect,
    pub oi_outer: PixelRect,
    pub operator_inner: PixelRect,
    pub operator_viewport: PixelRect,
    pub oi_inner: PixelRect,
    pub controls: PixelRect,
    pub prompt: PixelRect,
    pub rail: RailLayout,
}

impl NativePixelLayout {
    pub fn new(width: i32, height: i32) -> Self {
        Self::for_window(width, height, UiFontMetrics::fallback())
    }

    pub fn for_window(width: i32, height: i32, metrics: UiFontMetrics) -> Self {
        let width = width.max(1);
        let height = height.max(1);
        let width_f = width as f32;
        let height_f = height as f32;
        const DESIGN_WIDTH: f32 = 1672.0;
        const DESIGN_HEIGHT: f32 = 941.0;
        let scale = (width_f / DESIGN_WIDTH).min(height_f / DESIGN_HEIGHT);
        let canvas = PixelRect {
            x: (width_f - DESIGN_WIDTH * scale) * 0.5,
            y: (height_f - DESIGN_HEIGHT * scale) * 0.5,
            width: DESIGN_WIDTH * scale,
            height: DESIGN_HEIGHT * scale,
        };
        let compact = scale < 0.66 || width < 900 || height < 620;
        let map = |x: f32, y: f32, width: f32, height: f32| PixelRect {
            x: canvas.x + x * scale,
            y: canvas.y + y * scale,
            width: width * scale,
            height: height * scale,
        };
        // Canonical AthenaBox design space. The lower deck is deliberately a
        // substantial part of the object, so a larger monitor grows the whole
        // device instead of leaving a tiny fixed-height control strip.
        let header = map(88.0, 42.0, 1496.0, 80.0);
        let operator_outer = map(88.0, 158.0, 728.0, 522.0);
        let oi_outer = map(856.0, 158.0, 728.0, 522.0);
        let operator_inner = map(128.0, 210.0, 648.0, 430.0);
        let operator_viewport = map(144.0, 246.0, 616.0, 378.0);
        let oi_inner = map(896.0, 210.0, 648.0, 430.0);
        let controls = map(88.0, 704.0, 1496.0, 186.0);
        let left_x = operator_outer.x;
        let input_metrics = if compact {
            metrics.instrument
        } else {
            metrics.input
        };
        let prompt_padding_y = if compact { 4.0 } else { 8.0 };
        let prompt_gap = if compact { 2.0 } else { 4.0 };
        let prompt_bottom_padding = if compact { 4.0 } else { 8.0 };
        let prompt_required = PromptLayout::required_height(
            input_metrics,
            metrics.instrument,
            metrics.instrument,
            prompt_padding_y,
            prompt_gap,
            prompt_bottom_padding,
            !compact,
        );
        let prompt_rect = map(334.0, 726.0, 584.0, 142.0);
        let prompt_height = if compact {
            prompt_rect.height.min(controls.height)
        } else {
            prompt_rect.height.max(prompt_required).min(controls.height)
        };
        let prompt = PixelRect {
            y: controls.y + (controls.height - prompt_height).max(0.0) * 0.5,
            height: prompt_height,
            ..prompt_rect
        };
        let prompt_layout = PromptLayout::from_rect(
            prompt,
            input_metrics,
            metrics.instrument,
            prompt_padding_y,
            prompt_gap,
            prompt_bottom_padding,
            !compact,
        );
        let row_rect = |row: Option<TextRow>| {
            row.map(|row| PixelRect {
                x: prompt.x,
                y: row.top,
                width: prompt.width,
                height: row.height,
            })
            .unwrap_or_default()
        };
        let rail = RailLayout {
            rail: controls,
            speaker: map(106.0, 716.0, 188.0, 162.0),
            operator_panel: map(314.0, 716.0, 620.0, 162.0),
            operator_status: PixelRect {
                x: prompt.x,
                y: prompt_layout.status_row.map_or(prompt.y, |row| row.top),
                width: prompt.width,
                height: prompt_layout.status_row.map_or(0.0, |row| row.height),
            },
            operator_input: row_rect(Some(prompt_layout.input_row)),
            operator_hint: prompt_layout.hint_row.map(|row| PixelRect {
                x: prompt.x,
                y: row.top,
                width: prompt.width,
                height: row.height,
            }),
            system_status: map(950.0, 716.0, 164.0, 162.0),
            primary_encoder: map(1130.0, 716.0, 170.0, 162.0),
            brightness: map(1312.0, 716.0, 82.0, 162.0),
            focus: map(1408.0, 716.0, 82.0, 162.0),
            power: map(1504.0, 716.0, 66.0, 162.0),
            identity_plate: map(1138.0, 850.0, 154.0, 20.0),
        };
        let layout = Self {
            width,
            height,
            compact,
            left_x,
            cell_width: metrics.body.width.max(1.0),
            cell_height: metrics.body.height.max(1.0),
            prompt_cell_width: input_metrics.width.max(1.0),
            prompt_padding_x: if compact { 12.0 * scale } else { 20.0 * scale },
            prompt_padding_y,
            prompt_gap,
            prompt_bottom_padding,
            scale,
            canvas,
            chassis: canvas,
            header,
            operator_outer,
            oi_outer,
            operator_inner,
            operator_viewport,
            oi_inner,
            controls,
            prompt,
            rail,
        };
        for rect in [
            layout.header,
            layout.operator_outer,
            layout.oi_outer,
            layout.operator_inner,
            layout.operator_viewport,
            layout.oi_inner,
            layout.controls,
            layout.prompt,
            layout.rail.rail,
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
            (self.operator_viewport.width / self.cell_width)
                .floor()
                .max(1.0) as usize,
            (self.operator_viewport.height / self.cell_height)
                .floor()
                .max(1.0) as usize,
        )
    }

    pub fn operator_origin(&self) -> (i32, i32) {
        (
            self.operator_viewport.x.ceil() as i32,
            self.operator_viewport.y.ceil() as i32,
        )
    }

    pub fn cell_at(&self, x: i32, y: i32) -> Option<(usize, usize)> {
        let x_f = x as f32;
        let y_f = y as f32;
        const HIT_TOLERANCE: f32 = 0.01;
        if x_f + HIT_TOLERANCE < self.operator_viewport.x
            || x_f >= self.operator_viewport.right()
            || y_f + HIT_TOLERANCE < self.operator_viewport.y
            || y_f >= self.operator_viewport.bottom()
        {
            return None;
        }
        Some((
            ((x_f - self.operator_viewport.x).max(0.0) / self.cell_width) as usize,
            ((y_f - self.operator_viewport.y).max(0.0) / self.cell_height) as usize,
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::{
        CellMetrics, NativePixelLayout, NativeTerminalCore, PixelRect, PromptLayout, TerminalSize,
        UiFontMetrics,
    };
    use alacritty_terminal::term::TermMode;

    #[test]
    fn equal_surface_geometry_is_content_independent() {
        let layout = NativePixelLayout::for_window(1280, 800, UiFontMetrics::fallback());
        assert_eq!(layout.operator_outer.width, layout.oi_outer.width);
        assert_eq!(layout.operator_inner.width, layout.oi_inner.width);
        assert_eq!(layout.operator_inner.height, layout.oi_inner.height);
        assert_eq!(layout.rail.rail, layout.controls);
        assert!(layout.rail.speaker.right() <= layout.rail.rail.right());
        assert!(layout.rail.operator_status.bottom() <= layout.prompt.bottom());
        assert!(layout.rail.operator_input.bottom() <= layout.prompt.bottom());
        assert!(layout.rail.system_status.right() <= layout.rail.primary_encoder.x);
        assert!(layout.rail.primary_encoder.right() <= layout.rail.brightness.x);
        assert!(layout.rail.brightness.right() <= layout.rail.focus.x);
        assert!(layout.rail.focus.right() <= layout.rail.power.x);
        assert!(layout.rail.power.right() <= layout.rail.rail.right());
    }

    #[test]
    fn narrow_terminal_keeps_both_apertures_symmetric() {
        let layout = NativePixelLayout::for_window(320, 240, UiFontMetrics::fallback());
        assert_eq!(layout.operator_outer.width, layout.oi_outer.width);
        assert!(layout.oi_outer.right() <= 320.0);
    }

    #[test]
    fn layout_fits_supported_small_windows() {
        for (width, height) in [(1, 1), (80, 40), (320, 240), (640, 480), (1280, 800)] {
            let layout = NativePixelLayout::for_window(width, height, UiFontMetrics::fallback());
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
    fn prompt_rows_are_metric_derived_and_non_overlapping() {
        let body = CellMetrics::fallback();
        let micro = CellMetrics::new(6.0, 12.0, 9.0, 3.0);
        let prompt = PromptLayout::from_rect(
            PixelRect {
                x: 0.0,
                y: 0.0,
                width: 400.0,
                height: 72.0,
            },
            body,
            micro,
            6.0,
            4.0,
            6.0,
            true,
        );
        assert!(prompt.status_row.unwrap().bottom() + 4.0 <= prompt.input_row.top);
        assert!(prompt.input_row.bottom() + 4.0 <= prompt.hint_row.unwrap().top);
        assert!(prompt.rows_fit(6.0));

        let compact = PromptLayout::from_rect(
            PixelRect {
                x: 0.0,
                y: 0.0,
                width: 200.0,
                height: 28.0,
            },
            CellMetrics::new(6.0, 10.0, 8.0, 2.0),
            CellMetrics::new(6.0, 10.0, 8.0, 2.0),
            4.0,
            2.0,
            0.0,
            false,
        );
        assert!(compact.rows_fit(0.0));
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
