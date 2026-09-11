use super::super::*;
use super::chassis::{ChassisMaterial, PresentationSettings, draw_chassis};
use super::oi::{OiTarget, draw_oi_scene};
use super::primitives::{draw_rect, with_crt_mask, with_scissor};
use super::prompt::draw_status_cursor;
use super::terminal::{draw_terminal_background, draw_terminal_text};
use super::text::TextRenderer;

/// Compose the platform-owned terminal and the Athena-owned visual surfaces.
/// Event handling stays in x11.rs; this module owns render ordering and dirty
/// domain isolation.
pub(crate) fn draw_frame(
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

    // The cursor is a GL hardware mark in the offscreen surface. It must be
    // copied with the cabinet before the window-owned Xft text is painted.
    if dirty.full && !options.cabinet_only {
        text.with_clip(geometry.prompt, || {
            draw_status_cursor(text, &geometry, focused, input_buffer, phase);
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
pub(crate) fn draw_text_layer(
    display: *mut Display,
    width: i32,
    height: i32,
    core: &NativeTerminalCore,
    projection: &Projection,
    text: &TextRenderer,
    focused: bool,
    input_buffer: &InputBuffer,
    options: &RendererOptions,
    dirty: DirtyDomains,
) {
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
            draw_terminal_text(
                text,
                core,
                &geometry,
                VisualMode::from_projection(projection) == VisualMode::Idle,
            );
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
