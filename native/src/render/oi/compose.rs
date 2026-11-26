use super::*;
#[cfg(test)]
use crate::ProjectionEntity;
use crate::platform::*;
use crate::render::buddy::draw_buddy;
use crate::render::chassis::{BitmapTextStyle, PresentationSettings, draw_bitmap_text_styled};
use crate::render::primitives::{draw_line_alpha, draw_rect, draw_round_outline};
use crate::render::theme::GLASS_BACKGROUND;
use crate::x11::*;
use crate::{Projection, VisualMode};

/// Render the OI in a deliberately small logical scene, then scale it into
/// the physical aperture. Keeping all scene coordinates in this space gives
/// DAGOAL one coherent pixel grammar instead of a collection of high-DPI
/// diagnostic vectors.
pub(crate) struct OiSceneContext<'a> {
    pub(crate) target: &'a OiTarget,
    pub(crate) frame_width: i32,
    pub(crate) frame_height: i32,
    pub(crate) x: f32,
    pub(crate) y: f32,
    pub(crate) width: f32,
    pub(crate) height: f32,
    pub(crate) projection: &'a Projection,
    pub(crate) effect_phase: f32,
    pub(crate) motion_time: f32,
    pub(crate) options: &'a RendererOptions,
    pub(crate) presentation: PresentationSettings,
    pub(crate) stencil_available: bool,
}

pub(crate) fn draw_oi_scene(context: OiSceneContext<'_>) {
    let OiSceneContext {
        target,
        frame_width,
        frame_height,
        x,
        y,
        width,
        height,
        projection,
        effect_phase,
        motion_time,
        options,
        presentation,
        stencil_available,
    } = context;
    let mode = VisualMode::from_projection(projection);
    let buddy_position = target.buddy_position(
        projection,
        mode,
        motion_time,
        options.animations && !options.reduced_motion,
    );
    let safe_area = scene_safe_area(projection.attention_items.len());
    let buddy_position = super::motion::buddy_anchor_is_clear(
        buddy_position,
        projection,
        safe_area.unobscured_right,
    )
    .then_some(buddy_position);
    if target.enabled() {
        unsafe {
            // The caller has already established the CRT clip on the window
            // framebuffer. A separate target has neither that stencil nor its
            // physical scissor rectangle, so render the logical scene cleanly
            // before restoring the clip for composition.
            glDisable(GL_STENCIL_TEST);
            glDisable(GL_SCISSOR_TEST);
            glBindFramebuffer(GL_FRAMEBUFFER, target.framebuffer);
            set_projection(SCENE_WIDTH as i32, SCENE_HEIGHT as i32);
            glClearColor(
                GLASS_BACKGROUND.0,
                GLASS_BACKGROUND.1,
                GLASS_BACKGROUND.2,
                1.0,
            );
            glClear(GL_COLOR_BUFFER_BIT);
        }
        draw_scene_contents(
            projection,
            effect_phase,
            options,
            presentation,
            buddy_position,
            safe_area,
        );
        unsafe {
            glBindFramebuffer(GL_FRAMEBUFFER, 0);
            set_projection(frame_width, frame_height);
            if stencil_available {
                glEnable(GL_STENCIL_TEST);
            } else {
                glEnable(GL_SCISSOR_TEST);
            }
            draw_crt_texture(target.texture, x, y, width, height);
            draw_glass_surface(
                x,
                y,
                width,
                height,
                presentation.brightness,
                presentation.focus,
            );
            if stencil_available {
                glDisable(GL_STENCIL_TEST);
            } else {
                glDisable(GL_SCISSOR_TEST);
            }
        }
        return;
    }

    unsafe {
        glPushMatrix();
        glTranslatef(x, y, 0.0);
        glScalef(width / SCENE_WIDTH, height / SCENE_HEIGHT, 1.0);
    }
    draw_scene_contents(
        projection,
        effect_phase,
        options,
        presentation,
        buddy_position,
        safe_area,
    );
    unsafe { glPopMatrix() };
    draw_glass_surface(
        x,
        y,
        width,
        height,
        presentation.brightness,
        presentation.focus,
    );
}

