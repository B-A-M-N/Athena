//! Recessed panel and control drawing primitives for the chassis renderer.
//!
//! These are pure geometry+color helpers used by `chassis.rs`; they have no
//! state and no authority — they draw to the GL context provided by the
//! caller.

use athena_terminal::PixelRect;

use crate::render::primitives::*;
use crate::x11::FrameGeometry;

pub(crate) fn inset(rect: PixelRect, amount: f32) -> PixelRect {
    PixelRect {
        x: rect.x + amount,
        y: rect.y + amount,
        width: (rect.width - amount * 2.0).max(0.0),
        height: (rect.height - amount * 2.0).max(0.0),
    }
}

pub(crate) fn draw_recessed_panel(
    rect: PixelRect,
    scale: f32,
    outer: (f32, f32, f32),
    inner: (f32, f32, f32),
) {
    let shadow = inset(rect, -4.0 * scale);
    draw_round_rect(
        shadow.x + 4.0 * scale,
        shadow.y + 5.0 * scale,
        shadow.width,
        shadow.height,
        9.0 * scale,
        (0.006, 0.007, 0.008),
    );
    draw_round_rect(
        rect.x,
        rect.y,
        rect.width,
        rect.height,
        (rect.width.min(rect.height) * 0.06)
            .min(30.0 * scale)
            .max(4.0 * scale),
        outer,
    );
    draw_round_outline_radius(
        rect.x,
        rect.y,
        rect.width,
        rect.height,
        4.0 * scale,
        (0.17, 0.18, 0.18),
    );
    let cavity = inset(rect, 8.0 * scale);
    draw_round_rect(
        cavity.x,
        cavity.y,
        cavity.width,
        cavity.height,
        5.0 * scale,
        inner,
    );
}

pub(crate) fn draw_recessed_instrument(rect: PixelRect, scale: f32, focused: bool) {
    let shadow = inset(rect, -3.0 * scale);
    draw_round_rect(
        shadow.x + 3.0 * scale,
        shadow.y + 4.0 * scale,
        shadow.width,
        shadow.height,
        6.0 * scale,
        (0.005, 0.006, 0.007),
    );
    draw_round_rect(
        rect.x,
        rect.y,
        rect.width,
        rect.height,
        5.0 * scale,
        (0.050, 0.052, 0.052),
    );
    draw_round_outline_radius(
        rect.x,
        rect.y,
        rect.width,
        rect.height,
        2.0 * scale,
        (0.20, 0.21, 0.21),
    );
    let cavity = inset(rect, 7.0 * scale);
    draw_round_rect(
        cavity.x,
        cavity.y,
        cavity.width,
        cavity.height,
        2.0 * scale,
        if focused {
            (0.011, 0.025, 0.026)
        } else {
            (0.009, 0.015, 0.016)
        },
    );
}

pub(crate) fn draw_operator_well(geometry: &FrameGeometry, focused: bool, scale: f32) {
    let outer = geometry.operator_outer;
    draw_recessed_panel(outer, scale, (0.053, 0.055, 0.056), (0.008, 0.011, 0.012));
    let inner = geometry.operator_inner;
    draw_round_rect(
        inner.x - 5.0 * scale,
        inner.y - 5.0 * scale,
        inner.width + 10.0 * scale,
        inner.height + 10.0 * scale,
        6.0 * scale,
        if focused {
            (0.012, 0.030, 0.031)
        } else {
            (0.010, 0.015, 0.016)
        },
    );
    // Beveled inner aperture depth shading
    draw_rect(
        inner.x - 2.0 * scale,
        inner.y - 2.0 * scale,
        inner.width + 4.0 * scale,
        2.0 * scale,
        (0.003, 0.005, 0.006),
    );
    draw_rect(
        inner.x - 2.0 * scale,
        inner.y - 2.0 * scale,
        2.0 * scale,
        inner.height + 4.0 * scale,
        (0.003, 0.005, 0.006),
    );
    draw_round_outline_radius(
        inner.x,
        inner.y,
        inner.width,
        inner.height,
        2.0 * scale,
        (0.18, 0.23, 0.23),
    );
    draw_rect(
        inner.x + 8.0 * scale,
        inner.y + 8.0 * scale,
        (inner.width - 16.0 * scale).max(0.0),
        1.0 * scale,
        if focused {
            (0.13, 0.39, 0.37)
        } else {
            (0.07, 0.15, 0.15)
        },
    );
}

