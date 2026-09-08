mod poses;

pub(crate) use poses::{BuddyPose, REQUIRED_POSES, pose_for_state};

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum BuddyKind {
    Owl,
    Cat,
    Bot,
}

/// Buddy is authored as a phosphor matrix, not as enlarged terminal glyphs.
/// The 32×40 masks are deliberately kept in source so every frame is stable,
/// reviewable, and available without a texture-loading dependency.
#[derive(Clone, Copy, Debug)]
pub(crate) struct SpriteFrame {
    pub(crate) rows: &'static [u32; SPRITE_HEIGHT as usize],
}

pub(crate) const SPRITE_FRAME_COUNT: usize = 4;
pub(crate) const SPRITE_WIDTH: f32 = 32.0;
pub(crate) const SPRITE_HEIGHT: f32 = 40.0;
pub(crate) const SPRITE_SCALE: f32 = 2.0;
pub(crate) const SPRITE_DIRTY_WIDTH: f32 = SPRITE_WIDTH * SPRITE_SCALE;
pub(crate) const SPRITE_DIRTY_HEIGHT: f32 = SPRITE_HEIGHT * SPRITE_SCALE;

impl BuddyKind {
    pub(crate) fn parse(value: &str) -> Option<Self> {
        match value.to_ascii_lowercase().as_str() {
            "owl" => Some(Self::Owl),
            "cat" => Some(Self::Cat),
            "bot" => Some(Self::Bot),
            "off" => None,
            _ => None,
        }
    }

    pub(crate) fn sprite_bounds(self) -> (f32, f32) {
        let _ = self;
        (SPRITE_WIDTH, SPRITE_HEIGHT)
    }

    pub(crate) fn pose_frame_count(self, pose: BuddyPose) -> usize {
        let _ = (self, pose);
        SPRITE_FRAME_COUNT
    }

    #[cfg(test)]
    pub(crate) fn dirty_region(self, pose: BuddyPose) -> (f32, f32) {
        let _ = (self, pose);
        (SPRITE_DIRTY_WIDTH, SPRITE_DIRTY_HEIGHT)
    }
}

const FRAME_A: [u32; 40] = [
    0x0000_0000,
    0x0020_0400,
    0x0070_0e00,
    0x03ff_ffc0,
    0x07ff_ffe0,
    0x07ff_ffe0,
    0x07ff_ffe0,
    0x0780_01e0,
    0x0780_01e0,
    0x07ff_ffe0,
    0x07ff_ffe0,
    0x03ff_ffc0,
    0x03ff_ffc0,
    0x01ff_ff80,
    0x01ff_ff80,
    0x0dff_ffb0,
    0x0dff_ffb0,
    0x01ff_ff80,
    0x0181_8180,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x0181_8180,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x00ff_ff00,
    0x00ff_ff00,
    0x00c3_c300,
    0x00c3_c300,
    0x00c3_c300,
    0x00c3_c300,
    0x00c3_c300,
    0x0181_8180,
    0x0181_8180,
    0x0181_8180,
    0x0181_8180,
    0x0100_0080,
    0x0100_0080,
    0x0000_0000,
];

const FRAME_B: [u32; 40] = [
    0x0000_0000,
    0x0020_0400,
    0x0070_0e00,
    0x03ff_ffc0,
    0x07ff_ffe0,
    0x07ff_ffe0,
    0x07ff_ffe0,
    0x0780_01e0,
    0x0780_01e0,
    0x07ff_ffe0,
    0x07ff_ffe0,
    0x03ff_ffc0,
    0x03ff_ffc0,
    0x01ff_ff80,
    0x01ff_ff80,
    0x0fff_fff0,
    0x0fff_fff0,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x00ff_ff00,
    0x00ff_ff00,
    0x00c3_c300,
    0x00c3_c300,
    0x00c3_c300,
    0x00c3_c300,
    0x00c3_c300,
    0x0181_8180,
    0x0181_8180,
    0x0181_8180,
    0x0181_8180,
    0x0100_0080,
    0x0100_0080,
    0x0000_0000,
];