fn draw_crt_texture(texture: u32, x: f32, y: f32, width: f32, height: f32) {
    const GRID: usize = 24;
    let grid_f = GRID as f32;
    unsafe {
        glEnable(GL_TEXTURE_2D);
        glBindTexture(GL_TEXTURE_2D, texture);
        glColor3f(1.0, 1.0, 1.0);
        glBegin(GL_QUADS);
        for row in 0..GRID {
            let v0 = row as f32 / grid_f;
            let v1 = (row + 1) as f32 / grid_f;
            for column in 0..GRID {
                let u0 = column as f32 / grid_f;
                let u1 = (column + 1) as f32 / grid_f;
                let (pu0, pv0) = barrel_uv(u0, v0);
                let (pu1, pv1) = barrel_uv(u1, v1);
                glTexCoord2f(pu0, 1.0 - pv0);
                glVertex2f(x + u0 * width, y + v0 * height);
                glTexCoord2f(pu1, 1.0 - pv0);
                glVertex2f(x + u1 * width, y + v0 * height);
                glTexCoord2f(pu1, 1.0 - pv1);
                glVertex2f(x + u1 * width, y + v1 * height);
                glTexCoord2f(pu0, 1.0 - pv1);
                glVertex2f(x + u0 * width, y + v1 * height);
            }
        }
        glEnd();
        glBindTexture(GL_TEXTURE_2D, 0);
        glDisable(GL_TEXTURE_2D);
    }
}

fn barrel_uv(u: f32, v: f32) -> (f32, f32) {
    let edge = ((u - 0.5).abs().max((v - 0.5).abs()) * 2.0).powi(2);
    let gain = 1.0 + edge * 0.065;
    (0.5 + (u - 0.5) * gain, 0.5 + (v - 0.5) * gain)
}

fn draw_glass_surface(x: f32, y: f32, width: f32, height: f32, brightness: f32, focus: f32) {
    let edge = (
        0.030 + (1.0 - brightness) * 0.030,
        0.068 + (1.0 - focus) * 0.040,
        0.072 + (1.0 - focus) * 0.040,
    );
    // Beveled glass rim: outer bright, inner dark, creating cathode-tube depth.
    draw_round_outline(
        x + 2.0,
        y + 2.0,
        (width - 4.0).max(0.0),
        (height - 4.0).max(0.0),
        edge,
    );
    draw_round_outline(
        x + 5.0,
        y + 5.0,
        (width - 10.0).max(0.0),
        (height - 10.0).max(0.0),
        (edge.0 * 0.55, edge.1 * 0.55, edge.2 * 0.55),
    );
    draw_round_outline(
        x + 8.0,
        y + 8.0,
        (width - 16.0).max(0.0),
        (height - 16.0).max(0.0),
        (edge.0 * 0.22, edge.1 * 0.22, edge.2 * 0.22),
    );
    unsafe {
        glEnable(GL_BLEND);
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    }
    // Upper and left convex curved specular highlights
    draw_line_alpha(
        x + width * 0.08,
        y + 3.0,
        x + width * 0.78,
        y + 3.0,
        (0.16, 0.30, 0.31),
        0.42,
    );
    draw_line_alpha(
        x + 3.0,
        y + height * 0.08,
        x + 3.0,
        y + height * 0.72,
        (0.12, 0.24, 0.25),
        0.34,
    );
    draw_line_alpha(
        x + width * 0.12,
        y + 5.0,
        x + width * 0.48,
        y + 5.0,
        (0.10, 0.20, 0.22),
        0.28,
    );
    // Diagonal reflection sheen across the upper-left glass quadrant.
    draw_line_alpha(
        x + width * 0.18,
        y + 9.0,
        x + width * 0.06 + height * 0.24,
        y + height * 0.28,
        (0.04, 0.09, 0.10),
        0.55,
    );
    draw_line_alpha(
        x + width * 0.22,
        y + 9.0,
        x + width * 0.10 + height * 0.24,
        y + height * 0.28,
        (0.03, 0.07, 0.08),
        0.45,
    );
    // Bottom edge falloff
    draw_line_alpha(
        x + width * 0.22,
        y + height - 3.0,
        x + width * 0.90,
        y + height - 3.0,
        (0.016, 0.034, 0.036),
        0.40,
    );
    unsafe { glDisable(GL_BLEND) };
}

