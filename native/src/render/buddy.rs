use super::primitives::draw_rect;
use crate::buddy::{
    BuddyKind, BuddyPose, REQUIRED_POSES, SPRITE_FRAME_COUNT, SPRITE_SCALE, pose_for_state,
    sprite_frame,
};

pub(crate) fn draw_buddy(x: f32, y: f32, state: &str, status: &str, character: &str, phase: f32) {
    let Some(kind) = BuddyKind::parse(character) else {
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
    let frame_period = match pose {
        BuddyPose::Idle => 0.20,
        BuddyPose::Approval => 0.16,
        BuddyPose::Failure => 0.14,
        BuddyPose::Success => 0.22,
        _ => 0.18,
    };
    let frame = ((phase.max(0.0) / frame_period).floor() as usize) % SPRITE_FRAME_COUNT;
    let scale = SPRITE_SCALE;
    let (sprite_width, sprite_height) = kind.sprite_bounds();
    let left = (x - sprite_width * scale / 2.0).round();
    let top = (y - sprite_height * scale / 2.0).round();
    let rows = sprite_frame(kind, pose, frame).rows;
    // Every occupied logical cell is exactly one 2x2 rendered phosphor block.
    // Keep the glow out of the contract box: the surrounding scene already
    // supplies the CRT bloom and the Buddy must never expand into nearby UI.
    for (row, mask) in rows.iter().copied().enumerate() {
        for column in 0..sprite_width as usize {
            let mut lit = mask & (1_u32 << column) != 0;
            lit |= match kind {
                BuddyKind::Cat => {
                    (row == 3 && (column == 7 || column == 24))
                        || (row == 8 && (column == 10 || column == 21))
                }
                BuddyKind::Bot => {
                    (row == 1 && column == 16)
                        || (row == 2 && (column == 15 || column == 16 || column == 17))
                }
                BuddyKind::Owl => row == 5 && (column == 10 || column == 21),
            };
            if !lit {
                continue;
            }
            let mut pixel_color = color;
            if matches!(kind, BuddyKind::Cat) && (row == 8 || row == 9) {
                pixel_color = (0.98, 0.84, 0.42);
            }
            if matches!(kind, BuddyKind::Bot) && row <= 3 {
                pixel_color = (0.63, 0.88, 0.86);
            }
            let px = left + column as f32 * scale;
            let py = top + row as f32 * scale;
            draw_rect(px, py, scale, scale, pixel_color);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::buddy::{BuddyKind, REQUIRED_POSES, SPRITE_FRAME_COUNT, sprite_frame};

    #[test]
    fn all_semantic_poses_have_multiple_matrix_frames() {
        for kind in [BuddyKind::Owl, BuddyKind::Cat, BuddyKind::Bot] {
            for pose in REQUIRED_POSES {
                assert!(kind.pose_frame_count(pose) >= 3);
                assert_ne!(
                    sprite_frame(kind, pose, 0).rows,
                    sprite_frame(kind, pose, 1).rows
                );
            }
        }
        const { assert!(SPRITE_FRAME_COUNT >= 3) };
    }
}
