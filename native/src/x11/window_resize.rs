//! X11 configure-notify handling and resize-owned resource updates.

use super::*;

pub(crate) struct WindowResizeContext<'a> {
    pub(crate) display: *mut Display,
    pub(crate) screen: c_int,
    pub(crate) gl_context: GLXContext,
    pub(crate) setup: &'a mut WindowSetup,
    pub(crate) core: &'a mut NativeTerminalCore,
    pub(crate) pty: &'a mut tty::Pty,
    pub(crate) text: &'a mut TextRenderer,
    pub(crate) width: &'a mut i32,
    pub(crate) height: &'a mut i32,
    pub(crate) user_text_zoom: f32,
    pub(crate) metrics: &'a mut UiFontMetrics,
    pub(crate) configure_events: &'a mut u64,
    pub(crate) window_session: &'a mut WindowSession,
    pub(crate) window_move_telemetry: &'a mut WindowMoveTelemetry,
    pub(crate) window_move_strategy: WindowMoveStrategy,
    pub(crate) dirty: &'a mut bool,
    pub(crate) activity_dirty: &'a mut bool,
}

pub(crate) fn handle_configure_notify(
    event: &XEvent,
    context: WindowResizeContext<'_>,
) -> Result<(), String> {
    let configure = unsafe { &*(event as *const XEvent as *const XConfigureEvent) };
    *context.width = configure.width.max(1);
    *context.height = configure.height.max(1);
    unsafe { glXMakeCurrent(context.display, 0, ptr::null_mut()) };
    context
        .setup
        .presentation_surface
        .resize(*context.width as CUint, *context.height as CUint)?;
    context.setup.presentation_surface.reap_retired();
    if unsafe {
        glXMakeCurrent(
            context.display,
            context.setup.presentation_surface.glx_pixmap(),
            context.gl_context,
        )
    } == 0
    {
        return Err("could not bind the resized presentation surface".to_owned());
    }

    let text_scale = effective_text_scale(*context.width, *context.height, context.user_text_zoom);
    match context.text.reconfigure_for_scale(text_scale) {
        Ok(_) => {
            *context.metrics = UiFontMetrics {
                body: context.text.metrics_for(FontRole::Body),
                input: context.text.metrics_for(FontRole::Input),
                heading: context.text.metrics_for(FontRole::Heading),
                instrument: context.text.metrics_for(FontRole::Instrument),
            };
        }
        Err(error) => {
            eprintln!("could not reconfigure native fonts: {error}");
        }
    }
    resize_terminal(
        context.core,
        context.pty,
        *context.width,
        *context.height,
        *context.metrics,
    );
    *context.configure_events = context.configure_events.saturating_add(1);
    if let Some(pending) = context.window_session.pending_ewmh_gesture {
        if *context.configure_events > pending.configure_events_at_start
            && pending.geometry_changed(configure.x, configure.y, *context.width, *context.height)
        {
            context.window_session.pending_ewmh_gesture = None;
            context.window_move_telemetry.confirm_ewmh();
        }
    }
    write_runtime_layout_dump(
        context.display,
        context.screen,
        *context.width,
        *context.height,
        *context.metrics,
        context.text.font_pixel_sizes(),
        text_scale,
        *context.configure_events,
        context.window_move_strategy,
        *context.window_move_telemetry,
    );
    *context.dirty = true;
    *context.activity_dirty = true;
    Ok(())
}