fn set_projection(width: i32, height: i32) {
    unsafe {
        glViewport(0, 0, width, height);
        glMatrixMode(GL_PROJECTION);
        glLoadIdentity();
        glOrtho(0.0, width as f64, height as f64, 0.0, -1.0, 1.0);
        glMatrixMode(GL_MODELVIEW);
        glLoadIdentity();
    }
}

// ---------------------------------------------------------------------------
// Scene contents
// ---------------------------------------------------------------------------

fn draw_scene_contents(
    projection: &Projection,
    phase: f32,
    options: &RendererOptions,
    presentation: PresentationSettings,
    buddy_position: Option<(f32, f32)>,
    safe_area: SceneSafeArea,
) {
    let mode = VisualMode::from_projection(projection);
    let color = crate::render::theme::rgb(crate::render::theme::mode_color(mode.as_str()));
    let brightness = presentation.brightness;
    draw_rect(
        0.0,
        0.0,
        SCENE_WIDTH,
        SCENE_HEIGHT,
        (
            GLASS_BACKGROUND.0 * brightness,
            GLASS_BACKGROUND.1 * brightness,
            GLASS_BACKGROUND.2 * brightness,
        ),
    );
    if !presentation.display_enabled {
        return;
    }

    draw_crt_treatment(color, brightness, presentation.focus, phase);

    // Quiet ground plane shared by every scene.
    draw_terrain(
        color,
        phase,
        mode,
        presentation.focus,
        safe_area.unobscured_right,
        220.0,
    );

    let layout = scene_layout(safe_area);

    match mode {
        VisualMode::Idle => super::scenes::draw_idle_scene(projection, &layout, color, phase),
        VisualMode::Inspect | VisualMode::Search => {
            super::scenes::draw_workspace_scene(projection, &layout, color, phase, mode)
        }
        VisualMode::Read => super::scenes::draw_read_scene(projection, &layout, color, phase),
        VisualMode::Code => super::scenes::draw_code_scene(projection, &layout, color, phase),
        VisualMode::Execute | VisualMode::Generate | VisualMode::Recover => {
            super::scenes::draw_execute_scene(projection, &layout, color, phase, mode)
        }
        VisualMode::Test | VisualMode::Verify => {
            super::scenes::draw_test_scene(projection, &layout, color, phase)
        }
        VisualMode::Approval => {
            super::scenes::draw_approval_scene(projection, &layout, color, phase)
        }
        VisualMode::Failure => super::scenes::draw_failure_scene(projection, &layout, color, phase),
        VisualMode::Success => super::scenes::draw_success_scene(&layout, color, phase),
        VisualMode::Think | VisualMode::Respond => {
            super::scenes::draw_think_scene(projection, &layout, color, phase)
        }
    }

    // The DAGOAL readout is part of the same 384x256 phosphor composition as
    // the world. Keeping context and telemetry here prevents a high-resolution
    // Xft dashboard from fighting the matrix-rendered scene during animation.
    draw_chrome(projection, mode, &layout, color, safe_area.unobscured_right);
    super::telemetry::draw_operation_telemetry(projection, mode, layout.telemetry, color, phase);

    let state = projection
        .buddy
        .as_ref()
        .map(|buddy| buddy.state.as_str())
        .unwrap_or(mode.as_str());
    let status = projection
        .buddy
        .as_ref()
        .map(|buddy| buddy.status.as_str())
        .filter(|status| !status.is_empty())
        .unwrap_or(projection.status.as_str());
    let character = projection
        .buddy
        .as_ref()
        .map(|buddy| buddy.character.as_str())
        .filter(|character| !character.is_empty())
        .unwrap_or(options.mascot.as_str());
    if let Some((buddy_x, buddy_y)) = buddy_position {
        draw_buddy(buddy_x, buddy_y, state, status, character, phase);
    }
    super::attention::draw_attention_rail(projection, safe_area);
}

