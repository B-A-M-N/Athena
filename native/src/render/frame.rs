use super::super::*;
use super::chassis::{ChassisMaterial, PresentationSettings, draw_chassis};
use super::oi::{OiTarget, draw_oi_scene};
use super::primitives::{draw_rect, with_crt_mask, with_scissor};
use super::prompt::draw_status_text;
use super::terminal::{draw_terminal_background, draw_terminal_text};
use super::text::TextRenderer;

/// Compose the platform-owned terminal and the Athena-owned visual surfaces.
/// Event handling stays in x11.rs; this module owns render ordering and dirty
/// domain isolation.
pub(crate) fn draw_frame(
    display: *mut Display,
    width: i32,
    height: i32,
    core: &NativeTerminalCore,
    projection: &Projection,
    selection: Option<((usize, usize), (usize, usize))>,
    text: &TextRenderer,
    focused: bool,
    input_buffer: &InputBuffer,
    options: &RendererOptions,
    presentation: PresentationSettings,
    stencil_available: bool,
    oi_target: &OiTarget,
    chassis_material: &ChassisMaterial,
    dirty: DirtyDomains,
    phase: f32,
) {
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
            phase,
            presentation,
            chassis_material,
        );
        if !options.cabinet_only {
            with_scissor(height, geometry.operator_inner, || {
                draw_terminal_background(core, &geometry, selection);
            });
            with_crt_mask(height, geometry.oi_inner, stencil_available, || {
                draw_oi_scene(
                    oi_target,
                    width,
                    height,
                    geometry.oi_inner.x,
                    geometry.oi_inner.y,
                    geometry.oi_inner.width,
                    geometry.oi_inner.height,
                    projection,
                    phase,
                    options,
                    presentation,
                    stencil_available,
                );
            });
        }
    } else if dirty.terminal && !options.cabinet_only {
        with_scissor(height, geometry.operator_inner, || {
            draw_terminal_background(core, &geometry, selection);
        });
    } else if dirty.oi_motion && !options.cabinet_only {
        with_crt_mask(height, geometry.oi_inner, stencil_available, || {
            draw_rect(
                geometry.oi_inner.x,
                geometry.oi_inner.y,
                geometry.oi_inner.width,
                geometry.oi_inner.height,
                (0.010, 0.028, 0.037),
            );
            draw_oi_scene(
                oi_target,
                width,
                height,
                geometry.oi_inner.x,
                geometry.oi_inner.y,
                geometry.oi_inner.width,
                geometry.oi_inner.height,
                projection,
                phase,
                options,
                presentation,
                stencil_available,
            );
        });
    }

    if dirty.full && !options.cabinet_only {
        unsafe {
            glFinish();
            glXWaitGL();
        }
        with_scissor(height, geometry.operator_viewport, || {
            text.with_clip(geometry.operator_viewport, || {
                draw_terminal_text(text, core, &geometry);
            });
        });
        text.with_clip(geometry.prompt, || {
            draw_status_text(text, &geometry, projection, focused, input_buffer);
        });
    } else if dirty.terminal && !options.cabinet_only {
        unsafe {
            glFinish();
            glXWaitGL();
        }
        with_scissor(height, geometry.operator_viewport, || {
            text.with_clip(geometry.operator_viewport, || {
                draw_terminal_text(text, core, &geometry);
            });
        });
    }
    unsafe {
        glFlush();
        XFlush(display);
    }
}
