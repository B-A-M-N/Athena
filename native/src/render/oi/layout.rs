//! Logical OI geometry and attention-rail layout.

use super::{SCENE_HEIGHT, SCENE_WIDTH};
use crate::Projection;
use crate::{ProjectionEntity, ProjectionTreeNode};
use athena_terminal::PixelRect;

#[derive(Clone, Copy, Debug)]
pub(crate) struct SceneSafeArea {
    pub(crate) unobscured_right: f32,
    pub(crate) attention_rail: Option<PixelRect>,
}

pub(crate) fn scene_safe_area(attention_count: usize) -> SceneSafeArea {
    let full = PixelRect {
        x: 8.0,
        y: 8.0,
        width: SCENE_WIDTH - 16.0,
        height: SCENE_HEIGHT - 16.0,
    };
    if attention_count == 0 {
        return SceneSafeArea {
            unobscured_right: full.right(),
            attention_rail: None,
        };
    }
    SceneSafeArea {
        unobscured_right: full.right(),
        attention_rail: Some(PixelRect {
            x: 12.0,
            y: 174.0,
            width: 206.0,
            height: 76.0,
        }),
    }
}

pub(crate) const ATTENTION_PAGE_SIZE: usize = 3;

pub(crate) fn attention_page_range(projection: &Projection) -> (usize, usize) {
    let page_count = projection
        .attention_items
        .len()
        .div_ceil(ATTENTION_PAGE_SIZE)
        .max(1);
    let page = projection.attention_page.min(page_count - 1);
    let start = page * ATTENTION_PAGE_SIZE;
    (
        start,
        (start + ATTENTION_PAGE_SIZE).min(projection.attention_items.len()),
    )
}

pub(crate) fn attention_page_contains(
    projection: &Projection,
    physical_x: f32,
    physical_y: f32,
    oi_inner: PixelRect,
) -> bool {
    let Some(rail) = scene_safe_area(projection.attention_items.len()).attention_rail else {
        return false;
    };
    let scale_x = oi_inner.width / SCENE_WIDTH;
    let scale_y = oi_inner.height / SCENE_HEIGHT;
    if scale_x <= 0.0 || scale_y <= 0.0 {
        return false;
    }
    let scene_x = (physical_x - oi_inner.x) / scale_x;
    let scene_y = (physical_y - oi_inner.y) / scale_y;
    scene_x >= rail.x && scene_x < rail.right() && scene_y >= rail.y && scene_y < rail.bottom()
}

pub(crate) fn cycle_attention_page(projection: &mut Projection, delta: i32) -> bool {
    let page_count = projection
        .attention_items
        .len()
        .div_ceil(ATTENTION_PAGE_SIZE)
        .max(1);
    if page_count <= 1 {
        return false;
    }
    projection.attention_page =
        (projection.attention_page as i32 + delta).rem_euclid(page_count as i32) as usize;
    true
}

const HEADER_TOP: f32 = 8.0;
const HEADER_HEIGHT: f32 = 22.0;
const MARGIN: f32 = 10.0;

#[derive(Clone, Copy, Debug)]
#[allow(dead_code)]
pub(crate) struct SceneLayout {
    pub(crate) header: PixelRect,
    pub(crate) telemetry: PixelRect,
    pub(crate) world: PixelRect,
    pub(crate) stage: PixelRect,
}

pub(crate) fn scene_layout(_safe_area: SceneSafeArea) -> SceneLayout {
    let full = PixelRect {
        x: MARGIN,
        y: HEADER_TOP + HEADER_HEIGHT + 4.0,
        width: (SCENE_WIDTH - MARGIN * 2.0).max(20.0),
        height: SCENE_HEIGHT - HEADER_TOP - HEADER_HEIGHT - 14.0,
    };
    let header = PixelRect {
        x: MARGIN,
        y: HEADER_TOP,
        width: full.width,
        height: HEADER_HEIGHT,
    };
    let telemetry = PixelRect {
        x: 12.0,
        y: 40.0,
        width: 150.0,
        height: 112.0,
    };
    let world = PixelRect {
        x: 170.0,
        y: 38.0,
        width: (full.right() - 170.0).max(24.0),
        height: full.bottom() - 38.0,
    };
    SceneLayout {
        header,
        telemetry,
        world,
        stage: world,
    }
}

