//! Keyboard and text-input translation for the X11 window runtime.

use std::io::Write;

use super::*;

pub(crate) struct InputEventContext<'a> {
    pub(crate) display: *mut Display,
    pub(crate) window: Window,
    pub(crate) screen: c_int,
    pub(crate) core: &'a mut NativeTerminalCore,
    pub(crate) pty: &'a mut tty::Pty,
    pub(crate) writer: &'a mut dyn Write,
    pub(crate) input_method: &'a mut Option<InputMethod>,
    pub(crate) clipboard: &'a mut Clipboard,
    pub(crate) input_buffer: &'a mut InputBuffer,
    pub(crate) selection: &'a mut Selection,
    pub(crate) text: &'a mut TextRenderer,
    pub(crate) width: i32,
    pub(crate) height: i32,
    pub(crate) user_text_zoom: &'a mut f32,
    pub(crate) metrics: &'a mut UiFontMetrics,
    pub(crate) configure_events: &'a mut u64,
    pub(crate) window_move_strategy: WindowMoveStrategy,
    pub(crate) window_move_telemetry: WindowMoveTelemetry,
    pub(crate) presentation: &'a mut PresentationSettings,
    pub(crate) dirty: &'a mut bool,
    pub(crate) activity_dirty: &'a mut bool,
}

