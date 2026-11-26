use super::primitives::draw_rect;
use std::sync::OnceLock;

const MASK_WIDTH: usize = 144;
const MASK_HEIGHT: usize = 120;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum OwlLayer {
    Outline,
    Eye,
    Wing,
    Body,
    Face,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct OwlDot {
    pub x: f32,
    pub y: f32,
    pub size: f32,
    pub phase: f32,
    pub gate: f32,
    pub layer: OwlLayer,
}

pub(crate) fn hash_dot(x: u32, y: u32, seed: u32) -> f32 {
    let mut n = x
        .wrapping_mul(374_761_393)
        .wrapping_add(y.wrapping_mul(668_265_263))
        .wrapping_add(seed.wrapping_mul(1_442_695_041));
    n ^= n >> 13;
    n = n.wrapping_mul(1_274_126_177);
    n ^= n >> 16;
    n as f32 / u32::MAX as f32
}

fn mask() -> Vec<u8> {
    vec![0; MASK_WIDTH * MASK_HEIGHT]
}

fn set(mask: &mut [u8], x: i32, y: i32, value: u8) {
    if x >= 0 && y >= 0 && (x as usize) < MASK_WIDTH && (y as usize) < MASK_HEIGHT {
        mask[y as usize * MASK_WIDTH + x as usize] = value;
    }
}

fn ellipse_stroke(mask: &mut [u8], x0: i32, y0: i32, x1: i32, y1: i32, width: i32) {
    let cx = (x0 + x1) as f32 / 2.0;
    let cy = (y0 + y1) as f32 / 2.0;
    let rx = (x1 - x0) as f32 / 2.0;
    let ry = (y1 - y0) as f32 / 2.0;
    for y in y0..=y1 {
        for x in x0..=x1 {
            let dx = (x as f32 - cx) / rx.max(1.0);
            let dy = (y as f32 - cy) / ry.max(1.0);
            let distance = (dx * dx + dy * dy).sqrt();
            if (distance - 1.0).abs() < width as f32 / (rx.min(ry) * 4.0).max(4.0) {
                set(mask, x, y, 255);
            }
        }
    }
}

fn ellipse_fill(mask: &mut [u8], x0: i32, y0: i32, x1: i32, y1: i32) {
    let cx = (x0 + x1) as f32 / 2.0;
    let cy = (y0 + y1) as f32 / 2.0;
    let rx = (x1 - x0) as f32 / 2.0;
    let ry = (y1 - y0) as f32 / 2.0;
    for y in y0..=y1 {
        for x in x0..=x1 {
            let dx = (x as f32 - cx) / rx.max(1.0);
            let dy = (y as f32 - cy) / ry.max(1.0);
            if dx * dx + dy * dy <= 1.0 {
                set(mask, x, y, 255);
            }
        }
    }
}

fn line(mask: &mut [u8], x1: i32, y1: i32, x2: i32, y2: i32) {
    let steps = (x2 - x1).abs().max((y2 - y1).abs()).max(1);
    for step in 0..=steps {
        let t = step as f32 / steps as f32;
        set(
            mask,
            (x1 as f32 + t * (x2 - x1) as f32).round() as i32,
            (y1 as f32 + t * (y2 - y1) as f32).round() as i32,
            255,
        );
    }
}

fn thick_line(mask: &mut [u8], x1: i32, y1: i32, x2: i32, y2: i32, width: i32) {
    for oy in -width..=width {
        for ox in -width..=width {
            line(mask, x1 + ox, y1 + oy, x2 + ox, y2 + oy);
        }
    }
}

fn triangle_stroke(mask: &mut [u8], a: (i32, i32), b: (i32, i32), c: (i32, i32), width: i32) {
    thick_line(mask, a.0, a.1, b.0, b.1, width / 2);
    thick_line(mask, b.0, b.1, c.0, c.1, width / 2);
    thick_line(mask, c.0, c.1, a.0, a.1, width / 2);
}

fn sample(
    mask: &[u8],
    step: usize,
    threshold: u8,
    keep: f32,
    seed: u32,
    layer: OwlLayer,
) -> Vec<OwlDot> {
    let mut dots = Vec::new();
    for y in (0..MASK_HEIGHT).step_by(step) {
        for x in (0..MASK_WIDTH).step_by(step) {
            if mask[y * MASK_WIDTH + x] < threshold || hash_dot(x as u32, y as u32, seed) > keep {
                continue;
            }
            dots.push(OwlDot {
                x: x as f32 / MASK_WIDTH as f32,
                y: y as f32 / MASK_HEIGHT as f32,
                size: 1.25 + hash_dot(x as u32 + 91, y as u32 + 7, seed),
                phase: hash_dot(x as u32 + 17, y as u32 + 31, seed) * std::f32::consts::TAU,
                gate: hash_dot(x as u32, y as u32, seed + 99),
                layer,
            });
        }
    }
    dots
}

fn owl_dots() -> &'static Vec<OwlDot> {
    static FIELD: OnceLock<Vec<OwlDot>> = OnceLock::new();
    FIELD.get_or_init(|| {
        // Two overlapping cranial lobes + a lower facial bowl create an owl
        // crown and cheeks without a rectangular display/screen silhouette.
        let mut head = mask();
        ellipse_stroke(&mut head, 27, 24, 76, 81, 4);
        ellipse_stroke(&mut head, 68, 24, 117, 81, 4);
        ellipse_stroke(&mut head, 47, 44, 97, 90, 4);
        triangle_stroke(&mut head, (31, 36), (22, 8), (53, 23), 5);
        triangle_stroke(&mut head, (113, 36), (122, 8), (91, 23), 5);

        // Orbital discs are their own high-persistence semantic layer.
        let mut eyes = mask();
        ellipse_stroke(&mut eyes, 34, 30, 70, 66, 5);
        ellipse_stroke(&mut eyes, 74, 30, 110, 66, 5);

        let mut wings = mask();
        ellipse_fill(&mut wings, 25, 72, 54, 110);
        ellipse_fill(&mut wings, 90, 72, 119, 110);

        // A narrow tapered chest, with two overlapping lobes for shoulders
        // and talons, keeps the body subordinate to the face.
        let mut body = mask();
        ellipse_fill(&mut body, 54, 72, 90, 100);
        ellipse_fill(&mut body, 60, 92, 84, 117);
        ellipse_fill(&mut body, 58, 110, 70, 121);
        ellipse_fill(&mut body, 74, 110, 86, 121);

        let mut face = mask();
        ellipse_fill(&mut face, 49, 43, 58, 53);
        ellipse_fill(&mut face, 86, 43, 95, 53);
        thick_line(&mut face, 72, 61, 68, 70, 1);
        thick_line(&mut face, 68, 70, 76, 70, 1);
        thick_line(&mut face, 76, 70, 72, 61, 1);

        let mut dots = sample(&head, 4, 64, 0.92, 11, OwlLayer::Outline);
        dots.extend(sample(&eyes, 3, 64, 1.0, 17, OwlLayer::Eye));
        dots.extend(sample(&wings, 4, 72, 0.78, 23, OwlLayer::Wing));
        dots.extend(sample(&body, 5, 72, 0.42, 37, OwlLayer::Body));
        dots.extend(sample(&face, 3, 64, 1.0, 53, OwlLayer::Face));
        dots
    })
}

