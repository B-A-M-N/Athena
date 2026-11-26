//! Platform-facing modules for the X11 native host.
//!
//! The host owns the event loop and presentation lifecycle. This module only
//! gathers the OS mechanics so `x11.rs` does not also define every platform
//! implementation detail.

#[path = "ffi.rs"]
mod ffi;
pub(crate) use ffi::*;

#[path = "clipboard.rs"]
mod clipboard;
pub(crate) use clipboard::Clipboard;

#[path = "input_method.rs"]
mod input_method;
#[cfg(test)]
pub(crate) use input_method::bounded_lookup_length;
pub(crate) use input_method::{InputMethod, lookup_key, terminal_key_bytes};

#[path = "telemetry.rs"]
mod telemetry;
pub(crate) use telemetry::{
    RenderCounters, RenderStatsContext, finish_steady_interval, process_cpu_seconds,
    write_presentation_sync, write_render_stats,
};

#[path = "terminal.rs"]
mod terminal;
pub(crate) use terminal::resize_terminal;

#[path = "surface.rs"]
mod surface;
pub(crate) use surface::PresentationSurface;
