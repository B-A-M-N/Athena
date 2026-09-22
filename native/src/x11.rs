//! Linux/X11 AthenaBOX compositor.
//!
//! The terminal engine remains Alacritty. This module owns the native window,
//! physical chassis layout, and one composed Fontconfig/Xft/OpenGL frame.
#![allow(clippy::too_many_arguments)]

use std::env;
use std::io::Write;
use std::ptr;
use std::sync::mpsc::Receiver;
use std::thread;
use std::time::{Duration, Instant};

use alacritty_terminal::grid::Scroll;
use alacritty_terminal::term::TermMode;
use alacritty_terminal::tty::{self, ChildEvent, EventedPty};

use crate::input::InputBuffer;
use crate::platform::*;
use crate::render::chassis::{PresentationControl, PresentationSettings};
use crate::render::presentation::projection_is_animated;
use crate::render::text::{FontRole, TextRenderer};
use crate::{LatestProjection, Projection, apply_available};
pub(crate) use athena_terminal::{
    NativePixelLayout, NativeTerminalCore, PixelRect, PromptLayout, UiFontMetrics,
};
mod event_loop;
mod frame_runtime;
mod layout_diagnostics;
pub mod window_hints;
mod window_input;
pub mod window_manager;
mod window_pointer;
mod window_resize;
mod window_runtime;
mod window_setup;
pub(crate) use crate::render::fit::{fit_input_in, fit_text_in, selection_bounds};
use crate::window_management::{PendingEwmhGesture, WindowDrag, WindowDragKind, resize_zone};
pub(crate) use event_loop::WindowSession;
pub(crate) use layout_diagnostics::{dump_live_layout_json, write_runtime_layout_dump};
pub(crate) use window_hints::{set_window_hints, set_window_pid};
pub(crate) use window_manager::intern_atom;
pub(crate) use window_manager::{
    FrameGeometry, ResizeCursors, WindowMoveStrategy, WindowMoveTelemetry, apply_window_drag,
    begin_window_move, begin_window_resize, grab_window_pointer, initial_window_size,
    is_wm_delete_message, select_window_move_strategy, set_input_focus_if_mapped,
    window_management_diagnostics, write_attention_action,
};
pub(crate) use window_setup::WindowSetup;

type Selection = Option<((usize, usize), (usize, usize))>;

unsafe extern "C" fn ignore_shutdown_x_error(
    _display: *mut Display,
    _error: *mut XErrorEvent,
) -> c_int {
    0
}

#[derive(Debug, Clone, Copy)]
struct PresentationClock {
    started_at: Instant,
    fixed_step_seconds: Option<f32>,
}

impl PresentationClock {
    fn from_environment() -> Result<Self, String> {
        let fixed_step_seconds = match env::var("ATHENA_PRESENTATION_CLOCK") {
            Ok(value) if value == "fixed" => {
                Some(crate::platform::active_frame_interval().as_secs_f32())
            }
            Ok(value) => {
                let raw = value.strip_prefix("fixed:").ok_or_else(|| {
                    "ATHENA_PRESENTATION_CLOCK must be fixed or fixed:<seconds>".to_owned()
                })?;
                let seconds = raw
                    .parse::<f32>()
                    .map_err(|_| "fixed presentation clock step must be a number".to_owned())?;
                if !seconds.is_finite() || seconds <= 0.0 {
                    return Err("fixed presentation clock step must be positive".to_owned());
                }
                Some(seconds)
            }
            Err(env::VarError::NotPresent) => None,
            Err(error) => return Err(format!("could not read presentation clock: {error}")),
        };
        Ok(Self {
            started_at: Instant::now(),
            fixed_step_seconds,
        })
    }

    #[cfg(test)]
    fn fixed(step_seconds: f32) -> Self {
        Self {
            started_at: Instant::now(),
            fixed_step_seconds: Some(step_seconds),
        }
    }

    fn seconds_at_frame(&self, frame_sequence: u64) -> f32 {
        self.fixed_step_seconds
            .map(|step| step * frame_sequence as f32)
            .unwrap_or_else(|| self.started_at.elapsed().as_secs_f32())
    }
}