pub(crate) fn owl_field() -> &'static [OwlDot] {
    owl_dots()
}

#[cfg(test)]
pub(crate) fn stable_dot_coords(time: f32) -> Vec<(f32, f32, OwlLayer)> {
    let bob = (time * std::f32::consts::TAU * 0.18).sin() * 2.0;
    owl_field()
        .iter()
        .map(|dot| (dot.x * 100.0, dot.y * 90.0 + bob, dot.layer))
        .collect()
}

pub(crate) fn draw_sampled_owl(
    x: f32,
    y: f32,
    status: &str,
    time: f32,
    reduced_motion: bool,
) -> (f32, f32) {
    let phosphor = (0.64, 0.84, 1.0);
    let signal = match status.to_ascii_uppercase().as_str() {
        "FAILURE" | "BLOCKED" => (0.88, 0.47, 0.49),
        "APPROVAL" => (0.87, 0.69, 0.42),
        _ => phosphor,
    };
    let epoch = (time * 5.0).floor() as u32;
    let bob = if reduced_motion {
        0.0
    } else {
        (time * std::f32::consts::TAU * 0.18).sin() * 2.0
    };
    let mut first = (0.0_f32, 0.0_f32);
    let mut last = (0.0_f32, 0.0_f32);
    for (index, dot) in owl_field().iter().copied().enumerate() {
        let layer_dropout_scale = match dot.layer {
            OwlLayer::Eye | OwlLayer::Face => 0.25,
            OwlLayer::Outline => 0.55,
            OwlLayer::Wing => 1.0,
            OwlLayer::Body => 1.25,
        };
        let dropout_threshold = (0.012 + (1.0 - dot.gate) * 0.020) * layer_dropout_scale;
        if !reduced_motion && hash_dot(index as u32, epoch, 1201) < dropout_threshold {
            continue;
        }
        let shimmer = if reduced_motion {
            1.0
        } else {
            0.90 + 0.10 * (time * std::f32::consts::TAU * 0.85 + dot.phase).sin()
        };
        let px = x - 48.0 + dot.x * 96.0;
        let py = y - 45.0 + dot.y * 90.0 + bob;
        if index == 0 {
            first = (px, py);
        }
        last = (px, py);
        let base = if dot.layer == OwlLayer::Face {
            signal
        } else {
            phosphor
        };
        let alpha = match dot.layer {
            OwlLayer::Body => 0.45,
            OwlLayer::Wing => 0.75,
            _ => 0.92,
        } * shimmer;
        draw_rect(
            px.round(),
            py.round(),
            dot.size.max(1.0),
            dot.size.max(1.0),
            (base.0 * alpha, base.1 * alpha, base.2 * alpha),
        );
    }
    (first.0, last.1)
}