fn draw_crt_treatment(color: (f32, f32, f32), brightness: f32, focus: f32, phase: f32) {
    // This pass stays in the low-resolution target, so the scanline rhythm is
    // coherent after nearest-neighbour composition instead of becoming a
    // monitor-sized overlay that scales differently at every window size.
    let flicker = 0.998 + (phase * std::f32::consts::TAU * 1.15).sin() * 0.002;
    let scanline_strength = (0.010 + (1.0 - focus) * 0.010) * flicker;
    let scanline = (
        GLASS_BACKGROUND.0 * (1.0 - scanline_strength),
        GLASS_BACKGROUND.1 * (1.0 - scanline_strength),
        GLASS_BACKGROUND.2 * (1.0 - scanline_strength),
    );
    // Subtractive texture stays subordinate to the matrix dots. At this
    // logical resolution a four-row stripe becomes an overwhelming physical
    // band after nearest-neighbour scaling, so keep the rhythm sparse.
    for y in (4..SCENE_HEIGHT as i32 - 4).step_by(7) {
        draw_rect(2.0, y as f32, SCENE_WIDTH - 4.0, 1.0, scanline);
    }
    // Bulbous tube corner vignetting: corners are darker than edges.
    let edge = (
        0.003 + (1.0 - brightness) * 0.012,
        0.010 + (1.0 - brightness) * 0.018,
        0.014 + (1.0 - brightness) * 0.022,
    );
    let corner = (edge.0 * 2.2, edge.1 * 2.2, edge.2 * 2.2);
    draw_rect(0.0, 0.0, SCENE_WIDTH, 2.0, edge);
    draw_rect(0.0, SCENE_HEIGHT - 2.0, SCENE_WIDTH, 2.0, edge);
    draw_rect(0.0, 0.0, 2.0, SCENE_HEIGHT, edge);
    draw_rect(SCENE_WIDTH - 2.0, 0.0, 2.0, SCENE_HEIGHT, edge);
    draw_round_outline(
        4.0,
        4.0,
        SCENE_WIDTH - 8.0,
        SCENE_HEIGHT - 8.0,
        (color.0 * 0.16, color.1 * 0.16, color.2 * 0.16),
    );
    // DAGOAL: lighter corner vignette so buddy remains clearly visible
    for (cx, cy, w, h) in [
        (0.0, 0.0, 20.0, 20.0),
        (SCENE_WIDTH - 20.0, 0.0, 20.0, 20.0),
        (0.0, SCENE_HEIGHT - 20.0, 20.0, 20.0),
        (SCENE_WIDTH - 20.0, SCENE_HEIGHT - 20.0, 20.0, 20.0),
    ] {
        draw_rect(
            cx,
            cy,
            w,
            h,
            (corner.0 * 0.62, corner.1 * 0.62, corner.2 * 0.62),
        );
    }
}

