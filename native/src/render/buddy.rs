use super::primitives::draw_rect;
use super::theme::{AMBER, FAILURE, PRIMARY, SECONDARY, SUCCESS};
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
    let color = super::theme::rgb(PRIMARY);
    let status_accent = match pose {
        BuddyPose::Failure => super::theme::rgb(FAILURE),
        BuddyPose::Approval => super::theme::rgb(AMBER),
        BuddyPose::Success => super::theme::rgb(SUCCESS),
        _ => super::theme::rgb(SECONDARY),
    };
    let frame_period = match pose {
        BuddyPose::Idle => 0.52,
        BuddyPose::Approval => 0.34,
        BuddyPose::Failure => 0.40,
        BuddyPose::Success => 0.56,
        _ => 0.36,
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
                BuddyKind::Owl => false,
            };
            if !lit {
                continue;
            }
            let mut pixel_color = if matches!(kind, BuddyKind::Owl) && row >= 19 {
                super::theme::rgb(SECONDARY)
            } else {
                color
            };
            if matches!(kind, BuddyKind::Owl)
                && ((row == 11 && (column == 15 || column == 16))
                    || (row == 12 && (14..=17).contains(&column))
                    || (row == 13 && (column == 15 || column == 16)))
            {
                // A small warm beak gives the owl a readable species cue
                // without changing the restrained phosphor palette around it.
                pixel_color = super::theme::rgb(AMBER);
            } else if (matches!(kind, BuddyKind::Owl)
                && (row == 10 || row == 11)
                && (column == 10 || column == 21))
                || (matches!(kind, BuddyKind::Cat) && (row == 8 || row == 9))
                || (matches!(kind, BuddyKind::Bot) && row <= 3)
            {
                pixel_color = status_accent;
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
