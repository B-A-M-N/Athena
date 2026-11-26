//! Operator-attention interaction model for the OI scene.

use super::compose::pixel_text;
use super::{SCENE_HEIGHT, SCENE_WIDTH, attention_page_range, scene_safe_area};
use crate::render::primitives::{draw_round_outline, draw_round_rect};
use crate::render::theme::{AMBER, DIM, FAILURE, SECONDARY};
use crate::{Projection, ProjectionAttention};
use athena_terminal::PixelRect;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum AttentionAction {
    Approve { approval_id: String, scope: String },
    Deny { approval_id: String },
}

#[derive(Clone, Debug, PartialEq)]
struct AttentionHit {
    rect: PixelRect,
    action: AttentionAction,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub(crate) struct AttentionHitMap {
    hits: Vec<AttentionHit>,
}

impl AttentionHitMap {
    fn hit_logical(&self, x: f32, y: f32) -> Option<&AttentionAction> {
        self.hits
            .iter()
            .find(|hit| {
                x >= hit.rect.x && x < hit.rect.right() && y >= hit.rect.y && y < hit.rect.bottom()
            })
            .map(|hit| &hit.action)
    }

    pub(crate) fn hit_physical(
        &self,
        x: f32,
        y: f32,
        oi_inner: PixelRect,
    ) -> Option<&AttentionAction> {
        if oi_inner.width <= 0.0
            || oi_inner.height <= 0.0
            || x < oi_inner.x
            || x >= oi_inner.right()
            || y < oi_inner.y
            || y >= oi_inner.bottom()
        {
            return None;
        }
        let u = ((x - oi_inner.x) / oi_inner.width).clamp(0.0, 1.0);
        let v = ((y - oi_inner.y) / oi_inner.height).clamp(0.0, 1.0);
        let (content_u, content_v) = barrel_uv(u, v);
        self.hit_logical(content_u * SCENE_WIDTH, content_v * SCENE_HEIGHT)
    }
}

fn barrel_uv(u: f32, v: f32) -> (f32, f32) {
    let dx = u * 2.0 - 1.0;
    let dy = v * 2.0 - 1.0;
    let radius = dx * dx + dy * dy;
    let strength = 0.035;
    let scale = 1.0 + strength * radius;
    ((dx * scale + 1.0) * 0.5, (dy * scale + 1.0) * 0.5)
}

fn scope_label(scope: &str) -> &'static str {
    match scope.to_ascii_lowercase().as_str() {
        "call" => "ONCE",
        "task" => "TASK",
        "session" => "SESS",
        "project" => "PROJ",
        "profile" => "PROF",
        _ => "ALLOW",
    }
}

pub(crate) fn attention_button_rects(
    item: &ProjectionAttention,
    left: f32,
    top: f32,
    right: f32,
) -> Vec<(PixelRect, &'static str, AttentionAction)> {
    if item.approval_id.is_empty() {
        return Vec::new();
    }
    let approval_id = item.approval_id.clone();
    let scopes: Vec<String> = if item.scopes.is_empty() {
        vec!["call".to_owned()]
    } else {
        item.scopes
            .iter()
            .filter(|scope| !scope.is_empty())
            .cloned()
            .collect()
    };
    let mut specs = scopes
        .iter()
        .map(|scope| {
            (
                scope_label(scope),
                AttentionAction::Approve {
                    approval_id: approval_id.clone(),
                    scope: scope.clone(),
                },
            )
        })
        .collect::<Vec<_>>();
    specs.push(("DENY", AttentionAction::Deny { approval_id }));
    let gap = 2.0;
    let width =
        ((right - left) - gap * (specs.len().saturating_sub(1) as f32)) / specs.len() as f32;
    specs
        .into_iter()
        .enumerate()
        .map(|(index, (label, action))| {
            (
                PixelRect {
                    x: left + index as f32 * (width + gap),
                    y: top,
                    width,
                    height: 14.0,
                },
                label,
                action,
            )
        })
        .collect()
}

