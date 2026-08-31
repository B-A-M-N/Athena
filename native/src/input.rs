use std::collections::VecDeque;

/// Small line editor owned by the native prompt.
///
/// The PTY receives only completed lines (plus explicit interrupt/escape
/// bytes). This keeps the visual prompt and the service's stdin contract from
/// competing over where an editable line is displayed.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct InputBuffer {
    text: String,
    cursor: usize,
    history: VecDeque<String>,
    history_index: Option<usize>,
    selection_anchor: Option<usize>,
    composition: String,
}

impl InputBuffer {
    pub fn text(&self) -> &str {
        &self.text
    }

    pub fn cursor(&self) -> usize {
        self.cursor
    }

    pub fn composition(&self) -> &str {
        &self.composition
    }

    pub fn selected_text(&self) -> Option<&str> {
        let anchor = self.selection_anchor?;
        let (start, end) = ordered_bounds(anchor, self.cursor);
        Some(&self.text[start..end])
    }

    pub fn insert(&mut self, value: &str) -> bool {
        if value.is_empty() {
            return false;
        }
        self.delete_selection();
        self.text.insert_str(self.cursor, value);
        self.cursor += value.len();
        self.history_index = None;
        true
    }

    pub fn backspace(&mut self) -> bool {
        if self.delete_selection() {
            return true;
        }
        let Some(start) = previous_boundary(&self.text, self.cursor) else {
            return false;
        };
        self.text.replace_range(start..self.cursor, "");
        self.cursor = start;
        self.history_index = None;
        true
    }

    pub fn delete(&mut self) -> bool {
        if self.delete_selection() {
            return true;
        }
        let Some(end) = next_boundary(&self.text, self.cursor) else {
            return false;
        };
        self.text.replace_range(self.cursor..end, "");
        self.history_index = None;
        true
    }

    pub fn move_left(&mut self, selecting: bool) -> bool {
        if !selecting && self.selection_anchor.is_some() {
            self.cursor = ordered_bounds(self.selection_anchor.unwrap(), self.cursor).0;
            self.selection_anchor = None;
            return true;
        }
        if selecting && self.selection_anchor.is_none() {
            self.selection_anchor = Some(self.cursor);
        }
        let Some(previous) = previous_boundary(&self.text, self.cursor) else {
            return selecting && self.selection_anchor.take().is_some();
        };
        self.cursor = previous;
        true
    }

    pub fn move_right(&mut self, selecting: bool) -> bool {
        if !selecting && self.selection_anchor.is_some() {
            self.cursor = ordered_bounds(self.selection_anchor.unwrap(), self.cursor).1;
            self.selection_anchor = None;
            return true;
        }
        if selecting && self.selection_anchor.is_none() {
            self.selection_anchor = Some(self.cursor);
        }
        let Some(next) = next_boundary(&self.text, self.cursor) else {
            return selecting && self.selection_anchor.take().is_some();
        };
        self.cursor = next;
        true
    }

    pub fn home(&mut self, selecting: bool) -> bool {
        if selecting && self.selection_anchor.is_none() {
            self.selection_anchor = Some(self.cursor);
        } else if !selecting {
            self.selection_anchor = None;
        }
        let changed = self.cursor != 0;
        self.cursor = 0;
        changed || selecting
    }

    pub fn end(&mut self, selecting: bool) -> bool {
        if selecting && self.selection_anchor.is_none() {
            self.selection_anchor = Some(self.cursor);
        } else if !selecting {
            self.selection_anchor = None;
        }
        let end = self.text.len();
        let changed = self.cursor != end;
        self.cursor = end;
        changed || selecting
    }

    pub fn history_up(&mut self) -> bool {
        if self.history.is_empty() {
            return false;
        }
        let index = self
            .history_index
            .unwrap_or(self.history.len())
            .saturating_sub(1);
        self.history_index = Some(index);
        self.text = self.history[index].clone();
        self.cursor = self.text.len();
        self.selection_anchor = None;
        true
    }

    pub fn history_down(&mut self) -> bool {
        let Some(index) = self.history_index else {
            return false;
        };
        if index + 1 >= self.history.len() {
            self.history_index = None;
            self.text.clear();
        } else {
            self.history_index = Some(index + 1);
            self.text = self.history[index + 1].clone();
        }
        self.cursor = self.text.len();
        self.selection_anchor = None;
        true
    }

    pub fn move_word_left(&mut self) -> bool {
        if self.delete_selection() {
            return true;
        }
        let mut cursor = self.cursor;
        while let Some(start) = previous_boundary(&self.text, cursor) {
            if !self.text[start..cursor].chars().all(char::is_whitespace) {
                break;
            }
            cursor = start;
        }
        while let Some(start) = previous_boundary(&self.text, cursor) {
            if self.text[start..cursor].chars().any(char::is_whitespace) {
                break;
            }
            cursor = start;
        }
        let changed = cursor != self.cursor;
        self.text.replace_range(cursor..self.cursor, "");
        self.cursor = cursor;
        self.history_index = None;
        changed
    }

