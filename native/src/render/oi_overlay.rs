use super::super::*;
use super::oi::SCENE_WIDTH;
use super::text::{FontRole, TextRenderer};
use super::theme::{AMBER, DIM, FAILURE, PRIMARY, SECONDARY, SUCCESS};
use crate::{Projection, ProjectionTreeNode};

const LEFT: f32 = 14.0;
const RIGHT: f32 = 218.0;
const FIRST_LINE: f32 = 40.0;
const LINE_HEIGHT: f32 = 17.0;

/// Draw the OI's live information layer on the full-resolution presentation
/// surface. The CRT texture supplies the glass and phosphor world; this layer
/// uses the same Xft typography as the operator console so the OI reads like a
/// modern instrument inside that enclosure.
pub(crate) fn draw_oi_text_overlay(
    text: &TextRenderer,
    oi_inner: PixelRect,
    projection: &Projection,
) {
    let sx = oi_inner.width / SCENE_WIDTH;
    let sy = oi_inner.height / 256.0;
    if sx <= 0.0 || sy <= 0.0 {
        return;
    }
    let x = |logical: f32| oi_inner.x + logical * sx;
    let y = |logical: f32| oi_inner.y + logical * sy;
    let right = x(RIGHT);
    let primary = PRIMARY;
    let secondary = SECONDARY;
    let dim = DIM;
    let mut lines: Vec<(String, FontRole, (u8, u8, u8))> = Vec::new();

    if let Some(request) = projection.model_request.as_ref() {
        let provider = if request.provider.is_empty() {
            "unknown"
        } else {
            request.provider.as_str()
        };
        let model = if request.model.is_empty() {
            "unspecified"
        } else {
            request.model.as_str()
        };
        let status = if request.status.is_empty() {
            String::new()
        } else {
            format!("  [{}]", request.status)
        };
        lines.push((
            format!("> MODEL REQUEST  · {provider}/{model}{status}"),
            FontRole::Body,
            status_color(&request.status, primary),
        ));
    }

    if projection.active_operation.is_some() {
        lines.push(("> ACTIVE OPERATION".to_owned(), FontRole::Body, primary));
    }
    if let Some(operation) = projection.active_operation.as_ref() {
        let label = first_non_empty([
            operation.label.as_str(),
            operation.operation.as_str(),
            operation.capability.as_str(),
        ]);
        let target = if operation.target.is_empty() {
            String::new()
        } else {
            format!("  → {}", operation.target)
        };
        if !label.is_empty() {
            lines.push((format!("  {label}{target}"), FontRole::Body, secondary));
        }
    }

    if !projection.workspace_tree.is_empty() {
        lines.push(("> WORKSPACE".to_owned(), FontRole::Body, primary));
        let mut tree = Vec::new();
        tree_lines(&projection.workspace_tree, 0, &mut tree, 3);
        lines.extend(
            tree.into_iter()
                .map(|line| (line, FontRole::Body, secondary)),
        );
    }

    if let Some(action) = projection.current_action.as_ref() {
        let action = format!("> ACTION  {} {}", action.kind, action.target)
            .trim()
            .to_owned();
        if action != "> ACTION" {
            lines.push((action, FontRole::Body, secondary));
        }
    }
    if let Some(code) = projection.code_view.as_ref() {
        if !code.path.is_empty() {
            lines.push((format!("> CODE  {}", code.path), FontRole::Body, secondary));
        }
    }
    if !projection.verification.status.is_empty() {
        lines.push((
            format!("> VERIFY  {}", projection.verification.status),
            FontRole::Body,
            status_color(&projection.verification.status, secondary),
        ));
    }
    if let Some(diagnostic) = projection.diagnostics.first() {
        if !diagnostic.message.is_empty() {
            lines.push((
                format!("> RESULT  {}", diagnostic.message),
                FontRole::Body,
                FAILURE,
            ));
        }
    }
    if let Some(item) = projection.attention_items.first() {
        let title = first_non_empty([item.title.as_str(), item.kind.as_str()]);
        let summary = if item.summary.is_empty() {
            String::new()
        } else {
            format!("  · {}", item.summary)
        };
        lines.push((
            format!("> ATTENTION  {title}{summary}"),
            FontRole::Body,
            status_color(&item.severity, AMBER),
        ));
    }
    if let Some(tail) = projection.stream_tail.last() {
        if !tail.is_empty() {
            lines.push((format!("> STREAM  {tail}"), FontRole::Body, dim));
        }
    }

    // A single structural spine makes the text layer feel like an instrument
    // readout without putting a rounded card behind it.
    super::primitives::draw_line_alpha(
        x(LEFT - 4.0),
        y(33.0),
        x(LEFT - 4.0),
        y(236.0),
        super::theme::rgb(DIM),
        0.32,
    );
    super::primitives::draw_line_alpha(
        x(LEFT - 4.0),
        y(31.0),
        right,
        y(31.0),
        super::theme::rgb(SECONDARY),
        0.28,
    );

    for (index, (line, role, color)) in lines.iter().take(12).enumerate() {
        draw_line(
            text,
            *role,
            x(LEFT),
            y(FIRST_LINE + index as f32 * LINE_HEIGHT),
            line,
            *color,
            right,
        );
    }

    if let Some(operation) = projection.active_operation.as_ref() {
        if operation.progress_determinate {
            if let Some(progress) = operation.progress_value {
                let bar_left = x(LEFT + 12.0);
                let bar_top = y(FIRST_LINE + lines.len().min(12) as f32 * LINE_HEIGHT + 1.0);
                let bar_width = (right - bar_left).max(8.0);
                super::primitives::draw_rect(
                    bar_left,
                    bar_top,
                    bar_width,
                    (2.0 * sy).max(1.0),
                    (0.22, 0.27, 0.29),
                );
                super::primitives::draw_rect(
                    bar_left,
                    bar_top,
                    bar_width * (progress as f32).clamp(0.0, 1.0),
                    (2.0 * sy).max(1.0),
                    super::theme::rgb(SUCCESS),
                );
            }
        }
    }
}

