//! Pointer, selection, and window-gesture translation for the X11 runtime.

use std::time::{Duration, Instant};

use super::*;

pub(crate) struct PointerEventContext<'a> {
    pub(crate) display: *mut Display,
    pub(crate) window: Window,
    pub(crate) core: &'a mut NativeTerminalCore,
    pub(crate) projection: &'a mut Projection,
    pub(crate) writer: &'a mut std::fs::File,
    pub(crate) input_buffer: &'a mut InputBuffer,
    pub(crate) selection: &'a mut Selection,
    pub(crate) resize_cursors: &'a mut ResizeCursors,
    pub(crate) width: i32,
    pub(crate) height: i32,
    pub(crate) metrics: UiFontMetrics,
    pub(crate) mapped: bool,
    pub(crate) window_destroyed: bool,
    pub(crate) focused: &'a mut bool,
    pub(crate) presentation: &'a mut PresentationSettings,
    pub(crate) window_move_strategy: WindowMoveStrategy,
    pub(crate) window_move_telemetry: &'a mut WindowMoveTelemetry,
    pub(crate) window_session: &'a mut WindowSession,
    pub(crate) configure_events: u64,
    pub(crate) dirty: &'a mut bool,
    pub(crate) activity_dirty: &'a mut bool,
}

pub(crate) fn handle_button_press(event: &XEvent, context: PointerEventContext<'_>) {
    let button = unsafe { &*(event as *const XEvent).cast::<XButtonEvent>() };
    if button.button == 4 {
        let geometry = FrameGeometry::for_window(context.width, context.height, context.metrics);
        let over_encoder = geometry.rail.primary_encoder.contains(button.x, button.y);
        let over_attention = crate::render::oi::attention_page_contains(
            context.projection,
            button.x as f32,
            button.y as f32,
            geometry.oi_inner,
        );
        let changed = if over_encoder && context.projection.attention_items.len() > 1 {
            crate::render::oi::cycle_attention_page(context.projection, -1)
        } else if over_encoder || geometry.oi_inner.contains(button.x, button.y) {
            context.projection.cycle_oi_history(-1)
        } else if over_attention {
            crate::render::oi::cycle_attention_page(context.projection, -1)
        } else {
            false
        };
        if !changed {
            context.core.scroll_display(Scroll::Delta(3));
        }
        mark_dirty(context.dirty, context.activity_dirty);
    } else if button.button == 5 {
        let geometry = FrameGeometry::for_window(context.width, context.height, context.metrics);
        let over_encoder = geometry.rail.primary_encoder.contains(button.x, button.y);
        let over_attention = crate::render::oi::attention_page_contains(
            context.projection,
            button.x as f32,
            button.y as f32,
            geometry.oi_inner,
        );
        let changed = if over_encoder && context.projection.attention_items.len() > 1 {
            crate::render::oi::cycle_attention_page(context.projection, 1)
        } else if over_encoder || geometry.oi_inner.contains(button.x, button.y) {
            context.projection.cycle_oi_history(1)
        } else if over_attention {
            crate::render::oi::cycle_attention_page(context.projection, 1)
        } else {
            false
        };
        if !changed {
            context.core.scroll_display(Scroll::Delta(-3));
        }
        mark_dirty(context.dirty, context.activity_dirty);
    } else if button.button == 1 {
        if context.mapped && !context.window_destroyed {
            set_input_focus_if_mapped(context.display, context.window, context.mapped);
        }
        *context.focused = true;
        if let Some(zone) = resize_zone(button.x, button.y, context.width, context.height) {
            *context.selection = None;
            context.resize_cursors.set(context.window, Some(zone));
            let drag = WindowDrag::new(
                context.display,
                context.window,
                button,
                context.width,
                context.height,
                WindowDragKind::Resize(zone),
            );
            if context.window_move_strategy.uses_ewmh() {
                context.window_move_telemetry.begin_ewmh();
                begin_window_resize(context.display, context.window, button, zone);
                context.window_session.pending_ewmh_gesture =
                    Some(PendingEwmhGesture::new(drag, context.configure_events));
            } else {
                context.window_session.window_drag = Some(drag);
                grab_window_pointer(context.display, context.window);
            }
        } else {
            let geometry =
                FrameGeometry::for_window(context.width, context.height, context.metrics);
            let attention_action = crate::render::oi::attention_hit_map(context.projection)
                .hit_physical(button.x as f32, button.y as f32, geometry.oi_inner)
                .cloned();
            if let Some(action) = attention_action {
                if write_attention_action(context.writer, &action) {
                    mark_dirty(context.dirty, context.activity_dirty);
                }
            } else if geometry.header.contains(button.x, button.y) {
                let drag = WindowDrag::new(
                    context.display,
                    context.window,
                    button,
                    context.width,
                    context.height,
                    WindowDragKind::Move,
                );
                if context.window_move_strategy.uses_ewmh() {
                    context.window_move_telemetry.begin_ewmh();
                    begin_window_move(context.display, context.window, button);
                    context.window_session.pending_ewmh_gesture =
                        Some(PendingEwmhGesture::new(drag, context.configure_events));
                } else {
                    context.window_session.window_drag = Some(drag);
                    grab_window_pointer(context.display, context.window);
                }
            } else if geometry.rail.primary_encoder.contains(button.x, button.y) {
                context.projection.return_to_live_oi();
                mark_dirty(context.dirty, context.activity_dirty);
            } else if let Some(control) =
                PresentationSettings::control_at(&geometry, button.x, button.y)
            {
                context.presentation.activate(control, button.x, &geometry);
                mark_dirty(context.dirty, context.activity_dirty);
            } else if geometry.prompt.contains(button.x, button.y) {
                let offset = ((button.x as f32 - geometry.prompt.x - geometry.prompt_padding_x)
                    / geometry.prompt_cell_width)
                    .max(0.0) as usize;
                context
                    .input_buffer
                    .set_cursor_chars(offset, button.state & SHIFT_MASK != 0);
                mark_dirty(context.dirty, context.activity_dirty);
            } else if let Some(cell) = geometry.cell_at(button.x, button.y) {
                *context.selection = Some((cell, cell));
                mark_dirty(context.dirty, context.activity_dirty);
            }
        }
    }
}

