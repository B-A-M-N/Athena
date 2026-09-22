//! Low-resolution OI framebuffer and actor-motion target.

use super::{
    BuddyMotion, SCENE_HEIGHT, SCENE_WIDTH, buddy_target, motion_position, scene_safe_area,
};
use crate::buddy::{SPRITE_DIRTY_HEIGHT, SPRITE_DIRTY_WIDTH};
use crate::platform::*;
use crate::{Projection, VisualMode};
use std::cell::RefCell;
use std::ptr;

/// Low-resolution OI render target.
pub(crate) struct OiTarget {
    pub(super) framebuffer: u32,
    pub(super) texture: u32,
    pub(super) enabled: bool,
    pub(super) buddy_motion: RefCell<BuddyMotion>,
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

    pub(crate) fn enabled(&self) -> bool {
        self.enabled
    }

    pub(crate) fn buddy_position(
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
            clamp_x(motion.current.0).round(),
            clamp_y(motion.current.1).round(),
        )
    }
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