fn draw_terrain(
    color: (f32, f32, f32),
    phase: f32,
    mode: VisualMode,
    _focus: f32,
    right: f32,
    world_left: f32,
) {
    let horizon = 152.0;
    let quiet = matches!(mode, VisualMode::Idle);
    let base_grid = (color.0 * 0.34, color.1 * 0.34, color.2 * 0.34);
    let grid = if quiet {
        (base_grid.0 * 0.48, base_grid.1 * 0.48, base_grid.2 * 0.48)
    } else {
        base_grid
    };
    let near_grid = (grid.0 * 1.18, grid.1 * 1.18, grid.2 * 1.18);
    let far_grid = (grid.0 * 0.42, grid.1 * 0.42, grid.2 * 0.42);
    // A restrained perspective plane anchors the world. Its geometry is
    // fixed; semantic scene motion belongs to the actor/graph, not the floor.
    for index in 0..12 {
        let t = ((index as f32 + 0.5) / 12.0).min(0.995);
        let perspective = t * t;
        let row_y = horizon + (SCENE_HEIGHT - horizon - 8.0) * perspective;
        let line_color = if t > 0.75 { near_grid } else { grid };
        draw_dotted_span(14.0, row_y, world_left.min(right - 14.0), far_grid, 2.0);
        draw_dotted_span(
            world_left.min(right - 14.0),
            row_y,
            (right - 14.0).max(world_left),
            line_color,
            2.0,
        );
    }
    // Vertical perspective lines fanning out from a vanishing point behind
    // the active information panel, matching the DAGOAL reference wireframe.
    let vanish_x = right * 0.35;
    let bottom_y = SCENE_HEIGHT - 8.0;
    let line_count = 12;
    for index in 0..line_count {
        let bottom_t = index as f32 / (line_count - 1) as f32;
        let bottom_x = 14.0 + bottom_t * (right - 28.0);
        // Lines converge toward the vanishing point but stop at the horizon.
        draw_dotted_line(vanish_x, horizon, bottom_x, bottom_y, far_grid);
    }
    // Horizon accent
    draw_dotted_span(
        12.0,
        horizon,
        (right - 12.0).max(12.0),
        (color.0 * 0.65, color.1 * 0.65, color.2 * 0.65),
        3.0,
    );
    // Sparse fixed stars keep the upper viewport alive in every semantic
    // scene. Their restrained pulse adds liveness without making the whole
    // world drift like a screensaver.
    const STARS: [(f32, f32, f32); 8] = [
        (238.0, 23.0, 0.62),
        (264.0, 42.0, 0.46),
        (289.0, 18.0, 0.72),
        (316.0, 53.0, 0.52),
        (340.0, 27.0, 0.66),
        (356.0, 70.0, 0.42),
        (248.0, 84.0, 0.38),
        (301.0, 91.0, 0.32),
    ];
    for (index, (px, py, intensity)) in STARS.iter().copied().enumerate() {
        if px >= right - 8.0 {
            continue;
        }
        let pulse = if quiet {
            intensity * (0.88 + 0.08 * (phase * 1.7 + index as f32).sin())
        } else {
            intensity * (0.94 + 0.06 * (phase * 2.1 + index as f32 * 0.7).sin())
        };
        draw_rect(
            px,
            py,
            if index % 3 == 0 { 2.0 } else { 1.0 },
            if index % 3 == 0 { 2.0 } else { 1.0 },
            (color.0 * pulse, color.1 * pulse, color.2 * pulse),
        );
    }
}

// ---------------------------------------------------------------------------
// Chrome
// ---------------------------------------------------------------------------