pub(crate) fn draw_glass_crt_well(geometry: &FrameGeometry, scale: f32) {
    let outer = geometry.oi_outer;
    draw_recessed_panel(outer, scale, (0.057, 0.061, 0.062), (0.005, 0.017, 0.019));
    let inner = geometry.oi_inner;
    // Deep bulbous cathode tube rim
    draw_round_rect(
        inner.x - 8.0 * scale,
        inner.y - 8.0 * scale,
        inner.width + 16.0 * scale,
        inner.height + 16.0 * scale,
        (inner.width.min(inner.height) * 0.09).min(48.0 * scale),
        (0.003, 0.011, 0.013),
    );
    draw_round_rect(
        inner.x - 4.0 * scale,
        inner.y - 4.0 * scale,
        inner.width + 8.0 * scale,
        inner.height + 8.0 * scale,
        (inner.width.min(inner.height) * 0.075).min(40.0 * scale),
        (0.002, 0.007, 0.009),
    );
    draw_round_outline_radius(
        inner.x - 3.0 * scale,
        inner.y - 3.0 * scale,
        inner.width + 6.0 * scale,
        inner.height + 6.0 * scale,
        (inner.width.min(inner.height) * 0.09).min(48.0 * scale),
        (0.09, 0.25, 0.26),
    );
}

pub(crate) fn draw_encoder(rect: PixelRect, scale: f32, value: f32, power: bool) {
    if rect.width <= 0.0 || rect.height <= 0.0 {
        return;
    }
    let size = rect
        .height
        .min(rect.width * 0.72)
        .min(rect.height * 0.52)
        .max(8.0 * scale);
    let x = rect.x + rect.width * 0.5;
    let y = rect.y + rect.height * 0.52;
    // Outer dial tick markings
    if !power && size > 16.0 * scale {
        for i in 0..7 {
            let tick_angle =
                std::f32::consts::PI * 0.75 + i as f32 * (std::f32::consts::PI * 1.5 / 6.0);
            let tick_r1 = size * 0.58;
            let tick_r2 = size * 0.68;
            draw_line(
                x + tick_r1 * tick_angle.cos(),
                y + tick_r1 * tick_angle.sin(),
                x + tick_r2 * tick_angle.cos(),
                y + tick_r2 * tick_angle.sin(),
                (0.22, 0.26, 0.28),
            );
        }
    }
    draw_round_rect(
        x - size * 0.5 + 3.0 * scale,
        y - size * 0.5 + 4.0 * scale,
        size,
        size,
        size * 0.5,
        (0.007, 0.008, 0.009),
    );
    draw_round_rect(
        x - size * 0.5,
        y - size * 0.5,
        size,
        size,
        size * 0.5,
        if power {
            (0.043, 0.046, 0.046)
        } else {
            (0.067, 0.070, 0.070)
        },
    );
    draw_round_outline_radius(
        x - size * 0.5,
        y - size * 0.5,
        size,
        size,
        size * 0.5,
        (0.24, 0.25, 0.25),
    );
    if power {
        draw_round_rect(
            x - size * 0.17,
            y - size * 0.17,
            size * 0.34,
            size * 0.34,
            size * 0.08,
            if value > 0.5 {
                (0.23, 0.66, 0.61)
            } else {
                (0.07, 0.09, 0.09)
            },
        );
    } else {
        let tick_y = y - size * 0.40 + size * 0.58 * value.clamp(0.0, 1.0);
        draw_rect(
            x - 1.5 * scale,
            tick_y,
            3.0 * scale,
            size * 0.18,
            (0.36, 0.76, 0.72),
        );
        // Illuminated indicator pip
        let angle =
            std::f32::consts::PI * 0.75 + value.clamp(0.0, 1.0) * std::f32::consts::PI * 1.5;
        let pip_r = size * 0.32;
        draw_round_rect(
            x + pip_r * angle.cos() - 1.5 * scale,
            y + pip_r * angle.sin() - 1.5 * scale,
            3.0 * scale,
            3.0 * scale,
            1.5 * scale,
            (0.36, 0.82, 0.78),
        );
    }
}

pub(crate) fn draw_power_button(rect: PixelRect, scale: f32, enabled: bool) {
    let side = rect.width.min(rect.height * 0.48).max(12.0 * scale);
    let x = rect.x + (rect.width - side) * 0.5;
    let y = rect.y + rect.height * 0.40;
    draw_round_rect(
        x + 3.0 * scale,
        y + 4.0 * scale,
        side,
        side,
        4.0 * scale,
        (0.006, 0.007, 0.008),
    );
    draw_round_rect(x, y, side, side, 4.0 * scale, (0.038, 0.041, 0.042));
    draw_round_outline_radius(x, y, side, side, 4.0 * scale, (0.24, 0.25, 0.25));
    let lamp = if enabled {
        (0.72, 0.90, 0.94)
    } else {
        (0.16, 0.20, 0.21)
    };
    let inset = side * 0.28;
    draw_round_rect(
        x + inset,
        y + inset,
        (side - inset * 2.0).max(2.0),
        (side - inset * 2.0).max(2.0),
        2.0 * scale,
        lamp,
    );
}
