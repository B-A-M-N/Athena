use super::super::*;
use super::buddy::draw_buddy;
use super::chassis::{PresentationSettings, bitmap_width, draw_bitmap_text};
use super::primitives::{
    draw_line, draw_line_alpha, draw_node, draw_rect, draw_round_outline, draw_round_rect,
};
use crate::buddy::{
    SPRITE_DIRTY_HEIGHT, SPRITE_DIRTY_WIDTH, SPRITE_HEIGHT, SPRITE_SCALE, SPRITE_WIDTH,
};
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
        // DAGOAL: keep buddy clearly visible but respect safe area for attention rail
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
        (
            // Keep actor motion on logical pixels so it reads as stepped
            // phosphor movement instead of a floating sine-wave bob.
            clamp_x(motion.current.0).round(),
            clamp_y(motion.current.1).round(),
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
    let safe_area = scene_safe_area(projection.attention_items.len());
    let buddy_position =
        buddy_anchor_is_clear(buddy_position, projection, safe_area.unobscured_right)
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
            glClearColor(0.008, 0.026, 0.036, 1.0);
            glClear(GL_COLOR_BUFFER_BIT);
        }
        draw_scene_contents(
            projection,
            phase,
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
        phase,
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
// Scene layout
// ---------------------------------------------------------------------------

const HEADER_TOP: f32 = 8.0;
const HEADER_HEIGHT: f32 = 22.0;
const READOUT_HEIGHT: f32 = 96.0;
const MARGIN: f32 = 10.0;

#[derive(Clone, Copy, Debug)]
struct SceneLayout {
    full: PixelRect,
    header: PixelRect,
    stage: PixelRect,
    readout: Option<PixelRect>,
}

fn scene_layout(safe_area: SceneSafeArea) -> SceneLayout {
    let full = PixelRect {
        x: MARGIN,
        y: HEADER_TOP + HEADER_HEIGHT + 4.0,
        width: (safe_area.unobscured_right - MARGIN * 2.0).max(20.0),
        height: SCENE_HEIGHT - HEADER_TOP - HEADER_HEIGHT - 14.0,
    };
    let header = PixelRect {
        x: MARGIN,
        y: HEADER_TOP,
        width: full.width,
        height: HEADER_HEIGHT,
    };
    SceneLayout {
        full,
        header,
        stage: full,
        readout: None,
    }
}

fn scene_layout_with_readout(safe_area: SceneSafeArea) -> SceneLayout {
    let base = scene_layout(safe_area);
    let readout = PixelRect {
        x: base.full.x + 8.0,
        y: base.full.bottom() - READOUT_HEIGHT - 2.0,
        width: (base.full.width - 16.0).max(20.0),
        height: READOUT_HEIGHT,
    };
    let stage = PixelRect {
        x: base.full.x,
        y: base.full.y,
        width: base.full.width,
        height: (readout.y - base.full.y - 6.0).max(20.0),
    };
    SceneLayout {
        full: base.full,
        header: base.header,
        stage,
        readout: Some(readout),
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
    let color = rgb_f32(mode_color(mode.as_str()));
    let brightness = presentation.brightness;
    draw_rect(
        0.0,
        0.0,
        SCENE_WIDTH,
        SCENE_HEIGHT,
        (0.008 * brightness, 0.026 * brightness, 0.036 * brightness),
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
    );

    let layout = if mode.shows_readout() {
        scene_layout_with_readout(safe_area)
    } else {
        scene_layout(safe_area)
    };

    draw_chrome(projection, mode, &layout, color, safe_area.unobscured_right);

    match mode {
        VisualMode::Idle => draw_idle_scene(projection, &layout, color, phase),
        VisualMode::Inspect | VisualMode::Search => {
            draw_workspace_scene(projection, &layout, color, phase, mode)
        }
        VisualMode::Read => draw_read_scene(projection, &layout, color, phase),
        VisualMode::Code => draw_code_scene(projection, &layout, color, phase),
        VisualMode::Execute | VisualMode::Generate | VisualMode::Recover => {
            draw_execute_scene(projection, &layout, color, phase, mode)
        }
        VisualMode::Test | VisualMode::Verify => draw_test_scene(projection, &layout, color, phase),
        VisualMode::Approval => draw_approval_scene(projection, &layout, color, phase),
        VisualMode::Failure => draw_failure_scene(projection, &layout, color, phase),
        VisualMode::Success => draw_success_scene(&layout, color, phase),
        VisualMode::Think | VisualMode::Respond => {
            draw_think_scene(projection, &layout, color, phase)
        }
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
    if let Some((buddy_x, buddy_y)) = buddy_position {
        draw_buddy(buddy_x, buddy_y, state, status, character, phase);
    }
    draw_attention_rail(projection, safe_area);
}

fn draw_crt_treatment(color: (f32, f32, f32), brightness: f32, focus: f32, phase: f32) {
    // This pass stays in the low-resolution target, so the scanline rhythm is
    // coherent after nearest-neighbour composition instead of becoming a
    // monitor-sized overlay that scales differently at every window size.
    let flicker = 0.986 + (phase * std::f32::consts::TAU * 1.15).sin() * 0.010;
    let scanline_strength = (0.032 + (1.0 - focus) * 0.035) * flicker;
    let scanline = (
        color.0 * scanline_strength,
        color.1 * scanline_strength,
        color.2 * scanline_strength,
    );
    // Fine cathode-ray texture stays subordinate to the matrix dots.
    for y in (2..SCENE_HEIGHT as i32 - 2).step_by(4) {
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

fn draw_terrain(color: (f32, f32, f32), phase: f32, mode: VisualMode, _focus: f32, right: f32) {
    let horizon = 152.0;
    let quiet = matches!(mode, VisualMode::Idle);
    let base_grid = (color.0 * 0.34, color.1 * 0.34, color.2 * 0.34);
    let grid = if quiet {
        (base_grid.0 * 0.48, base_grid.1 * 0.48, base_grid.2 * 0.48)
    } else {
        base_grid
    };
    let near_grid = (grid.0 * 1.35, grid.1 * 1.35, grid.2 * 1.35);
    let far_grid = (grid.0 * 0.55, grid.1 * 0.55, grid.2 * 0.55);
    // A dense perspective vector ground plane receding beneath Buddy.
    // Horizontal rungs are spaced with quadratic perspective (t*t) and crawl
    // downward so the plane feels alive without drifting the horizon.
    let crawl = ((phase.max(0.0) * 0.25).floor() % 16.0) / 16.0;
    for index in 0..16 {
        let t = ((index as f32 + crawl) / 16.0).min(0.995);
        let perspective = t * t;
        let row_y = horizon + (SCENE_HEIGHT - horizon - 8.0) * perspective;
        let line_color = if t > 0.75 { near_grid } else { grid };
        draw_dotted_span(14.0, row_y, (right - 14.0).max(14.0), line_color, 2.0);
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
    // Sparse ambient markers only in idle; active scenes reserve the field
    // for semantic motion.
    if quiet {
        for index in 0..6 {
            let px = (index * 67 % ((right as usize).saturating_sub(36)) + 18) as f32;
            let py = (index * 31 % 96 + 18) as f32;
            let pulse = 0.45 + 0.25 * ((phase * 2.0 + index as f32 * 1.1).sin());
            draw_rect(
                px,
                py,
                1.0,
                1.0,
                (color.0 * pulse, color.1 * pulse, color.2 * pulse),
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Chrome
// ---------------------------------------------------------------------------

fn draw_chrome(
    projection: &Projection,
    mode: VisualMode,
    _layout: &SceneLayout,
    color: (f32, f32, f32),
    right: f32,
) {
    let bright = (color.0 * 0.92, color.1 * 0.92, color.2 * 0.92);
    let dim = (color.0 * 0.58, color.1 * 0.58, color.2 * 0.58);
    pixel_text(
        10.0,
        14.0,
        "ATHENA OI // GLASS COMPUTE",
        bright,
        right - 10.0,
    );

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
        (right - badge_width - 10.0).max(10.0),
        14.0,
        &badge,
        dim,
        right - 10.0,
    );

    // Dotted header rule.
    let mut rule_x = 10.0;
    while rule_x + 3.0 < right - 10.0 {
        draw_rect(
            rule_x,
            27.0,
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
        pixel_text(10.0, 35.0, &context, dim, right - 10.0);
    }

    if let Some(request) = projection.model_request.as_ref() {
        if request.status.eq_ignore_ascii_case("unconfigured") {
            pixel_text(
                (right - 170.0).max(10.0),
                35.0,
                "MODEL UNCONFIGURED",
                (0.91, 0.62, 0.22),
                right - 10.0,
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Live readout pane
// ---------------------------------------------------------------------------

fn draw_readout_frame(rect: PixelRect, color: (f32, f32, f32)) {
    if rect.width <= 0.0 || rect.height <= 0.0 {
        return;
    }
    let glass = (color.0 * 0.12, color.1 * 0.22, color.2 * 0.24);
    let rim = (color.0 * 0.55, color.1 * 0.55, color.2 * 0.55);
    draw_round_rect(
        rect.x,
        rect.y,
        rect.width,
        rect.height,
        5.0,
        (0.006, 0.014, 0.016),
    );
    draw_round_outline(rect.x, rect.y, rect.width, rect.height, rim);
    // Inner glass sheen
    draw_line_alpha(
        rect.x + 4.0,
        rect.y + 2.0,
        rect.x + rect.width * 0.55,
        rect.y + 2.0,
        glass,
        0.35,
    );
}

fn draw_live_readout(
    projection: &Projection,
    mode: VisualMode,
    rect: PixelRect,
    color: (f32, f32, f32),
    phase: f32,
) {
    draw_readout_frame(rect, color);
    if rect.width <= 14.0 || rect.height <= 14.0 {
        return;
    }
    let inner_x = rect.x + 6.0;
    let inner_y = rect.y + 7.0;
    let inner_right = rect.right() - 6.0;
    let line_height = 9.0;
    let bright = (color.0 * 0.92, color.1 * 0.92, color.2 * 0.92);
    let dim = (color.0 * 0.58, color.1 * 0.58, color.2 * 0.58);

    let mut y = inner_y;
    let max_lines = ((rect.height - 14.0) / line_height).floor().max(1.0) as usize;

    match mode {
        VisualMode::Read => {
            if let Some(code) = projection.code_view.as_ref() {
                let header = format!("READ  {}", code.path);
                pixel_text(inner_x, y, &header, bright, inner_right);
                y += line_height;
                let lines = if !code.lines.is_empty() {
                    &code.lines
                } else {
                    &code.text.lines().map(str::to_owned).collect::<Vec<_>>()
                };
                let scan_index = ((phase * 1.4).floor() as usize) % lines.len().max(1);
                for (index, line) in lines.iter().take(max_lines.saturating_sub(1)).enumerate() {
                    let _ = index;
                    let dimmed = if index == scan_index { 1.0 } else { 0.65 };
                    pixel_text(
                        inner_x,
                        y,
                        line,
                        (
                            color.0 * 0.75 * dimmed,
                            color.1 * 0.82 * dimmed,
                            color.2 * 0.85 * dimmed,
                        ),
                        inner_right,
                    );
                    y += line_height;
                }
                if code.preview_truncated {
                    pixel_text(inner_x, y, "...", dim, inner_right);
                }
            } else {
                pixel_text(inner_x, y, "AWAITING ARTIFACT", dim, inner_right);
            }
        }
        VisualMode::Code => {
            if let Some(code) = projection.code_view.as_ref() {
                let header = format!("MODIFY  {}", code.path);
                pixel_text(inner_x, y, &header, bright, inner_right);
                y += line_height;
                let lines = if !code.diff.is_empty() {
                    code.diff.clone()
                } else if !code.lines.is_empty() {
                    code.lines.clone()
                } else {
                    code.text.lines().map(str::to_owned).collect()
                };
                for line in lines.iter().take(max_lines.saturating_sub(1)) {
                    let line_color = if line.starts_with('+') {
                        (0.46, 0.91, 0.67)
                    } else if line.starts_with('-') {
                        (0.88, 0.28, 0.32)
                    } else {
                        (color.0 * 0.75, color.1 * 0.82, color.2 * 0.85)
                    };
                    pixel_text(inner_x, y, line, line_color, inner_right);
                    y += line_height;
                }
                if !code.mutation_state.is_empty() {
                    pixel_text(
                        inner_x,
                        y,
                        &code.mutation_state.to_ascii_uppercase(),
                        dim,
                        inner_right,
                    );
                }
            } else {
                pixel_text(inner_x, y, "AWAITING CODE VIEW", dim, inner_right);
            }
        }
        VisualMode::Execute | VisualMode::Generate | VisualMode::Recover => {
            let operation = projection.active_operation.as_ref();
            let command =
                operation.and_then(|op| (!op.command.is_empty()).then_some(op.command.as_str()));
            if let Some(command) = command {
                pixel_text(inner_x, y, &format!("$ {command}"), bright, inner_right);
                y += line_height;
            }
            let tail: Vec<&str> = projection
                .stream_tail
                .iter()
                .rev()
                .take(max_lines.saturating_sub(1))
                .map(String::as_str)
                .collect();
            for line in tail.iter().rev() {
                pixel_text(inner_x, y, line, dim, inner_right);
                y += line_height;
            }
            if operation.is_some_and(|op| op.progress_determinate) {
                if let Some(value) = operation.and_then(|op| op.progress_value) {
                    let bar_width = (rect.width - 16.0).max(4.0);
                    let filled = bar_width * value as f32;
                    draw_rect(
                        inner_x,
                        y,
                        bar_width,
                        3.0,
                        (color.0 * 0.22, color.1 * 0.22, color.2 * 0.22),
                    );
                    draw_rect(inner_x, y, filled, 3.0, color);
                }
            }
        }
        VisualMode::Test | VisualMode::Verify => {
            pixel_text(inner_x, y, "VERIFICATION", bright, inner_right);
            y += line_height;
            let status = projection.verification.status.to_ascii_uppercase();
            if !status.is_empty() {
                pixel_text(inner_x, y, &status, dim, inner_right);
                y += line_height;
            }
            for check in projection
                .verification
                .checks
                .iter()
                .take(max_lines.saturating_sub(2))
            {
                let text = check.to_string();
                let status = check_status(check);
                let check_color = if status == "failed" {
                    (0.88, 0.28, 0.32)
                } else if status == "passed" || status == "complete" {
                    (0.46, 0.91, 0.67)
                } else {
                    dim
                };
                pixel_text(inner_x, y, &text, check_color, inner_right);
                y += line_height;
            }
        }
        VisualMode::Failure => {
            pixel_text(inner_x, y, "DIAGNOSTICS", bright, inner_right);
            y += line_height;
            for diagnostic in projection
                .diagnostics
                .iter()
                .take(max_lines.saturating_sub(1))
            {
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
                pixel_text(inner_x, y, &location, (0.84, 0.45, 0.30), inner_right);
                y += line_height;
                if !diagnostic.detail.is_empty() {
                    pixel_text(inner_x, y, &diagnostic.detail, dim, inner_right);
                    y += line_height;
                }
            }
        }
        _ => {}
    }
}

// ---------------------------------------------------------------------------
// Scene primitives
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
#[allow(dead_code)]
struct TreeLayoutNode {
    id: String,
    label: String,
    kind: String,
    status: String,
    x: f32,
    y: f32,
    radius: f32,
    children: Vec<usize>,
    parent: Option<usize>,
}

fn layout_tree(tree: &[ProjectionTreeNode], stage: PixelRect) -> Vec<TreeLayoutNode> {
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
            label: node.label.clone(),
            kind: node.kind.clone(),
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

fn node_status_color(status: &str, base: (f32, f32, f32)) -> (f32, f32, f32) {
    match status.to_ascii_lowercase().as_str() {
        "failed" | "error" | "failure" => (0.88, 0.28, 0.32),
        "passed" | "complete" | "ready" | "ok" | "success" | "succeeded" => (0.46, 0.91, 0.67),
        "approval" | "warning" => (0.91, 0.62, 0.22),
        "reading" | "testing" | "running" | "active" | "working" => (
            base.0 * (0.72 + 0.28),
            base.1 * (0.72 + 0.28),
            base.2 * (0.72 + 0.28),
        ),
        _ => (base.0 * 0.72, base.1 * 0.72, base.2 * 0.72),
    }
}

fn draw_tree_nodes(
    nodes: &[TreeLayoutNode],
    color: (f32, f32, f32),
    phase: f32,
    active_id: Option<&str>,
) {
    let dim = (color.0 * 0.45, color.1 * 0.45, color.2 * 0.45);
    // Edges first so nodes sit on top.
    for node in nodes.iter() {
        if let Some(parent) = node.parent.and_then(|index| nodes.get(index)) {
            draw_dotted_line(parent.x, parent.y, node.x, node.y, dim);
        }
    }
    for node in nodes.iter() {
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
        if node.label.len() <= 16 {
            pixel_text(
                node.x - 18.0,
                node.y + radius + 6.0,
                &node.label,
                (node_color.0 * 0.8, node_color.1 * 0.8, node_color.2 * 0.8),
                node.x + 40.0,
            );
        }
    }
}

fn runtime_entity_nodes(projection: &Projection) -> Vec<&ProjectionEntity> {
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

fn layout_runtime_graph<'a>(
    entities: &'a [&'a ProjectionEntity],
    stage: PixelRect,
) -> Vec<(f32, f32, f32, &'a ProjectionEntity)> {
    let count = entities.len();
    if count == 0 {
        return Vec::new();
    }
    // Simple left-to-right pipeline layout with vertical spreading for branches.
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

fn draw_runtime_graph(
    layout: &[(f32, f32, f32, &ProjectionEntity)],
    color: (f32, f32, f32),
    phase: f32,
) {
    let dim = (color.0 * 0.45, color.1 * 0.45, color.2 * 0.45);
    // Edges by parent_id.
    let by_id: HashMap<&str, (f32, f32)> = layout
        .iter()
        .map(|(x, y, _, entity)| (entity.id.as_str(), (*x, *y)))
        .collect();
    for (x, y, _, entity) in layout.iter() {
        if let Some(parent_id) = entity.parent_id.as_deref() {
            if let Some((px, py)) = by_id.get(parent_id) {
                draw_dotted_line(*px, *py, *x, *y, dim);
            }
        }
    }
    for (x, y, radius, entity) in layout.iter() {
        let node_color = node_status_color(&entity.status, color);
        draw_node(*x, *y, *radius, node_color);
        let label = if entity.label.is_empty() {
            entity_label(entity)
        } else {
            entity.label.clone()
        };
        if label.len() <= 16 {
            pixel_text(
                x - 20.0,
                y + radius + 6.0,
                &label,
                (node_color.0 * 0.8, node_color.1 * 0.8, node_color.2 * 0.8),
                x + 40.0,
            );
        }
    }
    // Activity packets travel along edges.
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
// Individual scenes
// ---------------------------------------------------------------------------

fn draw_idle_scene(
    projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
) {
    // Understated Athena identity glyph in the upper stage.
    let cx = layout.stage.x + layout.stage.width * 0.5;
    let cy = layout.stage.y + layout.stage.height * 0.35;
    let pulse = 0.5 + 0.5 * (phase * std::f32::consts::TAU * 0.4).sin();
    draw_node(
        cx,
        cy,
        16.0,
        (color.0 * pulse, color.1 * pulse, color.2 * pulse),
    );
    // Small status word.
    let status = if projection.status.is_empty() {
        "READY".to_string()
    } else {
        projection.status.to_ascii_uppercase()
    };
    pixel_text(
        cx - bitmap_width(&status, 1.0) * 0.5,
        cy + 26.0,
        &status,
        (color.0 * 0.7, color.1 * 0.7, color.2 * 0.7),
        layout.stage.right(),
    );
}

fn draw_workspace_scene(
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

fn draw_read_scene(
    projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
) {
    // Stage shows the focused artifact with a scan ring.
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
    if let Some(code) = projection.code_view.as_ref() {
        let name = code
            .path
            .rsplit_once('/')
            .map_or(code.path.as_str(), |(_, name)| name);
        pixel_text(
            cx - 22.0,
            cy + 28.0,
            name,
            color,
            layout.stage.right() - 8.0,
        );
    }
    if let Some(readout) = layout.readout {
        draw_live_readout(projection, VisualMode::Read, readout, color, phase);
    }
}

fn draw_code_scene(
    projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
) {
    // Workspace topology above, active file highlighted.
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

    // Focal artifact glow near the readout.
    if let Some(readout) = layout.readout {
        let cx = readout.x + readout.width * 0.88;
        let cy = layout.stage.y + layout.stage.height * 0.72;
        let pulse = 0.5 + 0.5 * (phase * std::f32::consts::TAU * 1.2).sin();
        draw_rect(
            cx - 3.0,
            cy - 3.0,
            6.0,
            6.0,
            (color.0 * pulse, color.1 * pulse, color.2 * pulse),
        );
    }

    if let Some(readout) = layout.readout {
        draw_live_readout(projection, VisualMode::Code, readout, color, phase);
    }
}

fn draw_execute_scene(
    projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
    mode: VisualMode,
) {
    let entities = runtime_entity_nodes(projection);
    let graph = layout_runtime_graph(&entities, layout.stage);
    draw_runtime_graph(&graph, color, phase);
    // Process pulse in the lower stage.
    let cx = layout.stage.x + layout.stage.width * 0.5;
    let cy = layout.stage.y + layout.stage.height * 0.82;
    let pulse = 0.5 + 0.5 * (phase * std::f32::consts::TAU * 0.9).sin();
    draw_node(
        cx,
        cy,
        10.0,
        (color.0 * pulse, color.1 * pulse, color.2 * pulse),
    );
    let label = if mode == VisualMode::Recover {
        "RECOVER"
    } else {
        "EXECUTE"
    };
    pixel_text(
        cx - bitmap_width(label, 1.0) * 0.5,
        cy + 18.0,
        label,
        (color.0 * 0.7, color.1 * 0.7, color.2 * 0.7),
        layout.stage.right(),
    );
    if let Some(readout) = layout.readout {
        draw_live_readout(projection, mode, readout, color, phase);
    }
}

fn draw_test_scene(
    projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
) {
    let checks = &projection.verification.checks;
    let cx = layout.stage.x + layout.stage.width * 0.5;
    let cy = layout.stage.y + layout.stage.height * 0.42;
    if checks.is_empty() {
        draw_node(cx, cy, 14.0, color);
        pixel_text(
            cx - 28.0,
            cy + 22.0,
            "AWAITING EVIDENCE",
            color,
            layout.stage.right() - 8.0,
        );
    } else {
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
            let glyph = if status == "passed" || status == "complete" {
                "OK"
            } else if status == "failed" {
                "X"
            } else {
                ".."
            };
            pixel_text(x - 4.0, y + 3.0, glyph, gate_color, x + 12.0);
        }
        draw_node(cx, cy, 14.0, color);
    }
    if let Some(readout) = layout.readout {
        draw_live_readout(projection, VisualMode::Test, readout, color, phase);
    }
}

fn draw_approval_scene(
    projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
) {
    let amber = (0.91, 0.62, 0.22);
    let cx = layout.stage.x + layout.stage.width * 0.5;
    let cy = layout.stage.y + layout.stage.height * 0.45;
    let pulse = (phase / 0.28).clamp(0.0, 1.0);
    let width = 74.0 * pulse;
    let height = 48.0 * pulse;
    draw_round_outline(cx - width * 0.5, cy - height * 0.5, width, height, amber);
    draw_rect(cx - 14.0, cy, 28.0, 2.0, amber);
    draw_rect(cx, cy - 14.0, 2.0, 28.0, amber);
    pixel_text(
        cx - 40.0,
        cy + 32.0,
        "POLICY GATE",
        amber,
        layout.stage.right() - 8.0,
    );
    // Approval summary in readout area even though readout is not the primary
    // surface; it keeps the required action legible.
    if let Some(readout) = layout.readout {
        draw_readout_frame(readout, color);
        if let Some(item) = projection.attention_items.first() {
            let x = readout.x + 6.0;
            let mut y = readout.y + 9.0;
            pixel_text(x, y, &item.title, amber, readout.right() - 6.0);
            y += 9.0;
            pixel_text(
                x,
                y,
                &item.summary,
                (color.0 * 0.75, color.1 * 0.82, color.2 * 0.85),
                readout.right() - 6.0,
            );
        }
    }
}

fn draw_failure_scene(
    projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
) {
    let red = (0.88, 0.28, 0.32);
    let cx = layout.stage.x + layout.stage.width * 0.35;
    let cy = layout.stage.y + layout.stage.height * 0.45;
    // Fracture marks around the focal failure.
    let shift = 2.0 * (1.0 - (phase / 0.24).clamp(0.0, 1.0));
    draw_rect(cx - 24.0 + shift, cy - 22.0, 2.0, 44.0, red);
    draw_rect(cx - 24.0, cy, 48.0, 2.0, red);
    draw_rect(cx + 8.0, cy - 18.0, 2.0, 18.0, red);
    draw_rect(cx + 18.0, cy + 4.0, 2.0, 24.0, red);
    draw_node(cx, cy, 16.0, red);
    pixel_text(
        cx - 20.0,
        cy + 26.0,
        "FAILURE",
        red,
        layout.stage.right() - 8.0,
    );
    if let Some(readout) = layout.readout {
        draw_live_readout(projection, VisualMode::Failure, readout, color, phase);
    }
}

fn draw_success_scene(layout: &SceneLayout, _color: (f32, f32, f32), phase: f32) {
    let green = (0.46, 0.91, 0.67);
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
    pixel_text(
        cx - 34.0,
        cy + 30.0,
        "VERIFIED",
        green,
        layout.stage.right() - 8.0,
    );
}

fn draw_think_scene(
    projection: &Projection,
    layout: &SceneLayout,
    color: (f32, f32, f32),
    phase: f32,
) {
    let cx = layout.stage.x + layout.stage.width * 0.5;
    let cy = layout.stage.y + layout.stage.height * 0.42;
    for index in 0..3 {
        let radius = 18.0 + ((phase * 34.0 + index as f32 * 24.0) % 60.0);
        draw_round_outline(
            cx - radius,
            cy - radius,
            radius * 2.0,
            radius * 2.0,
            (color.0 * 0.34, color.1 * 0.34, color.2 * 0.34),
        );
    }
    draw_node(cx, cy, 10.0, color);
    let label = projection
        .progress
        .as_ref()
        .and_then(|value| value.as_object())
        .and_then(|object| object.get("label"))
        .and_then(serde_json::Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| "REASONING".to_owned());
    pixel_text(
        cx - bitmap_width(&label, 1.0) * 0.5,
        cy + 24.0,
        &label,
        (color.0 * 0.7, color.1 * 0.7, color.2 * 0.7),
        layout.stage.right() - 8.0,
    );
}

// ---------------------------------------------------------------------------
// Attention rail and helpers (preserved behavior)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Low-level primitives
// ---------------------------------------------------------------------------

fn draw_dotted_span(left: f32, y: f32, right: f32, color: (f32, f32, f32), dot: f32) {
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

fn draw_dotted_line(x1: f32, y1: f32, x2: f32, y2: f32, color: (f32, f32, f32)) {
    let distance = ((x2 - x1).powi(2) + (y2 - y1).powi(2)).sqrt();
    let segments = (distance / 8.0).ceil().max(1.0) as usize;
    for index in (0..segments).step_by(2) {
        let t0 = index as f32 / segments as f32;
        let t1 = ((index + 1).min(segments)) as f32 / segments as f32;
        draw_rect(
            x1 + (x2 - x1) * t0,
            y1 + (y2 - y1) * t0,
            ((x2 - x1) * (t1 - t0)).abs().max(1.0),
            ((y2 - y1) * (t1 - t0)).abs().max(1.0),
            color,
        );
    }
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

fn pixel_text(x: f32, y: f32, value: &str, color: (f32, f32, f32), right: f32) {
    draw_bitmap_text(x, y, value, 1.0, color, right);
}

#[allow(dead_code)]
fn pixel_text_scaled(x: f32, y: f32, value: &str, color: (f32, f32, f32), right: f32, scale: f32) {
    draw_bitmap_text(x, y, value, scale.max(1.0), color, right);
}

type PacketEdge = ((f32, f32), (f32, f32));

fn draw_packets(edges: &[PacketEdge], phase: f32, color: (f32, f32, f32), reverse: bool) {
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

fn buddy_target(projection: &Projection, mode: VisualMode, right: f32) -> (f32, f32) {
    // Buddy stands on the perspective grid near the right half of the scene,
    // mirroring the AthenaBOX / DAGOAL reference composition.
    let stage_x = (right * if right < 300.0 { 0.54 } else { 0.62 }).clamp(132.0, right - 52.0);
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
            Some("left") => (132.0, 184.0),
            Some("center") => (228.0, 184.0),
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

fn buddy_anchor_is_clear(candidate: (f32, f32), projection: &Projection, right: f32) -> bool {
    // Target selection uses the visible authored matrix. The exact sprite
    // envelope is the actor silhouette; no external pose marks are drawn.
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

// ---------------------------------------------------------------------------
// Mode helpers and tests
// ---------------------------------------------------------------------------

impl VisualMode {
    fn shows_readout(self) -> bool {
        matches!(
            self,
            VisualMode::Read
                | VisualMode::Code
                | VisualMode::Execute
                | VisualMode::Generate
                | VisualMode::Recover
                | VisualMode::Test
                | VisualMode::Verify
                | VisualMode::Failure
        )
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
