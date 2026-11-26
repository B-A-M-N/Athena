use crate::input::InputBuffer;

use crate::platform::*;
use crate::render::text::FontRole;
use crate::x11::*;

use super::chassis::{ChassisMaterial, PresentationSettings, draw_chassis};
use super::oi::{OiTarget, draw_oi_scene};
use super::primitives::{draw_rect, with_crt_mask, with_scissor};
use super::prompt::draw_status_cursor;
use super::terminal::{draw_terminal_background, draw_terminal_text};
use super::text::TextRenderer;
use super::transcript::draw_transcript;
use crate::{Projection, VisualMode};

#[derive(Clone, Copy)]
pub(crate) struct FrameContext<'a> {
    pub(crate) width: i32,
    pub(crate) height: i32,
    pub(crate) core: &'a NativeTerminalCore,
    pub(crate) projection: &'a Projection,
    pub(crate) selection: Option<((usize, usize), (usize, usize))>,
    pub(crate) text: &'a TextRenderer,
    pub(crate) focused: bool,
    pub(crate) input_buffer: &'a InputBuffer,
    pub(crate) options: &'a RendererOptions,
    pub(crate) presentation: PresentationSettings,
    pub(crate) stencil_available: bool,
    pub(crate) oi_target: &'a OiTarget,
    pub(crate) chassis_material: &'a ChassisMaterial,
    pub(crate) dirty: DirtyDomains,
    pub(crate) effect_phase: f32,
    pub(crate) motion_time: f32,
}

#[derive(Clone, Copy)]
pub(crate) struct TextLayerContext<'a> {
    pub(crate) display: *mut Display,
    pub(crate) width: i32,
    pub(crate) height: i32,
    pub(crate) core: &'a NativeTerminalCore,
    pub(crate) projection: &'a Projection,
    pub(crate) text: &'a TextRenderer,
    pub(crate) focused: bool,
    pub(crate) input_buffer: &'a InputBuffer,
    pub(crate) options: &'a RendererOptions,
    pub(crate) dirty: DirtyDomains,
}

/// Compose the platform-owned terminal and the Athena-owned visual surfaces.
/// Event handling stays in x11.rs; this module owns render ordering and dirty
/// domain isolation.
pub(crate) fn draw_frame(context: FrameContext<'_>) {
    let FrameContext {
        width,
        height,
        core,
        projection,
        selection,
        text,
        focused,
        input_buffer,
        options,
        presentation,
        stencil_available,
        oi_target,
        chassis_material,
        dirty,
        effect_phase,
        motion_time,
    } = context;
    let metrics = UiFontMetrics {
        body: text.metrics_for(FontRole::Body),
        input: text.metrics_for(FontRole::Input),
        heading: text.metrics_for(FontRole::Heading),
        instrument: text.metrics_for(FontRole::Instrument),
    };
    let geometry = FrameGeometry::for_window(width, height, metrics);
    unsafe { glXWaitX() };
    unsafe {
        glViewport(0, 0, width, height);
        glMatrixMode(GL_PROJECTION);
        glLoadIdentity();
        glOrtho(0.0, width as f64, height as f64, 0.0, -1.0, 1.0);
        glMatrixMode(GL_MODELVIEW);
        glLoadIdentity();
    }

    if dirty.full {
        unsafe {
            glDisable(GL_SCISSOR_TEST);
            glClearColor(0.026, 0.028, 0.048, 1.0);
            glClear(GL_COLOR_BUFFER_BIT);
        }
        draw_chassis(
            &geometry,
            projection,
            focused,
            effect_phase,
            presentation,
            chassis_material,
        );
        if !options.cabinet_only {
            with_scissor(height, geometry.operator_inner, || {
                draw_terminal_background(core, &geometry, selection);
            });
            with_crt_mask(height, geometry.oi_inner, stencil_available, || {
                draw_oi_scene(super::oi::OiSceneContext {
                    target: oi_target,
                    frame_width: width,
                    frame_height: height,
                    x: geometry.oi_inner.x,
                    y: geometry.oi_inner.y,
                    width: geometry.oi_inner.width,
                    height: geometry.oi_inner.height,
                    projection,
                    effect_phase,
                    motion_time,
                    options,
                    presentation,
                    stencil_available,
                });
            });
        }
    }
    if !dirty.full && dirty.terminal && !options.cabinet_only {
        with_scissor(height, geometry.operator_inner, || {
            draw_terminal_background(core, &geometry, selection);
        });
    }
    if !dirty.full && dirty.oi_motion && !options.cabinet_only {
        with_crt_mask(height, geometry.oi_inner, stencil_available, || {
            draw_rect(
                geometry.oi_inner.x,
                geometry.oi_inner.y,
                geometry.oi_inner.width,
                geometry.oi_inner.height,
                super::theme::GLASS_BACKGROUND,
            );
            draw_oi_scene(super::oi::OiSceneContext {
                target: oi_target,
                frame_width: width,
                frame_height: height,
                x: geometry.oi_inner.x,
                y: geometry.oi_inner.y,
                width: geometry.oi_inner.width,
                height: geometry.oi_inner.height,
                projection,
                effect_phase,
                motion_time,
                options,
                presentation,
                stencil_available,
            });
        });
    }

    // The cursor is a GL hardware mark in the offscreen surface. It must be
    // copied with the cabinet before the window-owned Xft text is painted.
    if dirty.full && !options.cabinet_only {
        text.with_clip(geometry.prompt, || {
            draw_status_cursor(text, &geometry, focused, input_buffer, effect_phase);
        });
    }

    unsafe {
        glFlush();
    }
}

/// Paint the modern text layer after the GL cabinet has been copied to the
/// visible X11 window. Xft is intentionally window-owned: several GLX/X11
/// implementations do not expose XRender writes made to a GLX pixmap when
/// that pixmap is subsequently copied with XCopyArea.
pub(crate) fn draw_text_layer(context: TextLayerContext<'_>) {
    let TextLayerContext {
        display,
        width,
        height,
        core,
        projection,
        text,
        focused,
        input_buffer,
        options,
        dirty,
    } = context;
    if options.cabinet_only || !(dirty.full || dirty.terminal) {
        return;
    }
    let metrics = UiFontMetrics {
        body: text.metrics_for(FontRole::Body),
        input: text.metrics_for(FontRole::Input),
        heading: text.metrics_for(FontRole::Heading),
        instrument: text.metrics_for(FontRole::Instrument),
    };
    let geometry = FrameGeometry::for_window(width, height, metrics);
    if dirty.full || dirty.terminal {
        text.with_clip(geometry.operator_viewport, || {
            if !projection.conversation.is_empty() {
                draw_transcript(text, projection, geometry.operator_viewport);
            } else {
                draw_terminal_text(
                    text,
                    core,
                    &geometry,
                    VisualMode::from_projection(projection) == VisualMode::Idle,
                );
            }
        });
    }
    if dirty.full {
        text.with_clip(geometry.prompt, || {
            super::prompt::draw_status_text(
                text,
                &geometry,
                projection,
                focused,
                input_buffer,
                false,
            );
        });
    }
    unsafe { XFlush(display) };
}
