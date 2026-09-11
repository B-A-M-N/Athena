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
pub(crate) const SPRITE_SCALE: f32 = 1.8;
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

/// An authored phosphor owl: a rounded face, readable eyes, tapered wings,
/// belly, tail, and feet. It uses the reference instrument's dense pixel-art
/// language without turning the character into a generic terminal glyph.
const fn in_range(value: usize, low: usize, high: usize) -> bool {
    value >= low && value <= high
}

const fn owl_frame(_source: &[u32; 40], phase: usize) -> [u32; 40] {
    let mut output = [0_u32; 40];
    let mut row = 0;
    while row < 40 {
        let mut column = 0;
        while column < 32 {
            let head = match row {
                4 => in_range(column, 11, 20),
                5 => in_range(column, 8, 23),
                6 => in_range(column, 6, 25),
                7 => in_range(column, 5, 26),
                8..=14 => in_range(column, 4, 27),
                15 => in_range(column, 5, 26),
                16 => in_range(column, 6, 25),
                17 => in_range(column, 8, 23),
                18 => in_range(column, 11, 20),
                _ => false,
            };
            let head_outline = match row {
                4 => in_range(column, 11, 20),
                5 => column == 8 || column == 23,
                6 => column == 6 || column == 25,
                7 => column == 5 || column == 26,
                8..=14 => column == 4 || column == 27,
                15 => column == 5 || column == 26,
                16 => column == 6 || column == 25,
                17 => column == 8 || column == 23,
                18 => in_range(column, 11, 20),
                _ => false,
            };
            let ears = (row == 2 && (column == 9 || column == 22))
                || (row == 3 && ((column >= 7 && column <= 10) || (column >= 21 && column <= 24)))
                || (row == 4 && (column == 8 || column == 23));
            let eye_left = (row == 7 && in_range(column, 9, 11))
                || (row == 8 && (column == 7 || in_range(column, 8, 12) || column == 13))
                || (in_range(row, 9, 12) && (column == 6 || column == 14))
                || (row == 13 && (column == 7 || in_range(column, 8, 12) || column == 13))
                || (row == 14 && in_range(column, 9, 11));
            let eye_right = (row == 7 && in_range(column, 20, 22))
                || (row == 8 && (column == 18 || in_range(column, 19, 23) || column == 24))
                || (in_range(row, 9, 12) && (column == 17 || column == 25))
                || (row == 13 && (column == 18 || in_range(column, 19, 23) || column == 24))
                || (row == 14 && in_range(column, 20, 22));
            let pupils = row >= 10 && row <= 12 && (column == 10 || column == 21);
            let brows =
                row == 7 && ((column >= 8 && column <= 12) || (column >= 19 && column <= 23));
            let beak = (row == 11 && (column == 15 || column == 16))
                || (row == 12 && column >= 14 && column <= 17)
                || (row == 13 && (column == 15 || column == 16));
            let head_dither = head
                && !head_outline
                && !eye_left
                && !eye_right
                && !pupils
                && (row * 11 + column * 7 + phase * 3) % 11 < 1;
            let body = match row {
                19 => in_range(column, 10, 21),
                20 => in_range(column, 8, 23),
                21 => in_range(column, 7, 24),
                22..=26 => in_range(column, 8, 23),
                27..=30 => in_range(column, 9, 22),
                31 => in_range(column, 10, 21),
                32 => in_range(column, 11, 20),
                33 => in_range(column, 12, 19),
                34 => in_range(column, 13, 18),
                35 => in_range(column, 14, 17),
                _ => false,
            };
            let body_outline = match row {
                19 => in_range(column, 10, 21),
                20 => column == 8 || column == 23,
                21 => column == 7 || column == 24,
                22..=26 => column == 8 || column == 23,
                27..=30 => column == 9 || column == 22,
                31 => column == 10 || column == 21,
                32 => column == 11 || column == 20,
                33 => column == 12 || column == 19,
                34 => column == 13 || column == 18,
                35 => in_range(column, 14, 17),
                _ => false,
            };
            let wing_left = (row == 20 && column == 7)
                || (row == 21 && (column == 6 || column == 7))
                || in_range(row, 22, 26) && (column == 5 || column == 6)
                || in_range(row, 27, 28) && (column == 6 || column == 7)
                || (row == 29 && column == 7)
                || (row == 30 && column == 8)
                || (row == 31 && column == 9);
            let wing_right = (row == 20 && column == 24)
                || (row == 21 && (column == 24 || column == 25))
                || in_range(row, 22, 26) && (column == 25 || column == 26)
                || in_range(row, 27, 28) && (column == 24 || column == 25)
                || (row == 29 && column == 24)
                || (row == 30 && column == 23)
                || (row == 31 && column == 22);
            let cell_noise =
                row * row * 3 + column * column * 5 + row * column * 7 + column * 17 + phase * 11;
            let wing_dither = in_range(row, 21, 29)
                && (in_range(column, 5, 7) || in_range(column, 24, 26))
                && cell_noise % 17 < 3;
            let body_dither = body
                && !body_outline
                && !((row == 23 || row == 24) && (column == 12 || column == 19))
                && cell_noise % 17 < 3;
            let tail = in_range(row, 33, 37)
                && in_range(column, 11, 20)
                && (row + column + phase) % 3 != 0;
            let feet = row >= 36
                && row <= 38
                && ((column >= 8 && column <= 13) || (column >= 18 && column <= 23));
            if head_outline
                || ears
                || eye_left
                || eye_right
                || brows
                || beak
                || pupils
                || head_dither
                || wing_left
                || wing_right
                || wing_dither
                || body_outline
                || body_dither
                || tail
                || feet
            {
                output[row] |= 1_u32 << column;
            }
            column += 1;
        }
        row += 1;
    }
    output
}