/// Native renderer switches that are intentionally presentation-only.
#[derive(Clone, Debug)]
pub struct RendererOptions {
    pub mascot: String,
    pub animations: bool,
    pub reduced_motion: bool,
    pub text_scale: f32,
    pub cabinet_only: bool,
    /// Bench-only: exit after this many presented frames (review item 24).
    pub benchmark_frames: Option<u64>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct DirtyDomains {
    pub(crate) full: bool,
    pub(crate) terminal: bool,
    pub(crate) oi_motion: bool,
}

impl Default for RendererOptions {
    fn default() -> Self {
        Self {
            mascot: "owl".to_owned(),
            animations: true,
            reduced_motion: false,
            text_scale: crate::DEFAULT_TEXT_SCALE,
            cabinet_only: false,
            benchmark_frames: None,
        }
    }
}

pub(crate) fn effective_text_scale(width: i32, height: i32, user_zoom: f32) -> f32 {
    let window_scale = NativePixelLayout::scale_for_window(width, height).clamp(0.75, 1.40);
    (user_zoom * window_scale).clamp(0.75, 2.50)
}

pub fn run(
    mut core: NativeTerminalCore,
    mut pty: tty::Pty,
    output_rx: Receiver<Vec<u8>>,
    bridge_rx: Option<LatestProjection>,
    mut projection: Projection,
    options: RendererOptions,
) -> Result<(), String> {
    let display = unsafe { XOpenDisplay(ptr::null()) };
    if display.is_null() {
        return Err("could not open an X11 display; use --headless for CI".to_owned());
    }
    // Window managers may destroy a drawable asynchronously while a final
    // GL/Xft request is still queued. Keep that normal close race from
    // invoking Xlib's process-aborting default handler.
    unsafe { XSetErrorHandler(Some(ignore_shutdown_x_error)) };
    let result = window_runtime::run_window(
        display,
        &mut core,
        &mut pty,
        output_rx,
        bridge_rx,
        &mut projection,
        &options,
    );
    unsafe { XCloseDisplay(display) };
    result
}

#[cfg(test)]
mod tests {
    use super::{
        FrameGeometry, PendingEwmhGesture, PresentationClock, Projection, WindowDrag,
        WindowDragKind, WindowMoveStrategy, WindowMoveTelemetry, is_wm_delete_message, resize_zone,
        write_attention_action,
    };
    use crate::VisualMode;
    use crate::render::chassis::{PresentationControl, PresentationSettings};
    use crate::render::oi::AttentionAction;
    use crate::window_management::ResizeZone;
    use crate::x11::window_manager::moveresize_message_data;
    use alacritty_terminal::term::TermMode;
    use std::time::Instant;

    #[test]
    fn frame_geometry_keeps_apertures_equal() {
        let geometry = FrameGeometry::new(1000, 700);
        let left_end = geometry.operator_outer.x + geometry.operator_outer.width;
        assert!(geometry.oi_outer.x > left_end);

        assert!((left_end - geometry.left_x - geometry.oi_outer.width).abs() < 0.01);
        assert!((geometry.operator_inner.width - geometry.oi_inner.width).abs() < 0.01);
        assert!((geometry.operator_inner.height - geometry.oi_inner.height).abs() < 0.01);
    }

    #[test]
    fn fixed_presentation_clock_is_reproducible_by_frame_sequence() {
        let clock = PresentationClock::fixed(0.1);
        assert_eq!(clock.seconds_at_frame(0), 0.0);
        assert!((clock.seconds_at_frame(3) - 0.3).abs() < f32::EPSILON);
        assert!((clock.seconds_at_frame(10) - 1.0).abs() < f32::EPSILON);
    }

    #[test]
    fn frame_geometry_maps_operator_cells_within_bounds() {
        let geometry = FrameGeometry::new(1000, 700);
        let (origin_x, origin_y) = geometry.operator_origin();

        assert_eq!(geometry.cell_at(origin_x, origin_y), Some((0, 0)));
        assert_eq!(
            geometry.cell_at(
                origin_x + geometry.cell_width as i32,
                origin_y + geometry.cell_height as i32,
            ),
            Some((1, 1))
        );
        assert_eq!(geometry.cell_at(origin_x - 1, origin_y), None);
        assert_eq!(geometry.cell_at(origin_x, origin_y - 1), None);
    }

    #[test]
    fn frame_geometry_preserves_minimum_resize() {
        let tiny = FrameGeometry::new(1, 1).terminal_size();
        assert!(tiny.columns >= 1 && tiny.rows >= 1);
        let geometry = FrameGeometry::new(1000, 700);
        let size = geometry.terminal_size();
        assert!(size.columns >= 1 && size.rows >= 1);
        assert!(size.columns > 1 && size.rows > 1);
    }

