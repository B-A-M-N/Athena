use crate::Projection;
use crate::platform::*;
use crate::x11::*;
use athena_terminal::PixelRect;

pub(crate) use super::chassis_panels::{
    draw_encoder, draw_glass_crt_well, draw_operator_well, draw_power_button,
    draw_recessed_instrument, draw_recessed_panel, inset,
};

use super::glyphs::static_glyph;
use super::primitives::*;
use super::theme::{GRAPHITE, PRIMARY, SECONDARY};

const CHASSIS_NOISE: &[u8] = include_bytes!("../../assets/athenabox/chassis_noise.ppm");

/// Retained shell material for the graphite enclosure.
pub(crate) struct ChassisMaterial {
    texture: u32,
    enabled: bool,
}

impl ChassisMaterial {
    pub(crate) fn new() -> Self {
        let Some((base_width, base_height, base_pixels)) = parse_ppm(CHASSIS_NOISE) else {
            return Self {
                texture: 0,
                enabled: false,
            };
        };
        let (width, height, pixels) =
            expand_graphite_material(base_width, base_height, &base_pixels);
        let mut texture = 0;
        unsafe {
            glGenTextures(1, &mut texture);
            if texture == 0 {
                return Self {
                    texture: 0,
                    enabled: false,
                };
            }
            glBindTexture(GL_TEXTURE_2D, texture);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT);
            glTexImage2D(
                GL_TEXTURE_2D,
                0,
                GL_RGB as c_int,
                width,
                height,
                0,
                GL_RGB,
                GL_UNSIGNED_BYTE,
                pixels.as_ptr().cast(),
            );
            glBindTexture(GL_TEXTURE_2D, 0);
        }
        Self {
            texture,
            enabled: true,
        }
    }

    fn draw_surface(&self, rect: PixelRect) {
        if !self.enabled || rect.width <= 0.0 || rect.height <= 0.0 {
            return;
        }
        unsafe {
            glEnable(GL_TEXTURE_2D);
            glBindTexture(GL_TEXTURE_2D, self.texture);
            glColor3f(1.0, 1.0, 1.0);
            glBegin(GL_QUADS);
            let u = rect.width / 34.0;
            let v = rect.height / 34.0;
            glTexCoord2f(0.0, 0.0);
            glVertex2f(rect.x, rect.y);
            glTexCoord2f(u, 0.0);
            glVertex2f(rect.right(), rect.y);
            glTexCoord2f(u, v);
            glVertex2f(rect.right(), rect.bottom());
            glTexCoord2f(0.0, v);
            glVertex2f(rect.x, rect.bottom());
            glEnd();
            glBindTexture(GL_TEXTURE_2D, 0);
            glDisable(GL_TEXTURE_2D);
        }
    }
}

impl Drop for ChassisMaterial {
    fn drop(&mut self) {
        if self.enabled {
            unsafe { glDeleteTextures(1, &self.texture) };
        }
    }
}

fn parse_ppm(bytes: &[u8]) -> Option<(c_int, c_int, Vec<u8>)> {
    let text = std::str::from_utf8(bytes).ok()?;
    let mut tokens = text
        .lines()
        .filter(|line| !line.trim_start().starts_with('#'))
        .flat_map(str::split_whitespace);
    if tokens.next()? != "P3" {
        return None;
    }
    let width: c_int = tokens.next()?.parse().ok()?;
    let height: c_int = tokens.next()?.parse().ok()?;
    let max_value: u16 = tokens.next()?.parse().ok()?;
    if width <= 0 || height <= 0 || max_value == 0 {
        return None;
    }
    let count = usize::try_from(width)
        .ok()?
        .checked_mul(usize::try_from(height).ok()?)?;
    let channels = count.checked_mul(3)?;
    let mut pixels = Vec::with_capacity(channels);
    for _ in 0..channels {
        let value: u16 = tokens.next()?.parse().ok()?;
        pixels.push((u32::from(value).saturating_mul(255) / u32::from(max_value)) as u8);
    }
    Some((width, height, pixels))
}

