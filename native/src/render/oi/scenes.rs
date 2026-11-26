//! Semantic scene helpers and golden tests.

use super::compose::{draw_dotted_line, pixel_text_styled};
use super::layout::{
    SceneLayout, TreeLayoutNode, layout_runtime_graph, layout_tree, runtime_entity_nodes,
};
use super::motion::draw_packets;
#[cfg(test)]
use super::*;
use crate::render::chassis::BitmapTextStyle;
use crate::render::primitives::{draw_line, draw_node, draw_rect, draw_round_outline};
use crate::render::theme::{AMBER, FAILURE, SUCCESS};
use crate::{Projection, ProjectionEntity, VisualMode};
#[cfg(test)]
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

pub(crate) fn check_status(check: &serde_json::Value) -> &str {
    if check.get("passed").and_then(serde_json::Value::as_bool) == Some(true) {
        return "passed";
    }
    check
        .get("status")
        .or_else(|| check.get("state"))
        .and_then(serde_json::Value::as_str)
        .map(|status| match status.to_ascii_lowercase().as_str() {
            "pass" | "passed" | "complete" | "completed" | "ok" => "passed",
            "fail" | "failed" | "failure" | "error" => "failed",
            _ => "running",
        })
        .unwrap_or("running")
}

fn node_status_color(status: &str, base: (f32, f32, f32)) -> (f32, f32, f32) {
    match status.to_ascii_lowercase().as_str() {
        "failed" | "error" | "failure" => crate::render::theme::rgb(FAILURE),
        "passed" | "complete" | "ready" | "ok" | "success" | "succeeded" => {
            crate::render::theme::rgb(SUCCESS)
        }
        "approval" | "warning" => crate::render::theme::rgb(AMBER),
        "reading" | "testing" | "running" | "active" | "working" => base,
        _ => (base.0 * 0.72, base.1 * 0.72, base.2 * 0.72),
    }
}

fn draw_tree_nodes(
    nodes: &[TreeLayoutNode],
    color: (f32, f32, f32),
    phase: f32,
    active_id: Option<&str>,
) {
    let dim = (color.0 * 0.62, color.1 * 0.62, color.2 * 0.62);
    for node in nodes {
        if let Some(parent) = node.parent.and_then(|index| nodes.get(index)) {
            draw_dotted_line(parent.x, parent.y, node.x, node.y, dim);
        }
    }
    for node in nodes {
        let is_active = active_id.is_some_and(|id| id == node.id);
        let node_color = node_status_color(&node.status, color);
        let radius = if is_active {
            node.radius + 2.0
        } else {
            node.radius
        };
        draw_node(node.x, node.y, radius, node_color);
        if is_active {
            let pulse = 0.6 + 0.4 * (phase * std::f32::consts::TAU * 1.3).sin();
            draw_round_outline(
                node.x - radius - 6.0,
                node.y - radius - 6.0,
                radius * 2.0 + 12.0,
                radius * 2.0 + 12.0,
                (
                    node_color.0 * pulse,
                    node_color.1 * pulse,
                    node_color.2 * pulse,
                ),
            );
        }
    }
}

fn draw_runtime_graph(
    layout: &[(f32, f32, f32, &ProjectionEntity)],
    color: (f32, f32, f32),
    phase: f32,
) {
    let dim = (color.0 * 0.45, color.1 * 0.45, color.2 * 0.45);
    let by_id: HashMap<&str, (f32, f32)> = layout
        .iter()
        .map(|(x, y, _, entity)| (entity.id.as_str(), (*x, *y)))
        .collect();
    for (x, y, _, entity) in layout {
        if let Some(parent_id) = entity.parent_id.as_deref() {
            if let Some((px, py)) = by_id.get(parent_id) {
                draw_dotted_line(*px, *py, *x, *y, dim);
            }
        }
    }
    for (x, y, radius, entity) in layout {
        draw_node(*x, *y, *radius, node_status_color(&entity.status, color));
    }
    let edges: Vec<((f32, f32), (f32, f32))> = layout
        .iter()
        .filter_map(|(x, y, _, entity)| {
            entity
                .parent_id
                .as_deref()
                .and_then(|id| by_id.get(id))
                .map(|parent| (*parent, (*x, *y)))
        })
        .collect();
    draw_packets(&edges, phase, color, false);
}

// ---------------------------------------------------------------------------
// Individual semantic scenes
// ---------------------------------------------------------------------------

pub(crate) fn draw_idle_scene(
    _projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
) {
    let cx = layout.stage.x + layout.stage.width * 0.5;
    let cy = layout.stage.y + layout.stage.height * 0.35;
    let pulse = 0.5 + 0.5 * (phase * std::f32::consts::TAU * 0.4).sin();
    draw_node(
        cx,
        cy,
        16.0,
        (color.0 * pulse, color.1 * pulse, color.2 * pulse),
    );
}

