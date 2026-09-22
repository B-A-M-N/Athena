//! Buddy motion state and easing.

use super::layout::runtime_entity_nodes;
use crate::buddy::{SPRITE_HEIGHT, SPRITE_SCALE, SPRITE_WIDTH};
use crate::render::primitives::draw_rect;
use crate::{Projection, VisualMode};

pub(crate) struct BuddyMotion {
    pub(crate) initialized: bool,
    pub(crate) mode: VisualMode,
    pub(crate) from: (f32, f32),
    pub(crate) current: (f32, f32),
    pub(crate) target: (f32, f32),
    pub(crate) started_at: f32,
}

impl Default for BuddyMotion {
    fn default() -> Self {
        Self {
            initialized: false,
            mode: VisualMode::Idle,
            from: (0.0, 0.0),
            current: (0.0, 0.0),
            target: (0.0, 0.0),
            started_at: 0.0,
        }
    }
}

pub(crate) fn motion_position(motion: &BuddyMotion, phase: f32) -> (f32, f32) {
    let progress = ((phase - motion.started_at) / 0.36).clamp(0.0, 1.0);
    let eased = 1.0 - (1.0 - progress).powi(3);
    (
        motion.from.0 + (motion.target.0 - motion.from.0) * eased,
        motion.from.1 + (motion.target.1 - motion.from.1) * eased,
    )
}

pub(crate) type PacketEdge = ((f32, f32), (f32, f32));

pub(crate) fn draw_packets(
    edges: &[PacketEdge],
    phase: f32,
    color: (f32, f32, f32),
    reverse: bool,
) {
    for (index, ((start_x, start_y), (end_x, end_y))) in edges.iter().enumerate() {
        let mut t = (phase * 0.42 + index as f32 * 0.17).fract();
        if reverse {
            t = 1.0 - t;
        }
        let x = *start_x + (*end_x - *start_x) * t;
        let y = *start_y + (*end_y - *start_y) * t;
        draw_rect(x - 1.0, y - 1.0, 2.0, 2.0, color);
    }
}

pub(crate) fn buddy_target(projection: &Projection, mode: VisualMode, right: f32) -> (f32, f32) {
    let stage_x = (right * 0.70).clamp(242.0, right - 40.0);
    let preferred = match mode {
        VisualMode::Failure => (stage_x, 190.0),
        VisualMode::Approval => (stage_x, 194.0),
        VisualMode::Success => (stage_x, 194.0),
        VisualMode::Code => (stage_x, 186.0),
        VisualMode::Test | VisualMode::Verify | VisualMode::Execute => (stage_x, 190.0),
        VisualMode::Search | VisualMode::Inspect => (stage_x, 190.0),
        VisualMode::Read => (stage_x, 190.0),
        VisualMode::Think | VisualMode::Respond | VisualMode::Generate | VisualMode::Recover => {
            (stage_x, 190.0)
        }
        VisualMode::Idle => match projection
            .buddy
            .as_ref()
            .map(|buddy| buddy.anchor.to_ascii_lowercase())
            .as_deref()
        {
            Some("left") => (246.0, 184.0),
            Some("center") => (270.0, 184.0),
            _ => (stage_x, 190.0),
        },
    };

    let candidates = [
        preferred,
        (196.0, 222.0),
        (196.0, 120.0),
        (52.0, 222.0),
        ((right - 52.0).max(150.0), 222.0),
    ];
    candidates
        .into_iter()
        .find(|candidate| buddy_anchor_is_clear(*candidate, projection, right))
        .unwrap_or(preferred)
}

pub(crate) fn buddy_anchor_is_clear(
    candidate: (f32, f32),
    projection: &Projection,
    right: f32,
) -> bool {
    let half_width = SPRITE_WIDTH * SPRITE_SCALE / 2.0 + 8.0;
    let half_height = SPRITE_HEIGHT * SPRITE_SCALE / 2.0 + 8.0;
    let entities = runtime_entity_nodes(projection);
    entities.iter().enumerate().all(|(index, entity)| {
        let node_x = if index % 2 == 0 {
            72.0
        } else {
            (right - 48.0).max(150.0)
        };
        let node_y = 154.0 + (index / 2) as f32 * 38.0;
        let node_radius = if entity.kind.eq_ignore_ascii_case("task") {
            20.0
        } else if entity.parent_id.is_some() {
            16.0
        } else {
            18.0
        };
        (candidate.0 - node_x).abs() >= half_width + node_radius
            || (candidate.1 - node_y).abs() >= half_height + node_radius
    })
}