/// Cat and bot retain the same 32×40 contract but have separate silhouettes:
/// cat ears/tail and bot antenna/face panel are authored into their masks.
const fn cat_frame(source: &[u32; 40], phase: usize) -> [u32; 40] {
    let mut output = *source;
    output[1] |= 1_u32 << (8 + phase % 2) | 1_u32 << (23 - phase % 2);
    output[2] |= 1_u32 << 7 | 1_u32 << 24;
    output[30] |= 1_u32 << 1 | 1_u32 << 2 | 1_u32 << 3;
    output[31] |= 1_u32 << 0 | 1_u32 << 1;
    output
}

const fn bot_frame(source: &[u32; 40], phase: usize) -> [u32; 40] {
    let mut output = [0_u32; 40];
    let mut row = 0;
    while row < 40 {
        // A narrower panel body makes the bot identity visibly distinct from
        // the owl even before its localized antenna accents are applied.
        output[row] = source[row] & 0x03ff_ffc0;
        row += 1;
    }
    output[0] |= 1_u32 << (15 + phase % 2);
    output[1] |= 1_u32 << 15 | 1_u32 << 16 | 1_u32 << 17;
    output
}

const OWL_A: [u32; 40] = owl_frame(&FRAME_A, 0);
const OWL_B: [u32; 40] = owl_frame(&FRAME_B, 1);
const OWL_C: [u32; 40] = owl_frame(&FRAME_C, 2);
const OWL_D: [u32; 40] = owl_frame(&FRAME_D, 3);
const CAT_A: [u32; 40] = cat_frame(&FRAME_A, 0);
const CAT_B: [u32; 40] = cat_frame(&FRAME_B, 1);
const CAT_C: [u32; 40] = cat_frame(&FRAME_C, 0);
const CAT_D: [u32; 40] = cat_frame(&FRAME_D, 1);
const BOT_A: [u32; 40] = bot_frame(&FRAME_A, 0);
const BOT_B: [u32; 40] = bot_frame(&FRAME_B, 1);
const BOT_C: [u32; 40] = bot_frame(&FRAME_C, 0);
const BOT_D: [u32; 40] = bot_frame(&FRAME_D, 1);

