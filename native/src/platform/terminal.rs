//! Terminal resize mechanics owned by the platform boundary.

use alacritty_terminal::event::{OnResize, WindowSize};
use alacritty_terminal::tty;
use athena_terminal::{NativePixelLayout, NativeTerminalCore, UiFontMetrics};

pub(crate) fn resize_terminal(
    core: &mut NativeTerminalCore,
    pty: &mut tty::Pty,
    width: i32,
    height: i32,
    metrics: UiFontMetrics,
) {
    let layout = NativePixelLayout::for_window(width, height, metrics);
    let terminal_size = layout.terminal_size();
    let columns = terminal_size.columns;
    let rows = terminal_size.rows;
    core.resize(columns, rows);
    pty.on_resize(WindowSize {
        num_cols: columns.min(u16::MAX as usize) as u16,
        num_lines: rows.min(u16::MAX as usize) as u16,
        cell_width: metrics.body.width.round().max(1.0) as u16,
        cell_height: metrics.body.height.round().max(1.0) as u16,
    });
}
