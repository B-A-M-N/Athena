use super::super::*;
use super::chassis::{ChassisMaterial, PresentationSettings, draw_chassis};
use super::oi::{OiTarget, draw_oi_scene};
use super::primitives::{draw_rect, with_crt_mask, with_scissor};
use super::prompt::draw_status_text;
use super::terminal::{draw_terminal_background, draw_terminal_text};
use super::text::{FontRole, TextRenderer};

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
    let semantic_mode = VisualMode::from_projection(projection).as_str();
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
    } else if dirty.terminal {
        with_scissor(height, geometry.operator_inner, || {
            draw_terminal_background(core, &geometry, selection);
        });
    } else if dirty.oi_motion {
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

    if dirty.full {
        unsafe {
            glFinish();
            glXWaitGL();
        }
        let heading_metrics = text.metrics_for(FontRole::Heading);
        let header_baseline = geometry.header.y as c_int
            + ((geometry.header.height - heading_metrics.height) / 2.0 + heading_metrics.baseline)
                as c_int;
        text.with_clip(geometry.header, || {
            text.draw_in(
                FontRole::Heading,
                geometry.header.x as c_int + 22,
                header_baseline,
                &fit_text_in(
                    text,
                    FontRole::Heading,
                    "ATHENA",
                    (geometry.header.width as c_int - 44).max(1),
                ),
                (229, 239, 247),
            );
            text.draw_in(
                FontRole::Instrument,
                geometry.header.x as c_int + 145,
                header_baseline,
                &fit_text_in(
                    text,
                    FontRole::Instrument,
                    "//  OPERATOR INSTRUMENT",
                    (geometry.header.width as c_int - 190).max(1),
                ),
                (112, 145, 174),
            );
            let glass_label = "GLASS COMPUTE ENGINE";
            text.draw_in(
                FontRole::Instrument,
                (geometry.header.right()
                    - text.text_width_in(FontRole::Instrument, glass_label) as f32
                    - 44.0) as c_int,
                header_baseline,
                glass_label,
                (184, 192, 184),
            );
        });
        let panel_metrics = text.metrics_for(FontRole::Instrument);
        let panel_baseline =
            |panel: PixelRect| panel.y as c_int + panel_metrics.baseline as c_int + 12;
        text.draw_in(
            FontRole::Instrument,
            geometry.operator_outer.x as c_int + 24,
            panel_baseline(geometry.operator_outer),
            &fit_text_in(
                text,
                FontRole::Instrument,
                "ATHENA // OPERATOR CONSOLE",
                (geometry.operator_outer.width as c_int - 48).max(1),
            ),
            (164, 189, 211),
        );
        text.draw_in(
            FontRole::Instrument,
            geometry.oi_outer.x as c_int + 24,
            panel_baseline(geometry.oi_outer),
            &fit_text_in(
                text,
                FontRole::Instrument,
                "ATHENA OI // GLASS COMPUTE",
                (geometry.oi_outer.width as c_int - 48).max(1),
            ),
            mode_color(semantic_mode),
        );
        with_scissor(height, geometry.operator_viewport, || {
            text.with_clip(geometry.operator_viewport, || {
                draw_terminal_text(text, core, &geometry);
            });
        });
        text.with_clip(geometry.prompt, || {
            draw_status_text(text, &geometry, projection, focused, input_buffer);
        });
        text.with_clip(geometry.controls, || {
            super::prompt::draw_instrument_labels(text, &geometry);
        });
    } else if dirty.terminal {
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
        if !dirty.full {
            glFlush();
        }
        XFlush(display);
    }
}
