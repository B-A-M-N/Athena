use super::super::*;
use super::primitives::*;

const CHASSIS_NOISE: &[u8] = include_bytes!("../../assets/athenabox/chassis_noise.ppm");

/// Retained shell material for the graphite enclosure.
pub(crate) struct ChassisMaterial {
    texture: u32,
    enabled: bool,
}

impl ChassisMaterial {
    pub(crate) fn new() -> Self {
        let Some((width, height, pixels)) = parse_ppm(CHASSIS_NOISE) else {
            return Self {
                texture: 0,
                enabled: false,
            };
        };
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

#[cfg(test)]
mod tests {
    use super::{CHASSIS_NOISE, parse_ppm};

    #[test]
    fn bundled_chassis_material_is_a_complete_rgb_texture() {
        let (width, height, pixels) = parse_ppm(CHASSIS_NOISE).expect("valid bundled PPM");
        assert_eq!((width, height), (8, 8));
        assert_eq!(pixels.len(), 8 * 8 * 3);
        assert!(pixels.iter().all(|value| *value > 0));
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
            brightness: 0.82,
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

    let shell = inset(chassis, 18.0 * scale);
    draw_round_rect(
        shell.x + 4.0 * scale,
        shell.y + 6.0 * scale,
        shell.width,
        shell.height,
        24.0 * scale,
        (0.006, 0.007, 0.008),
    );
    draw_round_rect(
        shell.x,
        shell.y,
        shell.width,
        shell.height,
        24.0 * scale,
        (0.090, 0.093, 0.095),
    );
    material.draw_surface(inset(shell, 10.0 * scale));
    draw_round_outline(
        shell.x,
        shell.y,
        shell.width,
        shell.height,
        (0.20, 0.21, 0.21),
    );

    let deck = inset(chassis, 38.0 * scale);
    draw_round_rect(
        deck.x,
        deck.y,
        deck.width,
        deck.height,
        14.0 * scale,
        (0.028, 0.030, 0.031),
    );
    draw_round_outline(deck.x, deck.y, deck.width, deck.height, (0.11, 0.12, 0.12));

    draw_round_rect(
        geometry.header.x,
        geometry.header.y,
        geometry.header.width,
        geometry.header.height,
        10.0 * scale,
        (0.075, 0.078, 0.079),
    );
    material.draw_surface(inset(geometry.header, 5.0 * scale));
    draw_round_outline(
        geometry.header.x,
        geometry.header.y,
        geometry.header.width,
        geometry.header.height,
        (0.22, 0.23, 0.23),
    );
    for index in 0..8 {
        let x = geometry.header.x + geometry.header.width * 0.48 + index as f32 * 13.0 * scale;
        draw_round_rect(
            x,
            geometry.header.y + geometry.header.height * 0.38,
            8.0 * scale,
            3.0 * scale,
            1.5 * scale,
            (0.012, 0.014, 0.015),
        );
    }

    let status_lower = projection.status.to_ascii_lowercase();
    let indicator = if status_lower.contains("fail") {
        (0.82, 0.24, 0.27)
    } else if status_lower.contains("approval") {
        (0.88, 0.59, 0.20)
    } else {
        (0.27, 0.78, 0.68)
    };
    let lamp_x = geometry.header.right() - 42.0 * scale;
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
    draw_rect(
        seam_x,
        geometry.operator_outer.y + 34.0 * scale,
        1.0 * scale,
        (geometry.operator_outer.height - 68.0 * scale).max(0.0),
        (0.08, 0.09, 0.09),
    );
    draw_round_outline(
        seam_x - 7.0 * scale,
        seam_y - 12.0 * scale,
        14.0 * scale,
        24.0 * scale,
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
    draw_round_outline(
        geometry.controls.x,
        geometry.controls.y,
        geometry.controls.width,
        geometry.controls.height,
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
    for (index, color) in [indicator, (0.25, 0.58, 0.67), (0.32, 0.34, 0.34)]
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
        presentation.focus,
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
    draw_round_outline(
        plate.x,
        plate.y,
        plate.width,
        plate.height,
        (0.26, 0.26, 0.26),
    );
    draw_static_labels(geometry, projection);
}

fn draw_static_labels(geometry: &FrameGeometry, projection: &Projection) {
    let scale = geometry.scale.max(0.08);
    let glyph_scale = (1.35 * scale).max(0.72);
    let instrument_color = (0.42, 0.54, 0.62);
    let heading_color = (0.64, 0.74, 0.83);
    let oi_color = rgb_f32(mode_color(VisualMode::from_projection(projection).as_str()));

    with_scissor(geometry.height, geometry.header, || {
        let header_y = geometry.header.y + (geometry.header.height - 7.0 * glyph_scale) * 0.5;
        draw_bitmap_text(
            geometry.header.x + geometry.u(22.0),
            header_y,
            "ATHENA",
            glyph_scale,
            (0.78, 0.86, 0.91),
            geometry.header.right() - geometry.u(44.0),
        );
        draw_bitmap_text(
            geometry.header.x + geometry.u(145.0),
            header_y + geometry.u(1.0),
            "// OPERATOR INSTRUMENT",
            glyph_scale * 0.76,
            instrument_color,
            geometry.header.right() - geometry.u(310.0),
        );
        let glass = "GLASS COMPUTE ENGINE";
        let glass_scale = glyph_scale * 0.76;
        draw_bitmap_text(
            geometry.header.right() - geometry.u(44.0) - bitmap_width(glass, glass_scale),
            header_y + geometry.u(1.0),
            glass,
            glass_scale,
            (0.72, 0.76, 0.72),
            geometry.header.right() - geometry.u(44.0),
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
                glyph_scale * 0.70,
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
            "AUDIO OUT",
            glyph_scale * 0.58,
            instrument_color,
            speaker.right() - geometry.u(9.0),
        );
    });

    let system = geometry.rail.system_status;
    with_scissor(geometry.height, system, || {
        for (index, label) in ["SYS", "NET", "ACT"].into_iter().enumerate() {
            draw_bitmap_text(
                system.x + geometry.u(5.0) + index as f32 * geometry.u(32.0),
                system.bottom() - geometry.u(17.0),
                label,
                glyph_scale * 0.56,
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
            glyph_scale * 0.62,
            instrument_color,
            encoder.right() - geometry.u(8.0),
        );
    });

    for (rect, label) in [
        (geometry.rail.brightness, "BRI"),
        (geometry.rail.focus, "FOCUS"),
        (geometry.rail.power, "POWER"),
    ] {
        with_scissor(geometry.height, rect, || {
            let label_scale = glyph_scale * 0.60;
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
        let plate_scale = glyph_scale * 0.62;
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
            "OI // GLASS COMPUTE",
            plate_scale * 0.72,
            instrument_color,
            plate.right() - geometry.u(18.0),
        );
        draw_bitmap_text(
            plate.x + geometry.u(18.0),
            plate.y + geometry.u(58.0),
            "MODEL 001-A",
            plate_scale * 0.62,
            instrument_color,
            plate.right() - geometry.u(18.0),
        );
        draw_bitmap_text(
            plate.x + geometry.u(18.0),
            plate.y + geometry.u(76.0),
            "SERIAL 0001-A",
            plate_scale * 0.62,
            instrument_color,
            plate.right() - geometry.u(18.0),
        );
    });
}

fn bitmap_width(value: &str, scale: f32) -> f32 {
    value.chars().count() as f32 * 6.0 * scale
}

fn draw_bitmap_text(x: f32, y: f32, value: &str, scale: f32, color: (f32, f32, f32), right: f32) {
    let scale = scale.max(0.5);
    let start_x = x;
    let mut x = x;
    unsafe {
        glColor3f(color.0, color.1, color.2);
        glBegin(GL_QUADS);
        for character in value.chars() {
            if character == '\n' {
                x = start_x;
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
                    let px = x + column as f32 * scale;
                    let py = y + row as f32 * scale;
                    glVertex2f(px, py);
                    glVertex2f(px + scale, py);
                    glVertex2f(px + scale, py + scale);
                    glVertex2f(px, py + scale);
                }
            }
            x += 6.0 * scale;
        }
        glEnd();
    }
}

fn static_glyph(character: char) -> [u8; 7] {
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
            0b01110, 0b10000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110,
        ],
        '7' => [
            0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000,
        ],
        '8' => [
            0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110,
        ],
        '9' => [
            0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00001, 0b01110,
        ],
        '/' => [
            0b00001, 0b00010, 0b00010, 0b00100, 0b01000, 0b01000, 0b10000,
        ],
        '-' => [
            0b00000, 0b00000, 0b00000, 0b11111, 0b00000, 0b00000, 0b00000,
        ],
        _ => [0; 7],
    }
}

fn inset(rect: PixelRect, amount: f32) -> PixelRect {
    PixelRect {
        x: rect.x + amount,
        y: rect.y + amount,
        width: (rect.width - amount * 2.0).max(0.0),
        height: (rect.height - amount * 2.0).max(0.0),
    }
}

fn draw_recessed_panel(
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
    draw_round_rect(rect.x, rect.y, rect.width, rect.height, 9.0 * scale, outer);
    draw_round_outline(rect.x, rect.y, rect.width, rect.height, (0.17, 0.18, 0.18));
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

fn draw_recessed_instrument(rect: PixelRect, scale: f32, focused: bool) {
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
    draw_round_outline(rect.x, rect.y, rect.width, rect.height, (0.20, 0.21, 0.21));
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

fn draw_operator_well(geometry: &FrameGeometry, focused: bool, scale: f32) {
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
    draw_round_outline(
        inner.x,
        inner.y,
        inner.width,
        inner.height,
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

fn draw_glass_crt_well(geometry: &FrameGeometry, scale: f32) {
    let outer = geometry.oi_outer;
    draw_recessed_panel(outer, scale, (0.057, 0.061, 0.062), (0.005, 0.017, 0.019));
    let inner = geometry.oi_inner;
    draw_round_rect(
        inner.x - 8.0 * scale,
        inner.y - 8.0 * scale,
        inner.width + 16.0 * scale,
        inner.height + 16.0 * scale,
        19.0 * scale,
        (0.003, 0.011, 0.013),
    );
    draw_round_outline(
        inner.x - 3.0 * scale,
        inner.y - 3.0 * scale,
        inner.width + 6.0 * scale,
        inner.height + 6.0 * scale,
        (0.09, 0.25, 0.26),
    );
}

fn draw_encoder(rect: PixelRect, scale: f32, value: f32, power: bool) {
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
    draw_round_outline(
        x - size * 0.5,
        y - size * 0.5,
        size,
        size,
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
            (0.29, 0.46, 0.45),
        );
    }
}

fn draw_power_button(rect: PixelRect, scale: f32, enabled: bool) {
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
    draw_round_outline(x, y, side, side, (0.24, 0.25, 0.25));
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
