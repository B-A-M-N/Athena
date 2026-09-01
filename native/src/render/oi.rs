use super::super::*;
use super::buddy::draw_buddy;
use super::chassis::PresentationSettings;
use super::primitives::{draw_line, draw_node, draw_rect, draw_round_outline, draw_round_rect};
use crate::buddy::{SPRITE_DIRTY_HEIGHT, SPRITE_DIRTY_WIDTH};
use crate::{ProjectionAttention, ProjectionEntity, ProjectionTreeNode};
#[cfg(test)]
use serde::{Deserialize, Serialize};
use std::cell::RefCell;
use std::collections::HashMap;
use std::ptr;

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
        // draw_crt_texture maps the physical quad through barrel_uv before
        // sampling the logical framebuffer. Applying the same map here is the
        // inverse of the complete physical->content interaction transform.
        let (content_u, content_v) = barrel_uv(u, v);
        self.hit_logical(content_u * SCENE_WIDTH, content_v * SCENE_HEIGHT)
    }
}

const SCENE_WIDTH: f32 = 384.0;
const SCENE_HEIGHT: f32 = 256.0;

struct BuddyMotion {
    initialized: bool,
    mode: VisualMode,
    from: (f32, f32),
    current: (f32, f32),
    target: (f32, f32),
    started_at: f32,
}

#[derive(Clone, Copy, Debug)]
struct SceneSafeArea {
    unobscured_right: f32,
    attention_rail: Option<PixelRect>,
}

fn scene_safe_area(attention_count: usize) -> SceneSafeArea {
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
    let rail = PixelRect {
        x: 252.0,
        y: 12.0,
        width: 122.0,
        height: 218.0,
    };
    SceneSafeArea {
        unobscured_right: rail.x - 8.0,
        attention_rail: Some(rail),
    }
}

const ATTENTION_PAGE_SIZE: usize = 3;

