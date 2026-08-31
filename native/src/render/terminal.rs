use super::super::*;
use super::primitives::{draw_outline_rect, draw_rect};
use super::text::TextRenderer;
use alacritty_terminal::term::cell::Flags;
use alacritty_terminal::term::color::Colors;
use alacritty_terminal::vte::ansi::{Color as TermColor, NamedColor};

pub(crate) fn draw_terminal_background(
    core: &NativeTerminalCore,
    geometry: &FrameGeometry,
    selection: Option<((usize, usize), (usize, usize))>,
) {
    let content = core.renderable_content();
    let columns = core.size().columns.max(1);
    for (index, indexed) in content.display_iter.enumerate() {
        let cell = indexed.cell;
        let row = index / columns;
        let column = index % columns;
        let mut background = resolve_term_color(cell.bg, content.colors, false);
        if cell.flags.contains(Flags::INVERSE) {
            background = resolve_term_color(cell.fg, content.colors, true);
        }
        if background != (7, 12, 19) {
            draw_rect(
                geometry.operator_viewport.x + column as f32 * geometry.cell_width,
                geometry.operator_viewport.y + row as f32 * geometry.cell_height,
                geometry.cell_width,
                geometry.cell_height,
                rgb_f32(background),
            );
        }
    }
    draw_selection(
        geometry.operator_viewport.x,
        geometry.operator_viewport.y,
        selection,
        geometry.cell_width,
        geometry.cell_height,
    );
}

pub(crate) fn draw_terminal_text(
    text: &TextRenderer,
    core: &NativeTerminalCore,
    geometry: &FrameGeometry,
) {
    let content = core.renderable_content();
    let columns = core.size().columns.max(1);
    let mut run_text = String::new();
    let mut run_row = 0;
    let mut run_start_column = 0;
    let mut run_next_column = 0;
    let mut run_color = (0, 0, 0);
    let mut has_run = false;
    for (index, indexed) in content.display_iter.enumerate() {
        let cell = indexed.cell;
        if cell
            .flags
            .intersects(Flags::HIDDEN | Flags::WIDE_CHAR_SPACER)
            || cell.c == ' '
        {
            if has_run {
                draw_terminal_run(
                    text,
                    geometry,
                    run_row,
                    run_start_column,
                    &run_text,
                    run_color,
                );
                run_text.clear();
                has_run = false;
            }
            continue;
        }
        let row = index / columns;
        let column = index % columns;
        let mut foreground = resolve_term_color(cell.fg, content.colors, true);
        if cell.flags.contains(Flags::INVERSE) {
            foreground = resolve_term_color(cell.bg, content.colors, false);
        }
        if cell.flags.contains(Flags::DIM) {
            foreground = (
                foreground.0.saturating_mul(2) / 3,
                foreground.1.saturating_mul(2) / 3,
                foreground.2.saturating_mul(2) / 3,
            );
        }
        if !has_run || run_row != row || run_next_column != column || run_color != foreground {
            if has_run {
                draw_terminal_run(
                    text,
                    geometry,
                    run_row,
                    run_start_column,
                    &run_text,
                    run_color,
                );
                run_text.clear();
            }
            run_row = row;
            run_start_column = column;
            run_color = foreground;
            has_run = true;
        }
        run_text.push(cell.c);
        run_next_column = column + 1;
    }
    if has_run {
        draw_terminal_run(
            text,
            geometry,
            run_row,
            run_start_column,
            &run_text,
            run_color,
        );
    }
}

fn draw_terminal_run(
    text: &TextRenderer,
    geometry: &FrameGeometry,
    row: usize,
    column: usize,
    value: &str,
    color: (u8, u8, u8),
) {
    text.draw(
        geometry.operator_viewport.x as c_int + column as c_int * geometry.cell_width as c_int,
        geometry.operator_viewport.y as c_int
            + row as c_int * geometry.cell_height as c_int
            + text.metrics().baseline as c_int,
        value,
        color,
    );
}

