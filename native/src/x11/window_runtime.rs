//! X11 window lifecycle, event, and frame-loop coordination.
//!
//! The parent module owns shared geometry and platform helpers; this module
//! owns the mutable runtime that binds them into one native window session.

use super::frame_runtime::{FrameDrawContext, FrameRuntime};
use super::window_input::{InputEventContext, handle_key_press};
use super::window_pointer::{
    PointerEventContext, handle_button_press, handle_button_release, handle_motion,
};
use super::window_resize::{WindowResizeContext, handle_configure_notify};
use super::*;

pub(crate) fn run_window(
    display: *mut Display,
    core: &mut NativeTerminalCore,
    pty: &mut tty::Pty,
    output_rx: Receiver<Vec<u8>>,
    bridge_rx: Option<LatestProjection>,
    projection: &mut Projection,
    options: &RendererOptions,
) -> Result<(), String> {
    let mut frame_runtime = FrameRuntime::from_options(options)?;
    let mut setup = WindowSetup::create(display, projection)?;
    let screen = setup.screen;
    let window = setup.window;
    let visual = setup.visual;
    let colormap = setup.colormap;
    let delete_atom = setup.delete_atom;
    let protocols_atom = setup.protocols_atom;
    let initial_width = setup.initial_width;
    let initial_height = setup.initial_height;
    let stencil_available = setup.stencil_available;
    let window_move_strategy = setup.window_move_strategy;
    let context = setup.context;
    let visual_ptr = unsafe { (*visual).visual };
    let mut user_text_zoom = options.text_scale.clamp(0.75, 2.5);
    let text_scale = effective_text_scale(initial_width, initial_height, user_text_zoom);
    let mut resize_cursors = ResizeCursors::new(display);
    let mut text =
        match TextRenderer::new(display, screen, window, visual_ptr, colormap, text_scale) {
            Ok(text) => text,
            Err(error) => {
                drop(resize_cursors);
                setup.destroy(false);
                return Err(error);
            }
        };
    let oi_target = crate::render::oi::OiTarget::new();
    let chassis_material = crate::render::chassis::ChassisMaterial::new();
    let mut input_method = InputMethod::new(display, window);
    let mut writer = pty
        .file()
        .try_clone()
        .map_err(|error| format!("could not clone PTY writer: {error}"))?;
    let mut clipboard = Clipboard::new(display);
    let mut input_buffer = InputBuffer::default();
    let mut presentation = PresentationSettings::default();
    let mut selection: Option<((usize, usize), (usize, usize))> = None;
    let mut width = initial_width;
    let mut height = initial_height;
    let mut metrics = UiFontMetrics {
        body: text.metrics_for(FontRole::Body),
        input: text.metrics_for(FontRole::Input),
        heading: text.metrics_for(FontRole::Heading),
        instrument: text.metrics_for(FontRole::Instrument),
    };
    let mut window_move_telemetry = WindowMoveTelemetry::for_strategy(window_move_strategy);
    resize_terminal(core, pty, width, height, metrics);
    write_runtime_layout_dump(
        display,
        screen,
        width,
        height,
        metrics,
        text.font_pixel_sizes(),
        text_scale,
        0,
        window_move_strategy,
        window_move_telemetry,
    );
    resize_cursors.set(window, None);
    // XSetInputFocus is a BadMatch until the WM has made the mapped window
    // viewable.  Defer the first request to MapNotify and only request focus
    // again after a remap; this also keeps minimize/restore from racing X11.
    let mut mapped = false;
    let mut focus_pending = true;
    let mut focused = true;
    let mut running = true;
    let mut window_destroyed = false;
    let mut child_exited = false;
    let mut window_session = WindowSession::default();
    let mut dirty = true;
    let mut terminal_dirty = false;
    let mut oi_motion_dirty = false;
    let mut activity_dirty = false;
    let mut configure_events = 0_u64;
    let mut steady_started: Option<Instant> = None;
    let mut steady_cpu_started: Option<f64> = None;
    let mut steady_elapsed_seconds = 0.0_f64;
    let mut steady_cpu_seconds = 0.0_f64;
    while running {
        let changes = apply_available(core, &output_rx, bridge_rx.as_ref(), projection);
        if changes.projection {
            dirty = true;
            activity_dirty = true;
        } else if changes.terminal {
            terminal_dirty = true;
            activity_dirty = true;
        }
        while unsafe { XPending(display) } > 0 {
            let mut event = XEvent {
                type_: 0,
                pad: [0; 24],
            };
            unsafe { XNextEvent(display, &mut event) };
            if let Some(bytes) = clipboard.handle_event(display, window, &mut event) {
                if core.mode().contains(TermMode::ALT_SCREEN) {
                    let filtered: Vec<u8> = bytes
                        .into_iter()
                        .filter(|byte| *byte != 0x1b && *byte != 0x03)
                        .collect();
                    let _ = writer.write_all(b"\x1b[200~");
                    let _ = writer.write_all(&filtered);
                    let _ = writer.write_all(b"\x1b[201~");
                    let _ = writer.flush();
                } else {
                    let text = String::from_utf8_lossy(&bytes);
                    if input_buffer.insert(&text) {
                        dirty = true;
                        activity_dirty = true;
                    }
                }
                continue;
            }
            match event.type_ {
                MAP_NOTIFY => {
                    mapped = true;
                    if focus_pending
                        && !window_destroyed
                        && set_input_focus_if_mapped(display, window, mapped)
                    {
                        if let Some(input_method) = input_method.as_ref() {
                            unsafe { XSetICFocus(input_method.ic) };
                        }
                        focus_pending = false;
                        focused = true;
                        dirty = true;
                        activity_dirty = true;
                    }
                }
                UNMAP_NOTIFY => {
                    mapped = false;
                    // A minimized window may later be remapped.  Do not
                    // issue a focus request against its unmapped drawable.
                    focus_pending = true;
                    focused = false;
                    dirty = true;
                    activity_dirty = true;
                }
                KEY_PRESS => {
                    handle_key_press(
                        &mut event,
                        InputEventContext {
                            display,
                            window,
                            screen,
                            core,
                            pty,
                            writer: &mut writer,
                            input_method: &mut input_method,
                            clipboard: &mut clipboard,
                            input_buffer: &mut input_buffer,
                            selection: &mut selection,
                            text: &mut text,
                            width,
                            height,
                            user_text_zoom: &mut user_text_zoom,
                            metrics: &mut metrics,
                            configure_events: &mut configure_events,
                            window_move_strategy,
                            window_move_telemetry,
                            presentation: &mut presentation,
                            dirty: &mut dirty,
                            activity_dirty: &mut activity_dirty,
                        },
                    );
                }
                BUTTON_PRESS => {
                    handle_button_press(
                        &event,
                        PointerEventContext {
                            display,
                            window,
                            core,
                            projection,
                            writer: &mut writer,
                            input_buffer: &mut input_buffer,
                            selection: &mut selection,
                            resize_cursors: &mut resize_cursors,
                            width,
                            height,
                            metrics,
                            mapped,
                            window_destroyed,
                            focused: &mut focused,
                            presentation: &mut presentation,
                            window_move_strategy,
                            window_move_telemetry: &mut window_move_telemetry,
                            window_session: &mut window_session,
                            configure_events,
                            dirty: &mut dirty,
                            activity_dirty: &mut activity_dirty,
                        },
                    );
                }
                MOTION_NOTIFY => {
                    let _ = handle_motion(
                        &event,
                        PointerEventContext {
                            display,
                            window,
                            core,
                            projection,
                            writer: &mut writer,
                            input_buffer: &mut input_buffer,
                            selection: &mut selection,
                            resize_cursors: &mut resize_cursors,
                            width,
                            height,
                            metrics,
                            mapped,
                            window_destroyed,
                            focused: &mut focused,
                            presentation: &mut presentation,
                            window_move_strategy,
                            window_move_telemetry: &mut window_move_telemetry,
                            window_session: &mut window_session,
                            configure_events,
                            dirty: &mut dirty,
                            activity_dirty: &mut activity_dirty,
                        },
                    );
                }
                BUTTON_RELEASE => {
                    handle_button_release(
                        &event,
                        PointerEventContext {
                            display,
                            window,
                            core,
                            projection,
                            writer: &mut writer,
                            input_buffer: &mut input_buffer,
                            selection: &mut selection,
                            resize_cursors: &mut resize_cursors,
                            width,
                            height,
                            metrics,
                            mapped,
                            window_destroyed,
                            focused: &mut focused,
                            presentation: &mut presentation,
                            window_move_strategy,
                            window_move_telemetry: &mut window_move_telemetry,
                            window_session: &mut window_session,
                            configure_events,
                            dirty: &mut dirty,
                            activity_dirty: &mut activity_dirty,
                        },
                    );
                }
                CONFIGURE_NOTIFY => {
                    if let Err(error) = handle_configure_notify(
                        &event,
                        WindowResizeContext {
                            display,
                            screen,
                            gl_context: context,
                            setup: &mut setup,
                            core,
                            pty,
                            text: &mut text,
                            width: &mut width,
                            height: &mut height,
                            user_text_zoom,
                            metrics: &mut metrics,
                            configure_events: &mut configure_events,
                            window_session: &mut window_session,
                            window_move_telemetry: &mut window_move_telemetry,
                            window_move_strategy,
                            dirty: &mut dirty,
                            activity_dirty: &mut activity_dirty,
                        },
                    ) {
                        eprintln!("could not resize native presentation surface: {error}");
                        running = false;
                        continue;
                    }
                }
                EXPOSE => {
                    dirty = true;
                    activity_dirty = true;
                }
                FOCUS_IN => {
                    mapped = true;
                    if set_input_focus_if_mapped(display, window, mapped) {
                        if let Some(input_method) = input_method.as_ref() {
                            unsafe { XSetICFocus(input_method.ic) };
                        }
                        focused = true;
                        dirty = true;
                        activity_dirty = true;
                    }
                }
                FOCUS_OUT => {
                    if let Some(input_method) = input_method.as_ref() {
                        unsafe { XUnsetICFocus(input_method.ic) };
                    }
                    focused = false;
                    dirty = true;
                    activity_dirty = true;
                }
                DESTROY_NOTIFY => {
                    // A window manager may destroy the surface in response to
                    // WM_DELETE_WINDOW before the event loop reaches cleanup.
                    // Xft/XIM resources reference that drawable, so they must
                    // not issue teardown requests after this notification.
                    window_destroyed = true;
                    running = false;
                }
                CLIENT_MESSAGE => {
                    let message =
                        unsafe { &*(&event as *const XEvent as *const XClientMessageEvent) };
                    if is_wm_delete_message(
                        message.message_type,
                        message.format,
                        message.data[0],
                        protocols_atom,
                        delete_atom,
                    ) {
                        // WM_DELETE_WINDOW is an external shutdown request.
                        // The window manager owns the drawable lifecycle from
                        // here; let the X connection reclaim Xft/XIM handles
                        // rather than racing it with a second teardown.
                        window_destroyed = true;
                        running = false;
                    }
                }
                _ => {}
            }
        }
        window_session.advance_fallback(display, window, &mut window_move_telemetry);
        if focus_pending
            && mapped
            && !window_destroyed
            && set_input_focus_if_mapped(display, window, mapped)
        {
            if let Some(input_method) = input_method.as_ref() {
                unsafe { XSetICFocus(input_method.ic) };
            }
            focus_pending = false;
            focused = true;
            dirty = true;
            activity_dirty = true;
        }
        if matches!(pty.next_child_event(), Some(ChildEvent::Exited(_))) {
            child_exited = true;
            dirty = true;
            activity_dirty = true;
        }
        if activity_dirty {
            finish_steady_interval(
                &mut steady_started,
                &mut steady_cpu_started,
                &mut steady_elapsed_seconds,
                &mut steady_cpu_seconds,
            );
        }
        let now = Instant::now();
        if options.animations && !options.reduced_motion && projection_is_animated(projection) {
            oi_motion_dirty = true;
            activity_dirty = true;
        }
        if let Some(frame_result) = frame_runtime.draw_if_ready(
            now,
            FrameDrawContext {
                display,
                width,
                height,
                core,
                projection,
                selection,
                text: &text,
                focused,
                input_buffer: &input_buffer,
                options,
                presentation,
                stencil_available,
                oi_target: &oi_target,
                chassis_material: &chassis_material,
                presentation_surface: &mut setup.presentation_surface,
                metrics,
                dirty: DirtyDomains {
                    full: dirty,
                    terminal: terminal_dirty,
                    oi_motion: oi_motion_dirty,
                },
                activity_dirty,
            },
        ) {
            dirty = false;
            terminal_dirty = false;
            oi_motion_dirty = false;
            activity_dirty = false;
            if frame_result.request_another_frame {
                // A presentation workload can be idle between projection
                // updates. The benchmark must measure steady idle frames,
                // so keep requesting a frame at the configured cadence.
                dirty = true;
            }
        }
        if !dirty && !terminal_dirty && !child_exited && steady_started.is_none() {
            steady_started = Some(Instant::now());
            steady_cpu_started = process_cpu_seconds();
        }
        if child_exited && dirty {
            if let Some(last) = frame_runtime.last_draw() {
                let remaining = crate::platform::active_frame_interval()
                    .saturating_sub(Instant::now().duration_since(last));
                if !remaining.is_zero() {
                    thread::sleep(remaining);
                    continue;
                }
            }
        }
        if child_exited || frame_runtime.benchmark_done() {
            // Keep one final frame visible long enough for a caller or bridge
            // to observe it, then restore/destroy the native surface.
            thread::sleep(Duration::from_millis(80));
            running = false;
        } else {
            let sleep_for = if dirty {
                frame_runtime
                    .last_draw()
                    .map(|last| {
                        crate::platform::active_frame_interval()
                            .saturating_sub(Instant::now().duration_since(last))
                            .min(IDLE_POLL_INTERVAL)
                    })
                    .unwrap_or(IDLE_POLL_INTERVAL)
            } else {
                IDLE_POLL_INTERVAL
            };
            thread::sleep(sleep_for);
        }
    }

    finish_steady_interval(
        &mut steady_started,
        &mut steady_cpu_started,
        &mut steady_elapsed_seconds,
        &mut steady_cpu_seconds,
    );
    frame_runtime.finish(steady_elapsed_seconds, steady_cpu_seconds);

    // Xft owns an XRender picture tied to the window. Tear it down before
    // destroying the drawable. If a window manager already destroyed the
    // window, leaking these process-local handles is safer than asking Xft/XIM
    // to destroy resources whose drawable no longer exists.
    if window_destroyed {
        drop(resize_cursors);
        std::mem::forget(input_method);
        std::mem::forget(oi_target);
        std::mem::forget(chassis_material);
        drop(text);
        setup.destroy(true);
        return Ok(());
    } else {
        drop(resize_cursors);
        drop(input_method);
        drop(oi_target);
        drop(chassis_material);
        drop(text);
    }
    setup.destroy(false);
    Ok(())
}
