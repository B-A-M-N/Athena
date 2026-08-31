mod bot;
mod cat;
mod owl;
mod poses;

pub(crate) use bot::SPRITE_ROWS as BOT_SPRITE_ROWS;
pub(crate) use cat::SPRITE_ROWS as CAT_SPRITE_ROWS;
pub(crate) use owl::SPRITE_ROWS as OWL_SPRITE_ROWS;
pub(crate) use poses::{BuddyPose, REQUIRED_POSES, pose_for_state};

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum BuddyKind {
    Owl,
    Cat,
    Bot,
}

pub(crate) const SPRITE_FRAME_COUNT: usize = 2;
pub(crate) const SPRITE_WIDTH: f32 = 14.0;
pub(crate) const SPRITE_HEIGHT: f32 = 11.0;
pub(crate) const SPRITE_SCALE: f32 = 5.2;

// Covers the enlarged body plus every pose marker/effect. All built-in
// sprites use this same compositor footprint, so a mascot choice cannot
// change scene collision, clamping, or dirty-region behavior.
pub(crate) const SPRITE_DIRTY_WIDTH: f32 = 94.0;
pub(crate) const SPRITE_DIRTY_HEIGHT: f32 = 78.0;

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

pub(crate) fn sprite_rows(kind: BuddyKind) -> [&'static str; 11] {
    match kind {
        BuddyKind::Owl => OWL_SPRITE_ROWS,
        BuddyKind::Cat => CAT_SPRITE_ROWS,
        BuddyKind::Bot => BOT_SPRITE_ROWS,
    }
}

/// Return an authored low-resolution pose frame. The base silhouettes stay
/// character-specific, while pose rows change the eyes, arms, and stance so
/// the two frame sets communicate state without requiring a texture atlas.
pub(crate) fn sprite_frame(kind: BuddyKind, pose: BuddyPose, frame: usize) -> Vec<Vec<char>> {
    let mut rows: Vec<Vec<char>> = sprite_rows(kind)
        .into_iter()
        .map(|row| row.chars().collect())
        .collect();
    let frame = frame % SPRITE_FRAME_COUNT;
    let set = |rows: &mut [Vec<char>], row: usize, column: usize, value: char| {
        if let Some(cell) = rows.get_mut(row).and_then(|line| line.get_mut(column)) {
            *cell = value;
        }
    };
    match pose {
        BuddyPose::Idle => {
            set(&mut rows, 2, 5, if frame == 0 { 'o' } else { '-' });
            set(&mut rows, 2, 8, if frame == 0 { 'o' } else { '-' });
        }
        BuddyPose::Listening | BuddyPose::Inspecting => {
            set(&mut rows, 6, 3, if frame == 0 { '/' } else { '\\' });
            set(&mut rows, 6, 10, if frame == 0 { '\\' } else { '/' });
        }
        BuddyPose::Thinking => {
            set(&mut rows, 3, 6, if frame == 0 { '^' } else { '-' });
            set(&mut rows, 4, 5, if frame == 0 { '-' } else { '^' });
        }
        BuddyPose::Searching | BuddyPose::Reading => {
            set(&mut rows, 7, 2, if frame == 0 { '/' } else { '-' });
            set(&mut rows, 7, 11, if frame == 0 { '\\' } else { '-' });
        }
        BuddyPose::Coding | BuddyPose::Executing => {
            set(&mut rows, 8, 4, if frame == 0 { '#' } else { '/' });
            set(&mut rows, 8, 9, if frame == 0 { '#' } else { '\\' });
        }
        BuddyPose::Testing | BuddyPose::Verifying => {
            set(&mut rows, 3, 5, if frame == 0 { 'o' } else { '^' });
            set(&mut rows, 3, 8, if frame == 0 { 'o' } else { '^' });
        }
        BuddyPose::Approval | BuddyPose::Success => {
            set(&mut rows, 4, 6, if frame == 0 { '^' } else { 'o' });
            set(&mut rows, 4, 7, if frame == 0 { '^' } else { 'o' });
        }
        BuddyPose::Failure => {
            set(&mut rows, 2, 5, 'x');
            set(&mut rows, 2, 8, 'x');
            set(&mut rows, 7, 3, if frame == 0 { '/' } else { '\\' });
            set(&mut rows, 7, 10, if frame == 0 { '\\' } else { '/' });
        }
        BuddyPose::Recovering => {
            set(&mut rows, 6, 3, if frame == 0 { '<' } else { '/' });
            set(&mut rows, 6, 10, if frame == 0 { '>' } else { '\\' });
        }
    }
    rows
}
