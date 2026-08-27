//! Native Athena terminal boundary.
//!
//! This crate is intentionally a small, compilable seam around the upstream
//! Alacritty terminal engine. It is not a second agent runtime: Python owns
//! the canonical service/event contracts while this frontend will eventually
//! consume their serialized projection.

use alacritty_terminal::event::VoidListener;
use alacritty_terminal::grid::Dimensions;
use alacritty_terminal::index::{Column, Line};
use alacritty_terminal::term::Config as TerminalConfig;
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

impl NativeTerminalCore {
    pub fn new(columns: usize, rows: usize) -> Self {
        let size = TerminalSize::new(columns, rows);
        let term = Term::new(TerminalConfig::default(), &size, VoidListener);
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
        for row in start_row..=end_row {
            let chars: Vec<char> = rows[row].chars().collect();
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
}

/// A terminal-cell rectangle in the Athena compositor coordinate system.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct Rect {
    pub x: u32,
    pub y: u32,
    pub width: u32,
    pub height: u32,
}

impl Rect {
    pub fn bottom(self) -> u32 {
        self.y.saturating_add(self.height)
    }
}

/// Geometry shared by the native compositor and the hosted/ANSI projections.
///
/// The operator and OI apertures intentionally have independent fields but
/// are validated as an equal-surface pair. Scene entities, Buddy, approvals,
/// and animation are content and cannot mutate this geometry.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AthenaLayout {
    pub columns: u32,
    pub rows: u32,
    pub chassis: Rect,
    pub header: Rect,
    pub operator: Rect,
    pub oi: Rect,
    pub controls: Rect,
    pub prompt: Rect,
}

impl AthenaLayout {
    /// Compute the native chassis geometry using the hosted frontend's
    /// equal-aperture rules. Content, animation, and renderer choice cannot
    /// change either surface's rectangle.
    pub fn for_terminal(columns: u32, rows: u32) -> Self {
        let columns = columns.max(1);
        let rows = rows.max(1);
        let margin = if columns >= 100 { 2 } else { 1 };
        let gap = 3;
        let header_height = 3;
        let rail_height = 4;
        let aperture_height = rows.saturating_sub(header_height + rail_height).max(1);
        let available = columns.saturating_sub(margin * 2 + gap).max(2);
        let aperture_width = (available / 2).max(1);
        let left_x = margin;
        let right_x = left_x + aperture_width + gap;
        let chassis = Rect {
            x: 0,
            y: 0,
            width: columns,
            height: rows,
        };
        let header = Rect {
            x: margin,
            y: 0,
            width: columns.saturating_sub(margin * 2),
            height: header_height,
        };
        let operator = Rect {
            x: left_x,
            y: header_height,
            width: aperture_width,
            height: aperture_height,
        };
        let oi = Rect {
            x: right_x,
            y: header_height,
            width: aperture_width,
            height: aperture_height,
        };
        let controls = Rect {
            x: margin,
            y: header_height + aperture_height,
            width: columns.saturating_sub(margin * 2),
            height: 2,
        };
        let prompt = Rect {
            x: margin,
            y: controls.bottom(),
            width: columns.saturating_sub(margin * 2),
            height: rows.saturating_sub(controls.bottom()).max(1),
        };
        Self {
            columns,
            rows,
            chassis,
            header,
            operator,
            oi,
            controls,
            prompt,
        }
    }

    pub fn apertures_equal(&self) -> bool {
        self.operator.width == self.oi.width && self.operator.height == self.oi.height
    }
}

/// Read-only compositor input. The native frontend renders this projection;
/// it does not approve capabilities, execute commands, or mutate state.
pub trait AthenaProjection {
    fn layout(&self) -> AthenaLayout;
    fn oi_scene_bytes(&self) -> &[u8];
}

/// The native frontend's ownership boundary.
///
/// The eventual implementation will embed Alacritty's PTY/parser/grid and
/// add Athena's GPU compositor around it. Keeping this trait here prevents a
/// future UI implementation from silently becoming another service loop.
pub trait AthenaCompositor {
    type Error;

    fn present<P: AthenaProjection>(&mut self, projection: &P) -> Result<(), Self::Error>;
    fn restore_terminal(&mut self) -> Result<(), Self::Error>;
}

#[cfg(test)]
mod tests {
    use super::AthenaLayout;

    #[test]
    fn equal_surface_geometry_is_content_independent() {
        let layout = AthenaLayout::for_terminal(160, 45);
        assert!(layout.apertures_equal());
        assert_eq!(layout.operator.width, 76);
        assert_eq!(layout.operator.height, 38);
        assert_eq!(layout.oi.x, 81);
    }

    #[test]
    fn narrow_terminal_keeps_both_apertures_symmetric() {
        let layout = AthenaLayout::for_terminal(90, 20);
        assert!(layout.apertures_equal());
        assert_eq!(layout.operator.width, 42);
        assert_eq!(layout.operator.height, 13);
    }

    #[test]
    fn pinned_alacritty_core_parses_and_resizes_terminal_content() {
        let mut terminal = super::NativeTerminalCore::new(12, 3);
        terminal.feed(b"ATHENA\r\nOI");

        assert!(terminal.snapshot()[0].starts_with("ATHENA"));
        assert!(terminal.snapshot()[1].starts_with("OI"));

        terminal.resize(8, 2);
        assert_eq!(terminal.size(), super::TerminalSize::new(8, 2));
        assert_eq!(terminal.snapshot().len(), 2);
        assert_eq!(terminal.snapshot()[0].len(), 8);
    }

    #[test]
    fn grid_selection_is_normalized_and_clipped() {
        let mut terminal = super::NativeTerminalCore::new(8, 3);
        terminal.feed(b"one\r\ntwo\r\nthree");

        assert_eq!(terminal.selection_text((5, 2), (1, 0)), "ne\ntwo\nthree");
    }
}