pub(crate) fn handle_motion(event: &XEvent, context: PointerEventContext<'_>) -> bool {
    let motion = unsafe { &*(event as *const XEvent).cast::<XMotionEvent>() };
    let zone = resize_zone(motion.x, motion.y, context.width, context.height);
    if motion.state & BUTTON1_MASK == 0 {
        context.resize_cursors.set(context.window, zone);
    }
    if let Some(pending) = context.window_session.pending_ewmh_gesture {
        if Instant::now() >= pending.deadline {
            context.window_session.pending_ewmh_gesture = None;
            context
                .window_move_telemetry
                .activate_fallback("ewmh_no_configure_before_grace");
            context.window_session.window_drag = Some(pending.drag);
            context.window_session.client_fallback_until =
                Some(Instant::now() + Duration::from_millis(500));
            grab_window_pointer(context.display, context.window);
        }
    }
    if let Some(drag) = context.window_session.window_drag {
        apply_window_drag(
            context.display,
            context.window,
            drag,
            motion.x_root,
            motion.y_root,
        );
        mark_dirty(context.dirty, context.activity_dirty);
        return true;
    }
    if motion.state & BUTTON1_MASK != 0 && zone.is_none() {
        let geometry = FrameGeometry::for_window(context.width, context.height, context.metrics);
        if let Some(control) = PresentationSettings::control_at(&geometry, motion.x, motion.y) {
            if matches!(
                control,
                crate::render::chassis::PresentationControl::Brightness
                    | crate::render::chassis::PresentationControl::Focus
            ) {
                context.presentation.activate(control, motion.x, &geometry);
                mark_dirty(context.dirty, context.activity_dirty);
            }
        } else if let (Some((anchor, _)), Some(cell)) =
            (*context.selection, geometry.cell_at(motion.x, motion.y))
        {
            *context.selection = Some((anchor, cell));
            mark_dirty(context.dirty, context.activity_dirty);
        }
    }
    false
}

pub(crate) fn handle_button_release(event: &XEvent, context: PointerEventContext<'_>) {
    let button = unsafe { &*(event as *const XEvent).cast::<XButtonEvent>() };
    if button.button != 1 {
        return;
    }
    if let Some(pending) = context.window_session.pending_ewmh_gesture.take() {
        context
            .window_move_telemetry
            .activate_fallback("ewmh_no_configure_before_release");
        apply_window_drag(
            context.display,
            context.window,
            pending.drag,
            button.x_root,
            button.y_root,
        );
    }
    let had_client_drag = context.window_session.window_drag.is_some();
    context.window_session.window_drag = None;
    context.window_session.client_fallback_until = None;
    if had_client_drag {
        unsafe { XUngrabPointer(context.display, CURRENT_TIME) };
    }
    if let (Some((anchor, _)), Some(cell)) = (
        *context.selection,
        FrameGeometry::for_window(context.width, context.height, context.metrics)
            .cell_at(button.x, button.y),
    ) {
        *context.selection = Some((anchor, cell));
        mark_dirty(context.dirty, context.activity_dirty);
    }
    context.resize_cursors.set(
        context.window,
        resize_zone(button.x, button.y, context.width, context.height),
    );
}

fn mark_dirty(dirty: &mut bool, activity_dirty: &mut bool) {
    *dirty = true;
    *activity_dirty = true;
}