    pub fn clear_to_start(&mut self) -> bool {
        if self.delete_selection() {
            return true;
        }
        if self.cursor == 0 {
            return false;
        }
        self.text.replace_range(0..self.cursor, "");
        self.cursor = 0;
        self.history_index = None;
        true
    }

    pub fn clear_to_end(&mut self) -> bool {
        if self.delete_selection() {
            return true;
        }
        if self.cursor == self.text.len() {
            return false;
        }
        self.text.truncate(self.cursor);
        self.history_index = None;
        true
    }

    pub fn select_all(&mut self) -> bool {
        if self.text.is_empty() {
            return false;
        }
        self.selection_anchor = Some(0);
        self.cursor = self.text.len();
        true
    }

    pub fn set_cursor(&mut self, byte_offset: usize, selecting: bool) -> bool {
        let cursor = clamp_boundary(&self.text, byte_offset);
        if selecting && self.selection_anchor.is_none() {
            self.selection_anchor = Some(self.cursor);
        } else if !selecting {
            self.selection_anchor = None;
        }
        let changed = cursor != self.cursor;
        self.cursor = cursor;
        changed || selecting
    }

    pub fn set_cursor_chars(&mut self, character_offset: usize, selecting: bool) -> bool {
        let byte_offset = self
            .text
            .char_indices()
            .nth(character_offset)
            .map_or(self.text.len(), |(offset, _)| offset);
        self.set_cursor(byte_offset, selecting)
    }

    pub fn take_line(&mut self) -> String {
        let line = std::mem::take(&mut self.text);
        if !line.is_empty() {
            if self.history.back() != Some(&line) {
                self.history.push_back(line.clone());
            }
            while self.history.len() > 100 {
                self.history.pop_front();
            }
        }
        self.cursor = 0;
        self.history_index = None;
        self.selection_anchor = None;
        self.composition.clear();
        line
    }

    pub fn clear(&mut self) -> bool {
        let changed = !self.text.is_empty() || self.cursor != 0 || self.selection_anchor.is_some();
        self.text.clear();
        self.cursor = 0;
        self.history_index = None;
        self.selection_anchor = None;
        self.composition.clear();
        changed
    }

    fn delete_selection(&mut self) -> bool {
        let Some(anchor) = self.selection_anchor.take() else {
            return false;
        };
        let (start, end) = ordered_bounds(anchor, self.cursor);
        if start != end {
            self.text.replace_range(start..end, "");
            self.cursor = start;
            self.history_index = None;
        }
        true
    }
}

fn ordered_bounds(anchor: usize, cursor: usize) -> (usize, usize) {
    if anchor <= cursor {
        (anchor, cursor)
    } else {
        (cursor, anchor)
    }
}

fn clamp_boundary(text: &str, offset: usize) -> usize {
    let offset = offset.min(text.len());
    if text.is_char_boundary(offset) {
        return offset;
    }
    let mut boundary = offset;
    while boundary > 0 && !text.is_char_boundary(boundary) {
        boundary -= 1;
    }
    boundary
}

fn previous_boundary(text: &str, offset: usize) -> Option<usize> {
    let offset = clamp_boundary(text, offset);
    if offset == 0 {
        None
    } else {
        Some(
            text[..offset]
                .char_indices()
                .next_back()
                .map_or(0, |(index, _)| index),
        )
    }
}

fn next_boundary(text: &str, offset: usize) -> Option<usize> {
    let offset = clamp_boundary(text, offset);
    text[offset..]
        .chars()
        .next()
        .map(|character| offset + character.len_utf8())
}

#[cfg(test)]
mod tests {
    use super::InputBuffer;

    #[test]
    fn edits_middle_of_unicode_line() {
        let mut input = InputBuffer::default();
        input.insert("ab🙂c");
        input.move_left(false);
        input.insert("X");
        assert_eq!(input.text(), "ab🙂Xc");
        input.backspace();
        assert_eq!(input.text(), "ab🙂c");
    }

    #[test]
    fn history_round_trips_and_deduplicates() {
        let mut input = InputBuffer::default();
        input.insert("first");
        assert_eq!(input.take_line(), "first");
        input.insert("second");
        assert_eq!(input.take_line(), "second");
        assert!(input.history_up());
        assert_eq!(input.text(), "second");
        assert!(input.history_up());
        assert_eq!(input.text(), "first");
        assert!(input.history_down());
        assert_eq!(input.text(), "second");
        assert!(input.history_down());
        assert_eq!(input.text(), "");
    }

    #[test]
    fn selection_replaces_and_ctrl_operations_are_safe() {
        let mut input = InputBuffer::default();
        input.insert("hello world");
        input.home(false);
        input.move_right(true);
        input.move_right(true);
        assert_eq!(input.selected_text(), Some("he"));
        input.insert("yo");
        assert_eq!(input.text(), "yollo world");
        input.end(false);
        input.move_word_left();
        assert_eq!(input.text(), "yollo ");
    }
}