pub(crate) fn attention_hit_map(projection: &Projection) -> AttentionHitMap {
    if projection.stale
        || (!projection.bridge_status.is_empty() && projection.bridge_status != "CONNECTED")
    {
        return AttentionHitMap::default();
    }
    let Some(rail) = scene_safe_area(projection.attention_items.len()).attention_rail else {
        return AttentionHitMap::default();
    };
    let mut hits = Vec::new();
    let mut y = rail.y + 12.0;
    let (start, end) = attention_page_range(projection);
    for item in projection.attention_items[start..end].iter().take(1) {
        if item.requires_action {
            hits.extend(
                attention_button_rects(item, rail.x + 10.0, y + 36.0, rail.right() - 10.0)
                    .into_iter()
                    .map(|(rect, _, action)| AttentionHit { rect, action }),
            );
        }
        y += 62.0;
    }
    AttentionHitMap { hits }
}

pub(crate) fn draw_attention_rail(projection: &Projection, safe_area: super::SceneSafeArea) {
    let Some(rail) = safe_area.attention_rail else {
        return;
    };
    draw_round_rect(
        rail.x,
        rail.y,
        rail.width,
        rail.height,
        5.0,
        (0.014, 0.035, 0.042),
    );
    draw_round_outline(rail.x, rail.y, rail.width, rail.height, (0.16, 0.32, 0.34));
    let (start, end) = attention_page_range(projection);
    let mut y = rail.y + 12.0;
    for item in projection.attention_items[start..end].iter().take(1) {
        let accent = match item.severity.to_ascii_lowercase().as_str() {
            "failure" | "error" => crate::render::theme::rgb(FAILURE),
            "warning" | "approval" => crate::render::theme::rgb(AMBER),
            _ => crate::render::theme::rgb(SECONDARY),
        };
        draw_round_rect(
            rail.x + 6.0,
            y,
            rail.width - 12.0,
            54.0,
            3.0,
            (0.020, 0.047, 0.052),
        );
        draw_round_outline(rail.x + 6.0, y, rail.width - 12.0, 54.0, accent);
        let title = if item.title.is_empty() {
            &item.kind
        } else {
            &item.title
        };
        pixel_text(rail.x + 10.0, y + 10.0, title, accent, rail.right() - 8.0);
        pixel_text(
            rail.x + 10.0,
            y + 20.0,
            &item.summary,
            crate::render::theme::rgb(SECONDARY),
            rail.right() - 8.0,
        );
        if item.requires_action {
            let buttons =
                attention_button_rects(item, rail.x + 10.0, y + 36.0, rail.right() - 10.0);
            for (rect, label, _) in buttons {
                draw_round_outline(rect.x, rect.y, rect.width, rect.height, accent);
                pixel_text(
                    rect.x + 3.0,
                    rect.y + 4.0,
                    label,
                    accent,
                    rect.right() - 2.0,
                );
            }
        } else if let Some(related) = item.related_object_id.as_deref() {
            pixel_text(
                rail.x + 10.0,
                y + 42.0,
                related,
                crate::render::theme::rgb(DIM),
                rail.right() - 8.0,
            );
        } else if !item.id.is_empty() {
            pixel_text(
                rail.x + 10.0,
                y + 42.0,
                &item.id,
                crate::render::theme::rgb(DIM),
                rail.right() - 8.0,
            );
        }
        y += 62.0;
    }
    let total = projection.attention_items.len();
    if total > super::ATTENTION_PAGE_SIZE {
        let page = start / super::ATTENTION_PAGE_SIZE + 1;
        let pages = total.div_ceil(super::ATTENTION_PAGE_SIZE);
        pixel_text(
            rail.x + 8.0,
            rail.bottom() - 6.0,
            &format!("PAGE {page}/{pages}  SCROLL"),
            crate::render::theme::rgb(SECONDARY),
            rail.right() - 6.0,
        );
        if end < total {
            pixel_text(
                rail.x + 8.0,
                rail.bottom() - 18.0,
                &format!("+{} MORE", total - end),
                crate::render::theme::rgb(AMBER),
                rail.right() - 6.0,
            );
        }
    }
    if projection.stale {
        pixel_text(
            rail.x + 8.0,
            rail.y + 4.0,
            &format!("BRIDGE {} // STALE", projection.bridge_status),
            crate::render::theme::rgb(FAILURE),
            rail.right() - 6.0,
        );
    }
}