fn draw_line(
    text: &TextRenderer,
    role: FontRole,
    x: f32,
    top: f32,
    value: &str,
    color: (u8, u8, u8),
    right: f32,
) {
    let metrics = text.metrics_for(role);
    let fitted = fit_text(text, role, value, (right - x).max(0.0));
    text.draw_in(
        role,
        x.round() as i32,
        (top + metrics.baseline as f32).round() as i32,
        &fitted,
        color,
    );
}

fn fit_text(text: &TextRenderer, role: FontRole, value: &str, width: f32) -> String {
    if text.text_width_in(role, value) as f32 <= width {
        return value.to_owned();
    }
    let suffix = "…";
    let mut fitted = String::new();
    for character in value.chars() {
        let candidate = format!("{fitted}{character}{suffix}");
        if text.text_width_in(role, &candidate) as f32 > width {
            break;
        }
        fitted.push(character);
    }
    if fitted.is_empty() {
        suffix.to_owned()
    } else {
        format!("{fitted}{suffix}")
    }
}

fn first_non_empty<'a>(values: impl IntoIterator<Item = &'a str>) -> &'a str {
    values
        .into_iter()
        .find(|value| !value.is_empty())
        .unwrap_or("")
}

fn tree_lines(nodes: &[ProjectionTreeNode], depth: usize, output: &mut Vec<String>, limit: usize) {
    for node in nodes {
        if output.len() >= limit {
            return;
        }
        let label = first_non_empty([node.label.as_str(), node.id.as_str(), node.kind.as_str()]);
        let marker = if node.children.is_empty() {
            "└─"
        } else {
            "├─"
        };
        output.push(format!("{}{} {}", "  ".repeat(depth), marker, label));
        tree_lines(&node.children, depth + 1, output, limit);
    }
}

fn status_color(status: &str, fallback: (u8, u8, u8)) -> (u8, u8, u8) {
    match status.to_ascii_lowercase().as_str() {
        "failed" | "failure" | "error" => FAILURE,
        "passed" | "complete" | "success" | "ready" => SUCCESS,
        "waiting" | "approval" | "warning" | "paused" => AMBER,
        _ => fallback,
    }
}
