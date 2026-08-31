use super::super::*;
use super::buddy::draw_buddy;
use super::chassis::PresentationSettings;
use super::primitives::{draw_line, draw_node, draw_rect, draw_round_outline, draw_round_rect};
use crate::buddy::{SPRITE_DIRTY_HEIGHT, SPRITE_DIRTY_WIDTH};
use crate::{ProjectionEntity, ProjectionTreeNode};
use std::cell::RefCell;
use std::ptr;

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
        x: 274.0,
        y: 12.0,
        width: 98.0,
        height: 218.0,
    };
    SceneSafeArea {
        unobscured_right: rail.x - 8.0,
        attention_rail: Some(rail),
    }
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
        let desired = buddy_target(projection, mode, safe_area.unobscured_right);
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
        let half_width = SPRITE_DIRTY_WIDTH / 2.0;
        let half_height = SPRITE_DIRTY_HEIGHT / 2.0;
        (
            (motion.current.0).clamp(
                half_width,
                (safe_area.unobscured_right - half_width).max(half_width),
            ),
            (motion.current.1 + (phase * 1.7).sin() * 1.5)
                .clamp(half_height, SCENE_HEIGHT - half_height),
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
    unsafe {
        glEnable(GL_TEXTURE_2D);
        glBindTexture(GL_TEXTURE_2D, texture);
        glColor3f(1.0, 1.0, 1.0);
        glBegin(GL_QUADS);
        for row in 0..4 {
            let v0 = row as f32 / 4.0;
            let v1 = (row + 1) as f32 / 4.0;
            for column in 0..4 {
                let u0 = column as f32 / 4.0;
                let u1 = (column + 1) as f32 / 4.0;
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
        0.025 + (1.0 - brightness) * 0.025,
        0.060 + (1.0 - focus) * 0.035,
        0.064 + (1.0 - focus) * 0.035,
    );
    draw_round_outline(
        x + 2.0,
        y + 2.0,
        (width - 4.0).max(0.0),
        (height - 4.0).max(0.0),
        edge,
    );
    draw_round_outline(
        x + 7.0,
        y + 7.0,
        (width - 14.0).max(0.0),
        (height - 14.0).max(0.0),
        (edge.0 * 0.42, edge.1 * 0.42, edge.2 * 0.42),
    );
    draw_line(
        x + width * 0.10,
        y + 3.0,
        x + width * 0.72,
        y + 3.0,
        (0.12, 0.22, 0.22),
    );
    draw_line(
        x + 3.0,
        y + height * 0.12,
        x + 3.0,
        y + height * 0.68,
        (0.08, 0.16, 0.17),
    );
    draw_line(
        x + width * 0.28,
        y + height - 3.0,
        x + width * 0.88,
        y + height - 3.0,
        (0.012, 0.028, 0.030),
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
        color.0 * (0.08 + (1.0 - focus) * 0.08),
        color.1 * (0.08 + (1.0 - focus) * 0.08),
        color.2 * (0.08 + (1.0 - focus) * 0.08),
    );
    for y in (3..SCENE_HEIGHT as i32 - 3).step_by(4) {
        draw_rect(2.0, y as f32, SCENE_WIDTH - 4.0, 1.0, scanline);
    }
    let edge = (
        0.003 + (1.0 - brightness) * 0.012,
        0.010 + (1.0 - brightness) * 0.018,
        0.014 + (1.0 - brightness) * 0.022,
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
}

fn draw_terrain(color: (f32, f32, f32), phase: f32, mode: VisualMode, focus: f32, right: f32) {
    let horizon = 148.0;
    let quiet = matches!(mode, VisualMode::Idle);
    let grid = (color.0 * 0.34, color.1 * 0.34, color.2 * 0.34);
    let grid = if quiet {
        (grid.0 * 0.48, grid.1 * 0.48, grid.2 * 0.48)
    } else {
        grid
    };
    for index in 0..7 {
        let t = index as f32 / 7.0;
        let row_y = horizon + (SCENE_HEIGHT - horizon) * t * t;
        draw_rect(18.0, row_y, (right - 18.0).max(0.0), 1.0, grid);
    }
    for index in 0..9 {
        let bottom_x = 18.0 + index as f32 * (right - 36.0).max(0.0) / 8.0;
        draw_line(192.0, horizon, bottom_x, SCENE_HEIGHT - 12.0, grid);
    }
    draw_rect(
        18.0,
        horizon,
        (right - 36.0).max(0.0),
        1.0,
        (color.0 * 0.65, color.1 * 0.65, color.2 * 0.65),
    );
    let particle_count = 8 + (focus * 14.0) as usize;
    for index in 0..particle_count {
        let px = (index * 67 % 361 + 12) as f32;
        let py = (index * 31 % 126 + 12) as f32;
        let pulse = 0.45 + 0.45 * ((phase * 1.4 + index as f32).sin().abs());
        draw_rect(
            px,
            py,
            2.0,
            2.0,
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
        &format!("DAGOAL // {}", mode.as_str().to_ascii_uppercase()),
        bright,
        right,
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
    pixel_text(
        14.0,
        30.0,
        &format!("{} // {}", mode.as_str(), label),
        bright,
        right,
    );
    if !target.is_empty() {
        pixel_text(14.0, 40.0, target, dim, right);
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
        pixel_text(14.0, 50.0, detail, dim, right);
    }
    if let Some(operation) = operation {
        if !operation.id.is_empty() {
            pixel_text(
                (right - 112.0).max(14.0),
                68.0,
                &format!("ID {}", operation.id),
                dim,
                right,
            );
        }
        let operation_kind = if !operation.action_kind.is_empty() {
            operation.action_kind.as_str()
        } else {
            operation.capability.as_str()
        };
        if !operation.state.is_empty() || !operation_kind.is_empty() {
            pixel_text(
                14.0,
                58.0,
                &format!("{} [{}]", operation_kind, operation.state),
                dim,
                right,
            );
        }
        if !operation.mutation_state.is_empty() {
            pixel_text(
                (right - 112.0).max(14.0),
                58.0,
                &format!("MUT {}", operation.mutation_state),
                dim,
                right,
            );
        }
    }

    if let Some(request) = projection.model_request.as_ref() {
        let model = if request.provider.is_empty() {
            request.model.clone()
        } else if request.model.is_empty() {
            request.provider.clone()
        } else {
            // A compact provider/model marker keeps plumbing visible without
            // taking the focal slot from the task itself.
            format!("{}/{}", request.provider, request.model)
        };
        if !model.is_empty()
            && (request.status.eq_ignore_ascii_case("active")
                || request.status.eq_ignore_ascii_case("failed")
                || request.status.eq_ignore_ascii_case("retry"))
        {
            pixel_text(
                (right - 112.0).max(14.0),
                30.0,
                &format!("M {}", model),
                dim,
                right,
            );
        }
        if !request.role.is_empty() && request.status.eq_ignore_ascii_case("active") {
            pixel_text(
                (right - 112.0).max(14.0),
                40.0,
                &format!("ROLE {}", request.role),
                dim,
                right,
            );
        }
    }

    let mut y = 66.0;
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
        VisualMode::Think | VisualMode::Respond | VisualMode::Idle => {
            if let Some(progress) = projection.progress.as_ref() {
                if let Some(object) = progress.as_object() {
                    if let Some(label) = object.get("label").and_then(serde_json::Value::as_str) {
                        pixel_text(14.0, y, label, dim, right);
                    }
                }
            } else if let Some(line) = projection.oi.first() {
                pixel_text(14.0, y, line, dim, right);
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
    for line in lines.into_iter().take(8) {
        pixel_text(14.0, y, &line, dim, right);
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
    let mut y = rail.y + 12.0;
    for item in projection.attention_items.iter().take(3) {
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
            pixel_text(
                rail.x + 10.0,
                y + 42.0,
                "[ALLOW] [DENY]",
                accent,
                rail.right() - 8.0,
            );
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
        lines.push(format!("{}{}", "  ".repeat(depth), label));
        if lines.len() >= 8 {
            return;
        }
        append_pixel_tree(&node.children, depth + 1, lines);
        if lines.len() >= 8 {
            return;
        }
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
        if index > 0 {
            let (_, previous_x, previous_y) = &positions[index - 1];
            draw_line(
                *previous_x,
                *previous_y,
                *px,
                *py,
                (
                    node_color.0 * 0.46,
                    node_color.1 * 0.46,
                    node_color.2 * 0.46,
                ),
            );
        }
    }
    match mode {
        VisualMode::Think => draw_pulses(192.0, 90.0, phase, color),
        VisualMode::Search => draw_scanner(phase, color),
        VisualMode::Read => draw_artifact(projection, color, phase, right),
        VisualMode::Code => draw_code_columns(projection, color, phase, right),
        VisualMode::Execute | VisualMode::Generate => draw_packets(&positions, phase, color, false),
        VisualMode::Recover => draw_packets(&positions, phase, color, true),
        VisualMode::Test => draw_test_gates(projection, &positions, phase, color, right),
        VisualMode::Verify => draw_verify_gate(projection, &positions, color, right),
        VisualMode::Approval => draw_approval_object(color, phase),
        VisualMode::Failure => draw_failure_fracture(color, phase),
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

fn buddy_target(projection: &Projection, mode: VisualMode, right: f32) -> (f32, f32) {
    let preferred = match mode {
        VisualMode::Failure => (306.0, 190.0),
        VisualMode::Approval => (282.0, 196.0),
        VisualMode::Code => ((right - 40.0).max(180.0), 98.0),
        VisualMode::Test | VisualMode::Verify | VisualMode::Execute => {
            ((right - 40.0).max(180.0), 142.0)
        }
        VisualMode::Search | VisualMode::Inspect => (112.0, 105.0),
        VisualMode::Read => (146.0, 92.0),
        VisualMode::Think | VisualMode::Respond | VisualMode::Generate | VisualMode::Recover => {
            (220.0, 98.0)
        }
        VisualMode::Idle => match projection
            .buddy
            .as_ref()
            .map(|buddy| buddy.anchor.to_ascii_lowercase())
            .as_deref()
        {
            Some("left") => (126.0, 108.0),
            Some("center") => (214.0, 108.0),
            _ => ((right - 72.0).max(150.0), 108.0),
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
        (192.0, 222.0),
        (192.0, 48.0),
        (48.0, 222.0),
        ((right - 40.0).max(150.0), 222.0),
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

fn draw_scanner(phase: f32, color: (f32, f32, f32)) {
    let sweep_x = 28.0 + phase.fract() * 328.0;
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
        for index in 0..5 {
            draw_rect(
                x + 14.0,
                62.0 + index as f32 * 13.0,
                (width - 34.0 - index as f32 * 9.0).max(8.0),
                2.0,
                color,
            );
        }
    }
    draw_line(
        x + width,
        92.0,
        (x + width + 46.0).min(right - 8.0),
        92.0,
        (color.0 * 0.72, color.1 * 0.72, color.2 * 0.72),
    );
}

fn draw_code_columns(projection: &Projection, color: (f32, f32, f32), phase: f32, right: f32) {
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
        draw_line(
            x + width + 5.0,
            102.0,
            (x + width + 25.0).min(right - 4.0),
            102.0,
            color,
        );
        return;
    }
    for column in 0..5 {
        let x = 36.0 + column as f32 * ((right - 58.0) / 4.0).max(22.0);
        let height = 52.0 + ((phase * 2.0 + column as f32).sin() + 1.0) * 34.0;
        draw_rect(x, 48.0, 3.0, height, color);
        for line in 0..5 {
            let width = 12.0 + ((line * 11 + column * 7) % 31) as f32;
            draw_rect(
                x + 8.0,
                54.0 + line as f32 * 14.0,
                width,
                2.0,
                (color.0 * 0.66, color.1 * 0.66, color.2 * 0.66),
            );
        }
    }
}

fn draw_packets(
    positions: &[(String, f32, f32)],
    phase: f32,
    color: (f32, f32, f32),
    reverse: bool,
) {
    for index in 1..positions.len() {
        let (_, start_x, start_y) = &positions[index - 1];
        let (_, end_x, end_y) = &positions[index];
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
    positions: &[(String, f32, f32)],
    phase: f32,
    color: (f32, f32, f32),
    right: f32,
) {
    let checks = &projection.verification.checks;
    if checks.is_empty() {
        for (index, (_, x, y)) in positions.iter().enumerate() {
            if ((phase * 2.0 + index as f32).floor() as usize) % 2 == 0 {
                draw_round_outline(*x - 17.0, *y - 17.0, 34.0, 34.0, color);
            }
        }
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
    positions: &[(String, f32, f32)],
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
    } else if let Some((_, x, y)) = positions.last() {
        draw_round_outline(*x - 24.0, *y - 24.0, 48.0, 48.0, color);
        draw_rect(*x - 9.0, *y, 18.0, 2.0, color);
        draw_rect(*x, *y - 9.0, 2.0, 18.0, color);
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

fn draw_approval_object(color: (f32, f32, f32), phase: f32) {
    let amber = (0.91, 0.62, 0.22);
    let pulse = 1.0 + ((phase * 2.0).sin() + 1.0) * 0.04;
    draw_round_outline(248.0, 166.0, 66.0 * pulse, 40.0 * pulse, amber);
    draw_rect(264.0, 184.0, 34.0, 2.0, color);
}

fn draw_failure_fracture(_color: (f32, f32, f32), phase: f32) {
    let red = (0.88, 0.28, 0.32);
    let shift = phase.sin() * 2.0;
    draw_rect(260.0 + shift, 150.0, 2.0, 44.0, red);
    draw_rect(260.0, 170.0, 48.0, 2.0, red);
    draw_rect(278.0, 138.0, 2.0, 18.0, red);
    draw_rect(292.0, 172.0, 2.0, 24.0, red);
}

#[cfg(test)]
mod tests {
    use super::{
        BuddyMotion, SCENE_WIDTH, VisualMode, buddy_anchor_is_clear, buddy_target, motion_position,
    };
    use crate::{Projection, ProjectionEntity};

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
}