pub(crate) fn draw_workspace_scene(
    projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
    mode: VisualMode,
) {
    let tree = layout_tree(&projection.workspace_tree, layout.stage);
    let active_id = projection
        .code_view
        .as_ref()
        .map(|code| code.path.as_str())
        .or_else(|| {
            projection
                .active_operation
                .as_ref()
                .and_then(|op| (!op.target.is_empty()).then_some(op.target.as_str()))
        });
    draw_tree_nodes(&tree, color, phase, active_id);
    if mode == VisualMode::Search {
        let right = layout.stage.right();
        let span = (right - layout.stage.x - 24.0).max(12.0);
        let sweep_x = layout.stage.x + 12.0 + phase.fract() * span;
        draw_dotted_line(
            sweep_x,
            layout.stage.y,
            sweep_x,
            layout.stage.bottom(),
            (color.0 * 0.84, color.1 * 0.84, color.2 * 0.84),
        );
    }
}

pub(crate) fn draw_read_scene(
    _projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
) {
    let cx = layout.stage.x + layout.stage.width * 0.35;
    let cy = layout.stage.y + layout.stage.height * 0.45;
    draw_node(cx, cy, 20.0, color);
    let scan_radius = 16.0 + ((phase * 0.8).fract() * 18.0);
    draw_round_outline(
        cx - scan_radius,
        cy - scan_radius,
        scan_radius * 2.0,
        scan_radius * 2.0,
        (color.0 * 0.55, color.1 * 0.55, color.2 * 0.55),
    );
}

pub(crate) fn draw_code_scene(
    projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
) {
    let tree = layout_tree(&projection.workspace_tree, layout.stage);
    let active_id = projection
        .code_view
        .as_ref()
        .map(|code| code.path.as_str())
        .or_else(|| {
            projection
                .active_operation
                .as_ref()
                .and_then(|op| (!op.target.is_empty()).then_some(op.target.as_str()))
        });
    draw_tree_nodes(&tree, color, phase, active_id);
    let cx = layout.world.x + layout.world.width * 0.82;
    let cy = layout.world.y + layout.world.height * 0.72;
    let pulse = 0.5 + 0.5 * (phase * std::f32::consts::TAU * 1.2).sin();
    draw_rect(
        cx - 3.0,
        cy - 3.0,
        6.0,
        6.0,
        (color.0 * pulse, color.1 * pulse, color.2 * pulse),
    );
}

pub(crate) fn draw_execute_scene(
    projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
    _mode: VisualMode,
) {
    let entities = runtime_entity_nodes(projection);
    let graph = layout_runtime_graph(&entities, layout.stage);
    draw_runtime_graph(&graph, color, phase);
    let cx = layout.stage.x + layout.stage.width * 0.5;
    let cy = layout.stage.y + layout.stage.height * 0.82;
    let pulse = 0.5 + 0.5 * (phase * std::f32::consts::TAU * 0.9).sin();
    draw_node(
        cx,
        cy,
        10.0,
        (color.0 * pulse, color.1 * pulse, color.2 * pulse),
    );
}

pub(crate) fn draw_test_scene(
    projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    _phase: f32,
) {
    let checks = &projection.verification.checks;
    let cx = layout.stage.x + layout.stage.width * 0.5;
    let cy = layout.stage.y + layout.stage.height * 0.42;
    if checks.is_empty() {
        draw_node(cx, cy, 14.0, color);
        return;
    }
    let count = checks.len().min(5);
    let radius = 48.0_f32.min(layout.stage.width * 0.22);
    let start_angle = -std::f32::consts::FRAC_PI_2;
    for (index, check) in checks.iter().take(count).enumerate() {
        let angle = start_angle + index as f32 * (std::f32::consts::TAU / count.max(1) as f32);
        let x = cx + angle.cos() * radius;
        let y = cy + angle.sin() * radius;
        let status = check_status(check);
        let gate_color = if status == "failed" {
            (0.88, 0.28, 0.32)
        } else if status == "passed" || status == "complete" {
            (0.46, 0.91, 0.67)
        } else {
            color
        };
        draw_node(x, y, 10.0, gate_color);
        draw_dotted_line(
            cx,
            cy,
            x,
            y,
            (color.0 * 0.45, color.1 * 0.45, color.2 * 0.45),
        );
    }
    draw_node(cx, cy, 14.0, color);
}

pub(crate) fn draw_approval_scene(
    _projection: &Projection,
    layout: &SceneLayout,
    _color: (f32, f32, f32),
    phase: f32,
) {
    let amber = crate::render::theme::rgb(AMBER);
    let cx = layout.stage.x + layout.stage.width * 0.5;
    let cy = layout.stage.y + layout.stage.height * 0.45;
    let pulse = (phase / 0.28).clamp(0.0, 1.0);
    let width = 74.0 * pulse;
    let height = 48.0 * pulse;
    draw_round_outline(cx - width * 0.5, cy - height * 0.5, width, height, amber);
    draw_rect(cx - 14.0, cy, 28.0, 2.0, amber);
    draw_rect(cx, cy - 14.0, 2.0, 28.0, amber);
}