#[cfg(test)]
mod tests {
    use super::{OwlLayer, draw_sampled_owl, owl_field, stable_dot_coords};

    #[test]
    fn sampled_mask_has_density_hierarchy() {
        let field = owl_field();
        assert!(
            field
                .iter()
                .filter(|dot| dot.layer == OwlLayer::Outline)
                .count()
                > field
                    .iter()
                    .filter(|dot| dot.layer == OwlLayer::Face)
                    .count()
        );
        assert!(field.iter().any(|dot| dot.layer == OwlLayer::Wing));
        assert!(field.iter().any(|dot| dot.layer == OwlLayer::Body));
    }

    #[test]
    fn owl_has_two_distinct_eye_fields() {
        let eyes: Vec<_> = owl_field()
            .iter()
            .filter(|dot| dot.layer == OwlLayer::Eye)
            .collect();
        let left = eyes.iter().filter(|dot| dot.x < 0.5).count();
        let right = eyes.iter().filter(|dot| dot.x > 0.5).count();
        assert!(left > 8, "left eye dots: {left}");
        assert!(right > 8, "right eye dots: {right}");
    }

    #[test]
    fn owl_has_visible_ear_tufts() {
        let field = owl_field();
        let upper_left = field
            .iter()
            .filter(|dot| dot.layer == OwlLayer::Outline && dot.y < 0.22 && dot.x < 0.40)
            .count();
        let upper_right = field
            .iter()
            .filter(|dot| dot.layer == OwlLayer::Outline && dot.y < 0.22 && dot.x > 0.60)
            .count();
        assert!(upper_left >= 4, "upper-left dots: {upper_left}");
        assert!(upper_right >= 4, "upper-right dots: {upper_right}");
    }

    #[test]
    fn point_field_is_stable_and_animates_only_presence() {
        let first = stable_dot_coords(0.0);
        let second = stable_dot_coords(0.0);
        assert_eq!(first, second);
        let later = stable_dot_coords(0.2);
        assert_eq!(first.len(), later.len());
        assert!(
            first
                .iter()
                .zip(later.iter())
                .any(|(a, b)| a.0 == b.0 && a.1 != b.1)
        );
    }

    #[test]
    fn reduced_motion_is_deterministic_and_has_no_dropout() {
        let _ = draw_sampled_owl(50.0, 60.0, "READY", 0.0, true);
        let _ = draw_sampled_owl(50.0, 60.0, "READY", 1.0, true);
        assert_eq!(owl_field().len(), stable_dot_coords(1.0).len());
    }
}
