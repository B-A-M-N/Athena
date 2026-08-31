use super::super::*;
use super::primitives::{draw_rect, with_scissor};
use super::text::{FontRole, TextRenderer};

/// Render the operator instrument and semantic labels. Layout is owned here;
/// x11.rs only forwards input and invalidation events to the compositor.
pub(crate) fn draw_status_text(
    text: &TextRenderer,
    geometry: &FrameGeometry,
    projection: &Projection,
    focused: bool,
    input: &InputBuffer,
) {
    let input_role = if geometry.compact {
        // The lower rail collapses on small windows. Keep the prompt an
        // actual two-row instrument there rather than letting the large
        // editable face spill into the chassis.
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
    let (displayed, display_cursor) = super::super::fit_input_in(
        text,
        input_role,
        &input_value,
        input.cursor(),
        (content_width - text.text_width_in(input_role, "> ")).max(1),
    );
    let prompt_x = geometry.prompt.x as c_int + geometry.prompt_padding_x as c_int;
    let status = human_status(projection);
    let status = super::super::fit_text_in(text, FontRole::Instrument, status, content_width);
    if let Some(status_row) = prompt_layout.status_row {
        text.draw_in(
            FontRole::Instrument,
            prompt_x,
            status_row.baseline as c_int,
            &status,
            (110, 150, 174),
        );
    }
    text.draw_in(
        input_role,
        prompt_x,
        prompt_layout.input_row.baseline as c_int,
        &format!("> {displayed}"),
        if focused {
            (206, 220, 230)
        } else {
            (132, 145, 156)
        },
    );
    if focused {
        let cursor_prefix: String = displayed.chars().take(display_cursor).collect();
        let cursor_x = prompt_x + text.text_width_in(input_role, &format!("> {cursor_prefix}"));
        with_scissor(geometry.height, geometry.prompt, || {
            draw_rect(
                cursor_x as f32,
                prompt_layout.input_row.top,
                2.0,
                prompt_layout.input_row.height,
                (0.36, 0.76, 0.72),
            );
        });
    }
    if let Some(hint_row) = prompt_layout.hint_row {
        text.draw_in(
            FontRole::Instrument,
            prompt_x,
            hint_row.baseline as c_int,
            "↑↓ SCROLL   ←→ EDIT   CTRL-C CANCEL",
            (94, 126, 153),
        );
    }
}

fn human_status(projection: &Projection) -> &str {
    match VisualMode::from_projection(projection).prompt_state(projection) {
        "APPROVAL" => "Waiting for approval.",
        "FAILURE" | "DISCONNECTED" => "Verification failed.",
        "READY" => "Athena is ready.",
        _ => "Athena is working through the request.",
    }
}

pub(crate) fn draw_instrument_labels(text: &TextRenderer, geometry: &FrameGeometry) {
    text.draw_in(
        FontRole::Instrument,
        geometry.rail.speaker.x as c_int + 13,
        geometry.rail.speaker.bottom() as c_int - 6,
        "SPEAKER GRILLE",
        (99, 123, 145),
    );
    for (rect, label) in [
        (geometry.rail.brightness, "BRI"),
        (geometry.rail.focus, "FOCUS"),
        (geometry.rail.power, "POWER"),
    ]
    .into_iter()
    {
        text.draw_in(
            FontRole::Instrument,
            rect.x as c_int + 10,
            geometry.controls.y as c_int + 18,
            &super::super::fit_text_in(text, FontRole::Instrument, label, rect.width as c_int - 16),
            (105, 132, 153),
        );
    }
    let system = geometry.rail.system_status;
    for (index, label) in ["SYS", "NET", "ACT"].into_iter().enumerate() {
        let x = system.x as c_int + 18 + index as c_int * 42;
        text.draw_in(
            FontRole::Instrument,
            x,
            (system.y + system.height * 0.72) as c_int,
            label,
            (108, 132, 136),
        );
    }
    let encoder = geometry.rail.primary_encoder;
    text.draw_in(
        FontRole::Instrument,
        encoder.x as c_int + 12,
        (encoder.y + encoder.height * 0.18) as c_int,
        "ENC",
        (112, 136, 138),
    );
    let plate = geometry.rail.identity_plate;
    text.draw_in(
        FontRole::Instrument,
        plate.x as c_int + 10,
        (plate.y + plate.height * 0.75) as c_int,
        "ATHENA BOX",
        (184, 187, 181),
    );
}