pub(crate) fn draw_failure_scene(
    projection: &Projection,
    layout: &SceneLayout,
    _color: (f32, f32, f32),
    phase: f32,
) {
    let red = crate::render::theme::rgb(FAILURE);
    let dim_red = (red.0 * 0.38, red.1 * 0.38, red.2 * 0.38);
    let cx = layout.stage.x + layout.stage.width * 0.38;
    let cy = layout.stage.y + layout.stage.height * 0.48;
    let shift = 1.0 * (1.0 - (phase / 0.24).clamp(0.0, 1.0));
    draw_rect(cx - 14.0 + shift, cy - 12.0, 2.0, 24.0, dim_red);
    draw_node(cx, cy, 10.0, dim_red);
    let title = if projection.failure.kind == "model_routing" {
        "MODEL ROUTE FAILED"
    } else {
        "TASK FAILED"
    };
    let reason = projection.failure.reason.lines().next().unwrap_or("");
    let detail = if projection.failure.stage.is_empty() {
        reason.to_owned()
    } else if reason.is_empty() {
        projection.failure.stage.clone()
    } else {
        format!("{} · {}", projection.failure.stage, reason)
    };
    pixel_text_styled(
        layout.world.x + 14.0,
        layout.world.y + 10.0,
        title,
        red,
        layout.world.right() - 14.0,
        STATUS_TEXT_SCALE,
        BitmapTextStyle::Phosphor,
    );
    if !detail.is_empty() {
        pixel_text_styled(
            layout.world.x + 14.0,
            layout.world.y + 26.0,
            &detail,
            (0.86, 0.90, 0.94),
            layout.world.right() - 14.0,
            DETAIL_TEXT_SCALE,
            BitmapTextStyle::Crisp,
        );
    }
}

pub(crate) fn draw_success_scene(layout: &SceneLayout, _color: (f32, f32, f32), phase: f32) {
    let green = crate::render::theme::rgb(SUCCESS);
    let cx = layout.stage.x + layout.stage.width * 0.5;
    let cy = layout.stage.y + layout.stage.height * 0.45;
    let pulse = 0.85 + 0.15 * (phase * std::f32::consts::TAU * 0.6).sin();
    draw_node(
        cx,
        cy,
        18.0,
        (green.0 * pulse, green.1 * pulse, green.2 * pulse),
    );
    draw_line(cx - 12.0, cy + 2.0, cx - 2.0, cy + 12.0, green);
    draw_line(cx - 2.0, cy + 12.0, cx + 14.0, cy - 8.0, green);
}

pub(crate) fn draw_think_scene(
    _projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
) {
    let cx = layout.world.x + layout.world.width * 0.52;
    let cy = layout.world.y + layout.world.height * 0.36;
    let active_index = (phase.max(0.0) * 1.4).floor() as usize % 4;
    for index in 0..4 {
        let x = layout.world.x + 14.0 + index as f32 * 22.0;
        let y = cy + index as f32 * 2.0;
        let activity = if index == active_index { 0.82 } else { 0.34 };
        draw_dotted_line(
            x,
            y,
            x + 14.0,
            y - 8.0,
            (color.0 * 0.30, color.1 * 0.36, color.2 * 0.38),
        );
        draw_rect(
            x + 12.0,
            y - 9.0,
            3.0,
            3.0,
            (color.0 * activity, color.1 * activity, color.2 * activity),
        );
    }
    draw_node(
        cx,
        cy,
        7.0,
        (color.0 * 0.52, color.1 * 0.58, color.2 * 0.60),
    );
}

const DETAIL_TEXT_SCALE: f32 = 1.0;
const STATUS_TEXT_SCALE: f32 = 2.0;

#[cfg(test)]
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
struct SemanticProgressSnapshot {
    source: String,
    determinate: bool,
    value_percent: Option<u8>,
}

#[cfg(test)]
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
struct SemanticSceneSnapshot {
    mode: String,
    focal_object: String,
    entity_ids: Vec<String>,
    entity_labels: Vec<String>,
    entity_statuses: Vec<String>,
    failed_entity_ids: Vec<String>,
    successful_entity_ids: Vec<String>,
    active_operation_id: Option<String>,
    active_operation_action: Option<String>,
    active_operation_state: Option<String>,
    current_action_kind: Option<String>,
    verification_status: String,
    verification_check_statuses: Vec<String>,
    attention_ids: Vec<String>,
    attention_requires_action: Vec<bool>,
    progress: SemanticProgressSnapshot,
    buddy_state: String,
    buddy_anchor: String,
    buddy_status: String,
    buddy_character: String,
    attention_rail: bool,
    unobscured_right: u16,
    failure_diagnostic_locations: Vec<String>,
}

#[cfg(test)]
pub(crate) fn focal_object(mode: VisualMode) -> &'static str {
    match mode {
        VisualMode::Idle => "world",
        VisualMode::Think => "thought-pulses",
        VisualMode::Respond => "response",
        VisualMode::Inspect => "workspace-tree",
        VisualMode::Read => "artifact",
        VisualMode::Search => "scanner",
        VisualMode::Code => "code-diff",
        VisualMode::Execute | VisualMode::Generate => "runtime-packets",
        VisualMode::Test => "verification-gates",
        VisualMode::Verify => "verification-gate",
        VisualMode::Approval => "approval-object",
        VisualMode::Recover => "recovery-packets",
        VisualMode::Failure => "failure-fracture",
        VisualMode::Success => "success-seal",
    }
}