pub(crate) fn handle_key_press(event: &mut XEvent, context: InputEventContext<'_>) {
    let key_event = unsafe { &mut *(&mut *event as *mut XEvent as *mut XKeyEvent) };
    let lookup = lookup_key(context.input_method.as_mut(), key_event);
    let keysym = lookup.keysym;
    let control = key_event.state & CONTROL_MASK != 0;
    let selecting = key_event.state & SHIFT_MASK != 0;
    let terminal_app = context.core.mode().contains(TermMode::ALT_SCREEN);
    if key_event.state & (CONTROL_MASK | SHIFT_MASK) == (CONTROL_MASK | SHIFT_MASK)
        && matches!(keysym, k if k == 'c' as c_ulong || k == 'C' as c_ulong)
    {
        if let Some(selected) = context.input_buffer.selected_text() {
            context
                .clipboard
                .own(context.display, context.window, selected.to_owned());
        } else if let Some((anchor, extent)) = *context.selection {
            let (start, end) = selection_bounds(anchor, extent);
            let end = (end.0.saturating_add(1), end.1);
            context.clipboard.own(
                context.display,
                context.window,
                context.core.selection_text(start, end),
            );
        }
    } else if key_event.state & (CONTROL_MASK | SHIFT_MASK) == (CONTROL_MASK | SHIFT_MASK)
        && matches!(keysym, k if k == 'v' as c_ulong || k == 'V' as c_ulong)
    {
        context.clipboard.request(context.display, context.window);
    } else if keysym == XK_ESCAPE {
        let _ = context.writer.write_all(&[0x1b]);
        let _ = context.writer.flush();
        mark_dirty(context.dirty, context.activity_dirty);
    } else if control && matches!(keysym, k if k == 'c' as c_ulong || k == 'C' as c_ulong) {
        context.input_buffer.clear();
        let _ = context.writer.write_all(&[0x03]);
        let _ = context.writer.flush();
        mark_dirty(context.dirty, context.activity_dirty);
    } else if control
        && matches!(
            keysym,
            k if k == '+' as c_ulong
                || k == '=' as c_ulong
                || k == '-' as c_ulong
                || k == '_' as c_ulong
                || k == '0' as c_ulong
        )
    {
        *context.user_text_zoom = match keysym {
            k if k == '+' as c_ulong || k == '=' as c_ulong => {
                (*context.user_text_zoom * 1.10).min(2.5)
            }
            k if k == '-' as c_ulong || k == '_' as c_ulong => {
                (*context.user_text_zoom / 1.10).max(0.75)
            }
            _ => crate::DEFAULT_TEXT_SCALE,
        };
        let text_scale =
            effective_text_scale(context.width, context.height, *context.user_text_zoom);
        match context.text.reconfigure_for_scale(text_scale) {
            Ok(_) => {
                *context.metrics = UiFontMetrics {
                    body: context.text.metrics_for(FontRole::Body),
                    input: context.text.metrics_for(FontRole::Input),
                    heading: context.text.metrics_for(FontRole::Heading),
                    instrument: context.text.metrics_for(FontRole::Instrument),
                };
                resize_terminal(
                    context.core,
                    context.pty,
                    context.width,
                    context.height,
                    *context.metrics,
                );
                *context.configure_events = context.configure_events.saturating_add(1);
                write_runtime_layout_dump(
                    context.display,
                    context.screen,
                    context.width,
                    context.height,
                    *context.metrics,
                    context.text.font_pixel_sizes(),
                    text_scale,
                    *context.configure_events,
                    context.window_move_strategy,
                    context.window_move_telemetry,
                );
                mark_dirty(context.dirty, context.activity_dirty);
            }
            Err(error) => eprintln!("could not apply native text zoom: {error}"),
        }
    } else if terminal_app {
        let bytes = terminal_key_bytes(keysym, context.core.mode(), &lookup.bytes);
        if !bytes.is_empty() {
            let _ = context.writer.write_all(&bytes);
            let _ = context.writer.flush();
        }
    } else if matches!(keysym, XK_F1 | XK_F2 | XK_F3 | XK_F4 | XK_F5) {
        let changed = match keysym {
            XK_F1 => {
                context
                    .presentation
                    .adjust(PresentationControl::Brightness, -0.05);
                true
            }
            XK_F2 => {
                context
                    .presentation
                    .adjust(PresentationControl::Brightness, 0.05);
                true
            }
            XK_F3 => {
                context
                    .presentation
                    .adjust(PresentationControl::Focus, -0.05);
                true
            }
            XK_F4 => {
                context
                    .presentation
                    .adjust(PresentationControl::Focus, 0.05);
                true
            }
            XK_F5 => {
                context.presentation.adjust(PresentationControl::Power, 0.0);
                true
            }
            _ => false,
        };
        if changed {
            mark_dirty(context.dirty, context.activity_dirty);
        }
    } else if control {
        let key = (keysym as u8).to_ascii_lowercase();
        let changed = match key {
            b'a' if selecting => context.input_buffer.select_all(),
            b'a' => context.input_buffer.home(false),
            b'e' => context.input_buffer.end(selecting),
            b'u' => context.input_buffer.clear_to_start(),
            b'k' => context.input_buffer.clear_to_end(),
            b'w' => context.input_buffer.move_word_left(),
            _ => false,
        };
        if changed {
            mark_dirty(context.dirty, context.activity_dirty);
        }
    } else {
        let changed = match keysym {
            XK_BACKSPACE => context.input_buffer.backspace(),
            XK_DELETE => context.input_buffer.delete(),
            XK_LEFT => context.input_buffer.move_left(selecting),
            XK_RIGHT => context.input_buffer.move_right(selecting),
            XK_HOME => context.input_buffer.home(selecting),
            XK_END => context.input_buffer.end(selecting),
            XK_UP => context.input_buffer.history_up(),
            XK_DOWN => context.input_buffer.history_down(),
            XK_PAGE_UP if selecting => {
                context.core.scroll_display(Scroll::PageUp);
                true
            }
            XK_PAGE_DOWN if selecting => {
                context.core.scroll_display(Scroll::PageDown);
                true
            }
            XK_RETURN => {
                let mut line = context.input_buffer.take_line();
                line.push('\n');
                let _ = context.writer.write_all(line.as_bytes());
                let _ = context.writer.flush();
                true
            }
            XK_TAB => context.input_buffer.insert("\t"),
            _ => {
                let text = String::from_utf8_lossy(&lookup.bytes);
                context.input_buffer.insert(&text)
            }
        };
        if changed {
            mark_dirty(context.dirty, context.activity_dirty);
        }
    }
}

fn mark_dirty(dirty: &mut bool, activity_dirty: &mut bool) {
    *dirty = true;
    *activity_dirty = true;
}