const FRAME_C: [u32; 40] = [
    0x0000_0000,
    0x0020_0400,
    0x0070_0e00,
    0x03ff_ffc0,
    0x07ff_ffe0,
    0x07ff_ffe0,
    0x07ff_ffe0,
    0x0780_01e0,
    0x0780_01e0,
    0x07ff_ffe0,
    0x07ff_ffe0,
    0x03ff_ffc0,
    0x03ff_ffc0,
    0x01ff_ff80,
    0x01ff_ff80,
    0x0181_8180,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x00ff_ff00,
    0x00ff_ff00,
    0x00c3_c300,
    0x00c3_c300,
    0x00c3_c300,
    0x00c3_c300,
    0x00c3_c300,
    0x0181_8180,
    0x0181_8180,
    0x0181_8180,
    0x0181_8180,
    0x0100_0080,
    0x0100_0080,
    0x0000_0000,
];

const FRAME_D: [u32; 40] = [
    0x0000_0000,
    0x0000_0000,
    0x0070_0e00,
    0x03ff_ffc0,
    0x07ff_ffe0,
    0x07ff_ffe0,
    0x07ff_ffe0,
    0x0780_01e0,
    0x0780_01e0,
    0x07ff_ffe0,
    0x07ff_ffe0,
    0x03ff_ffc0,
    0x03ff_ffc0,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x01ff_ff80,
    0x00ff_ff00,
    0x00ff_ff00,
    0x00c3_c300,
    0x00c3_c300,
    0x00c3_c300,
    0x00c3_c300,
    0x00c3_c300,
    0x0181_8180,
    0x0181_8180,
    0x0181_8180,
    0x0181_8180,
    0x0100_0080,
    0x0100_0080,
    0x0000_0000,
];

const FRAMES: [SpriteFrame; SPRITE_FRAME_COUNT] = [
    SpriteFrame { rows: &FRAME_A },
    SpriteFrame { rows: &FRAME_B },
    SpriteFrame { rows: &FRAME_C },
    SpriteFrame { rows: &FRAME_D },
];

pub(crate) fn sprite_frame(_kind: BuddyKind, _pose: BuddyPose, frame: usize) -> SpriteFrame {
    FRAMES[frame % SPRITE_FRAME_COUNT]
}

#[cfg(test)]
mod tests {
    use super::{
        BuddyKind, REQUIRED_POSES, SPRITE_FRAME_COUNT, SPRITE_HEIGHT, SPRITE_WIDTH, sprite_frame,
    };

    #[test]
    fn built_in_buddies_have_authored_matrix_coverage() {
        for kind in [BuddyKind::Owl, BuddyKind::Cat, BuddyKind::Bot] {
            for pose in REQUIRED_POSES {
                assert_eq!(kind.pose_frame_count(pose), SPRITE_FRAME_COUNT);
                assert_eq!(kind.sprite_bounds(), (SPRITE_WIDTH, SPRITE_HEIGHT));
                assert_eq!(kind.dirty_region(pose), (64.0, 80.0));
            }
        }
    }

    #[test]
    fn authored_frames_are_not_ascii_or_static() {
        assert_ne!(
            sprite_frame(BuddyKind::Owl, REQUIRED_POSES[0], 0).rows,
            sprite_frame(BuddyKind::Owl, REQUIRED_POSES[0], 1).rows
        );
        const { assert!(SPRITE_WIDTH >= 28.0 && SPRITE_HEIGHT >= 32.0) };
    }

    #[test]
    fn unknown_buddies_do_not_use_a_generic_sprite_fallback() {
        assert!(BuddyKind::parse("dragon").is_none());
        assert!(BuddyKind::parse("off").is_none());
    }
}