fn expand_graphite_material(
    base_width: c_int,
    base_height: c_int,
    base_pixels: &[u8],
) -> (c_int, c_int, Vec<u8>) {
    // AthenaBOX reference is a fine graphite, not an 8px checkerboard.
    // Expand to 64 with deterministic mid/fine variation so the DAGOAL
    // phosphor and the BOX shell share one coherent material scale.
    const WIDTH: usize = 64;
    const HEIGHT: usize = 64;
    let source_width = base_width.max(1) as usize;
    let source_height = base_height.max(1) as usize;
    let mut pixels = Vec::with_capacity(WIDTH * HEIGHT * 3);
    for y in 0..HEIGHT {
        for x in 0..WIDTH {
            let source_x = x * source_width / WIDTH;
            let source_y = y * source_height / HEIGHT;
            let source = (source_y * source_width + source_x) * 3;
            let mid = ((x / 4 + y / 4) % 3) as i16 - 1;
            let fine = ((x * 13 + y * 17 + x / 3 * 7) % 7) as i16 - 3;
            let wear = if (x * 19 + y * 23) % 127 == 0 { -7 } else { 0 };
            for channel in 0..3 {
                let value = base_pixels.get(source + channel).copied().unwrap_or(32) as i16
                    + mid
                    + fine
                    + wear;
                pixels.push(value.clamp(8, 96) as u8);
            }
        }
    }
    (WIDTH as c_int, HEIGHT as c_int, pixels)
}

#[cfg(test)]
mod tests {
    use super::{CHASSIS_NOISE, expand_graphite_material, parse_ppm};

    #[test]
    fn bundled_chassis_material_is_a_complete_rgb_texture() {
        let (width, height, pixels) = parse_ppm(CHASSIS_NOISE).expect("valid bundled PPM");
        assert_eq!((width, height), (8, 8));
        assert_eq!(pixels.len(), 8 * 8 * 3);
        assert!(pixels.iter().all(|value| *value > 0));
    }

