//! Presentation-only projection decisions.

use crate::{Projection, VisualMode};

pub(crate) fn projection_is_animated(projection: &Projection) -> bool {
    VisualMode::from_projection(projection).is_animated(projection)
}