#[derive(Clone, Debug)]
pub(crate) struct TreeLayoutNode {
    pub(crate) id: String,
    pub(crate) status: String,
    pub(crate) x: f32,
    pub(crate) y: f32,
    pub(crate) radius: f32,
    pub(crate) children: Vec<usize>,
    pub(crate) parent: Option<usize>,
}

pub(crate) fn layout_tree(tree: &[ProjectionTreeNode], stage: PixelRect) -> Vec<TreeLayoutNode> {
    let mut nodes: Vec<TreeLayoutNode> = Vec::new();
    let mut stack: Vec<(usize, usize, f32, f32, f32)> = Vec::new();
    for (root_index, _root) in tree.iter().enumerate() {
        let root_x = stage.x + stage.width * (0.25 + (root_index as f32 * 0.25).min(0.5));
        let root_y = stage.y + stage.height * 0.18;
        stack.push((usize::MAX, root_index, root_x, root_y, stage.width * 0.35));
    }
    while let Some((parent, tree_index, x, y, spread)) = stack.pop() {
        let node = &tree[tree_index];
        let current_index = nodes.len();
        nodes.push(TreeLayoutNode {
            id: node.id.clone(),
            status: node.status.clone(),
            x,
            y,
            radius: if node.kind.eq_ignore_ascii_case("directory") {
                7.0
            } else {
                5.0
            },
            children: Vec::new(),
            parent: if parent == usize::MAX {
                None
            } else {
                Some(parent)
            },
        });
        if parent != usize::MAX {
            nodes[parent].children.push(current_index);
        }
        let child_count = node.children.len();
        if child_count == 0 {
            continue;
        }
        let step = spread / (child_count as f32).max(1.0);
        let start_x = x - step * (child_count.saturating_sub(1) as f32) * 0.5;
        let child_y = y + stage.height * 0.18;
        for (child_i, child) in node.children.iter().enumerate() {
            let Some(child_index) = tree.iter().position(|node| node.id == child.id) else {
                continue;
            };
            let child_x = start_x + child_i as f32 * step;
            stack.push((current_index, child_index, child_x, child_y, spread * 0.55));
        }
    }
    nodes
}

pub(crate) fn runtime_entity_nodes(projection: &Projection) -> Vec<&ProjectionEntity> {
    let source = if !projection.runtime_entities.is_empty() {
        &projection.runtime_entities
    } else {
        &projection.entities
    };
    source
        .iter()
        .filter(|entity| !entity.id.is_empty())
        .take(8)
        .collect()
}

pub(crate) fn layout_runtime_graph<'a>(
    entities: &'a [&'a ProjectionEntity],
    stage: PixelRect,
) -> Vec<(f32, f32, f32, &'a ProjectionEntity)> {
    let count = entities.len();
    if count == 0 {
        return Vec::new();
    }
    let columns = (count as f32).sqrt().ceil().max(1.0) as usize;
    let col_step = stage.width / (columns.max(2) as f32);
    let row_count = count.div_ceil(columns.max(1));
    let row_step = stage.height * 0.55 / (row_count.max(2) as f32);
    entities
        .iter()
        .enumerate()
        .map(|(index, entity)| {
            let col = index % columns;
            let row = index / columns;
            let x = stage.x + col_step * 0.5 + col as f32 * col_step;
            let y = stage.y + stage.height * 0.22 + row as f32 * row_step;
            let radius = if entity.kind.eq_ignore_ascii_case("task") {
                12.0
            } else {
                9.0
            };
            (x, y, radius, *entity)
        })
        .collect()
}
