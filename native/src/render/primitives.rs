use super::super::*;

pub(crate) fn draw_outline_rect(x: f32, y: f32, width: f32, height: f32) {
    unsafe {
        glBegin(GL_LINE_LOOP);
        glVertex2f(x, y);
        glVertex2f(x + width, y);
        glVertex2f(x + width, y + height);
        glVertex2f(x, y + height);
        glEnd();
    }
}

pub(crate) fn draw_line(x1: f32, y1: f32, x2: f32, y2: f32, color: (f32, f32, f32)) {
    unsafe {
        glColor3f(color.0, color.1, color.2);
        glBegin(GL_LINES);
        glVertex2f(x1, y1);
        glVertex2f(x2, y2);
        glEnd();
    }
}

pub(crate) fn draw_round_rect(
    x: f32,
    y: f32,
    width: f32,
    height: f32,
    radius: f32,
    color: (f32, f32, f32),
) {
    let radius = radius.min(width / 2.0).min(height / 2.0).max(0.0);
    unsafe {
        glColor3f(color.0, color.1, color.2);
        glBegin(GL_POLYGON);
        for corner in 0..4 {
            let center = match corner {
                0 => (x + radius, y + radius),
                1 => (x + width - radius, y + radius),
                2 => (x + width - radius, y + height - radius),
                _ => (x + radius, y + height - radius),
            };
            let start = corner as f32 * std::f32::consts::FRAC_PI_2 - std::f32::consts::PI;
            for step in 0..=5 {
                let angle = start + step as f32 * std::f32::consts::FRAC_PI_2 / 5.0;
                glVertex2f(
                    center.0 + radius * angle.cos(),
                    center.1 + radius * angle.sin(),
                );
            }
        }
        glEnd();
    }
}

pub(crate) fn draw_round_outline(x: f32, y: f32, width: f32, height: f32, color: (f32, f32, f32)) {
    // Large panels stay rectangular with softened corners. Capping the radius
    // prevents wide CRTs from degenerating into capsules.
    let radius = (width.min(height) / 2.0).min(18.0);
    unsafe {
        glColor3f(color.0, color.1, color.2);
        glLineWidth(1.0);
        glBegin(GL_LINE_LOOP);
        for corner in 0..4 {
            let center = match corner {
                0 => (x + radius, y + radius),
                1 => (x + width - radius, y + radius),
                2 => (x + width - radius, y + height - radius),
                _ => (x + radius, y + height - radius),
            };
            let start = corner as f32 * std::f32::consts::FRAC_PI_2 - std::f32::consts::PI;
            for step in 0..=5 {
                let angle = start + step as f32 * std::f32::consts::FRAC_PI_2 / 5.0;
                glVertex2f(
                    center.0 + radius * angle.cos(),
                    center.1 + radius * angle.sin(),
                );
            }
        }
        glEnd();
    }
}

pub(crate) fn draw_rect(x: f32, y: f32, width: f32, height: f32, color: (f32, f32, f32)) {
    unsafe {
        glColor3f(color.0, color.1, color.2);
        glBegin(GL_QUADS);
        glVertex2f(x, y);
        glVertex2f(x + width, y);
        glVertex2f(x + width, y + height);
        glVertex2f(x, y + height);
        glEnd();
    }
}

pub(crate) fn draw_node(x: f32, y: f32, radius: f32, color: (f32, f32, f32)) {
    draw_round_outline(
        x - radius - 5.0,
        y - radius - 5.0,
        radius * 2.0 + 10.0,
        radius * 2.0 + 10.0,
        (color.0 * 0.28, color.1 * 0.28, color.2 * 0.28),
    );
    unsafe {
        glColor3f(color.0, color.1, color.2);
        glLineWidth(1.5);
        glBegin(GL_LINE_LOOP);
        for index in 0..8 {
            let angle = std::f32::consts::TAU * index as f32 / 8.0;
            glVertex2f(x + radius * angle.cos(), y + radius * angle.sin());
        }
        glEnd();
    }
    draw_rect(
        x - 2.0,
        y - 2.0,
        4.0,
        4.0,
        (color.0 * 0.82, color.1 * 0.82, color.2 * 0.82),
    );
}

pub(crate) fn with_scissor(draw_height: i32, rect: PixelRect, draw: impl FnOnce()) {
    let x = rect.x.max(0.0).round() as c_int;
    let width = rect.width.max(0.0).round() as c_int;
    let height = rect.height.max(0.0).round() as c_int;
    let y = (draw_height as f32 - rect.bottom()).max(0.0).round() as c_int;
    unsafe {
        glEnable(GL_SCISSOR_TEST);
        glScissor(x, y, width, height);
    }
    draw();
    unsafe { glDisable(GL_SCISSOR_TEST) };
}

/// Clip OI pixels to the rounded CRT aperture. The stencil mask prevents
/// scene geometry from leaking into the surrounding bezel corners.
pub(crate) fn with_crt_mask(
    draw_height: i32,
    rect: PixelRect,
    stencil_available: bool,
    draw: impl FnOnce(),
) {
    if !stencil_available {
        with_scissor(draw_height, rect, draw);
        return;
    }
    let x = rect.x.max(0.0).round() as c_int;
    let width = rect.width.max(0.0).round() as c_int;
    let height = rect.height.max(0.0).round() as c_int;
    let y = (draw_height as f32 - rect.bottom()).max(0.0).round() as c_int;
    unsafe {
        glEnable(GL_SCISSOR_TEST);
        glScissor(x, y, width, height);
        glClearStencil(0);
        glClear(GL_STENCIL_BUFFER_BIT);
        glDisable(GL_SCISSOR_TEST);
        glEnable(GL_STENCIL_TEST);
        glStencilMask(u32::MAX);
        glStencilFunc(GL_ALWAYS, 1, u32::MAX);
        glStencilOp(GL_KEEP, GL_KEEP, GL_REPLACE);
        glColorMask(0, 0, 0, 0);
    }
    draw_round_rect(
        rect.x,
        rect.y,
        rect.width,
        rect.height,
        26.0,
        (0.0, 0.0, 0.0),
    );
    unsafe {
        glColorMask(1, 1, 1, 1);
        glStencilMask(0);
        glStencilFunc(GL_EQUAL, 1, u32::MAX);
        glStencilOp(GL_KEEP, GL_KEEP, GL_KEEP);
    }
    draw();
    unsafe {
        glStencilMask(u32::MAX);
        glDisable(GL_STENCIL_TEST);
    }
}
