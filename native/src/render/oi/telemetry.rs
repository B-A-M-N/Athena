//! Semantic telemetry color policy for the OI scene.

use super::compose::{draw_dotted_span, pixel_text_styled};
use crate::render::chassis::BitmapTextStyle;
use crate::render::theme::{self, AMBER, FAILURE, PRIMARY, SECONDARY, SUCCESS};
use crate::{Projection, VisualMode};

pub(crate) fn telemetry_status_color(status: &str, base: (f32, f32, f32)) -> (f32, f32, f32) {
    match status.to_ascii_lowercase().as_str() {
        "failed" | "failure" | "error" => theme::rgb(FAILURE),
        "passed" | "complete" | "success" | "ready" => theme::rgb(SUCCESS),
        "waiting" | "approval" | "paused" => theme::rgb(AMBER),
        _ => base,
    }
}

/// Render the semantic progress and operation readout for the active scene.
pub(crate) fn draw_operation_telemetry(
    projection: &Projection,
    mode: VisualMode,
    rect: athena_terminal::PixelRect,
    color: (f32, f32, f32),
    _phase: f32,
) {
    let primary = theme::rgb(PRIMARY);
    let secondary = theme::rgb(SECONDARY);
    let active = secondary;
    let mut lines: Vec<(String, (f32, f32, f32))> =
        vec![(format!("TELEMETRY // {}", mode.as_str()), primary)];

    if matches!(mode, VisualMode::Respond | VisualMode::Think) {
        if let Some(request) = projection.model_request.as_ref() {
            let provider = if request.provider.is_empty() {
                "UNKNOWN"
            } else {
                &request.provider
            };
            let model = if request.model.is_empty() {
                "UNSPECIFIED"
            } else {
                &request.model
            };
            lines.push((format!("MODEL {provider}/{model}"), active));
        }
        lines.push(("RESPONDING".to_owned(), active));
        let tail = projection
            .stream_tail
            .last()
            .filter(|tail| !tail.is_empty())
            .map_or_else(|| "generating response...".to_owned(), Clone::clone);
        lines.push((format!("STREAM {tail}"), secondary));
    } else if matches!(mode, VisualMode::Test | VisualMode::Verify) {
        if let Some(operation) = projection.active_operation.as_ref() {
            let label = if operation.label.is_empty() {
                operation.capability.as_str()
            } else {
                operation.label.as_str()
            };
            lines.push((format!("TESTING {label}"), active));
        }
        if let Some(value) = projection
            .progress
            .as_ref()
            .and_then(serde_json::Value::as_object)
            .and_then(|progress| progress.get("value"))
            .and_then(serde_json::Value::as_f64)
        {
            let filled = ((value.clamp(0.0, 1.0) * 10.0).round()) as usize;
            lines.push((
                format!(
                    "PROGRESS {}{} {:>3.0}%",
                    "█".repeat(filled),
                    "░".repeat(10 - filled),
                    value * 100.0
                ),
                active,
            ));
        }
        if let Some(check) = projection.verification.checks.first() {
            lines.push((
                format!(
                    "CHECK {}",
                    check
                        .get("criterion")
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or("acceptance")
                ),
                secondary,
            ));
        }
        let result = if projection.verification.status.is_empty() {
            "running"
        } else {
            projection.verification.status.as_str()
        };
        lines.push((format!("RESULT {result}"), active));
    } else if mode == VisualMode::Failure {
        if let Some(diagnostic) = projection.diagnostics.first() {
            lines.push(("RESULT MISMATCH DETECTED".to_owned(), theme::rgb(FAILURE)));
            lines.push((
                format!(
                    "expected {}",
                    diagnostic
                        .expected
                        .as_ref()
                        .map_or_else(|| "?".to_owned(), serde_json::Value::to_string)
                ),
                secondary,
            ));
            lines.push((
                format!(
                    "actual {}",
                    diagnostic
                        .actual
                        .as_ref()
                        .map_or_else(|| "?".to_owned(), serde_json::Value::to_string)
                ),
                secondary,
            ));
            let at = if diagnostic.path.is_empty() {
                diagnostic.message.as_str()
            } else {
                diagnostic.path.as_str()
            };
            lines.push((format!("at {at}"), secondary));
            lines.push((
                format!("severity {}", diagnostic.severity),
                theme::rgb(AMBER),
            ));
        } else {
            lines.push(("RESULT FAILURE".to_owned(), theme::rgb(FAILURE)));
            lines.push(("DETAIL unavailable".to_owned(), secondary));
        }
    } else {
        if let Some(operation) = projection.active_operation.as_ref() {
            let label = if operation.label.is_empty() {
                operation.capability.as_str()
            } else {
                operation.label.as_str()
            };
            let target = if operation.target.is_empty() {
                String::new()
            } else {
                format!(" -> {}", operation.target)
            };
            lines.push((format!("OP {label}{target}"), active));
        } else if let Some(action) = projection.current_action.as_ref() {
            lines.push((
                format!("ACTION {} {}", action.kind, action.target)
                    .trim()
                    .to_owned(),
                active,
            ));
        }
        if let Some(value) = projection
            .progress
            .as_ref()
            .and_then(serde_json::Value::as_object)
            .and_then(|progress| progress.get("value"))
            .and_then(serde_json::Value::as_f64)
        {
            lines.push((
                format!("PROGRESS {:>3.0}%", (value * 100.0).clamp(0.0, 100.0)),
                active,
            ));
        }
        if let Some(item) = projection.attention_items.first() {
            lines.push((
                format!("ATTN {}", item.title),
                telemetry_status_color(&item.severity, theme::rgb(AMBER)),
            ));
        }
    }
    while lines.len() < 4 {
        lines.push((String::new(), secondary));
    }
    lines.truncate(7);
    draw_dotted_span(
        rect.x,
        rect.y,
        rect.right(),
        (color.0 * 0.48, color.1 * 0.48, color.2 * 0.48),
        2.0,
    );
    draw_dotted_span(
        rect.x,
        rect.bottom(),
        rect.right(),
        (color.0 * 0.30, color.1 * 0.30, color.2 * 0.30),
        2.0,
    );
    for (index, (line, line_color)) in lines.iter().take(7).enumerate() {
        let (y, scale, style) = if index == 0 {
            (rect.y + 10.0, 2.0, BitmapTextStyle::Phosphor)
        } else {
            (
                rect.y + 28.0 + (index - 1) as f32 * 10.0,
                1.0,
                BitmapTextStyle::Crisp,
            )
        };
        pixel_text_styled(rect.x, y, line, *line_color, rect.right(), scale, style);
    }
}
