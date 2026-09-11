use super::super::*;
use super::chassis::{bitmap_width, draw_bitmap_text};
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
    phase: f32,
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
        with_scissor(geometry.height, geometry.prompt, || {
            let scale = (text.metrics_for(FontRole::Instrument).height / 7.0)
                .round()
                .max(2.0);
            draw_bitmap_text(
                prompt_x as f32,
                status_row.top,
                &status,
                fit_bitmap_scale(&status, scale, content_width as f32),
                (0.62, 0.77, 0.85),
                (prompt_x + content_width) as f32,
            );
        });
    }
    let input_bitmap = format!("> {displayed}");
    if !input_bitmap.is_ascii() {
        text.draw_in(
            input_role,
            prompt_x,
            prompt_layout.input_row.baseline as c_int,
            &input_bitmap,
            if focused {
                (206, 220, 230)
            } else {
                (132, 145, 156)
            },
        );
    }
    if input_bitmap.is_ascii() {
        with_scissor(geometry.height, geometry.prompt, || {
            let scale = (text.metrics_for(input_role).height / 7.0).round().max(2.0);
            draw_bitmap_text(
                prompt_x as f32,
                prompt_layout.input_row.top,
                &input_bitmap,
                fit_bitmap_scale(&input_bitmap, scale, content_width as f32),
                if focused {
                    (0.81, 0.86, 0.90)
                } else {
                    (0.70, 0.76, 0.81)
                },
                (prompt_x + content_width) as f32,
            );
        });
    }
    // Hardware cursor cadence is intentionally stepped: it reads as a
    // terminal cursor, not a smooth web animation. A zero phase keeps the
    // cursor visible for deterministic still captures.
    if focused && (phase <= 0.0 || (phase * 2.0).floor() as i32 % 2 == 0) {
        let cursor_prefix: String = displayed.chars().take(display_cursor).collect();
        let cursor_x = if input_bitmap.is_ascii() {
            let cursor_scale = (text.metrics_for(input_role).height / 7.0).round().max(2.0);
            prompt_x + bitmap_width(&format!("> {cursor_prefix}"), cursor_scale).round() as c_int
        } else {
            prompt_x + text.text_width_in(input_role, &format!("> {cursor_prefix}"))
        };
        with_scissor(geometry.height, geometry.prompt, || {
            draw_rect(
                cursor_x as f32,
                prompt_layout.input_row.top,
                geometry.u(2.0).max(1.0),
                prompt_layout.input_row.height,
                (0.36, 0.76, 0.72),
            );
        });
    }
    if let Some(hint_row) = prompt_layout.hint_row {
        let hint = "↑↓ SCROLL   ←→ EDIT   CTRL-C CANCEL";
        if hint.is_ascii() {
            with_scissor(geometry.height, geometry.prompt, || {
                let scale = (text.metrics_for(FontRole::Instrument).height / 7.0)
                    .round()
                    .max(2.0);
                draw_bitmap_text(
                    prompt_x as f32,
                    hint_row.top,
                    hint,
                    fit_bitmap_scale(hint, scale, content_width as f32),
                    (0.58, 0.72, 0.80),
                    (prompt_x + content_width) as f32,
                );
            });
        } else {
            text.draw_in(
                FontRole::Instrument,
                prompt_x,
                hint_row.baseline as c_int,
                hint,
                (148, 184, 204),
            );
        }
    }
}

fn fit_bitmap_scale(value: &str, preferred: f32, width: f32) -> f32 {
    let glyph_count = value.chars().filter(|character| *character != '\n').count() as f32;
    if glyph_count == 0.0 {
        return preferred;
    }
    preferred.min(width / (glyph_count * 6.0)).max(1.0)
}

fn human_status(projection: &Projection) -> &str {
    match VisualMode::from_projection(projection).prompt_state(projection) {
        "APPROVAL" => "Waiting for approval.",
        "FAILURE" | "DISCONNECTED" => "Verification failed.",
        "READY" => "Athena is ready.",
        _ => "Athena is working through the request.",
    }
}
