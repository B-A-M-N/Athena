use super::super::*;
use super::primitives::{draw_rect, with_scissor};
use super::text::{FontRole, TextRenderer};
use super::theme::{PRIMARY, SECONDARY};

/// Render the operator instrument and semantic labels. Layout is owned here;
/// x11.rs only forwards input and invalidation events to the compositor.
pub(crate) fn draw_status_text(
    text: &TextRenderer,
    geometry: &FrameGeometry,
    projection: &Projection,
    focused: bool,
    input: &InputBuffer,
    draw_cursor: bool,
) {
    let input_role = if geometry.compact {
        FontRole::Instrument
    } else {
        FontRole::Input
    };
    let prompt_layout = PromptLayout::from_rect(
        geometry.prompt,
        text.metrics_for(input_role),
        text.metrics_for(FontRole::Instrument),
        geometry.prompt_padding_y,
        geometry.prompt_gap,
        geometry.prompt_bottom_padding,
        !geometry.compact,
    );
    debug_assert!(prompt_layout.rows_fit(geometry.prompt_bottom_padding));
    let content_width =
        (geometry.prompt.width as c_int - geometry.prompt_padding_x as c_int * 2).max(1);
    let input_value = format!("{}{}", input.text(), input.composition());
    let prefix = "ATHENA ";
    let (displayed, _display_cursor) = super::super::fit_input_in(
        text,
        input_role,
        &input_value,
        input.cursor(),
        (content_width - text.text_width_in(input_role, prefix)).max(1),
    );
    let prompt_x = geometry.prompt.x as c_int + geometry.prompt_padding_x as c_int;
    let input_line = format!("{prefix}{displayed}");
    text.draw_in(
        input_role,
        prompt_x,
        prompt_layout.input_row.baseline as c_int,
        &input_line,
        if focused { PRIMARY } else { SECONDARY },
    );
    // Hardware cursor cadence is intentionally stepped: it reads as a
    // terminal cursor, not a smooth web animation. A zero phase keeps the
    // cursor visible for deterministic still captures.
    if draw_cursor {
        draw_status_cursor(text, geometry, focused, input, 0.0);
    }
    let footer = format!(
        "{}  |  ↑↓ SCROLL  ←→ EDIT  CTRL-C CANCEL",
        human_status(projection)
    );
    let footer = super::super::fit_text_in(text, FontRole::Instrument, &footer, content_width);
    text.draw_in(
        FontRole::Instrument,
        prompt_x,
        prompt_layout.footer_row.baseline as c_int,
        &footer,
        SECONDARY,
    );
}

pub(crate) fn draw_status_cursor(
    text: &TextRenderer,
    geometry: &FrameGeometry,
    focused: bool,
    input: &InputBuffer,
    phase: f32,
) {
    if !focused || (phase > 0.0 && (phase * 2.0).floor() as i32 % 2 != 0) {
        return;
    }
    let input_role = if geometry.compact {
        FontRole::Instrument
    } else {
        FontRole::Input
    };
    let prompt_layout = PromptLayout::from_rect(
        geometry.prompt,
        text.metrics_for(input_role),
        text.metrics_for(FontRole::Instrument),
        geometry.prompt_padding_y,
        geometry.prompt_gap,
        geometry.prompt_bottom_padding,
        !geometry.compact,
    );
    let content_width =
        (geometry.prompt.width as c_int - geometry.prompt_padding_x as c_int * 2).max(1);
    let input_value = format!("{}{}", input.text(), input.composition());
    let prefix = "ATHENA ";
    let (displayed, display_cursor) = super::super::fit_input_in(
        text,
        input_role,
        &input_value,
        input.cursor(),
        (content_width - text.text_width_in(input_role, prefix)).max(1),
    );
    let prompt_x = geometry.prompt.x as c_int + geometry.prompt_padding_x as c_int;
    let cursor_prefix: String = displayed.chars().take(display_cursor).collect();
    let cursor_x = prompt_x + text.text_width_in(input_role, &format!("{prefix}{cursor_prefix}"));
    with_scissor(geometry.height, geometry.prompt, || {
        draw_rect(
            cursor_x as f32,
            prompt_layout.input_row.top + geometry.u(2.0),
            geometry.u(2.0).max(1.0),
            (prompt_layout.input_row.height - geometry.u(4.0)).max(2.0),
            (0.70, 0.82, 0.86),
        );
    });
}

fn human_status(projection: &Projection) -> &str {
    match VisualMode::from_projection(projection).prompt_state(projection) {
        "APPROVAL" => "WAITING APPROVAL",
        "FAILURE" | "DISCONNECTED" => "VERIFICATION FAILED",
        "READY" => "ATHENA READY",
        _ => "ATHENA WORKING",
    }
}