    #[test]
    fn expanded_graphite_material_has_bounded_multi_frequency_variation() {
        let (width, height, pixels) = parse_ppm(CHASSIS_NOISE).expect("valid bundled PPM");
        let (expanded_width, expanded_height, expanded) =
            expand_graphite_material(width, height, &pixels);
        assert_eq!((expanded_width, expanded_height), (64, 64));
        assert_eq!(expanded.len(), 64 * 64 * 3);
        assert!(expanded.iter().min() >= Some(&8));
        assert!(expanded.iter().max() <= Some(&96));
        assert!(expanded.windows(3).any(|window| window[0] != window[1]));
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct PresentationSettings {
    pub(crate) brightness: f32,
    pub(crate) focus: f32,
    pub(crate) display_enabled: bool,
}

impl Default for PresentationSettings {
    fn default() -> Self {
        Self {
            brightness: 0.92,
            focus: 0.72,
            display_enabled: true,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum PresentationControl {
    Brightness,
    Focus,
    Power,
}

impl PresentationSettings {
    pub(crate) fn control_at(
        geometry: &FrameGeometry,
        x: i32,
        y: i32,
    ) -> Option<PresentationControl> {
        if geometry.compact || !geometry.controls.contains(x, y) {
            return None;
        }
        [
            (geometry.rail.brightness, PresentationControl::Brightness),
            (geometry.rail.focus, PresentationControl::Focus),
            (geometry.rail.power, PresentationControl::Power),
        ]
        .into_iter()
        .find(|(rect, _)| rect.contains(x, y))
        .map(|(_, control)| control)
    }

    pub(crate) fn activate(
        &mut self,
        control: PresentationControl,
        x: i32,
        geometry: &FrameGeometry,
    ) {
        match control {
            PresentationControl::Brightness => {
                let rect = geometry.rail.brightness;
                self.brightness = ((x as f32 - rect.x) / rect.width.max(1.0)).clamp(0.15, 1.0);
            }
            PresentationControl::Focus => {
                let rect = geometry.rail.focus;
                self.focus = ((x as f32 - rect.x) / rect.width.max(1.0)).clamp(0.15, 1.0);
            }
            PresentationControl::Power => self.display_enabled = !self.display_enabled,
        }
    }

    pub(crate) fn adjust(&mut self, control: PresentationControl, delta: f32) {
        match control {
            PresentationControl::Brightness => {
                self.brightness = (self.brightness + delta).clamp(0.15, 1.0);
            }
            PresentationControl::Focus => {
                self.focus = (self.focus + delta).clamp(0.15, 1.0);
            }
            PresentationControl::Power => self.display_enabled = !self.display_enabled,
        }
    }
}

pub(crate) fn draw_chassis(
    geometry: &FrameGeometry,
    projection: &Projection,
    focused: bool,
    _phase: f32,
    presentation: PresentationSettings,
    material: &ChassisMaterial,
) {
    let scale = geometry.scale.max(0.08);
    let chassis = geometry.chassis;
    draw_rect(
        chassis.x,
        chassis.y,
        chassis.width,
        chassis.height,
        (0.016, 0.017, 0.018),
    );

    let shell = inset(chassis, 16.0 * scale);
    draw_round_rect(
        shell.x + 4.0 * scale,
        shell.y + 7.0 * scale,
        shell.width,
        shell.height,
        28.0 * scale,
        (0.006, 0.007, 0.008),
    );
    draw_round_rect(
        shell.x,
        shell.y,
        shell.width,
        shell.height,
        28.0 * scale,
        (0.090, 0.093, 0.095),
    );
    material.draw_surface(inset(shell, 10.0 * scale));
    draw_round_outline_radius(
        shell.x,
        shell.y,
        shell.width,
        shell.height,
        28.0 * scale,
        (0.20, 0.21, 0.21),
    );

    let deck = inset(chassis, 24.0 * scale);
    draw_round_rect(
        deck.x,
        deck.y,
        deck.width,
        deck.height,
        14.0 * scale,
        GRAPHITE,
    );
    draw_round_outline_radius(
        deck.x,
        deck.y,
        deck.width,
        deck.height,
        14.0 * scale,
        (0.11, 0.12, 0.12),
    );

    // The top rail is a small physical nameplate and vent, not a second
    // dashboard. Screen titles live at their CRT apertures below.
    draw_rect(
        geometry.header.x,
        geometry.header.y + geometry.header.height * 0.70,
        geometry.header.width,
        1.0 * scale,
        (0.12, 0.14, 0.15),
    );
    draw_round_rect(
        geometry.header.x + 10.0 * scale,
        geometry.header.y + 12.0 * scale,
        94.0 * scale,
        28.0 * scale,
        4.0 * scale,
        (0.045, 0.050, 0.053),
    );
    draw_round_outline_radius(
        geometry.header.x + 10.0 * scale,
        geometry.header.y + 12.0 * scale,
        94.0 * scale,
        28.0 * scale,
        4.0 * scale,
        (0.18, 0.22, 0.23),
    );
    for index in 0..8 {
        let x = geometry.header.right() - 112.0 * scale + index as f32 * 11.0 * scale;
        draw_round_rect(
            x,
            geometry.header.y + 19.0 * scale,
            3.0 * scale,
            10.0 * scale,
            1.5 * scale,
            (0.012, 0.014, 0.015),
        );
    }

    let system_status = projection.system_status.to_ascii_lowercase();
    let indicator = if projection.stale
        || projection.bridge_status.contains("DISCONNECTED")
        || projection.bridge_status.contains("ERROR")
        || matches!(
            system_status.as_str(),
            "error" | "recovery_required" | "failed"
        ) {
        (0.82, 0.24, 0.27)
    } else if matches!(
        system_status.as_str(),
        "waiting" | "degraded" | "unknown" | "not_ready"
    ) {
        (0.88, 0.59, 0.20)
    } else if system_status == "ready" {
        (0.27, 0.78, 0.68)
    } else {
        (0.45, 0.48, 0.48)
    };
    let lamp_x = geometry.header.right() - 20.0 * scale;
    draw_round_rect(
        lamp_x - 5.0 * scale,
        geometry.header.y + geometry.header.height * 0.26,
        21.0 * scale,
        21.0 * scale,
        4.0 * scale,
        (0.018, 0.020, 0.021),
    );
    draw_round_rect(
        lamp_x,
        geometry.header.y + geometry.header.height * 0.34,
        11.0 * scale,
        11.0 * scale,
        2.0 * scale,
        indicator,
    );

    draw_operator_well(geometry, focused, scale);
    draw_glass_crt_well(geometry, scale);
    let seam_x = (geometry.operator_outer.right() + geometry.oi_outer.x) * 0.5;
    let seam_y = geometry.operator_outer.y + geometry.operator_outer.height * 0.52;
    // Central structural mullion
    draw_rect(
        seam_x - 3.0 * scale,
        geometry.operator_outer.y + 20.0 * scale,
        6.0 * scale,
        (geometry.operator_outer.height - 40.0 * scale).max(0.0),
        (0.045, 0.048, 0.050),
    );
    draw_rect(
        seam_x - 0.5 * scale,
        geometry.operator_outer.y + 24.0 * scale,
        1.0 * scale,
        (geometry.operator_outer.height - 48.0 * scale).max(0.0),
        (0.09, 0.10, 0.11),
    );
    // Mechanical fastener latch plate
    draw_round_rect(
        seam_x - 8.0 * scale,
        seam_y - 14.0 * scale,
        16.0 * scale,
        28.0 * scale,
        4.0 * scale,
        (0.060, 0.063, 0.065),
    );
    draw_round_outline_radius(
        seam_x - 8.0 * scale,
        seam_y - 14.0 * scale,
        16.0 * scale,
        28.0 * scale,
        4.0 * scale,
        (0.24, 0.28, 0.28),
    );
    draw_rect(
        seam_x - 4.0 * scale,
        seam_y - 3.0 * scale,
        3.0 * scale,
        3.0 * scale,
        (0.28, 0.70, 0.66),
    );
    draw_rect(
        seam_x + 1.0 * scale,
        seam_y - 3.0 * scale,
        3.0 * scale,
        3.0 * scale,
        (0.28, 0.70, 0.66),
    );

    draw_round_rect(
        geometry.controls.x,
        geometry.controls.y,
        geometry.controls.width,
        geometry.controls.height,
        10.0 * scale,
        (0.068, 0.070, 0.071),
    );
    draw_round_outline_radius(
        geometry.controls.x,
        geometry.controls.y,
        geometry.controls.width,
        geometry.controls.height,
        10.0 * scale,
        (0.17, 0.18, 0.18),
    );

    let speaker = geometry.rail.speaker;
    draw_recessed_panel(speaker, scale, (0.036, 0.038, 0.039), (0.009, 0.010, 0.011));
    if !geometry.compact {
        for row in 0..5 {
            for column in 0..10 {
                draw_round_rect(
                    speaker.x + speaker.width * 0.14 + column as f32 * speaker.width * 0.072,
                    speaker.y + speaker.height * 0.16 + row as f32 * speaker.height * 0.13,
                    6.0 * scale,
                    4.0 * scale,
                    1.5 * scale,
                    (0.005, 0.006, 0.007),
                );
            }
        }
    }
    draw_recessed_panel(
        geometry.rail.operator_panel,
        scale,
        (0.048, 0.050, 0.051),
        (0.011, 0.013, 0.014),
    );
    draw_recessed_instrument(geometry.prompt, scale, focused);
    draw_rect(
        geometry.prompt.x + 12.0 * scale,
        geometry.prompt.y + 7.0 * scale,
        (geometry.prompt.width - 24.0 * scale).max(0.0),
        1.0 * scale,
        (0.16, 0.18, 0.18),
    );

    let system = geometry.rail.system_status;
    draw_recessed_panel(system, scale, (0.036, 0.038, 0.039), (0.010, 0.012, 0.013));
    let network_indicator = match projection.network_status.to_ascii_lowercase().as_str() {
        "available" => (0.25, 0.58, 0.67),
        "restricted" => (0.88, 0.59, 0.20),
        "blocked" => (0.82, 0.24, 0.27),
        _ => (0.42, 0.45, 0.45),
    };
    let activity_indicator = match projection.activity_status.to_ascii_lowercase().as_str() {
        "active" => (0.27, 0.78, 0.68),
        "waiting" => (0.88, 0.59, 0.20),
        "error" => (0.82, 0.24, 0.27),
        _ => (0.32, 0.34, 0.34),
    };
    for (index, color) in [indicator, network_indicator, activity_indicator]
        .into_iter()
        .enumerate()
    {
        let x = system.x + 12.0 * scale + index as f32 * 38.0 * scale;
        draw_round_rect(
            x,
            system.y + 34.0 * scale,
            14.0 * scale,
            14.0 * scale,
            7.0 * scale,
            (0.018, 0.020, 0.020),
        );
        draw_round_rect(
            x + 3.5 * scale,
            system.y + 37.5 * scale,
            7.0 * scale,
            7.0 * scale,
            3.5 * scale,
            color,
        );
    }
    draw_rect(
        system.x + 17.0 * scale,
        system.y + 86.0 * scale,
        system.width - 34.0 * scale,
        1.0 * scale,
        (0.12, 0.18, 0.18),
    );

    draw_encoder(
        geometry.rail.primary_encoder,
        scale,
        projection.navigation_value(),
        false,
    );
    draw_encoder(
        geometry.rail.brightness,
        scale,
        presentation.brightness,
        false,
    );
    draw_encoder(geometry.rail.focus, scale, presentation.focus, false);
    draw_power_button(geometry.rail.power, scale, presentation.display_enabled);
    let plate = geometry.rail.identity_plate;
    draw_round_rect(
        plate.x,
        plate.y,
        plate.width,
        plate.height,
        3.0 * scale,
        (0.11, 0.11, 0.11),
    );
    draw_round_outline_radius(
        plate.x,
        plate.y,
        plate.width,
        plate.height,
        3.0 * scale,
        (0.26, 0.26, 0.26),
    );
    draw_static_labels(geometry, projection);
}

fn draw_static_labels(geometry: &FrameGeometry, _projection: &Projection) {
    let scale = geometry.scale.max(0.08);
    // The authored cabinet scale bottoms out near 0.76 at 1280x800. Keep
    // cabinet labels on a readable matrix instead of rounding them down to
    // the 2px glyphs that made the native screen look like a dim status HUD.
    let glyph_scale = (1.55 * scale).max(2.5);
    let instrument_color = super::theme::rgb(SECONDARY);
    let heading_color = super::theme::rgb(PRIMARY);
    let oi_color = (0.62, 0.72, 0.76);
    with_scissor(geometry.height, geometry.header, || {
        let header_y = geometry.header.y + (geometry.header.height - 7.0 * glyph_scale) * 0.5;
        draw_bitmap_text(
            geometry.header.x + geometry.u(22.0),
            header_y,
            "ATHENA",
            (glyph_scale * 0.82).max(2.25),
            (0.74, 0.82, 0.86),
            geometry.header.x + geometry.u(112.0),
        );
    });

    for (rect, label, color) in [
        (
            geometry.operator_outer,
            "ATHENA // OPERATOR CONSOLE",
            heading_color,
        ),
        (geometry.oi_outer, "ATHENA OI // GLASS COMPUTE", oi_color),
    ] {
        with_scissor(geometry.height, rect, || {
            draw_bitmap_text(
                rect.x + geometry.u(24.0),
                rect.y + geometry.u(14.0),
                label,
                (glyph_scale * 0.70).max(2.25),
                color,
                rect.right() - geometry.u(24.0),
            );
        });
    }

    let speaker = geometry.rail.speaker;
    with_scissor(geometry.height, speaker, || {
        draw_bitmap_text(
            speaker.x + geometry.u(9.0),
            speaker.bottom() - geometry.u(17.0),
            "VENT",
            (glyph_scale * 0.58).max(2.25),
            instrument_color,
            speaker.right() - geometry.u(9.0),
        );
    });

    let system = geometry.rail.system_status;
    with_scissor(geometry.height, system, || {
        // The three compact indicators keep one crisp glyph each; the module
        // itself carries the grouped SYS/NET/ACT meaning without clipping a
        // long label into an unreadable fragment at compact sizes.
        for (index, label) in ["S", "N", "A"].into_iter().enumerate() {
            draw_bitmap_text(
                system.x + geometry.u(5.0) + index as f32 * geometry.u(32.0),
                system.bottom() - geometry.u(17.0),
                label,
                (glyph_scale * 0.56).max(2.25),
                instrument_color,
                system.x + geometry.u(5.0) + index as f32 * geometry.u(32.0) + geometry.u(26.0),
            );
        }
    });

    let encoder = geometry.rail.primary_encoder;
    with_scissor(geometry.height, encoder, || {
        draw_bitmap_text(
            encoder.x + geometry.u(12.0),
            encoder.y + geometry.u(11.0),
            "ENC",
            (glyph_scale * 0.62).max(2.25),
            instrument_color,
            encoder.right() - geometry.u(8.0),
        );
    });

    for (rect, label) in [
        (geometry.rail.brightness, "BRI"),
        (geometry.rail.focus, "FOCUS"),
        (geometry.rail.power, "CRT"),
    ] {
        with_scissor(geometry.height, rect, || {
            let label_scale = (glyph_scale * 0.60).max(2.25);
            let x = rect.x + (rect.width - bitmap_width(label, label_scale)) * 0.5;
            draw_bitmap_text(
                x,
                rect.y + geometry.u(11.0),
                label,
                label_scale,
                instrument_color,
                rect.right() - geometry.u(6.0),
            );
        });
    }

    let plate = geometry.rail.identity_plate;
    with_scissor(geometry.height, plate, || {
        let plate_scale = (glyph_scale * 0.62).max(2.25);
        draw_bitmap_text(
            plate.x + geometry.u(18.0),
            plate.y + geometry.u(16.0),
            "ATHENA",
            plate_scale,
            (0.70, 0.73, 0.70),
            plate.right() - geometry.u(18.0),
        );
        draw_bitmap_text(
            plate.x + geometry.u(18.0),
            plate.y + geometry.u(34.0),
            "OI // GLASS",
            (plate_scale * 0.72).max(2.0),
            instrument_color,
            plate.right() - geometry.u(18.0),
        );
        draw_bitmap_text(
            plate.x + geometry.u(18.0),
            plate.y + geometry.u(58.0),
            "MODEL 001-A",
            2.0,
            instrument_color,
            plate.right() - geometry.u(18.0),
        );
        draw_bitmap_text(
            plate.x + geometry.u(18.0),
            plate.y + geometry.u(76.0),
            "SERIAL 0001-A",
            2.0,
            instrument_color,
            plate.right() - geometry.u(18.0),
        );
    });
}

pub(crate) fn bitmap_width(value: &str, scale: f32) -> f32 {
    value.chars().count() as f32 * 6.0 * scale
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum BitmapTextStyle {
    Phosphor,
    Crisp,
}

pub(crate) fn draw_bitmap_text(
    x: f32,
    y: f32,
    value: &str,
    scale: f32,
    color: (f32, f32, f32),
    right: f32,
) {
    draw_bitmap_text_styled(x, y, value, scale, color, right, BitmapTextStyle::Phosphor);
}

pub(crate) fn draw_bitmap_text_styled(
    x: f32,
    y: f32,
    value: &str,
    scale: f32,
    color: (f32, f32, f32),
    right: f32,
    style: BitmapTextStyle,
) {
    let scale = scale.max(0.5).round().max(1.0);
    let start_x = x;
    let start_y = y;
    let mut x;
    let mut y;
    let dot_size = (scale * 0.48).round().clamp(1.0, scale);
    let dot_offset = ((scale - dot_size) * 0.5).round();
    let glow_size = (dot_size + (scale * 0.68).round()).max(dot_size + 1.0);
    let glow_offset = ((dot_size - glow_size) * 0.5).round();
    let core_color = (
        (color.0 * 1.08).min(1.0),
        (color.1 * 1.08).min(1.0),
        (color.2 * 1.08).min(1.0),
    );

    // Two crisp passes give each lit cell a small phosphor halo without
    // turning the type into a soft web-font glow. The core remains square,
    // snapped to the same matrix as the buddy and the OI scene.
    let passes: [(bool, f32); 2] = if style == BitmapTextStyle::Crisp {
        [(false, 1.0), (false, 0.0)]
    } else {
        [(true, 0.18), (false, 1.0)]
    };
    for (glow, alpha) in passes {
        if alpha <= 0.0 {
            continue;
        }
        x = start_x;
        y = start_y;
        unsafe {
            let layer = if glow { color } else { core_color };
            glColor3f(layer.0 * alpha, layer.1 * alpha, layer.2 * alpha);
            glBegin(GL_QUADS);
            for character in value.chars() {
                if character == '\n' {
                    x = start_x;
                    y += 9.0 * scale;
                    continue;
                }
                if x + 5.0 * scale > right {
                    break;
                }
                for (row, bits) in static_glyph(character).into_iter().enumerate() {
                    for column in 0..5 {
                        if bits & (1 << (4 - column)) == 0 {
                            continue;
                        }
                        let (offset, size) = if glow {
                            (glow_offset, glow_size)
                        } else {
                            (dot_offset, dot_size)
                        };
                        let px = (x + column as f32 * scale + offset).round();
                        let py = (y + row as f32 * scale + offset).round();
                        glVertex2f(px, py);
                        glVertex2f(px + size, py);
                        glVertex2f(px + size, py + size);
                        glVertex2f(px, py + size);
                    }
                }
                x += 6.0 * scale;
            }
            glEnd();
        }
    }
}
