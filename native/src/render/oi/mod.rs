//! OI scene boundary and focused rendering components.

pub(crate) const SCENE_WIDTH: f32 = 384.0;
pub(crate) const SCENE_HEIGHT: f32 = 256.0;

pub(crate) mod attention;

#[path = "compose.rs"]
mod compose;
#[path = "layout.rs"]
pub(crate) mod layout;
pub(crate) use compose::OiSceneContext;
#[path = "motion.rs"]
pub(crate) mod motion;
#[path = "scenes.rs"]
pub(crate) mod scenes;
#[path = "target.rs"]
pub(crate) mod target;
#[path = "telemetry.rs"]
pub(crate) mod telemetry;
pub(crate) use attention::{AttentionAction, attention_hit_map};
pub(crate) use compose::draw_oi_scene;
#[cfg(test)]
pub(crate) use compose::entity_label;
pub(crate) use layout::{
    ATTENTION_PAGE_SIZE, SceneLayout, SceneSafeArea, attention_page_contains, attention_page_range,
    cycle_attention_page, scene_layout, scene_safe_area,
};
#[cfg(test)]
pub(crate) use motion::buddy_anchor_is_clear;
pub(crate) use motion::buddy_target;
pub(crate) use motion::{BuddyMotion, motion_position};
pub(crate) use target::{OiTarget, dump_framebuffer};