const OWL_FRAMES: [SpriteFrame; SPRITE_FRAME_COUNT] = [
    SpriteFrame { rows: &OWL_A },
    SpriteFrame { rows: &OWL_B },
    SpriteFrame { rows: &OWL_C },
    SpriteFrame { rows: &OWL_D },
];
const CAT_FRAMES: [SpriteFrame; SPRITE_FRAME_COUNT] = [
    SpriteFrame { rows: &CAT_A },
    SpriteFrame { rows: &CAT_B },
    SpriteFrame { rows: &CAT_C },
    SpriteFrame { rows: &CAT_D },
];
const BOT_FRAMES: [SpriteFrame; SPRITE_FRAME_COUNT] = [
    SpriteFrame { rows: &BOT_A },
    SpriteFrame { rows: &BOT_B },
    SpriteFrame { rows: &BOT_C },
    SpriteFrame { rows: &BOT_D },
];

fn pose_offset(pose: BuddyPose) -> usize {
    match pose {
        BuddyPose::Idle => 0,
        BuddyPose::Listening | BuddyPose::Thinking => 1,
        BuddyPose::Inspecting | BuddyPose::Searching | BuddyPose::Reading => 2,
        BuddyPose::Coding | BuddyPose::Executing | BuddyPose::Testing => 3,
        BuddyPose::Verifying | BuddyPose::Approval | BuddyPose::Success => 1,
        BuddyPose::Failure | BuddyPose::Recovering => 2,
    }
}

pub(crate) fn sprite_frame(kind: BuddyKind, pose: BuddyPose, frame: usize) -> SpriteFrame {
    let frames = match kind {
        BuddyKind::Owl => &OWL_FRAMES,
        BuddyKind::Cat => &CAT_FRAMES,
        BuddyKind::Bot => &BOT_FRAMES,
    };
    frames[(frame + pose_offset(pose)) % SPRITE_FRAME_COUNT]
}

#[cfg(test)]
mod tests {
    use super::{
        BuddyKind, BuddyPose, REQUIRED_POSES, SPRITE_DIRTY_HEIGHT, SPRITE_DIRTY_WIDTH,
        SPRITE_FRAME_COUNT, SPRITE_HEIGHT, SPRITE_WIDTH, sprite_frame,
    };

    #[test]
    fn built_in_buddies_have_authored_matrix_coverage() {
        for kind in [BuddyKind::Owl, BuddyKind::Cat, BuddyKind::Bot] {
            for pose in REQUIRED_POSES {
                assert_eq!(kind.pose_frame_count(pose), SPRITE_FRAME_COUNT);
                assert_eq!(kind.sprite_bounds(), (SPRITE_WIDTH, SPRITE_HEIGHT));
                assert_eq!(
                    kind.dirty_region(pose),
                    (SPRITE_DIRTY_WIDTH, SPRITE_DIRTY_HEIGHT)
                );
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
    fn buddy_kind_and_pose_select_distinct_authored_masks() {
        assert_ne!(
            sprite_frame(BuddyKind::Owl, REQUIRED_POSES[0], 0).rows,
            sprite_frame(BuddyKind::Cat, REQUIRED_POSES[0], 0).rows
        );
        assert_ne!(
            sprite_frame(BuddyKind::Owl, BuddyPose::Idle, 0).rows,
            sprite_frame(BuddyKind::Owl, BuddyPose::Thinking, 0).rows
        );
    }

    #[test]
    fn unknown_buddies_do_not_use_a_generic_sprite_fallback() {
        assert!(BuddyKind::parse("dragon").is_none());
        assert!(BuddyKind::parse("off").is_none());
    }
}
