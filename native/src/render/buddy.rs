use super::primitives::draw_rect;
use crate::buddy::{
    BuddyKind, BuddyPose, REQUIRED_POSES, SPRITE_FRAME_COUNT, SPRITE_HEIGHT, SPRITE_SCALE,
    SPRITE_WIDTH, pose_for_state, sprite_frame,
};

pub(crate) fn draw_buddy(x: f32, y: f32, state: &str, status: &str, character: &str, phase: f32) {
    let Some(kind) = BuddyKind::parse(character) else {
        // Unknown names are intentionally invisible. Native only promises
        // the three built-in sprite sets; it must not render a fake fallback.
        return;
    };
    let pose = pose_for_state(if state.is_empty() { status } else { state });
    debug_assert_eq!(kind.pose_frame_count(pose), SPRITE_FRAME_COUNT);
    let _ = REQUIRED_POSES;
    let color = match pose {
        BuddyPose::Failure => (0.92, 0.32, 0.36),
        BuddyPose::Approval => (0.93, 0.69, 0.25),
        BuddyPose::Success => (0.46, 0.91, 0.67),
        _ => (0.36, 0.82, 0.78),
    };
    let frame = if phase.sin() >= 0.0 { 0 } else { 1 };
    // The actor is deliberately legible at the CRT's logical resolution.
    // Its footprint is bounded by the OI scene, not by projection geometry;
    // nodes and semantic edges remain owned by the scene renderer.
    let scale = SPRITE_SCALE;
    let left = x - SPRITE_WIDTH * scale / 2.0;
    let top = y - SPRITE_HEIGHT * scale / 2.0;
    let (sprite_width, _sprite_height) = kind.sprite_bounds();
    let rows = sprite_frame(kind, pose, frame);
    for (row, line) in rows.iter().enumerate() {
        for (column, pixel) in line.iter().copied().enumerate() {
            if pixel == ' ' {
                continue;
            }
            let mut pixel_color = color;
            if pixel == 'o' {
                pixel_color = if kind == BuddyKind::Cat {
                    (0.98, 0.84, 0.42)
                } else {
                    (0.90, 0.96, 0.83)
                };
            } else if pixel == '-' || pixel == '^' || pixel == '_' {
                pixel_color = (color.0 * 0.76, color.1 * 0.76, color.2 * 0.76);
            }
            let wobble = if frame == 1 && (row == 0 || row == 1) {
                0.45
            } else {
                0.0
            };
            draw_rect(
                left + column as f32 * scale + wobble,
                top + row as f32 * scale,
                scale + 0.3,
                scale + 0.3,
                pixel_color,
            );
        }
    }
    draw_pose_effect(left, top, scale, pose, color, frame);
    // A small pose marker is an actor cue, not a status card.
    let marker = match pose {
        BuddyPose::Searching | BuddyPose::Inspecting => (sprite_width * scale + 8.0, 4.0),
        BuddyPose::Approval => (sprite_width * scale + 8.0, 10.0),
        BuddyPose::Failure => (sprite_width * scale + 8.0, 16.0),
        _ => (sprite_width * scale + 8.0, 0.0),
    };
    if marker.1 > 0.0 {
        draw_rect(left + marker.0, top + marker.1, 3.0, 3.0, color);
    }
}