fn draw_selection(
    x: f32,
    y: f32,
    selection: Option<((usize, usize), (usize, usize))>,
    cell_width: f32,
    cell_height: f32,
) {
    let Some((anchor, extent)) = selection else {
        return;
    };
    let (start, end) = if (anchor.1, anchor.0) <= (extent.1, extent.0) {
        (anchor, extent)
    } else {
        (extent, anchor)
    };
    unsafe {
        glColor3f(0.42, 0.68, 0.80);
        glLineWidth(1.0);
    }
    for row in start.1..=end.1 {
        let first = if row == start.1 { start.0 } else { 0 };
        let last = if row == end.1 {
            end.0.saturating_add(1).max(first + 1)
        } else {
            end.0.max(first + 1)
        };
        draw_outline_rect(
            x + first as f32 * cell_width,
            y + row as f32 * cell_height,
            (last.saturating_sub(first)) as f32 * cell_width,
            cell_height,
        );
    }
}

fn resolve_term_color(color: TermColor, colors: &Colors, foreground: bool) -> (u8, u8, u8) {
    let fallback = if foreground {
        (211, 225, 234)
    } else {
        (7, 12, 19)
    };
    match color {
        TermColor::Spec(rgb) => (rgb.r, rgb.g, rgb.b),
        TermColor::Named(name) => colors[name]
            .map(|rgb| (rgb.r, rgb.g, rgb.b))
            .unwrap_or_else(|| named_color(name, fallback)),
        TermColor::Indexed(index) => colors[index as usize]
            .map(|rgb| (rgb.r, rgb.g, rgb.b))
            .unwrap_or_else(|| indexed_color(index, fallback)),
    }
}

fn named_color(name: NamedColor, fallback: (u8, u8, u8)) -> (u8, u8, u8) {
    match name {
        NamedColor::Black | NamedColor::DimBlack => (18, 24, 31),
        NamedColor::Red | NamedColor::DimRed => (231, 102, 111),
        NamedColor::Green | NamedColor::DimGreen => (100, 205, 159),
        NamedColor::Yellow | NamedColor::DimYellow => (235, 190, 106),
        NamedColor::Blue | NamedColor::DimBlue => (112, 165, 232),
        NamedColor::Magenta | NamedColor::DimMagenta => (205, 132, 221),
        NamedColor::Cyan | NamedColor::DimCyan => (92, 199, 216),
        NamedColor::White | NamedColor::DimWhite => (205, 218, 229),
        NamedColor::BrightBlack => (87, 105, 119),
        NamedColor::BrightRed => (255, 133, 140),
        NamedColor::BrightGreen => (130, 240, 186),
        NamedColor::BrightYellow => (255, 216, 125),
        NamedColor::BrightBlue => (145, 193, 255),
        NamedColor::BrightMagenta => (232, 165, 245),
        NamedColor::BrightCyan => (125, 232, 240),
        NamedColor::BrightWhite => (244, 248, 251),
        NamedColor::Background => (7, 12, 19),
        NamedColor::Cursor => (128, 220, 209),
        NamedColor::Foreground | NamedColor::DimForeground | NamedColor::BrightForeground => {
            fallback
        }
    }
}

fn indexed_color(index: u8, fallback: (u8, u8, u8)) -> (u8, u8, u8) {
    if index < 16 {
        return named_color(
            match index {
                0 => NamedColor::Black,
                1 => NamedColor::Red,
                2 => NamedColor::Green,
                3 => NamedColor::Yellow,
                4 => NamedColor::Blue,
                5 => NamedColor::Magenta,
                6 => NamedColor::Cyan,
                7 => NamedColor::White,
                8 => NamedColor::BrightBlack,
                9 => NamedColor::BrightRed,
                10 => NamedColor::BrightGreen,
                11 => NamedColor::BrightYellow,
                12 => NamedColor::BrightBlue,
                13 => NamedColor::BrightMagenta,
                14 => NamedColor::BrightCyan,
                _ => NamedColor::BrightWhite,
            },
            fallback,
        );
    }
    if (16..=231).contains(&index) {
        let value = index - 16;
        let r = value / 36;
        let g = (value % 36) / 6;
        let b = value % 6;
        let channel = |value: u8| if value == 0 { 0 } else { value * 40 + 55 };
        return (channel(r), channel(g), channel(b));
    }
    if (232..=255).contains(&index) {
        let value = 8 + (index - 232) * 10;
        return (value, value, value);
    }
    fallback
}
