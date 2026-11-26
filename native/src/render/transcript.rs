use super::primitives::draw_rect;
use super::text::{FontRole, TextRenderer};
use crate::Projection;
use crate::platform::*;
use crate::x11::*;
use athena_terminal::CellMetrics;

const USER_LABEL: (u8, u8, u8) = (112, 174, 198);
const USER_BODY: (u8, u8, u8) = (174, 195, 218);
const ATHENA_LABEL: (u8, u8, u8) = (208, 228, 244);
const ATHENA_BODY: (u8, u8, u8) = (188, 211, 232);
const ATHENA_RAIL: (f32, f32, f32) = (0.30, 0.56, 0.67);

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct TranscriptTurn {
    pub label: &'static str,
    pub label_color: (u8, u8, u8),
    pub body_color: (u8, u8, u8),
    pub has_rail: bool,
}

pub(crate) fn transcript_turn(role: &str) -> Option<TranscriptTurn> {
    match role.to_ascii_lowercase().as_str() {
        "user" => Some(TranscriptTurn {
            label: "YOU",
            label_color: USER_LABEL,
            body_color: USER_BODY,
            has_rail: false,
        }),
        "assistant" => Some(TranscriptTurn {
            label: "ATHENA",
            label_color: ATHENA_LABEL,
            body_color: ATHENA_BODY,
            has_rail: true,
        }),
        _ => None,
    }
}

pub(crate) fn wrap_transcript_text(value: &str, width: f32, metrics: CellMetrics) -> Vec<String> {
    let max_columns = (((width * 0.90) / metrics.width.max(1.0)).floor() as usize).max(8);
    let mut lines = Vec::new();
    for paragraph in value.split('\n') {
        let mut line = String::new();
        for word in paragraph.split_whitespace() {
            if !line.is_empty() && line.chars().count() + 1 + word.chars().count() > max_columns {
                lines.push(std::mem::take(&mut line));
            }
            if !line.is_empty() {
                line.push(' ');
            }
            line.push_str(word);
        }
        lines.push(line);
    }
    lines
}

pub(crate) fn draw_transcript(
    text: &TextRenderer,
    projection: &Projection,
    viewport: PixelRect,
) -> ((u8, u8, u8), i32) {
    let body_metrics = text.metrics_for(FontRole::Body);
    let heading_metrics = text.metrics_for(FontRole::Heading);
    let left = viewport.x + 20.0;
    let content_width = viewport.width - 40.0;
    // Render chronologically. Compute the bounded tail first so the newest
    // conversation remains visible while earlier turns scroll upward.
    let mut total_height = 6.0;
    for message in projection.conversation.iter() {
        let Some(_turn) = transcript_turn(&message.role) else {
            continue;
        };
        total_height += heading_metrics.height + 10.0;
        total_height += wrap_transcript_text(&message.text, content_width, body_metrics).len()
            as f32
            * (body_metrics.height + 3.0)
            + 14.0;
    }
    let mut y = if total_height > viewport.height {
        viewport.bottom() - total_height
    } else {
        viewport.y + 6.0
    };
    let mut color_seen: Option<(u8, u8, u8)> = None;
    let mut rail_seen = false;

    for message in projection.conversation.iter() {
        let Some(turn) = transcript_turn(&message.role) else {
            continue;
        };
        if y + heading_metrics.height > viewport.bottom() {
            break;
        }
        text.draw_in(
            FontRole::Heading,
            left as c_int,
            (y + heading_metrics.ascent) as c_int,
            turn.label,
            turn.label_color,
        );
        color_seen = Some(color_seen.map_or(turn.label_color, |seen| {
            if seen == turn.label_color {
                seen
            } else {
                (0, 0, 0)
            }
        }));
        y += heading_metrics.height + 10.0;

        let body_left = if turn.has_rail { left + 10.0 } else { left };
        let width = if turn.has_rail {
            content_width - 10.0
        } else {
            content_width
        };
        if turn.has_rail {
            rail_seen = true;
            draw_rect(left + 2.0, y - 2.0, 2.0, 1.0, ATHENA_RAIL);
        }

        for line in wrap_transcript_text(&message.text, width, body_metrics) {
            if y + body_metrics.height > viewport.bottom() {
                return (color_seen.unwrap(), rail_seen as i32);
            }
            if turn.has_rail {
                draw_rect(
                    left + 2.0,
                    y + body_metrics.ascent - 3.0,
                    2.0,
                    2.0,
                    ATHENA_RAIL,
                );
            }
            text.draw_in(
                FontRole::Body,
                body_left as c_int,
                (y + body_metrics.ascent) as c_int,
                &line,
                turn.body_color,
            );
            y += body_metrics.height + 3.0;
        }
        y += 14.0;
    }
    (color_seen.unwrap_or((0, 0, 0)), rail_seen as i32)
}

#[cfg(test)]
mod tests {
    use super::{ATHENA_LABEL, Projection, USER_LABEL, transcript_turn, wrap_transcript_text};
    use crate::ProjectionMessage;
    use athena_terminal::CellMetrics;

    #[test]
    fn user_and_assistant_resolve_to_distinct_semantics() {
        let user = transcript_turn("user").expect("user turn");
        let assistant = transcript_turn("assistant").expect("assistant turn");
        assert_eq!(user.label, "YOU");
        assert_eq!(assistant.label, "ATHENA");
        assert_ne!(user.label_color, assistant.label_color);
        assert_eq!(user.label_color, USER_LABEL);
        assert_eq!(assistant.label_color, ATHENA_LABEL);
        assert!(!user.has_rail);
        assert!(assistant.has_rail);
    }

    #[test]
    fn conversation_wraps_inside_logical_width() {
        let metrics = CellMetrics::new(10.0, 24.0, 18.0, 6.0);
        let lines = wrap_transcript_text("alpha beta gamma", 100.0, metrics);
        assert!(lines.len() > 1);
        assert!(lines.iter().all(|line| line.chars().count() <= 9));
    }

    #[test]
    fn structured_projection_is_semantically_typed() {
        let projection = Projection {
            conversation: vec![
                ProjectionMessage {
                    id: 1,
                    role: "user".into(),
                    text: "hello".into(),
                },
                ProjectionMessage {
                    id: 2,
                    role: "assistant".into(),
                    text: "ready".into(),
                },
            ],
            ..Projection::default()
        };
        assert_eq!(projection.conversation[0].id, 1);
        assert_eq!(projection.conversation[1].role, "assistant");
    }
}