fn draw_pose_effect(
    left: f32,
    top: f32,
    scale: f32,
    pose: BuddyPose,
    color: (f32, f32, f32),
    frame: usize,
) {
    let dim = (color.0 * 0.58, color.1 * 0.58, color.2 * 0.58);
    let bright = (color.0 * 0.92, color.1 * 0.92, color.2 * 0.92);
    let bottom = top + SPRITE_HEIGHT * scale;
    match pose {
        BuddyPose::Idle => {
            if frame == 1 {
                draw_rect(left + 4.0 * scale, top - 2.0, 2.0, 2.0, dim);
            }
        }
        BuddyPose::Listening => {
            draw_rect(left - 4.0, top + 4.0 * scale, 2.0, 7.0 * scale, bright);
            draw_rect(left - 2.0, top + 2.0 * scale, 2.0, 2.0, bright);
        }
        BuddyPose::Thinking => {
            draw_rect(left + 5.0 * scale, top - 5.0, 3.0, 3.0, bright);
            draw_rect(left + 9.0 * scale, top - 10.0, 2.0, 2.0, dim);
        }
        BuddyPose::Inspecting => {
            draw_rect(left + 15.0 * scale, top + 4.0 * scale, 8.0, 2.0, bright);
            draw_rect(left + 21.0 * scale, top + 2.0 * scale, 2.0, 6.0, bright);
        }
        BuddyPose::Searching => {
            draw_rect(left - 7.0, top + 1.0 * scale, 2.0, 8.0 * scale, bright);
            draw_rect(left - 10.0, top + 1.0 * scale, 8.0, 2.0, dim);
        }
        BuddyPose::Reading => {
            draw_rect(left + 1.0 * scale, bottom + 3.0, 6.0 * scale, 2.0, bright);
            draw_rect(left + 7.0 * scale, bottom + 3.0, 6.0 * scale, 2.0, bright);
            draw_rect(left + 7.0 * scale, bottom + 3.0, 2.0, 7.0, dim);
        }
        BuddyPose::Coding => {
            for index in 0..4 {
                draw_rect(
                    left + (2.0 + index as f32 * 2.5) * scale,
                    bottom + 3.0,
                    1.5 * scale,
                    2.0,
                    if index % 2 == frame { bright } else { dim },
                );
            }
        }
        BuddyPose::Executing => {
            draw_rect(left - 6.0, top + 8.0 * scale, 4.0, 2.0, bright);
            draw_rect(
                left + SPRITE_WIDTH * scale + 2.0,
                top + 8.0 * scale,
                4.0,
                2.0,
                bright,
            );
            draw_rect(left - 9.0, top + 5.0 * scale, 2.0, 2.0, dim);
        }
        BuddyPose::Testing => {
            let bracket = (color.0 * 0.76, color.1 * 0.76, color.2 * 0.76);
            draw_rect(left - 6.0, top + 4.0 * scale, 2.0, 8.0 * scale, bracket);
            draw_rect(
                left + SPRITE_WIDTH * scale + 4.0,
                top + 4.0 * scale,
                2.0,
                8.0 * scale,
                bracket,
            );
        }
        BuddyPose::Verifying => {
            draw_rect(
                left + SPRITE_WIDTH * scale + 3.0,
                top + 8.0 * scale,
                3.0,
                2.0,
                bright,
            );
            draw_rect(
                left + SPRITE_WIDTH * scale + 6.0,
                top + 5.0 * scale,
                2.0,
                5.0,
                bright,
            );
        }
        BuddyPose::Approval => {
            let amber = (0.93, 0.69, 0.25);
            draw_rect(
                left + SPRITE_WIDTH * scale + 4.0,
                top + 4.0 * scale,
                7.0,
                2.0,
                amber,
            );
            draw_rect(
                left + SPRITE_WIDTH * scale + 6.0,
                top + 2.0 * scale,
                3.0,
                6.0,
                amber,
            );
        }
        BuddyPose::Success => {
            draw_rect(left - 8.0, top - 4.0, 3.0, 3.0, bright);
            draw_rect(
                left + SPRITE_WIDTH * scale + 5.0,
                top - 8.0,
                3.0,
                3.0,
                bright,
            );
        }
        BuddyPose::Failure => {
            let red = (0.92, 0.32, 0.36);
            draw_rect(left - 5.0, top + 2.0 * scale, 2.0, 13.0 * scale, red);
            draw_rect(left - 8.0, top + 7.0 * scale, 8.0, 2.0, red);
        }
        BuddyPose::Recovering => {
            let repair = (0.46, 0.91, 0.67);
            draw_rect(
                left + SPRITE_WIDTH * scale + 4.0,
                top + 5.0 * scale,
                8.0,
                2.0,
                repair,
            );
            draw_rect(
                left + SPRITE_WIDTH * scale + 7.0,
                top + 2.0 * scale,
                2.0,
                8.0,
                repair,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::SPRITE_FRAME_COUNT;
    use crate::buddy::{BuddyKind, REQUIRED_POSES, sprite_frame};

    #[test]
    fn built_in_buddies_have_equal_pose_and_frame_coverage() {
        let kinds = [BuddyKind::Owl, BuddyKind::Cat, BuddyKind::Bot];
        for pose in REQUIRED_POSES {
            let counts: Vec<usize> = kinds
                .into_iter()
                .map(|kind| kind.pose_frame_count(pose))
                .collect();
            assert!(counts.iter().all(|count| *count == SPRITE_FRAME_COUNT));
            assert_eq!(counts[0], counts[1]);
            assert_eq!(counts[1], counts[2]);
            let regions: Vec<(f32, f32)> = kinds
                .into_iter()
                .map(|kind| kind.dirty_region(pose))
                .collect();
            assert!(regions.iter().all(|region| *region == regions[0]));
        }
        assert_eq!(
            kinds.map(BuddyKind::sprite_bounds),
            [(14.0, 11.0), (14.0, 11.0), (14.0, 11.0)]
        );
    }

    #[test]
    fn pose_frames_change_authored_actor_features() {
        for kind in [BuddyKind::Owl, BuddyKind::Cat, BuddyKind::Bot] {
            for pose in REQUIRED_POSES {
                assert_ne!(
                    sprite_frame(kind, pose, 0),
                    sprite_frame(kind, pose, 1),
                    "{kind:?} {pose:?} has identical animation frames"
                );
            }
        }
    }

    #[test]
    fn unknown_buddies_do_not_use_a_generic_sprite_fallback() {
        assert!(BuddyKind::parse("dragon").is_none());
        assert!(BuddyKind::parse("off").is_none());
        assert!(REQUIRED_POSES.len() > 1);
    }
}