    #[test]
    fn presentation_controls_change_only_their_owned_setting() {
        let geometry = FrameGeometry::new(1280, 800);
        let y = geometry.controls.y as i32 + 20;
        let brightness = geometry.rail.brightness;
        let focus = geometry.rail.focus;
        let power = geometry.rail.power;
        assert_eq!(
            PresentationSettings::control_at(&geometry, brightness.x as i32 + 20, y),
            Some(PresentationControl::Brightness)
        );
        assert_eq!(
            PresentationSettings::control_at(&geometry, focus.x as i32 + 20, y),
            Some(PresentationControl::Focus)
        );
        assert_eq!(
            PresentationSettings::control_at(&geometry, power.x as i32 + 20, y),
            Some(PresentationControl::Power)
        );

        let mut settings = PresentationSettings::default();
        settings.activate(
            PresentationControl::Brightness,
            brightness.x as i32 + brightness.width as i32 / 2,
            &geometry,
        );
        assert!((settings.brightness - 0.5).abs() < 0.02);
        assert_eq!(settings.focus, PresentationSettings::default().focus);
        assert!(settings.display_enabled);
        settings.activate(PresentationControl::Power, power.x as i32 + 20, &geometry);
        assert!(!settings.display_enabled);
    }

    #[test]
    fn native_attention_actions_use_the_service_command_bridge() {
        let mut output = Vec::new();
        assert!(write_attention_action(
            &mut output,
            &AttentionAction::Approve {
                approval_id: "apr-1".to_owned(),
                scope: "task".to_owned(),
            },
        ));
        assert_eq!(output, b"/approve apr-1 task\n");

        output.clear();
        assert!(write_attention_action(
            &mut output,
            &AttentionAction::Deny {
                approval_id: "apr-1".to_owned(),
            },
        ));
        assert_eq!(output, b"/deny apr-1\n");

        output.clear();
        assert!(!write_attention_action(
            &mut output,
            &AttentionAction::Approve {
                approval_id: "apr 1".to_owned(),
                scope: "task".to_owned(),
            },
        ));
        assert!(output.is_empty());
    }

    #[test]
    fn prompt_state_is_explicit_and_projection_driven() {
        let mut projection = Projection {
            status: "Approval requested".to_owned(),
            ..Projection::default()
        };
        assert_eq!(
            VisualMode::from_projection(&projection).prompt_state(&projection),
            "APPROVAL"
        );
        projection.status = "Disconnected".to_owned();
        assert_eq!(
            VisualMode::from_projection(&projection).prompt_state(&projection),
            "DISCONNECTED"
        );
        projection.status = "Working".to_owned();
        projection.visual_mode = "search".to_owned();
        assert_eq!(
            VisualMode::from_projection(&projection).prompt_state(&projection),
            "WORKING"
        );
    }

    #[test]
    fn only_wm_protocol_delete_client_messages_close_the_window() {
        assert!(is_wm_delete_message(10, 32, 20, 10, 20));
        assert!(!is_wm_delete_message(10, 32, 21, 10, 20));
        assert!(!is_wm_delete_message(11, 32, 20, 10, 20));
        assert!(!is_wm_delete_message(10, 16, 20, 10, 20));
    }

    #[test]
    fn resize_zones_cover_all_edges_and_corners() {
        let width = 1280;
        let height = 800;
        assert_eq!(resize_zone(0, 0, width, height), Some(ResizeZone::TopLeft));
        assert_eq!(
            resize_zone(width - 1, 0, width, height),
            Some(ResizeZone::TopRight)
        );
        assert_eq!(
            resize_zone(0, height - 1, width, height),
            Some(ResizeZone::BottomLeft)
        );
        assert_eq!(
            resize_zone(width - 1, height - 1, width, height),
            Some(ResizeZone::BottomRight)
        );
        assert_eq!(
            resize_zone(width / 2, 0, width, height),
            Some(ResizeZone::Top)
        );
        assert_eq!(
            resize_zone(width / 2, height - 1, width, height),
            Some(ResizeZone::Bottom)
        );
        assert_eq!(
            resize_zone(0, height / 2, width, height),
            Some(ResizeZone::Left)
        );
        assert_eq!(
            resize_zone(width - 1, height / 2, width, height),
            Some(ResizeZone::Right)
        );
        assert_eq!(resize_zone(width / 2, height / 2, width, height), None);
    }

