use std::env;
use std::time::Instant;

use crate::Projection;

pub(crate) fn process_cpu_seconds() -> Option<f64> {
    let mut usage = unsafe { std::mem::zeroed::<libc::rusage>() };
    if unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut usage) } != 0 {
        return None;
    }
    Some(
        usage.ru_utime.tv_sec as f64
            + usage.ru_utime.tv_usec as f64 / 1_000_000.0
            + usage.ru_stime.tv_sec as f64
            + usage.ru_stime.tv_usec as f64 / 1_000_000.0,
    )
}

#[derive(Clone, Copy)]
pub(crate) struct RenderStatsContext<'a> {
    pub(crate) started: &'a Instant,
    pub(crate) cpu_started: Option<f64>,
    pub(crate) steady_elapsed_seconds: f64,
    pub(crate) steady_cpu_seconds: f64,
    pub(crate) counters: RenderCounters,
}

#[derive(Clone, Copy, Default)]
pub(crate) struct RenderCounters {
    pub(crate) redraws: u64,
    pub(crate) initial_redraws: u64,
    pub(crate) event_redraws: u64,
    pub(crate) idle_redraws: u64,
    pub(crate) presented_frame_sequence: u64,
}

pub(crate) fn write_render_stats(context: RenderStatsContext<'_>) {
    let Ok(path) = env::var("ATHENA_NATIVE_RENDER_STATS") else {
        return;
    };
    let elapsed_seconds = context.started.elapsed().as_secs_f64();
    let cpu_seconds = process_cpu_seconds()
        .zip(context.cpu_started)
        .map(|(end, start)| end - start);
    let value = serde_json::json!({
        "elapsed_seconds": elapsed_seconds,
        "redraws": context.counters.redraws,
        "initial_redraws": context.counters.initial_redraws,
        "event_redraws": context.counters.event_redraws,
        "idle_redraws": context.counters.idle_redraws,
        "presented_frame_sequence": context.counters.presented_frame_sequence,
        "cpu_seconds": cpu_seconds,
        "steady_elapsed_seconds": context.steady_elapsed_seconds,
        "steady_cpu_seconds": context.steady_cpu_seconds,
    });
    let _ = std::fs::write(path, value.to_string());
}

pub(crate) fn write_presentation_sync(
    sequence: u64,
    width: i32,
    height: i32,
    projection: &Projection,
) {
    let Ok(path) = env::var("ATHENA_NATIVE_PRESENTATION_SYNC") else {
        return;
    };
    let value = serde_json::json!({
        "presented_frame_sequence": sequence,
        "width": width,
        "height": height,
        "status": projection.status.as_str(),
        "semantic_state": projection.semantic_state.as_str(),
        "current_action_kind": projection.current_action.as_ref().map(|action| action.kind.as_str()),
        "bridge_status": projection.bridge_status.as_str(),
        "bridge_generation": projection.bridge_generation,
        "last_frame_sequence": projection.last_frame_sequence,
        "last_frame_age_ms": projection.last_frame_age_ms(),
        "stale": projection.stale,
    });
    let path = std::path::PathBuf::from(path);
    let Some(file_name) = path.file_name().and_then(|name| name.to_str()) else {
        return;
    };
    let temporary = path.with_file_name(format!(".{file_name}.tmp"));
    if let Err(error) = std::fs::write(&temporary, value.to_string()) {
        eprintln!("could not write native presentation sync: {error}");
        return;
    }
    if let Err(error) = std::fs::rename(&temporary, &path) {
        eprintln!("could not publish native presentation sync: {error}");
        let _ = std::fs::remove_file(temporary);
    }
}

pub(crate) fn finish_steady_interval(
    started: &mut Option<Instant>,
    cpu_started: &mut Option<f64>,
    elapsed_seconds: &mut f64,
    cpu_seconds: &mut f64,
) {
    let Some(start) = started.take() else {
        cpu_started.take();
        return;
    };
    *elapsed_seconds += start.elapsed().as_secs_f64();
    if let (Some(start_cpu), Some(end_cpu)) = (cpu_started.take(), process_cpu_seconds()) {
        *cpu_seconds += end_cpu - start_cpu;
    }
}