#[allow(dead_code)]
fn draw_chrome(
    projection: &Projection,
    mode: VisualMode,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    right: f32,
) {
    let dim = (0.48, 0.58, 0.63);
    let operation = projection.active_operation.as_ref();
    let action = projection.current_action.as_ref();
    let label = operation
        .and_then(|value| (!value.label.is_empty()).then_some(value.label.as_str()))
        .or_else(|| {
            action.and_then(|value| (!value.label.is_empty()).then_some(value.label.as_str()))
        })
        .unwrap_or(mode.as_str());
    let target = operation
        .and_then(|value| (!value.target.is_empty()).then_some(value.target.as_str()))
        .or_else(|| {
            action.and_then(|value| (!value.target.is_empty()).then_some(value.target.as_str()))
        })
        .unwrap_or("");

    // Mode badge on the right of the header.
    let badge = mode.as_str().to_ascii_uppercase();
    let badge_width = badge.chars().count() as f32 * 7.0;
    pixel_text(
        (layout.header.right() - badge_width - 4.0).max(layout.header.x),
        layout.header.y + 6.0,
        &badge,
        dim,
        layout.header.right(),
    );

    // Dotted header rule.
    let mut rule_x = layout.header.x;
    while rule_x + 3.0 < layout.header.right() {
        draw_rect(
            rule_x,
            layout.header.bottom() - 1.0,
            2.0,
            1.0,
            (color.0 * 0.42, color.1 * 0.42, color.2 * 0.42),
        );
        rule_x += 5.0;
    }

    // Secondary context line just below the header, inside the stage margin.
    let context = if !target.is_empty() {
        format!("{label}  //  {target}")
    } else {
        label.to_owned()
    };
    if !context.is_empty() {
        pixel_text(
            layout.header.x,
            layout.header.bottom() + 5.0,
            &context,
            dim,
            right - 10.0,
        );
    }

    if let Some(request) = projection.model_request.as_ref() {
        if request.status.eq_ignore_ascii_case("unconfigured") {
            pixel_text(
                (right - 170.0).max(layout.header.x),
                layout.header.bottom() + 5.0,
                "MODEL UNCONFIGURED",
                (0.91, 0.62, 0.22),
                right - 10.0,
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Low-level primitives
// ---------------------------------------------------------------------------

pub(crate) fn draw_dotted_span(left: f32, y: f32, right: f32, color: (f32, f32, f32), dot: f32) {
    let mut x = left;
    let mut index = 0_u32;
    while x < right {
        if index % 2 == 0 {
            draw_rect(x, y, dot.min(right - x), 1.0, color);
        }
        x += dot.max(1.0) * 2.0;
        index += 1;
    }
}

pub(crate) fn draw_dotted_line(x1: f32, y1: f32, x2: f32, y2: f32, color: (f32, f32, f32)) {
    let distance = ((x2 - x1).powi(2) + (y2 - y1).powi(2)).sqrt();
    let samples = (distance / 6.0).ceil().max(1.0) as usize;
    for index in 0..=samples {
        let t = index as f32 / samples as f32;
        let x = x1 + (x2 - x1) * t;
        let y = y1 + (y2 - y1) * t;
        draw_rect(x.round(), y.round(), 1.5, 1.5, color);
    }
}

#[cfg(test)]
pub(crate) fn entity_label(entity: &ProjectionEntity) -> String {
    if !entity.label.is_empty() {
        return entity.label.clone();
    }
    for key in ["canonical_path", "path", "uri", "resource"] {
        if let Some(value) = entity.metadata.get(key).and_then(serde_json::Value::as_str) {
            if !value.is_empty() {
                return value.to_owned();
            }
        }
    }
    entity.id.clone()
}

pub(crate) fn pixel_text(x: f32, y: f32, value: &str, color: (f32, f32, f32), right: f32) {
    pixel_text_styled(
        x,
        y,
        value,
        color,
        right,
        DETAIL_TEXT_SCALE,
        BitmapTextStyle::Phosphor,
    );
}

pub(crate) fn pixel_text_styled(
    x: f32,
    y: f32,
    value: &str,
    color: (f32, f32, f32),
    right: f32,
    scale: f32,
    style: BitmapTextStyle,
) {
    let available = (right - x).max(0.0);
    let max_chars = (available / (6.0 * scale)).floor() as usize;
    if max_chars == 0 {
        return;
    }
    let fitted = if value.chars().count() > max_chars {
        if max_chars <= 3 {
            value.chars().take(max_chars).collect::<String>()
        } else {
            format!(
                "{}...",
                value
                    .chars()
                    .take(max_chars.saturating_sub(3))
                    .collect::<String>()
            )
        }
    } else {
        value.to_owned()
    };
    draw_bitmap_text_styled(x, y, &fitted, scale, color, right, style);
}

const DETAIL_TEXT_SCALE: f32 = 1.0;

#[allow(dead_code)]
fn pixel_text_scaled(x: f32, y: f32, value: &str, color: (f32, f32, f32), right: f32, scale: f32) {
    draw_bitmap_text_styled(
        x,
        y,
        value,
        scale.max(1.0),
        color,
        right,
        BitmapTextStyle::Phosphor,
    );
}