#[cfg(test)]
fn semantic_progress_snapshot(projection: &Projection) -> SemanticProgressSnapshot {
    let operation = projection.active_operation.as_ref().and_then(|operation| {
        operation
            .progress_determinate
            .then_some(("operation", operation.progress_value))
    });
    let action = projection.current_action.as_ref().and_then(|action| {
        action
            .progress_determinate
            .then_some(("action", action.progress_value))
    });
    let (source, value) = operation.or(action).unwrap_or(("none", None));
    SemanticProgressSnapshot {
        source: source.to_owned(),
        determinate: source != "none",
        value_percent: value.and_then(|value| {
            value
                .is_finite()
                .then(|| (value * 100.0).round().clamp(0.0, 100.0) as u8)
        }),
    }
}

#[cfg(test)]
fn diagnostic_location(diagnostic: &crate::ProjectionDiagnostic) -> Option<String> {
    (!diagnostic.path.is_empty()).then(|| {
        diagnostic.line.map_or_else(
            || diagnostic.path.clone(),
            |line| format!("{}:{line}", diagnostic.path),
        )
    })
}

#[cfg(test)]
fn semantic_scene_snapshot(projection: &Projection) -> SemanticSceneSnapshot {
    let mode = VisualMode::from_projection(projection);
    let entities = semantic_entities(projection);
    let progress = semantic_progress_snapshot(projection);
    SemanticSceneSnapshot {
        mode: mode.as_str().to_owned(),
        focal_object: focal_object(mode).to_owned(),
        entity_ids: entities.iter().map(|entity| entity.id.clone()).collect(),
        entity_labels: entities.iter().map(|entity| entity_label(entity)).collect(),
        entity_statuses: entities
            .iter()
            .map(|entity| entity.status.clone())
            .collect(),
        failed_entity_ids: entities
            .iter()
            .filter(|entity| {
                entity.status.eq_ignore_ascii_case("failed")
                    || entity.status.eq_ignore_ascii_case("failure")
            })
            .map(|entity| entity.id.clone())
            .collect(),
        successful_entity_ids: entities
            .iter()
            .filter(|entity| {
                entity.status.eq_ignore_ascii_case("success")
                    || entity.status.eq_ignore_ascii_case("succeeded")
                    || entity.status.eq_ignore_ascii_case("passed")
            })
            .map(|entity| entity.id.clone())
            .collect(),
        active_operation_id: projection
            .active_operation
            .as_ref()
            .map(|operation| operation.id.clone())
            .filter(|id| !id.is_empty()),
        active_operation_action: projection
            .active_operation
            .as_ref()
            .map(|operation| {
                if operation.action_kind.is_empty() {
                    operation.capability.clone()
                } else {
                    operation.action_kind.clone()
                }
            })
            .filter(|action| !action.is_empty()),
        active_operation_state: projection
            .active_operation
            .as_ref()
            .map(|operation| operation.state.clone())
            .filter(|state| !state.is_empty()),
        current_action_kind: projection
            .current_action
            .as_ref()
            .map(|action| action.kind.clone())
            .filter(|kind| !kind.is_empty()),
        verification_status: projection.verification.status.clone(),
        verification_check_statuses: projection
            .verification
            .checks
            .iter()
            .map(check_status)
            .map(str::to_owned)
            .collect(),
        attention_ids: projection
            .attention_items
            .iter()
            .map(|item| item.id.clone())
            .collect(),
        attention_requires_action: projection
            .attention_items
            .iter()
            .map(|item| item.requires_action)
            .collect(),
        progress,
        buddy_state: projection
            .buddy
            .as_ref()
            .map(|buddy| buddy.state.clone())
            .unwrap_or_else(|| mode.as_str().to_owned()),
        buddy_anchor: projection
            .buddy
            .as_ref()
            .map(|buddy| buddy.anchor.clone())
            .unwrap_or_default(),
        buddy_status: projection
            .buddy
            .as_ref()
            .map(|buddy| buddy.status.clone())
            .unwrap_or_default(),
        buddy_character: projection
            .buddy
            .as_ref()
            .map(|buddy| buddy.character.clone())
            .unwrap_or_default(),
        attention_rail: !projection.attention_items.is_empty(),
        unobscured_right: scene_safe_area(projection.attention_items.len())
            .unobscured_right
            .round() as u16,
        failure_diagnostic_locations: projection
            .diagnostics
            .iter()
            .filter_map(diagnostic_location)
            .collect(),
    }
}