    #[test]
    fn resize_zones_use_ewmh_moveresize_directions() {
        assert_eq!(ResizeZone::TopLeft.direction(), 0);
        assert_eq!(ResizeZone::Top.direction(), 1);
        assert_eq!(ResizeZone::TopRight.direction(), 2);
        assert_eq!(ResizeZone::Right.direction(), 3);
        assert_eq!(ResizeZone::BottomRight.direction(), 4);
        assert_eq!(ResizeZone::Bottom.direction(), 5);
        assert_eq!(ResizeZone::BottomLeft.direction(), 6);
        assert_eq!(ResizeZone::Left.direction(), 7);
    }

    #[test]
    fn moveresize_message_uses_normal_application_source() {
        assert_eq!(
            moveresize_message_data(40, 50, ResizeZone::BottomRight.direction(), 1),
            [40, 50, 4, 1, 1]
        );
    }

    #[test]
    fn window_move_strategy_is_exclusive() {
        assert!(WindowMoveStrategy::Ewmh.uses_ewmh());
        assert!(!WindowMoveStrategy::ClientManagedFallback.uses_ewmh());
        assert_ne!(
            WindowMoveStrategy::Ewmh.name(),
            WindowMoveStrategy::ClientManagedFallback.name()
        );
    }

    #[test]
    fn adaptive_window_move_telemetry_requires_a_real_confirmation() {
        let mut telemetry = WindowMoveTelemetry::for_strategy(WindowMoveStrategy::Ewmh);
        assert_eq!(telemetry.active_strategy, "ewmh_preferred");
        assert!(!telemetry.ewmh_attempted);
        assert!(!telemetry.ewmh_confirmed);

        telemetry.begin_ewmh();
        telemetry.confirm_ewmh();
        assert_eq!(telemetry.active_strategy, "ewmh_confirmed");
        assert!(telemetry.ewmh_confirmed);

        telemetry.activate_fallback("late_test_fallback");
        assert_eq!(telemetry.active_strategy, "ewmh_confirmed");
        assert_eq!(telemetry.fallback_reason, None);

        let mut fallback = WindowMoveTelemetry::for_strategy(WindowMoveStrategy::Ewmh);
        fallback.begin_ewmh();
        fallback.activate_fallback("no_configure");
        fallback.confirm_ewmh();
        assert_eq!(fallback.active_strategy, "client_managed");
        assert!(!fallback.ewmh_confirmed);
        assert_eq!(fallback.fallback_reason, Some("no_configure"));
    }

    #[test]
    fn delayed_ewmh_configure_after_fallback_cannot_reactivate_protocol() {
        let drag = WindowDrag {
            kind: WindowDragKind::Move,
            start_root_x: 10,
            start_root_y: 10,
            start_window_x: 100,
            start_window_y: 100,
            start_parent_x: 100,
            start_parent_y: 100,
            start_width: 800,
            start_height: 600,
        };
        let pending = PendingEwmhGesture {
            drag,
            configure_events_at_start: 4,
            deadline: Instant::now(),
        };
        assert!(pending.geometry_changed(130, 140, 800, 600));
        let mut telemetry = WindowMoveTelemetry::for_strategy(WindowMoveStrategy::Ewmh);
        telemetry.begin_ewmh();
        telemetry.activate_fallback("ewmh_no_configure_before_grace");
        telemetry.confirm_ewmh();
        assert_eq!(telemetry.active_strategy, "client_managed");
        assert!(!telemetry.ewmh_confirmed);
        assert_eq!(
            telemetry.fallback_reason,
            Some("ewmh_no_configure_before_grace")
        );
    }

    #[test]
    fn terminal_modes_choose_application_cursor_sequences() {
        assert_eq!(
            crate::platform::terminal_key_bytes(crate::platform::XK_LEFT, TermMode::empty(), &[]),
            b"\x1b[D"
        );
        assert_eq!(
            crate::platform::terminal_key_bytes(
                crate::platform::XK_LEFT,
                TermMode::APP_CURSOR,
                &[]
            ),
            b"\x1bOD"
        );
        assert_eq!(
            crate::platform::terminal_key_bytes(
                crate::platform::XK_PAGE_DOWN,
                TermMode::empty(),
                &[]
            ),
            b"\x1b[6~"
        );
    }

    #[test]
    fn xim_lookup_lengths_are_never_allowed_to_escape_the_buffer() {
        assert_eq!(crate::platform::bounded_lookup_length(4, 8), Some(4));
        assert_eq!(crate::platform::bounded_lookup_length(8, 8), Some(8));
        assert_eq!(crate::platform::bounded_lookup_length(9, 8), None);
        assert_eq!(crate::platform::bounded_lookup_length(-1, 8), None);
    }
}