fn attention_page_range(projection: &Projection) -> (usize, usize) {
    let page_count = projection
        .attention_items
        .len()
        .div_ceil(ATTENTION_PAGE_SIZE)
        .max(1);
    let page = projection.attention_page.min(page_count - 1);
    let start = page * ATTENTION_PAGE_SIZE;
    let end = (start + ATTENTION_PAGE_SIZE).min(projection.attention_items.len());
    (start, end)
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
    let page = projection.attention_page as i32;
    projection.attention_page = (page + delta).rem_euclid(page_count as i32) as usize;
    true
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

/// Low-resolution OI render target.
///
/// The scene is authored at a fixed logical resolution and composed into the
/// physical CRT with nearest-neighbour sampling. This keeps animation work
/// bounded by the scene grammar instead of the user's monitor resolution.
pub(crate) struct OiTarget {
    framebuffer: u32,
    texture: u32,
    enabled: bool,
    buddy_motion: RefCell<BuddyMotion>,
}

pub(crate) fn dump_framebuffer(target: &OiTarget, path: &str) -> Result<(), String> {
    if !target.enabled() {
        return Err("OI framebuffer is unavailable on this OpenGL visual".to_owned());
    }
    let width = SCENE_WIDTH as usize;
    let height = SCENE_HEIGHT as usize;
    let mut pixels = vec![0_u8; width * height * 4];
    unsafe {
        glBindFramebuffer(GL_FRAMEBUFFER, target.framebuffer);
        glReadPixels(
            0,
            0,
            width as c_int,
            height as c_int,
            GL_RGBA,
            GL_UNSIGNED_BYTE,
            pixels.as_mut_ptr().cast(),
        );
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
    }
    let file = std::fs::File::create(path).map_err(|error| format!("create OI dump: {error}"))?;
    let writer = std::io::BufWriter::new(file);
    let mut encoder = png::Encoder::new(writer, width as u32, height as u32);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    let mut output = encoder
        .write_header()
        .map_err(|error| format!("write OI PNG header: {error}"))?;
    // OpenGL's origin is bottom-left; PNG consumers expect top-left.
    for row in 0..height / 2 {
        let opposite = height - 1 - row;
        for column in 0..width * 4 {
            pixels.swap(row * width * 4 + column, opposite * width * 4 + column);
        }
    }
    output
        .write_image_data(&pixels)
        .map_err(|error| format!("write OI PNG: {error}"))?;
    Ok(())
}

impl OiTarget {
    pub(crate) fn new() -> Self {
        let mut framebuffer = 0;
        let mut texture = 0;
        unsafe {
            glGenFramebuffers(1, &mut framebuffer);
            glGenTextures(1, &mut texture);
            if framebuffer == 0 || texture == 0 {
                if framebuffer != 0 {
                    glDeleteFramebuffers(1, &framebuffer);
                }
                if texture != 0 {
                    glDeleteTextures(1, &texture);
                }
                return Self {
                    framebuffer: 0,
                    texture: 0,
                    enabled: false,
                    buddy_motion: RefCell::new(BuddyMotion::default()),
                };
            }
            glBindTexture(GL_TEXTURE_2D, texture);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
            glTexImage2D(
                GL_TEXTURE_2D,
                0,
                GL_RGBA as c_int,
                SCENE_WIDTH as c_int,
                SCENE_HEIGHT as c_int,
                0,
                GL_RGBA,
                GL_UNSIGNED_BYTE,
                ptr::null(),
            );
            glBindFramebuffer(GL_FRAMEBUFFER, framebuffer);
            glFramebufferTexture2D(
                GL_FRAMEBUFFER,
                GL_COLOR_ATTACHMENT0,
                GL_TEXTURE_2D,
                texture,
                0,
            );
            let complete = glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE;
            glBindFramebuffer(GL_FRAMEBUFFER, 0);
            glBindTexture(GL_TEXTURE_2D, 0);
            if !complete {
                glDeleteFramebuffers(1, &framebuffer);
                glDeleteTextures(1, &texture);
                return Self {
                    framebuffer: 0,
                    texture: 0,
                    enabled: false,
                    buddy_motion: RefCell::new(BuddyMotion::default()),
                };
            }
        }
        Self {
            framebuffer,
            texture,
            enabled: true,
            buddy_motion: RefCell::new(BuddyMotion::default()),
        }
    }

    fn enabled(&self) -> bool {
        self.enabled
    }

    fn buddy_position(
        &self,
        projection: &Projection,
        mode: VisualMode,
        phase: f32,
        animated: bool,
    ) -> (f32, f32) {
        let safe_area = scene_safe_area(projection.attention_items.len());
        let half_width = SPRITE_DIRTY_WIDTH / 2.0;
        let half_height = SPRITE_DIRTY_HEIGHT / 2.0;
        let clamp_x = |x: f32| {
            x.clamp(
                half_width,
                (safe_area.unobscured_right - half_width).max(half_width),
            )
        };
        let clamp_y = |y: f32| y.clamp(half_height, SCENE_HEIGHT - half_height);
        let raw = buddy_target(projection, mode, safe_area.unobscured_right);
        // Ease in the rendered coordinate space so transitions land on the
        // visible clamp rather than on an unclamped authoring target.
        let desired = (clamp_x(raw.0), clamp_y(raw.1));
        let mut motion = self.buddy_motion.borrow_mut();
        if !motion.initialized {
            motion.initialized = true;
            motion.mode = mode;
            motion.from = desired;
            motion.current = desired;
            motion.target = desired;
        } else if motion.mode != mode || motion.target != desired {
            let current = motion_position(&motion, phase);
            motion.mode = mode;
            motion.from = current;
            motion.current = current;
            motion.target = desired;
            motion.started_at = phase;
        }
        if !animated {
            motion.current = motion.target;
        } else {
            motion.current = motion_position(&motion, phase);
        }
        let bob = if animated {
            (phase * 1.7).sin() * 1.5
        } else {
            0.0
        };
        (
            clamp_x(motion.current.0),
            clamp_y(motion.current.1 + bob),
        )
    }
}

fn motion_position(motion: &BuddyMotion, phase: f32) -> (f32, f32) {
    let progress = ((phase - motion.started_at) / 0.36).clamp(0.0, 1.0);
    let eased = 1.0 - (1.0 - progress).powi(3);
    (
        motion.from.0 + (motion.target.0 - motion.from.0) * eased,
        motion.from.1 + (motion.target.1 - motion.from.1) * eased,
    )
}

impl Drop for OiTarget {
    fn drop(&mut self) {
        if !self.enabled {
            return;
        }
        unsafe {
            glDeleteFramebuffers(1, &self.framebuffer);
            glDeleteTextures(1, &self.texture);
        }
    }
}

/// Render the OI in a deliberately small logical scene, then scale it into
/// the physical aperture. Keeping all scene coordinates in this space gives
/// DAGOAL one coherent pixel grammar instead of a collection of high-DPI
/// diagnostic vectors.
pub(crate) fn draw_oi_scene(
    target: &OiTarget,
    frame_width: i32,
    frame_height: i32,
    x: f32,
    y: f32,
    width: f32,
    height: f32,
    projection: &Projection,
    phase: f32,
    options: &RendererOptions,
    presentation: PresentationSettings,
    stencil_available: bool,
) {
    let mode = VisualMode::from_projection(projection);
    let buddy_position = target.buddy_position(
        projection,
        mode,
        phase,
        options.animations && !options.reduced_motion,
    );
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
            glClearColor(0.008, 0.026, 0.036, 1.0);
            glClear(GL_COLOR_BUFFER_BIT);
        }
        draw_scene_contents(
            projection,
            phase,
            options,
            presentation,
            buddy_position,
            scene_safe_area(projection.attention_items.len()),
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
        phase,
        options,
        presentation,
        buddy_position,
        scene_safe_area(projection.attention_items.len()),
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
    const GRID: usize = 16;
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
    let gain = 1.0 + edge * 0.045;
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
    // Upper and left convex curved specular highlights
    draw_line(
        x + width * 0.08,
        y + 3.0,
        x + width * 0.78,
        y + 3.0,
        (0.16, 0.30, 0.31),
    );
    draw_line(
        x + 3.0,
        y + height * 0.08,
        x + 3.0,
        y + height * 0.72,
        (0.12, 0.24, 0.25),
    );
    draw_line(
        x + width * 0.12,
        y + 5.0,
        x + width * 0.48,
        y + 5.0,
        (0.10, 0.20, 0.22),
    );
    // Diagonal reflection sheen across the upper-left glass quadrant.
    draw_line(
        x + width * 0.18,
        y + 9.0,
        x + width * 0.06 + height * 0.24,
        y + height * 0.28,
        (0.04, 0.09, 0.10),
    );
    draw_line(
        x + width * 0.22,
        y + 9.0,
        x + width * 0.10 + height * 0.24,
        y + height * 0.28,
        (0.03, 0.07, 0.08),
    );
    // Bottom edge falloff
    draw_line(
        x + width * 0.22,
        y + height - 3.0,
        x + width * 0.90,
        y + height - 3.0,
        (0.016, 0.034, 0.036),
    );
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

fn draw_scene_contents(
    projection: &Projection,
    phase: f32,
    options: &RendererOptions,
    presentation: PresentationSettings,
    buddy_position: (f32, f32),
    safe_area: SceneSafeArea,
) {
    let mode = VisualMode::from_projection(projection);
    let color = rgb_f32(mode_color(mode.as_str()));
    let brightness = presentation.brightness;
    draw_rect(
        0.0,
        0.0,
        SCENE_WIDTH,
        SCENE_HEIGHT,
        (0.008 * brightness, 0.026 * brightness, 0.036 * brightness),
    );
    if presentation.display_enabled {
        draw_terrain(
            color,
            phase,
            mode,
            presentation.focus,
            safe_area.unobscured_right,
        );
        // Idle is a world, not a report about the absence of work. Semantic
        // nodes and action grammar appear only when there is an active mode.
        if !matches!(mode, VisualMode::Idle) {
            draw_semantic_world(
                projection,
                mode,
                color,
                phase,
                presentation.focus,
                safe_area.unobscured_right,
            );
        }
        draw_oi_information(projection, mode, color, safe_area.unobscured_right);
    }
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
    if presentation.display_enabled {
        draw_buddy(
            buddy_position.0,
            buddy_position.1,
            state,
            status,
            character,
            phase,
        );
        draw_crt_treatment(color, presentation.brightness, presentation.focus);
        draw_attention_rail(projection, safe_area);
    }
}

fn draw_crt_treatment(color: (f32, f32, f32), brightness: f32, focus: f32) {
    // This pass stays in the low-resolution target, so the scanline rhythm is
    // coherent after nearest-neighbour composition instead of becoming a
    // monitor-sized overlay that scales differently at every window size.
    let scanline = (
        color.0 * (0.06 + (1.0 - focus) * 0.06),
        color.1 * (0.06 + (1.0 - focus) * 0.06),
        color.2 * (0.06 + (1.0 - focus) * 0.06),
    );
    // Tight 2-pixel cathode-ray beam modulation.
    for y in (2..SCENE_HEIGHT as i32 - 2).step_by(2) {
        draw_rect(2.0, y as f32, SCENE_WIDTH - 4.0, 1.0, scanline);
    }
    // Bulbous tube corner vignetting: corners are darker than edges.
    let edge = (
        0.003 + (1.0 - brightness) * 0.012,
        0.010 + (1.0 - brightness) * 0.018,
        0.014 + (1.0 - brightness) * 0.022,
    );
    let corner = (
        edge.0 * 2.2,
        edge.1 * 2.2,
        edge.2 * 2.2,
    );
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
    // Additional corner shadow quads for tube bulb falloff.
    for (cx, cy, w, h) in [
        (0.0, 0.0, 28.0, 28.0),
        (SCENE_WIDTH - 28.0, 0.0, 28.0, 28.0),
        (0.0, SCENE_HEIGHT - 28.0, 28.0, 28.0),
        (SCENE_WIDTH - 28.0, SCENE_HEIGHT - 28.0, 28.0, 28.0),
    ] {
        draw_rect(cx, cy, w, h, corner);
    }
}

fn draw_terrain(color: (f32, f32, f32), phase: f32, mode: VisualMode, focus: f32, right: f32) {
    let horizon = 148.0;
    let quiet = matches!(mode, VisualMode::Idle);
    let base_grid = (color.0 * 0.34, color.1 * 0.34, color.2 * 0.34);
    let grid = if quiet {
        (base_grid.0 * 0.48, base_grid.1 * 0.48, base_grid.2 * 0.48)
    } else {
        base_grid
    };
    let near_grid = (
        grid.0 * 1.35,
        grid.1 * 1.35,
        grid.2 * 1.35,
    );
    let far_grid = (
        grid.0 * 0.55,
        grid.1 * 0.55,
        grid.2 * 0.55,
    );
    // A dense perspective vector ground plane receding beneath Buddy.
    // Horizontal rungs are spaced with quadratic perspective (t*t) and crawl
    // downward so the plane feels alive without drifting the horizon.
    let crawl = ((phase.max(0.0) * 0.25).floor() % 16.0) / 16.0;
    for index in 0..16 {
        let t = ((index as f32 + crawl) / 16.0).min(0.995);
        let perspective = t * t;
        let row_y = horizon + (SCENE_HEIGHT - horizon - 8.0) * perspective;
        let line_color = if t > 0.75 { near_grid } else { grid };
        draw_rect(12.0, row_y, (right - 24.0).max(0.0), 1.0, line_color);
    }
    // Vertical perspective lines fanning out from a vanishing point behind
    // the active information panel, matching the DAGOAL reference wireframe.
    let vanish_x = right * 0.35;
    let bottom_y = SCENE_HEIGHT - 8.0;
    let line_count = 18;
    for index in 0..line_count {
        let bottom_t = index as f32 / (line_count - 1) as f32;
        let bottom_x = 14.0 + bottom_t * (right - 28.0);
        // Lines converge toward the vanishing point but stop at the horizon.
        draw_line(vanish_x, horizon, bottom_x, bottom_y, far_grid);
    }
    // Horizon accent
    draw_rect(
        12.0,
        horizon,
        (right - 24.0).max(0.0),
        1.0,
        (color.0 * 0.65, color.1 * 0.65, color.2 * 0.65),
    );
    // Floating phosphor starfield above the grid, with deterministic twinkle.
    let particle_count = 12 + (focus * 18.0) as usize;
    for index in 0..particle_count {
        let px = (index * 67 % ((right as usize).saturating_sub(28)) + 14) as f32;
        let py = (index * 31 % 126 + 12) as f32;
        let twinkle_tick = (phase.max(0.0) * 8.0 + index as f32 * 1.7).floor();
        let pulse = if (twinkle_tick as usize) % 5 == 0 {
            0.92
        } else if (twinkle_tick as usize) % 3 == 0 {
            0.62
        } else {
            0.42
        };
        let star_size = if (twinkle_tick as usize) % 7 == 0 { 2.0 } else { 1.0 };
        draw_rect(
            px,
            py,
            star_size,
            star_size,
            (color.0 * pulse, color.1 * pulse, color.2 * pulse),
        );
    }
}

fn draw_oi_information(
    projection: &Projection,
    mode: VisualMode,
    color: (f32, f32, f32),
    right: f32,
) {
    let right = right.clamp(46.0, SCENE_WIDTH - 8.0);
    let bright = (color.0 * 0.92, color.1 * 0.92, color.2 * 0.92);
    let dim = (color.0 * 0.58, color.1 * 0.58, color.2 * 0.58);
    pixel_text(
        14.0,
        18.0,
        "ATHENA OI // GLASS COMPUTE",
        bright,
        right,
    );
    let viewport_label = "LIVE VIEWPORT";
    let viewport_width = viewport_label.chars().count() as f32 * 6.0;
    pixel_text(
        (right - viewport_width - 14.0).max(14.0),
        18.0,
        viewport_label,
        dim,
        right,
    );
    // Dotted header rule beneath the operational header.
    let mut rule_x = 14.0;
    while rule_x + 3.0 < right - 14.0 {
        draw_rect(rule_x, 29.0, 2.0, 1.0, (color.0 * 0.42, color.1 * 0.42, color.2 * 0.42));
        rule_x += 5.0;
    }
    if !projection.self_host_phase.is_empty() {
        pixel_text(
            (right - 134.0).max(14.0),
            18.0,
            &format!("SELF-HOST // {}", projection.self_host_phase),
            bright,
            right,
        );
    }

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

    // Operational telemetry hierarchy with chevron callouts.
    let mut info_y = 32.0;
    if let Some(request) = projection.model_request.as_ref() {
        let model = if request.provider.is_empty() {
            request.model.clone()
        } else if request.model.is_empty() {
            request.provider.clone()
        } else {
            format!("{}/{}", request.provider, request.model)
        };
        if !model.is_empty() {
            pixel_text(
                14.0,
                info_y,
                &format!("> MODEL REQUEST · {}", model),
                bright,
                right,
            );
            info_y += 10.0;
        }
    }
    pixel_text(
        14.0,
        info_y,
        &format!("> ACTIVE OPERATION // {}", mode.as_str().to_ascii_uppercase()),
        bright,
        right,
    );
    info_y += 10.0;
    if !label.is_empty() {
        pixel_text(
            14.0,
            info_y,
            &format!("  {}", label),
            dim,
            right,
        );
        info_y += 10.0;
    }
    if !target.is_empty() {
        pixel_text(
            14.0,
            info_y,
            &format!("  {}", target),
            (color.0 * 0.72, color.1 * 0.72, color.2 * 0.72),
            right,
        );
        info_y += 10.0;
    }

    let detail = operation
        .and_then(|value| (!value.operation.is_empty()).then_some(value.operation.as_str()))
        .or_else(|| {
            action.and_then(|value| (!value.detail.is_empty()).then_some(value.detail.as_str()))
        })
        .or_else(|| {
            action.and_then(|value| (!value.query.is_empty()).then_some(value.query.as_str()))
        })
        .unwrap_or("");
    if !detail.is_empty() {
        pixel_text(14.0, info_y, &format!("> {}", detail), dim, right);
        info_y += 10.0;
    }
    if let Some(operation) = operation {
        if !operation.mutation_state.is_empty() {
            pixel_text(
                (right - 112.0).max(14.0),
                info_y - 10.0,
                &format!("MUT {}", operation.mutation_state),
                dim,
                right,
            );
        }
    }

    if let Some(request) = projection.model_request.as_ref() {
        if request.status.eq_ignore_ascii_case("unconfigured") {
            pixel_text(
                (right - 170.0).max(14.0),
                32.0,
                "MODEL UNCONFIGURED",
                (0.91, 0.62, 0.22),
                right,
            );
        }
    }

    let mut y = info_y + 6.0;
    match mode {
        VisualMode::Inspect | VisualMode::Search => {
            draw_tree_information(
                "WORKSPACE",
                &projection.workspace_tree,
                y,
                bright,
                dim,
                right,
            );
        }
        VisualMode::Read | VisualMode::Code => {
            if let Some(code) = projection.code_view.as_ref() {
                pixel_text(
                    14.0,
                    y,
                    &format!("{} {}", code.language, code.path),
                    bright,
                    right,
                );
                y += 10.0;
                let lines = if !code.diff.is_empty() {
                    code.diff.clone()
                } else if !code.lines.is_empty() {
                    code.lines.clone()
                } else {
                    code.text.lines().map(str::to_owned).collect()
                };
                for line in lines.iter().take(6) {
                    pixel_text(14.0, y, line, dim, right);
                    y += 8.0;
                }
                if code.preview_truncated {
                    pixel_text(14.0, y, "... PREVIEW TRUNCATED", dim, right);
                }
                if !code.mutation_state.is_empty() {
                    pixel_text(
                        14.0,
                        224.0,
                        &format!("MUTATION {}", code.mutation_state),
                        bright,
                        right,
                    );
                }
            } else {
                draw_tree_information(
                    "WORKSPACE",
                    &projection.workspace_tree,
                    y,
                    bright,
                    dim,
                    right,
                );
            }
        }
        VisualMode::Execute | VisualMode::Generate | VisualMode::Recover => {
            y = draw_tree_information("RUNTIME", &projection.runtime_tree, y, bright, dim, right);
            if let Some(operation) = operation {
                if !operation.command.is_empty() {
                    pixel_text(14.0, y, &format!("$ {}", operation.command), dim, right);
                }
                if !operation.progress.is_empty() {
                    pixel_text(
                        14.0,
                        y + 9.0,
                        &format!("PROGRESS {}", operation.progress),
                        bright,
                        right,
                    );
                }
                if let Some(value) = operation.progress_value {
                    if operation.progress_determinate {
                        pixel_text(
                            14.0,
                            y + 18.0,
                            &format!("{}%", (value * 100.0).round()),
                            bright,
                            right,
                        );
                    }
                }
            }
            if let Some(action) = action {
                if !action.progress.is_empty() {
                    pixel_text(
                        14.0,
                        y + 27.0,
                        &format!("ACTION {}", action.progress),
                        dim,
                        right,
                    );
                }
                if action.progress_determinate {
                    if let Some(value) = action.progress_value {
                        pixel_text(
                            14.0,
                            y + 36.0,
                            &format!("{}%", (value * 100.0).round()),
                            bright,
                            right,
                        );
                    }
                }
            }
        }
        VisualMode::Test | VisualMode::Verify => {
            pixel_text(14.0, y, "EVIDENCE", bright, right);
            y += 10.0;
            if !projection.verification.status.is_empty() {
                pixel_text(14.0, y, &projection.verification.status, dim, right);
                y += 8.0;
            }
            for check in projection.verification.checks.iter().take(6) {
                let text = check.to_string();
                pixel_text(14.0, y, &text, dim, right);
                y += 8.0;
            }
        }
        VisualMode::Failure => {
            pixel_text(14.0, y, "DIAGNOSTICS", bright, right);
            y += 10.0;
            for diagnostic in projection.diagnostics.iter().take(5) {
                let location = if diagnostic.path.is_empty() {
                    diagnostic.message.clone()
                } else {
                    format!(
                        "{}{}",
                        diagnostic.path,
                        diagnostic
                            .line
                            .map_or(String::new(), |line| format!(":{line}"))
                    )
                };
                let severity = if diagnostic.severity.is_empty() {
                    String::new()
                } else {
                    format!(" [{}]", diagnostic.severity)
                };
                pixel_text(
                    14.0,
                    y,
                    &format!("{}{}", location, severity),
                    (0.84, 0.45, 0.30),
                    right,
                );
                y += 8.0;
                if !diagnostic.detail.is_empty() {
                    pixel_text(14.0, y, &diagnostic.detail, dim, right);
                    y += 8.0;
                }
                if diagnostic.expected.is_some() || diagnostic.actual.is_some() {
                    pixel_text(
                        14.0,
                        y,
                        &format!(
                            "E {} A {}",
                            diagnostic
                                .expected
                                .as_ref()
                                .unwrap_or(&serde_json::Value::Null),
                            diagnostic
                                .actual
                                .as_ref()
                                .unwrap_or(&serde_json::Value::Null)
                        ),
                        dim,
                        right,
                    );
                    y += 8.0;
                }
            }
        }
        VisualMode::Approval => {
            pixel_text(
                14.0,
                y,
                "ACTION PAUSED AT POLICY GATE",
                (0.91, 0.62, 0.22),
                right,
            );
        }
        VisualMode::Think | VisualMode::Respond => {
            if let Some(progress) = projection.progress.as_ref() {
                if let Some(object) = progress.as_object() {
                    if let Some(label) = object.get("label").and_then(serde_json::Value::as_str) {
                        pixel_text(14.0, y, label, dim, right);
                    }
                }
            } else if let Some(line) = projection.display_oi().first() {
                pixel_text(14.0, y, line, dim, right);
            }
        }
        VisualMode::Idle => {
            if let Some(index) = projection.history_index {
                pixel_text(
                    14.0,
                    y,
                    &format!("OI HISTORY // {}", index + 1),
                    bright,
                    right,
                );
                y += 10.0;
                for line in projection.display_oi().iter().take(10) {
                    pixel_text(14.0, y, line, dim, right);
                    y += 8.0;
                }
            } else if projection.view.history {
                let _ = draw_tree_information(
                    "HISTORY",
                    &projection.runtime_tree,
                    y,
                    bright,
                    dim,
                    right,
                );
            } else if let Some(line) = projection.display_oi().first() {
                pixel_text(14.0, y, line, dim, right);
            }
        }
        VisualMode::Success => {
            pixel_text(14.0, y, "OUTCOME VERIFIED", (0.46, 0.91, 0.67), right);
            if projection.view.history {
                let _ = draw_tree_information(
                    "HISTORY",
                    &projection.runtime_tree,
                    y + 12.0,
                    bright,
                    dim,
                    right,
                );
            }
        }
    }
}

fn draw_tree_information(
    title: &str,
    tree: &[ProjectionTreeNode],
    mut y: f32,
    bright: (f32, f32, f32),
    dim: (f32, f32, f32),
    right: f32,
) -> f32 {
    if tree.is_empty() {
        return y;
    }
    pixel_text(14.0, y, title, bright, right);
    y += 10.0;
    let mut lines = Vec::new();
    append_pixel_tree(tree, 0, &mut lines);
    let total = lines.len();
    for line in lines.into_iter().take(8) {
        pixel_text(14.0, y, &line, dim, right);
        y += 8.0;
    }
    if total > 8 {
        pixel_text(
            14.0,
            y,
            &format!("+{} MORE", total - 8),
            (0.91, 0.62, 0.22),
            right,
        );
        y += 8.0;
    }
    y
}

fn entity_label(entity: &ProjectionEntity) -> String {
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

fn draw_attention_rail(projection: &Projection, safe_area: SceneSafeArea) {
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
    for item in projection.attention_items[start..end].iter() {
        let accent = match item.severity.to_ascii_lowercase().as_str() {
            "failure" | "error" => (0.88, 0.28, 0.32),
            "warning" | "approval" => (0.91, 0.62, 0.22),
            _ => (0.34, 0.76, 0.72),
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
            (0.68, 0.78, 0.80),
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
                (0.40, 0.58, 0.60),
                rail.right() - 8.0,
            );
        } else if !item.id.is_empty() {
            pixel_text(
                rail.x + 10.0,
                y + 42.0,
                &item.id,
                (0.40, 0.58, 0.60),
                rail.right() - 8.0,
            );
        }
        y += 62.0;
    }
    let total = projection.attention_items.len();
    if total > ATTENTION_PAGE_SIZE {
        let page = start / ATTENTION_PAGE_SIZE + 1;
        let pages = total.div_ceil(ATTENTION_PAGE_SIZE);
        pixel_text(
            rail.x + 8.0,
            rail.bottom() - 6.0,
            &format!("PAGE {page}/{pages}  SCROLL"),
            (0.42, 0.72, 0.74),
            rail.right() - 6.0,
        );
        if end < total {
            pixel_text(
                rail.x + 8.0,
                rail.bottom() - 18.0,
                &format!("+{} MORE", total - end),
                (0.91, 0.62, 0.22),
                rail.right() - 6.0,
            );
        }
    }
    if projection.stale {
        pixel_text(
            rail.x + 8.0,
            rail.y + 4.0,
            &format!("BRIDGE {} // STALE", projection.bridge_status),
            (0.88, 0.28, 0.32),
            rail.right() - 6.0,
        );
    }
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

fn attention_button_specs(item: &ProjectionAttention) -> Vec<(&'static str, AttentionAction)> {
    // Approval identity is a canonical projection field.  The renderer must
    // never recover authority-bearing IDs by parsing display strings.
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
    specs
}

fn attention_button_rects(
    item: &ProjectionAttention,
    left: f32,
    top: f32,
    right: f32,
) -> Vec<(PixelRect, &'static str, AttentionAction)> {
    let specs = attention_button_specs(item);
    if specs.is_empty() {
        return Vec::new();
    }
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
        // A disconnected/error bridge may leave informational cards visible,
        // but never leaves authority-bearing hit targets active.
        return AttentionHitMap::default();
    }
    let safe_area = scene_safe_area(projection.attention_items.len());
    let Some(rail) = safe_area.attention_rail else {
        return AttentionHitMap::default();
    };
    let mut hits = Vec::new();
    let mut y = rail.y + 12.0;
    let (start, end) = attention_page_range(projection);
    for item in projection.attention_items[start..end].iter() {
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

fn append_pixel_tree(nodes: &[ProjectionTreeNode], depth: usize, lines: &mut Vec<String>) {
    for node in nodes {
        let label = if node.label.is_empty() {
            if node.kind.is_empty() {
                &node.id
            } else {
                &node.kind
            }
        } else {
            &node.label
        };
        let badge = match node.status.to_ascii_lowercase().as_str() {
            "passed" | "complete" | "ready" | "ok" => " [OK]",
            "reading" => " [READ]",
            "testing" => " [TEST]",
            "running" => " [...]",
            "failed" | "error" => " [FAIL]",
            _ => "",
        };
        let prefix = if depth == 0 { "" } else { "├── " };
        lines.push(format!(
            "{}{}{}{}",
            "  ".repeat(depth.saturating_sub(1)),
            prefix,
            label,
            badge
        ));
        append_pixel_tree(&node.children, depth + 1, lines);
    }
}

fn pixel_text(x: f32, y: f32, value: &str, color: (f32, f32, f32), right: f32) {
    let start_x = x;
    let mut x = x;
    let mut y = y;
    unsafe {
        glColor3f(color.0, color.1, color.2);
        glBegin(GL_QUADS);
        for character in value.chars() {
            if character == '\n' {
                x = start_x;
                y += 9.0;
                continue;
            }
            let glyph = pixel_glyph(character);
            if x + 5.0 > right {
                break;
            }
            for (row, bits) in glyph.into_iter().enumerate() {
                for column in 0..5 {
                    if bits & (1 << (4 - column)) == 0 {
                        continue;
                    }
                    let px = x + column as f32;
                    let py = y + row as f32;
                    glVertex2f(px, py);
                    glVertex2f(px + 1.0, py);
                    glVertex2f(px + 1.0, py + 1.0);
                    glVertex2f(px, py + 1.0);
                }
            }
            x += 6.0;
        }
        glEnd();
    }
}

fn pixel_glyph(character: char) -> [u8; 7] {
    match character.to_ascii_uppercase() {
        'A' => [
            0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001,
        ],
        'B' => [
            0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110,
        ],
        'C' => [
            0b01111, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b01111,
        ],
        'D' => [
            0b11110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11110,
        ],
        'E' => [
            0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111,
        ],
        'F' => [
            0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000,
        ],
        'G' => [
            0b01111, 0b10000, 0b10000, 0b10111, 0b10001, 0b10001, 0b01111,
        ],
        'H' => [
            0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001,
        ],
        'I' => [
            0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b11111,
        ],
        'J' => [
            0b00111, 0b00010, 0b00010, 0b00010, 0b10010, 0b10010, 0b01100,
        ],
        'K' => [
            0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001,
        ],
        'L' => [
            0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111,
        ],
        'M' => [
            0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001,
        ],
        'N' => [
            0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001,
        ],
        'O' => [
            0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110,
        ],
        'P' => [
            0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000,
        ],
        'Q' => [
            0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101,
        ],
        'R' => [
            0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001,
        ],
        'S' => [
            0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110,
        ],
        'T' => [
            0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100,
        ],
        'U' => [
            0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110,
        ],
        'V' => [
            0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100,
        ],
        'W' => [
            0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b11011, 0b10001,
        ],
        'X' => [
            0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001,
        ],
        'Y' => [
            0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100,
        ],
        'Z' => [
            0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111,
        ],
        '0' => [
            0b01110, 0b10011, 0b10101, 0b10101, 0b10101, 0b11001, 0b01110,
        ],
        '1' => [
            0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110,
        ],
        '2' => [
            0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b01000, 0b11111,
        ],
        '3' => [
            0b11110, 0b00001, 0b00001, 0b01110, 0b00001, 0b00001, 0b11110,
        ],
        '4' => [
            0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010,
        ],
        '5' => [
            0b11111, 0b10000, 0b10000, 0b11110, 0b00001, 0b00001, 0b11110,
        ],
        '6' => [
            0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110,
        ],
        '7' => [
            0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000,
        ],
        '8' => [
            0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110,
        ],
        '9' => [
            0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b11100,
        ],
        '+' => [0, 0b00100, 0b00100, 0b11111, 0b00100, 0b00100, 0],
        '=' => [0, 0, 0b11111, 0, 0b11111, 0, 0],
        '<' => [0b00001, 0b00010, 0b00100, 0b01000, 0b00100, 0b00010, 0b00001],
        '%' => [0b11001, 0b11010, 0b00100, 0b01000, 0b01011, 0b10011, 0],
        '|' => [0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100],
        '$' => [0b00100, 0b01111, 0b10100, 0b01110, 0b00101, 0b11110, 0b00100],
        '#' => [0b01010, 0b01010, 0b11111, 0b01010, 0b11111, 0b01010, 0b01010],
        '(' => [0b00010, 0b00100, 0b01000, 0b01000, 0b01000, 0b00100, 0b00010],
        ')' => [0b01000, 0b00100, 0b00010, 0b00010, 0b00010, 0b00100, 0b01000],
        '-' => [0, 0, 0, 0b11111, 0, 0, 0],
        '_' => [0, 0, 0, 0, 0, 0, 0b11111],
        '/' => [0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0, 0],
        ':' => [0, 0b00100, 0, 0, 0b00100, 0, 0],
        '.' => [0, 0, 0, 0, 0, 0b00110, 0b00110],
        '[' => [
            0b01110, 0b01000, 0b01000, 0b01000, 0b01000, 0b01000, 0b01110,
        ],
        ']' => [
            0b01110, 0b00010, 0b00010, 0b00010, 0b00010, 0b00010, 0b01110,
        ],
        '!' => [0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0, 0b00100],
        '>' => [
            0b10000, 0b01000, 0b00100, 0b00010, 0b00100, 0b01000, 0b10000,
        ],
        _ => [0; 7],
    }
}

fn draw_semantic_world(
    projection: &Projection,
    mode: VisualMode,
    color: (f32, f32, f32),
    phase: f32,
    focus: f32,
    right: f32,
) {
    let entities = semantic_entities(projection);
    let hidden_entities = semantic_entity_count(projection).saturating_sub(entities.len());
    let positions: Vec<(String, f32, f32)> = entities
        .iter()
        .enumerate()
        .map(|(index, entity)| {
            let column = index % 2;
            let row = index / 2;
            (
                entity.id.clone(),
                if column == 0 {
                    72.0
                } else {
                    (right - 48.0).max(150.0)
                },
                74.0 + row as f32 * 42.0,
            )
        })
        .collect();
    let position_by_id: HashMap<&str, (f32, f32)> = positions
        .iter()
        .map(|(id, x, y)| (id.as_str(), (*x, *y)))
        .collect();
    let edges: Vec<((f32, f32), (f32, f32))> = entities
        .iter()
        .enumerate()
        .filter_map(|(index, entity)| {
            let parent_id = entity.parent_id.as_deref()?;
            let parent = position_by_id.get(parent_id)?;
            let child = positions.get(index)?;
            Some((*parent, (child.1, child.2)))
        })
        .collect();
    let active_color = (
        color.0 * (0.62 + focus * 0.24),
        color.1 * (0.62 + focus * 0.24),
        color.2 * (0.62 + focus * 0.24),
    );
    for (index, entity) in entities.iter().enumerate() {
        let (_, px, py) = &positions[index];
        let node_color = if entity.status.eq_ignore_ascii_case("failed")
            || entity.status.eq_ignore_ascii_case("failure")
        {
            (0.88, 0.28, 0.32)
        } else if entity.status.eq_ignore_ascii_case("approval") {
            (0.91, 0.62, 0.22)
        } else {
            active_color
        };
        let radius = if entity.kind.eq_ignore_ascii_case("task") {
            15.0
        } else if entity.parent_id.is_some() {
            11.0
        } else {
            13.0
        };
        draw_node(*px, *py, radius, node_color);
        let label = if entity.label.is_empty() {
            entity_label(entity)
        } else {
            entity.label.clone()
        };
        pixel_text(
            (*px - 30.0).max(12.0),
            *py + radius + 10.0,
            &label,
            (
                node_color.0 * 0.78,
                node_color.1 * 0.78,
                node_color.2 * 0.78,
            ),
            (right - 8.0).max(40.0),
        );
    }
    for ((start_x, start_y), (end_x, end_y)) in &edges {
        draw_line(
            *start_x,
            *start_y,
            *end_x,
            *end_y,
            (color.0 * 0.46, color.1 * 0.46, color.2 * 0.46),
        );
    }
    if hidden_entities > 0 {
        pixel_text(
            12.0,
            (SCENE_HEIGHT - 14.0).max(12.0),
            &format!("+{hidden_entities} MORE ENTITIES"),
            (0.91, 0.62, 0.22),
            (right - 8.0).max(40.0),
        );
    }
    match mode {
        VisualMode::Think => draw_pulses((right * 0.5).max(28.0), 90.0, phase, color),
        VisualMode::Search => draw_scanner(phase, color, right),
        VisualMode::Read => draw_artifact(projection, color, phase, right),
        VisualMode::Code => draw_code_columns(projection, color, phase, right),
        VisualMode::Execute | VisualMode::Generate => draw_packets(&edges, phase, color, false),
        VisualMode::Recover => draw_packets(&edges, phase, color, true),
        VisualMode::Test => draw_test_gates(projection, &positions, phase, color, right),
        VisualMode::Verify => draw_verify_gate(projection, &positions, color, right),
        VisualMode::Approval => draw_approval_object(color, phase, right),
        VisualMode::Failure => draw_failure_fracture(color, phase, right),
        VisualMode::Success => draw_success_seal(color, right),
        VisualMode::Respond | VisualMode::Inspect | VisualMode::Idle => {}
    }
}

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

fn semantic_entity_count(projection: &Projection) -> usize {
    let source = if !projection.runtime_entities.is_empty() {
        &projection.runtime_entities
    } else {
        &projection.entities
    };
    source.iter().filter(|entity| !entity.id.is_empty()).count()
}

fn buddy_target(projection: &Projection, mode: VisualMode, right: f32) -> (f32, f32) {
    // Buddy stands on the perspective grid near the right half of the scene,
    // mirroring the AthenaBOX / DAGOAL reference composition.
    let preferred = match mode {
        VisualMode::Failure => ((right - 58.0).max(150.0), 188.0),
        VisualMode::Approval => ((right - 58.0).max(150.0), 194.0),
        VisualMode::Success => ((right - 58.0).max(150.0), 194.0),
        VisualMode::Code => ((right - 52.0).max(180.0), 180.0),
        VisualMode::Test | VisualMode::Verify | VisualMode::Execute => {
            ((right - 52.0).max(180.0), 184.0)
        }
        VisualMode::Search | VisualMode::Inspect => ((right - 96.0).max(150.0), 184.0),
        VisualMode::Read => ((right - 120.0).max(150.0), 182.0),
        VisualMode::Think | VisualMode::Respond | VisualMode::Generate | VisualMode::Recover => {
            ((right - 80.0).max(150.0), 184.0)
        }
        VisualMode::Idle => match projection
            .buddy
            .as_ref()
            .map(|buddy| buddy.anchor.to_ascii_lowercase())
            .as_deref()
        {
            Some("left") => (132.0, 184.0),
            Some("center") => (228.0, 184.0),
            _ => ((right - 96.0).max(150.0), 184.0),
        },
    };
    if semantic_entities(projection).is_empty() {
        return preferred;
    }

    // Buddy is an actor, not an opaque projection card. Prefer the authored
    // pose anchor, then move through open scene lanes if the enlarged sprite
    // would cover a projected node. The fallback anchors deliberately keep
    // the graph's two columns readable while retaining a visible actor.
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

fn buddy_anchor_is_clear(candidate: (f32, f32), projection: &Projection, right: f32) -> bool {
    let half_width = SPRITE_DIRTY_WIDTH / 2.0 + 4.0;
    let half_height = SPRITE_DIRTY_HEIGHT / 2.0 + 4.0;
    semantic_entities(projection)
        .iter()
        .enumerate()
        .all(|(index, entity)| {
            let node_x = if index % 2 == 0 {
                72.0
            } else {
                (right - 48.0).max(150.0)
            };
            let node_y = 74.0 + (index / 2) as f32 * 42.0;
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

fn draw_pulses(x: f32, y: f32, phase: f32, color: (f32, f32, f32)) {
    for index in 0..3 {
        let radius = 24.0 + ((phase * 34.0 + index as f32 * 24.0) % 74.0);
        draw_round_outline(
            x - radius,
            y - radius,
            radius * 2.0,
            radius * 2.0,
            (color.0 * 0.34, color.1 * 0.34, color.2 * 0.34),
        );
    }
}

fn draw_scanner(phase: f32, color: (f32, f32, f32), right: f32) {
    let right = right.clamp(46.0, SCENE_WIDTH - 8.0);
    let span = (right - 56.0).max(12.0);
    let sweep_x = 28.0 + phase.fract() * span;
    draw_rect(
        sweep_x,
        28.0,
        2.0,
        174.0,
        (color.0 * 0.84, color.1 * 0.84, color.2 * 0.84),
    );
    draw_rect(
        sweep_x - 16.0,
        28.0,
        32.0,
        174.0,
        (color.0 * 0.08, color.1 * 0.10, color.1 * 0.12),
    );
}

fn draw_artifact(projection: &Projection, color: (f32, f32, f32), phase: f32, right: f32) {
    let x = 78.0 + (phase * 8.0).sin() * 2.0;
    let width = (right - x - 18.0).clamp(80.0, 176.0);
    draw_round_outline(x, 46.0, width, 92.0, color);
    if let Some(code) = projection.code_view.as_ref() {
        let lines = if !code.lines.is_empty() {
            code.lines.clone()
        } else {
            code.text.lines().map(str::to_owned).collect()
        };
        for (index, line) in lines.iter().take(5).enumerate() {
            pixel_text(
                x + 10.0,
                60.0 + index as f32 * 13.0,
                line,
                color,
                x + width - 8.0,
            );
        }
    } else {
        pixel_text(
            x + 10.0,
            72.0,
            "ARTIFACT // NO PREVIEW",
            color,
            x + width - 8.0,
        );
    }
    draw_line(
        x + width,
        92.0,
        (x + width + 46.0).min(right - 8.0),
        92.0,
        (color.0 * 0.72, color.1 * 0.72, color.2 * 0.72),
    );
}

fn draw_code_columns(projection: &Projection, color: (f32, f32, f32), _phase: f32, right: f32) {
    if let Some(code) = projection.code_view.as_ref() {
        let x = 28.0;
        let width = (right - 42.0).max(100.0);
        draw_round_outline(x, 46.0, width, 112.0, color);
        let lines = if !code.diff.is_empty() {
            code.diff.clone()
        } else if !code.lines.is_empty() {
            code.lines.clone()
        } else {
            code.text.lines().map(str::to_owned).collect()
        };
        for (index, line) in lines.iter().take(8).enumerate() {
            let line_color = if line.starts_with('+') {
                (0.46, 0.91, 0.67)
            } else if line.starts_with('-') {
                (0.88, 0.28, 0.32)
            } else {
                (color.0 * 0.78, color.1 * 0.78, color.2 * 0.78)
            };
            pixel_text(
                x + 9.0,
                58.0 + index as f32 * 11.0,
                line,
                line_color,
                x + width - 8.0,
            );
        }
        if lines.is_empty() {
            pixel_text(
                x + 9.0,
                70.0,
                "CODE // NO PREVIEW DATA",
                color,
                x + width - 8.0,
            );
        } else if code.preview_truncated || lines.len() > 8 {
            let hidden = lines.len().saturating_sub(8);
            let summary = if hidden > 0 {
                format!("+{hidden} MORE LINES")
            } else {
                "MORE LINES // PREVIEW TRUNCATED".to_owned()
            };
            pixel_text(
                x + 9.0,
                150.0,
                &summary,
                (0.91, 0.62, 0.22),
                x + width - 8.0,
            );
        }
        draw_line(
            x + width + 5.0,
            102.0,
            (x + width + 25.0).min(right - 4.0),
            102.0,
            color,
        );
        return;
    }
    pixel_text(
        28.0,
        70.0,
        "CODE // WAITING FOR PROJECTION",
        color,
        (right - 8.0).max(40.0),
    );
}

fn draw_packets(
    edges: &[((f32, f32), (f32, f32))],
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
        draw_rect(x - 3.0, y - 3.0, 6.0, 6.0, color);
    }
}

fn draw_test_gates(
    projection: &Projection,
    _positions: &[(String, f32, f32)],
    _phase: f32,
    color: (f32, f32, f32),
    right: f32,
) {
    let checks = &projection.verification.checks;
    if checks.is_empty() {
        pixel_text(
            18.0,
            150.0,
            "TEST // AWAITING EVIDENCE",
            color,
            (right - 8.0).max(40.0),
        );
        return;
    }
    let count = checks.len().min(5);
    let start = 34.0;
    let spacing = ((right - start - 28.0) / count.max(1) as f32).max(24.0);
    let mut previous = None;
    for (index, check) in checks.iter().take(count).enumerate() {
        let x = start + index as f32 * spacing;
        let y = 150.0;
        let status = check_status(check);
        let gate_color = if status == "failed" {
            (0.88, 0.28, 0.32)
        } else if status == "passed" || status == "complete" {
            (0.46, 0.91, 0.67)
        } else {
            color
        };
        if let Some((previous_x, previous_y)) = previous {
            draw_line(
                previous_x,
                previous_y,
                x,
                y,
                (
                    gate_color.0 * 0.48,
                    gate_color.1 * 0.48,
                    gate_color.2 * 0.48,
                ),
            );
        }
        draw_round_outline(x - 14.0, y - 14.0, 28.0, 28.0, gate_color);
        pixel_text(
            x - 9.0,
            y + 3.0,
            if status == "passed" || status == "complete" {
                "OK"
            } else if status == "failed" {
                "X"
            } else {
                ".."
            },
            gate_color,
            (x + 12.0).min(right),
        );
        previous = Some((x, y));
    }
}

fn draw_verify_gate(
    projection: &Projection,
    _positions: &[(String, f32, f32)],
    color: (f32, f32, f32),
    right: f32,
) {
    if !projection.verification.checks.is_empty() {
        let target = (right - 54.0).max(160.0);
        let target_y = 172.0;
        for (index, check) in projection.verification.checks.iter().take(5).enumerate() {
            let x = 36.0 + index as f32 * ((target - 60.0) / 4.0).max(26.0);
            let y = 92.0 + (index % 3) as f32 * 30.0;
            let status = check_status(check);
            let check_color = if status == "failed" {
                (0.88, 0.28, 0.32)
            } else if status == "passed" || status == "complete" {
                (0.46, 0.91, 0.67)
            } else {
                color
            };
            draw_node(x, y, 8.0, check_color);
            draw_line(
                x,
                y,
                target,
                target_y,
                (
                    check_color.0 * 0.44,
                    check_color.1 * 0.44,
                    check_color.2 * 0.44,
                ),
            );
        }
        draw_round_outline(target - 22.0, target_y - 22.0, 44.0, 44.0, color);
        draw_rect(target - 9.0, target_y, 18.0, 2.0, color);
        draw_rect(target, target_y - 9.0, 2.0, 18.0, color);
    } else {
        pixel_text(
            18.0,
            150.0,
            "VERIFY // AWAITING EVIDENCE",
            color,
            (right - 8.0).max(40.0),
        );
    }
}

fn check_status(check: &serde_json::Value) -> &str {
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

fn draw_approval_object(color: (f32, f32, f32), phase: f32, right: f32) {
    let amber = (0.91, 0.62, 0.22);
    let pulse = (phase / 0.28).clamp(0.0, 1.0);
    let width = 66.0 * pulse;
    let x = (right - width - 12.0).max(120.0);
    draw_round_outline(x, 166.0 + 20.0 * (1.0 - pulse), width, 40.0 * pulse, amber);
    draw_rect(x + 16.0, 184.0, 34.0, 2.0, color);
}

fn draw_failure_fracture(_color: (f32, f32, f32), phase: f32, right: f32) {
    let red = (0.88, 0.28, 0.32);
    let shift = 2.0 * (1.0 - (phase / 0.24).clamp(0.0, 1.0));
    let x = (right - 48.0).max(120.0);
    draw_rect(x + shift, 150.0, 2.0, 44.0, red);
    draw_rect(x, 170.0, 48.0, 2.0, red);
    draw_rect(x + 18.0, 138.0, 2.0, 18.0, red);
    draw_rect(x + 32.0, 172.0, 2.0, 24.0, red);
}

fn draw_success_seal(color: (f32, f32, f32), right: f32) {
    let green = (0.46, 0.91, 0.67);
    let x = (right - 58.0 - 12.0).max(120.0);
    draw_round_outline(x, 148.0, 58.0, 44.0, green);
    draw_line(x + 12.0, 170.0, x + 22.0, 180.0, green);
    draw_line(x + 22.0, 180.0, x + 46.0, 158.0, green);
    draw_rect(x + 8.0, 194.0, 42.0, 2.0, color);
}

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
fn focal_object(mode: VisualMode) -> &'static str {
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
mod tests {
    use super::{
        AttentionAction, BuddyMotion, OiTarget, SCENE_HEIGHT, SCENE_WIDTH, SemanticSceneSnapshot,
        VisualMode, attention_hit_map, buddy_anchor_is_clear, buddy_target, motion_position,
        scene_safe_area, semantic_scene_snapshot,
    };
    use crate::buddy::SPRITE_DIRTY_WIDTH;
    use crate::{AnimationState, Projection, ProjectionAttention, ProjectionEntity};
    use crate::{
        ProjectionAction, ProjectionBuddy, ProjectionCodeView, ProjectionDiagnostic,
        ProjectionOperation, ProjectionVerification,
    };
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
                assert!(safe_area.unobscured_right < rail.x);
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
        assert_eq!(actual, expected);
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

        let (x, y) = physical(278.0, 67.0);
        assert_eq!(
            map.hit_physical(x, y, oi_inner),
            Some(&AttentionAction::Approve {
                approval_id: "apr_1".to_owned(),
                scope: "call".to_owned(),
            })
        );
        let (x, y) = physical(313.0, 67.0);
        assert_eq!(
            map.hit_physical(x, y, oi_inner),
            Some(&AttentionAction::Approve {
                approval_id: "apr_1".to_owned(),
                scope: "task".to_owned(),
            })
        );
        let (x, y) = physical(347.0, 67.0);
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
        let goldens: BTreeMap<String, SemanticSceneSnapshot> =
            serde_json::from_str(include_str!("../../assets/oi/semantic-scene-goldens.json"))
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