#[cfg(test)]
fn semantic_entities(projection: &Projection) -> Vec<&ProjectionEntity> {
    let source = if !projection.runtime_entities.is_empty() {
        &projection.runtime_entities
    } else if !projection.entities.is_empty() {
        &projection.entities
    } else {
        return Vec::new();
    };
    source
        .iter()
        .filter(|entity| !entity.id.is_empty())
        .take(8)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::{
        AttentionAction, BuddyMotion, OiTarget, SCENE_HEIGHT, SCENE_WIDTH, SemanticSceneSnapshot,
        VisualMode, attention_hit_map, buddy_anchor_is_clear, buddy_target, motion_position,
        scene_safe_area, semantic_scene_snapshot,
    };
    use crate::buddy::SPRITE_DIRTY_WIDTH;
    use crate::{
        AnimationState, ProjectionAction, ProjectionBuddy, ProjectionCodeView,
        ProjectionDiagnostic, ProjectionOperation, ProjectionVerification,
    };
    use crate::{Projection, ProjectionAttention, ProjectionEntity};
    use athena_terminal::PixelRect;
    use std::cell::RefCell;
    use std::collections::BTreeMap;

    fn disabled_oi_target() -> OiTarget {
        OiTarget {
            framebuffer: 0,
            texture: 0,
            enabled: false,
            buddy_motion: RefCell::new(BuddyMotion::default()),
        }
    }

    fn entity(id: &str, kind: &str, label: &str, status: &str) -> ProjectionEntity {
        ProjectionEntity {
            id: id.to_owned(),
            kind: kind.to_owned(),
            label: label.to_owned(),
            status: status.to_owned(),
            ..ProjectionEntity::default()
        }
    }

    fn buddy(state: &str, anchor: &str, status: &str) -> ProjectionBuddy {
        ProjectionBuddy {
            state: state.to_owned(),
            anchor: anchor.to_owned(),
            status: status.to_owned(),
            character: "owl".to_owned(),
        }
    }

    fn operation(id: &str, action: &str, state: &str) -> ProjectionOperation {
        ProjectionOperation {
            id: id.to_owned(),
            label: action.to_owned(),
            operation: action.to_owned(),
            action_kind: action.to_owned(),
            state: state.to_owned(),
            ..ProjectionOperation::default()
        }
    }

    fn action(kind: &str) -> ProjectionAction {
        ProjectionAction {
            kind: kind.to_owned(),
            label: kind.to_owned(),
            ..ProjectionAction::default()
        }
    }

    fn semantic_fixture(name: &str) -> Projection {
        let mut projection = Projection {
            semantic_state: name.to_owned(),
            status: name.to_ascii_uppercase(),
            ..Projection::default()
        };
        match name {
            "idle" => {
                projection.buddy = Some(buddy("IDLE", "center", "ready"));
            }
            "thinking" => {
                projection.entities = vec![entity("plan", "task", "form plan", "active")];
                projection.active_operation = Some(operation("op-thinking", "think", "running"));
                projection.buddy = Some(buddy("THINKING", "center", "reasoning"));
            }
            "search" => {
                projection.entities = vec![
                    entity("workspace", "operation", "workspace", "active"),
                    entity("native", "task", "native.rs", "active"),
                ];
                projection.active_operation = Some(operation("op-search", "search", "running"));
                projection.current_action = Some(action("search"));
                projection.buddy = Some(buddy("SEARCHING", "left", "active"));
            }
            "inspect" => {
                projection.entities = vec![entity("workspace", "workspace", "workspace", "active")];
                projection.active_operation = Some(operation("op-inspect", "inspect", "running"));
                projection.current_action = Some(action("inspect"));
                projection.buddy = Some(buddy("INSPECTING", "left", "active"));
            }
            "read" => {
                projection.entities = vec![entity("native", "file", "native.rs", "reading")];
                projection.active_operation = Some(operation("op-read", "read", "running"));
                projection.current_action = Some(action("read"));
                projection.code_view = Some(ProjectionCodeView {
                    path: "native/src/main.rs".to_owned(),
                    language: "rust".to_owned(),
                    lines: vec!["fn main() {".to_owned(), "    run();".to_owned()],
                    ..ProjectionCodeView::default()
                });
                projection.buddy = Some(buddy("READING", "left", "active"));
            }
            "code" => {
                projection.entities = vec![entity("native", "file", "native.rs", "active")];
                projection.active_operation = Some(operation("op-code", "code", "running"));
                projection.current_action = Some(action("code"));
                projection.code_view = Some(ProjectionCodeView {
                    path: "native/src/main.rs".to_owned(),
                    language: "rust".to_owned(),
                    diff: vec!["+    render();".to_owned()],
                    mutation_state: "working".to_owned(),
                    ..ProjectionCodeView::default()
                });
                projection.buddy = Some(buddy("CODING", "right", "active"));
            }
            "execute" => {
                projection.entities = vec![
                    entity("execute", "task", "run command", "running"),
                    entity("child", "operation", "cargo test", "active"),
                ];
                let mut op = operation("op-execute", "execute", "running");
                op.progress = "halfway".to_owned();
                op.progress_value = Some(0.5);
                op.progress_determinate = true;
                projection.active_operation = Some(op);
                projection.current_action = Some(action("execute"));
                projection.buddy = Some(buddy("EXECUTING", "right", "active"));
            }
            "test" => {
                projection.entities = vec![entity("test-run", "task", "test suite", "running")];
                projection.active_operation = Some(operation("op-test", "test", "running"));
                projection.current_action = Some(action("test"));
                projection.verification = ProjectionVerification {
                    status: "RUNNING".to_owned(),
                    checks: vec![
                        serde_json::json!({"id":"unit","status":"passed"}),
                        serde_json::json!({"id":"integration","status":"running"}),
                    ],
                };
                projection.buddy = Some(buddy("TESTING", "right", "active"));
            }
            "verify" => {
                projection.entities = vec![entity("verify", "task", "release proof", "passed")];
                projection.active_operation = Some(operation("op-verify", "verify", "complete"));
                projection.current_action = Some(action("verify"));
                projection.verification = ProjectionVerification {
                    status: "PASSED".to_owned(),
                    checks: vec![
                        serde_json::json!({"id":"unit","status":"passed"}),
                        serde_json::json!({"id":"release","status":"passed"}),
                    ],
                };
                projection.buddy = Some(buddy("VERIFYING", "right", "checking"));
            }
            "approval" => {
                projection.entities =
                    vec![entity("write", "operation", "write workspace", "approval")];
                projection.active_operation = Some(operation("op-approval", "execute", "paused"));
                projection.current_action = Some(action("approval"));
                projection.attention_items = vec![ProjectionAttention {
                    id: "approval:apr-1".to_owned(),
                    approval_id: "apr-1".to_owned(),
                    kind: "approval".to_owned(),
                    severity: "warning".to_owned(),
                    requires_action: true,
                    scopes: vec!["call".to_owned(), "task".to_owned()],
                    ..ProjectionAttention::default()
                }];
                projection.buddy = Some(buddy("APPROVAL", "left", "paused"));
            }
            "failure" => {
                projection.entities = vec![
                    entity("failed-call", "operation", "compile", "failed"),
                    entity("unrelated-success", "task", "lint", "success"),
                ];
                projection.active_operation = Some(operation("op-failure", "execute", "failed"));
                projection.current_action = Some(action("execute"));
                projection.diagnostics = vec![ProjectionDiagnostic {
                    path: "src/main.rs".to_owned(),
                    line: Some(42),
                    message: "compile error".to_owned(),
                    severity: "error".to_owned(),
                    ..ProjectionDiagnostic::default()
                }];
                projection.attention_items = vec![ProjectionAttention {
                    id: "event:failure".to_owned(),
                    kind: "notification".to_owned(),
                    severity: "failure".to_owned(),
                    ..ProjectionAttention::default()
                }];
                projection.buddy = Some(buddy("FAILURE", "right", "blocked"));
            }
            "recover" => {
                projection.entities = vec![
                    entity("failed-call", "operation", "compile", "failed"),
                    entity("recovery", "task", "recover workspace", "active"),
                ];
                projection.active_operation = Some(operation("op-recover", "recover", "running"));
                projection.current_action = Some(action("recover"));
                projection.buddy = Some(buddy("RECOVERING", "right", "repairing"));
            }
            "success" => {
                projection.entities = vec![entity("execute", "operation", "release", "success")];
                projection.active_operation = Some(operation("op-success", "execute", "complete"));
                projection.current_action = Some(action("success"));
                projection.verification = ProjectionVerification {
                    status: "PASSED".to_owned(),
                    checks: vec![serde_json::json!({"id":"release","status":"passed"})],
                };
                projection.buddy = Some(buddy("SUCCESS", "right", "complete"));
            }
            _ => panic!("unknown semantic fixture {name}"),
        }
        projection
    }

    #[test]
    fn buddy_motion_eases_from_fixed_origin_to_target() {
        let motion = BuddyMotion {
            initialized: true,
            mode: VisualMode::Search,
            from: (10.0, 20.0),
            current: (10.0, 20.0),
            target: (110.0, 80.0),
            started_at: 2.0,
        };
        assert_eq!(motion_position(&motion, 2.0), (10.0, 20.0));
        assert_eq!(motion_position(&motion, 2.36), (110.0, 80.0));
        let midpoint = motion_position(&motion, 2.18);
        assert!(midpoint.0 > 10.0 && midpoint.0 < 110.0);
        assert!(midpoint.1 > 20.0 && midpoint.1 < 80.0);
    }

    #[test]
    fn dagoal_temporal_fixtures_are_fixed_timestamped_and_safe() {
        let fixtures = [
            "idle", "thinking", "search", "read", "code", "execute", "test", "verify", "approval",
            "failure", "success",
        ];
        let timestamps = [0.0_f32, 0.10, 0.36, 0.42, 0.70];
        for (sequence, name) in fixtures.into_iter().enumerate() {
            let projection = semantic_fixture(name);
            let mode = VisualMode::from_projection(&projection);
            let mut animation = AnimationState::default();
            animation.observe(mode.as_str().to_owned(), sequence as u64);
            assert_eq!(animation.elapsed, 0.0, "{name} must enter at t=0");
            assert_eq!(
                animation.transition_progress, 0.0,
                "{name} must enter at t=0"
            );

            let mut prior = 0.0;
            for timestamp in timestamps {
                animation.advance(timestamp - prior);
                prior = timestamp;
                if mode.is_active() && timestamp > 0.0 {
                    assert!(
                        animation.channel(mode, 0.0) > 0.0,
                        "{name} has no deterministic activity at t={timestamp}"
                    );
                }
            }
            if matches!(mode, VisualMode::Approval | VisualMode::Failure) {
                assert!(!mode.is_animated(&Projection {
                    animation: animation.clone(),
                    ..projection.clone()
                }));
            }

            let safe_area = scene_safe_area(projection.attention_items.len());
            let full_right = SCENE_WIDTH - 8.0;
            assert!(safe_area.unobscured_right <= full_right);
            if let Some(rail) = safe_area.attention_rail {
                assert!(rail.x >= 0.0 && rail.y >= 0.0);
                assert!(rail.right() <= SCENE_WIDTH && rail.bottom() <= SCENE_HEIGHT);
                assert!(rail.right() < safe_area.unobscured_right);
            }
            let target = buddy_target(&projection, mode, safe_area.unobscured_right);
            assert!(buddy_anchor_is_clear(
                target,
                &projection,
                safe_area.unobscured_right
            ));

            let snapshot = semantic_scene_snapshot(&projection);
            let packet_mode = matches!(
                mode,
                VisualMode::Execute | VisualMode::Generate | VisualMode::Recover
            );
            assert_eq!(snapshot.focal_object.ends_with("-packets"), packet_mode);
            assert_eq!(
                snapshot.progress.determinate,
                matches!(mode, VisualMode::Execute)
            );
        }
    }

    #[test]
    fn dagoal_transition_sequence_preserves_buddy_continuity_then_settles() {
        let transitions = [
            ("search", "read"),
            ("read", "code"),
            ("code", "test"),
            ("test", "failure"),
            ("failure", "recover"),
            ("verify", "success"),
            ("execute", "approval"),
            ("approval", "execute"),
        ];
        let target = disabled_oi_target();
        let mut phase = 0.0;
        for (from_name, to_name) in transitions {
            let from = semantic_fixture(from_name);
            let to = semantic_fixture(to_name);
            let from_mode = VisualMode::from_projection(&from);
            let to_mode = VisualMode::from_projection(&to);
            let before = target.buddy_position(&from, from_mode, phase, true);
            let at_entry = target.buddy_position(&to, to_mode, phase, true);
            let safe_right = scene_safe_area(to.attention_items.len()).unobscured_right;
            let destination = buddy_target(&to, to_mode, safe_right);
            let half_width = SPRITE_DIRTY_WIDTH / 2.0;
            let rendered_destination = (
                destination
                    .0
                    .clamp(half_width, (safe_right - half_width).max(half_width)),
                destination.1,
            );
            let rendered_destination = (
                rendered_destination.0.round(),
                rendered_destination.1.round(),
            );
            let old_safe_right = scene_safe_area(from.attention_items.len()).unobscured_right;
            let safe_area_changed = (old_safe_right - safe_right).abs() > 0.001;
            let motion_start = if safe_area_changed {
                let reflowed_x = before
                    .0
                    .clamp(half_width, (safe_right - half_width).max(half_width));
                assert!(
                    (at_entry.0 - reflowed_x).abs() < 0.001,
                    "{from_name}->{to_name} crossed the new safe area: before={before:?} entry={at_entry:?} safe_right={safe_right}"
                );
                at_entry
            } else {
                assert!(
                    (before.0 - at_entry.0).abs() < 0.001,
                    "{from_name}->{to_name} jumped"
                );
                before
            };
            let midpoint = target.buddy_position(&to, to_mode, phase + 0.18, true);
            if (rendered_destination.0 - motion_start.0).abs() > 4.0 {
                assert!(
                    midpoint.0 > motion_start.0.min(rendered_destination.0)
                        && midpoint.0 < motion_start.0.max(rendered_destination.0),
                    "{from_name}->{to_name} did not ease horizontally: before={before:?} midpoint={midpoint:?} destination={rendered_destination:?} phase={phase}"
                );
            } else if (rendered_destination.1 - motion_start.1).abs() > 4.0 {
                assert!(
                    midpoint.1 > motion_start.1.min(rendered_destination.1)
                        && midpoint.1 < motion_start.1.max(rendered_destination.1),
                    "{from_name}->{to_name} did not ease vertically: before={before:?} midpoint={midpoint:?} destination={rendered_destination:?} phase={phase}"
                );
            }
            let settled = target.buddy_position(&to, to_mode, phase + 0.36, true);
            assert!(
                (settled.0 - rendered_destination.0).abs() < 0.001,
                "{to_name} did not settle: settled={settled:?} destination={rendered_destination:?} phase={phase}"
            );
            phase += 1.0;
        }
    }

    #[test]
    fn reduced_motion_snaps_buddy_without_bob_or_intermediate_motion() {
        let target = disabled_oi_target();
        let search = semantic_fixture("search");
        let read = semantic_fixture("read");
        let search_mode = VisualMode::from_projection(&search);
        let read_mode = VisualMode::from_projection(&read);
        let _ = target.buddy_position(&search, search_mode, 0.0, true);
        let actual = target.buddy_position(&read, read_mode, 10.0, false);
        let expected = buddy_target(
            &read,
            read_mode,
            scene_safe_area(read.attention_items.len()).unobscured_right,
        );
        assert_eq!(actual, (expected.0.round(), expected.1.round()));
    }

    #[test]
    fn enlarged_buddy_chooses_an_open_lane_for_projected_nodes() {
        let projection = Projection {
            entities: vec![
                ProjectionEntity {
                    id: "root".to_owned(),
                    kind: "task".to_owned(),
                    ..ProjectionEntity::default()
                },
                ProjectionEntity {
                    id: "child".to_owned(),
                    ..ProjectionEntity::default()
                },
            ],
            ..Projection::default()
        };
        let target = buddy_target(&projection, VisualMode::Search, SCENE_WIDTH - 8.0);
        assert_ne!(target, (112.0, 105.0));
        assert!(buddy_anchor_is_clear(
            target,
            &projection,
            SCENE_WIDTH - 8.0
        ));
    }

    #[test]
    fn approval_hit_map_uses_canonical_ids_scopes_and_physical_transform() {
        let projection = Projection {
            attention_items: vec![ProjectionAttention {
                id: "approval:apr_1".to_owned(),
                approval_id: "apr_1".to_owned(),
                requires_action: true,
                scopes: vec!["call".to_owned(), "task".to_owned()],
                ..ProjectionAttention::default()
            }],
            ..Projection::default()
        };
        let map = attention_hit_map(&projection);
        let oi_inner = PixelRect {
            x: 100.0,
            y: 50.0,
            width: 768.0,
            height: 512.0,
        };
        let physical = |logical_x: f32, logical_y: f32| {
            (
                oi_inner.x + logical_x / SCENE_WIDTH * oi_inner.width,
                oi_inner.y + logical_y / SCENE_HEIGHT * oi_inner.height,
            )
        };

        let (x, y) = physical(52.0, 229.0);
        assert_eq!(
            map.hit_physical(x, y, oi_inner),
            Some(&AttentionAction::Approve {
                approval_id: "apr_1".to_owned(),
                scope: "call".to_owned(),
            })
        );
        let (x, y) = physical(114.0, 229.0);
        assert_eq!(
            map.hit_physical(x, y, oi_inner),
            Some(&AttentionAction::Approve {
                approval_id: "apr_1".to_owned(),
                scope: "task".to_owned(),
            })
        );
        let (x, y) = physical(176.0, 229.0);
        assert_eq!(
            map.hit_physical(x, y, oi_inner),
            Some(&AttentionAction::Deny {
                approval_id: "apr_1".to_owned(),
            })
        );
        assert!(map.hit_physical(oi_inner.x, oi_inner.y, oi_inner).is_none());
    }

    #[test]
    fn approval_hit_map_rejects_display_only_authority_identity() {
        let projection = Projection {
            attention_items: vec![ProjectionAttention {
                id: "approval:display-only-id".to_owned(),
                requires_action: true,
                ..ProjectionAttention::default()
            }],
            ..Projection::default()
        };
        let map = attention_hit_map(&projection);
        let oi_inner = PixelRect {
            x: 0.0,
            y: 0.0,
            width: 384.0,
            height: 256.0,
        };
        assert!(map.hit_physical(278.0, 67.0, oi_inner).is_none());
    }

    #[test]
    fn dagoal_semantic_scene_goldens_cover_all_required_states() {
        let goldens: BTreeMap<String, SemanticSceneSnapshot> = serde_json::from_str(include_str!(
            "../../../assets/oi/semantic-scene-goldens.json"
        ))
        .expect("semantic scene goldens should be valid JSON");
        let required = [
            "idle", "thinking", "search", "inspect", "read", "code", "execute", "test", "verify",
            "approval", "failure", "recover", "success",
        ];
        assert_eq!(goldens.len(), required.len());
        for name in required {
            let projection = semantic_fixture(name);
            let expected = goldens
                .get(name)
                .unwrap_or_else(|| panic!("missing semantic golden {name}"));
            let actual = semantic_scene_snapshot(&projection);
            assert_eq!(&actual, expected, "semantic scene mismatch for {name}");
            assert!(
                buddy_anchor_is_clear(
                    buddy_target(
                        &projection,
                        VisualMode::from_projection(&projection),
                        scene_safe_area(projection.attention_items.len()).unobscured_right,
                    ),
                    &projection,
                    scene_safe_area(projection.attention_items.len()).unobscured_right,
                ),
                "Buddy overlaps a semantic node in {name}"
            );
        }
    }

    #[test]
    fn failure_scene_keeps_localized_failure_and_unrelated_success_without_progress() {
        let snapshot = semantic_scene_snapshot(&semantic_fixture("failure"));
        assert_eq!(snapshot.failed_entity_ids, vec!["failed-call"]);
        assert_eq!(snapshot.successful_entity_ids, vec!["unrelated-success"]);
        assert_eq!(
            snapshot.failure_diagnostic_locations,
            vec!["src/main.rs:42"]
        );
        assert!(!snapshot.progress.determinate);
        assert_eq!(snapshot.progress.value_percent, None);
    }

    #[test]
    fn verification_scene_projects_the_same_gate_statuses_as_the_evidence() {
        let snapshot = semantic_scene_snapshot(&semantic_fixture("verify"));
        assert_eq!(snapshot.focal_object, "verification-gate");
        assert_eq!(snapshot.verification_status, "PASSED");
        assert_eq!(
            snapshot.verification_check_statuses,
            vec!["passed", "passed"]
        );
        assert_eq!(snapshot.buddy_state, "VERIFYING");
    }
}
