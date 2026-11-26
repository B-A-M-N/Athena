//! Native Athena terminal vertical slice.
//!
//! This executable owns a real Alacritty-backed PTY.  The first slice keeps
//! the Athena compositor deliberately small, but establishes the important
//! ownership boundary: terminal bytes and input belong to this process,
//! semantic OI content arrives through an explicit serialized projection
//! bridge, and the compositor never decides or executes anything.

use std::env;
use std::io::{self, BufRead, Read, Write};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::net::UnixListener;

use alacritty_terminal::event::WindowSize;
use alacritty_terminal::tty::{self, ChildEvent, EventedPty};

use athena_terminal::{NativePixelLayout, NativeTerminalCore, PromptLayout, UiFontMetrics};

mod buddy;
mod input;
#[cfg(unix)]
mod platform;
#[cfg(unix)]
mod projection_schema;
pub(crate) use projection_schema::*;
mod render;
#[cfg(unix)]
mod window_management;
#[cfg(unix)]
mod x11;

pub(crate) const DEFAULT_TEXT_SCALE: f32 = 1.0;

#[derive(Default)]
struct Args {
    headless: bool,
    dump_layout: bool,
    dump_width: i32,
    dump_height: i32,
    cabinet_only: bool,
    bridge_stdin: bool,
    bridge_socket: Option<String>,
    command: Option<String>,
    columns: usize,
    rows: usize,
    mascot: String,
    animations: bool,
    reduced_motion: bool,
    text_scale: f32,
    benchmark_frames: Option<u64>,
}

fn parse_args() -> Result<Args, String> {
    parse_args_from(env::args().skip(1))
}

fn parse_args_from(values: impl IntoIterator<Item = String>) -> Result<Args, String> {
    let mut args = Args {
        columns: 100,
        rows: 32,
        dump_width: 1280,
        dump_height: 800,
        mascot: "owl".to_owned(),
        animations: true,
        text_scale: env::var("ATHENA_NATIVE_TEXT_SCALE")
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(DEFAULT_TEXT_SCALE),
        ..Args::default()
    };
    let mut values = values.into_iter();
    while let Some(arg) = values.next() {
        match arg.as_str() {
            "--headless" => args.headless = true,
            "--benchmark-frames" => {
                let value = values.next().ok_or("--benchmark-frames needs a count")?;
                args.benchmark_frames = Some(
                    value
                        .parse()
                        .map_err(|_| "--benchmark-frames must be an integer")?,
                );
            }
            "--dump-layout" => args.dump_layout = true,
            "--dump-layout-size" => {
                let value = values
                    .next()
                    .ok_or("--dump-layout-size needs WIDTHxHEIGHT")?;
                let (width, height) = parse_layout_size(&value)?;
                args.dump_width = width;
                args.dump_height = height;
            }
            "--cabinet-only" => args.cabinet_only = true,
            "--bridge-stdin" => args.bridge_stdin = true,
            "--bridge-socket" => {
                args.bridge_socket = Some(values.next().ok_or("--bridge-socket needs a path")?);
            }
            "--command" => {
                args.command = Some(values.next().ok_or("--command needs a value")?);
            }
            "--columns" => {
                args.columns = values
                    .next()
                    .ok_or("--columns needs a value")?
                    .parse()
                    .map_err(|_| "--columns must be an integer")?;
            }
            "--rows" => {
                args.rows = values
                    .next()
                    .ok_or("--rows needs a value")?
                    .parse()
                    .map_err(|_| "--rows must be an integer")?;
            }
            "--mascot" => {
                args.mascot = values.next().ok_or("--mascot needs a value")?;
            }
            "--no-animations" => args.animations = false,
            "--reduced-motion" => args.reduced_motion = true,
            "--text-scale" => {
                args.text_scale = values
                    .next()
                    .ok_or("--text-scale needs a multiplier")?
                    .parse()
                    .map_err(|_| "--text-scale must be a number")?;
            }
            "--help" | "-h" => {
                println!(
                    "athena-terminal [--headless] [--benchmark-frames N] [--dump-layout] [--cabinet-only] [--bridge-stdin|--bridge-socket PATH] [--command SHELL_CODE] [--mascot owl|cat|bot|off] [--no-animations] [--reduced-motion] [--text-scale MULTIPLIER]"
                );
                println!("  --headless       run the PTY/core slice without opening a window");
                println!(
                    "  --benchmark-frames N  exit after N presented frames (cadence benchmark)"
                );
                println!(
                    "  --dump-layout    print layout JSON; use live Xft metrics when DISPLAY is available"
                );
                println!("  --dump-layout-size WIDTHxHEIGHT  choose layout probe dimensions");
                println!("  --cabinet-only   render the deterministic physical cabinet baseline");
                println!("  --bridge-stdin   read JSON projection frames from stdin");
                println!(
                    "  --mascot         select Buddy (default: owl; built-ins: owl, cat, bot, off)"
                );
                println!(
                    "  --text-scale     multiply native UI text size (also ATHENA_NATIVE_TEXT_SCALE)"
                );
                return Err(String::new());
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    args.columns = args.columns.max(1);
    args.rows = args.rows.max(1);
    if !args.text_scale.is_finite() || !(0.75..=2.5).contains(&args.text_scale) {
        return Err("--text-scale must be between 0.75 and 2.5".to_owned());
    }
    if !matches!(
        args.mascot.to_ascii_lowercase().as_str(),
        "owl" | "cat" | "bot" | "off"
    ) {
        return Err(format!(
            "unknown mascot {:?}; choose owl, cat, bot, or off",
            args.mascot
        ));
    }
    Ok(args)
}

fn parse_layout_size(value: &str) -> Result<(i32, i32), String> {
    let (width, height) = value
        .split_once('x')
        .or_else(|| value.split_once('X'))
        .ok_or("--dump-layout-size must be WIDTHxHEIGHT")?;
    let width: i32 = width
        .parse()
        .map_err(|_| "--dump-layout-size width must be an integer")?;
    let height: i32 = height
        .parse()
        .map_err(|_| "--dump-layout-size height must be an integer")?;
    if width <= 0 || height <= 0 {
        return Err("--dump-layout-size dimensions must be positive".to_owned());
    }
    Ok((width, height))
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = match parse_args() {
        Ok(args) => args,
        Err(error) if error.is_empty() => return Ok(()),
        Err(error) => return Err(error.into()),
    };

    if args.dump_layout {
        #[cfg(unix)]
        {
            match x11::dump_live_layout_json(args.dump_width, args.dump_height, args.text_scale) {
                Ok(dump) => {
                    println!("{}", serde_json::to_string_pretty(&dump)?);
                    return Ok(());
                }
                Err(error) => {
                    eprintln!("live layout probe unavailable: {error}");
                }
            }
        }
        let metrics = UiFontMetrics::fallback();
        let layout = NativePixelLayout::for_window(args.dump_width, args.dump_height, metrics);
        let prompt_layout = PromptLayout::from_rect(
            layout.prompt,
            metrics.input,
            metrics.instrument,
            layout.prompt_padding_y,
            layout.prompt_gap,
            layout.prompt_bottom_padding,
            !layout.compact,
        );
        let mut dump = serde_json::to_value(layout)?;
        dump.as_object_mut()
            .expect("NativePixelLayout serializes as an object")
            .insert(
                "metrics_source".to_owned(),
                serde_json::json!("fallback_static"),
            );
        dump.as_object_mut()
            .expect("NativePixelLayout serializes as an object")
            .insert("text_scale".to_owned(), serde_json::json!(args.text_scale));
        dump.as_object_mut()
            .expect("NativePixelLayout serializes as an object")
            .insert(
                "font_pixel_sizes".to_owned(),
                serde_json::json!([16, 16, 14, 12]),
            );
        dump.as_object_mut()
            .expect("NativePixelLayout serializes as an object")
            .insert("metrics".to_owned(), serde_json::to_value(metrics)?);
        dump.as_object_mut()
            .expect("NativePixelLayout serializes as an object")
            .insert(
                "terminal_size".to_owned(),
                serde_json::to_value(layout.terminal_size())?,
            );
        dump.as_object_mut()
            .expect("NativePixelLayout serializes as an object")
            .insert(
                "prompt_layout".to_owned(),
                serde_json::to_value(prompt_layout)?,
            );
        dump.as_object_mut()
            .expect("NativePixelLayout serializes as an object")
            .insert(
                "window_management".to_owned(),
                serde_json::json!({
                    "moveresize_supported": false,
                    "strategy": "unavailable",
                    "protocol": "_NET_WM_MOVERESIZE",
                    "reason": "no live X11 display",
                }),
            );
        println!("{}", serde_json::to_string_pretty(&dump)?);
        return Ok(());
    }

    #[cfg(unix)]
    let bridge_socket = args
        .bridge_socket
        .as_deref()
        .map(spawn_projection_socket)
        .transpose()?;
    #[cfg(not(unix))]
    let bridge_socket: Option<LatestProjection> = None;
    if args.bridge_socket.is_some() && !cfg!(unix) {
        return Err("--bridge-socket is only supported on Unix targets".into());
    }

    let window_size = window_size(args.columns, args.rows);
    let mut pty_options = tty::Options::default();
    if let Some(path) = args.bridge_socket.as_ref() {
        pty_options
            .env
            .insert("ATHENA_NATIVE_BRIDGE_SOCKET".to_owned(), path.clone());
    }
    if let Some(command) = args.command {
        pty_options.shell = Some(tty::Shell::new(
            "/bin/sh".to_owned(),
            vec!["-lc".to_owned(), command],
        ));
    }
    let pty = tty::new(&pty_options, window_size, 0)?;
    let reader = pty.file().try_clone()?;
    let (output_tx, output_rx) = mpsc::sync_channel::<Vec<u8>>(64);
    spawn_pty_reader(reader, output_tx);
    let bridge_rx = if bridge_socket.is_some() {
        bridge_socket
    } else if args.bridge_stdin {
        Some(spawn_projection_reader())
    } else {
        None
    };

    let core = NativeTerminalCore::new(args.columns, args.rows);
    let projection = Projection {
        title: "ATHENA // NATIVE TERMINAL".to_owned(),
        status: "WAITING FOR PROJECTION".to_owned(),
        oi: vec![
            "ATHENA OI // GLASS COMPUTE".to_owned(),
            "no projection frame received".to_owned(),
        ],
        entities: Vec::new(),
        alerts: Vec::new(),
        ..Projection::default()
    };

    let result = if args.headless {
        run_headless(core, pty, output_rx, bridge_rx, projection)
    } else {
        #[cfg(unix)]
        {
            x11::run(
                core,
                pty,
                output_rx,
                bridge_rx,
                projection,
                x11::RendererOptions {
                    mascot: args.mascot,
                    animations: args.animations,
                    reduced_motion: args.reduced_motion,
                    text_scale: args.text_scale,
                    cabinet_only: args.cabinet_only,
                    benchmark_frames: args.benchmark_frames,
                },
            )
            .map_err(|error| error.into())
        }

        #[cfg(not(unix))]
        {
            let _ = (core, pty, output_rx, bridge_rx, projection);
            Err("native window frontend is not implemented on this target yet".into())
        }
    };

    #[cfg(unix)]
    if let Some(path) = args.bridge_socket {
        let _ = std::fs::remove_file(path);
    }
    result
}

fn window_size(columns: usize, rows: usize) -> WindowSize {
    WindowSize {
        num_cols: columns.min(u16::MAX as usize) as u16,
        num_lines: rows.min(u16::MAX as usize) as u16,
        cell_width: 9,
        cell_height: 18,
    }
}

fn spawn_pty_reader(mut reader: std::fs::File, output_tx: SyncSender<Vec<u8>>) {
    thread::spawn(move || {
        let mut buffer = [0_u8; 8192];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => break,
                Ok(count) => {
                    if output_tx.send(buffer[..count].to_vec()).is_err() {
                        break;
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(5));
                }
                Err(_) => break,
            }
        }
    });
}

#[derive(Clone, Default)]
struct LatestProjection {
    frame: Arc<Mutex<Option<ProjectionFrame>>>,
    error: Arc<Mutex<Option<String>>>,
    lifecycle: Arc<Mutex<Vec<String>>>,
    connected: Arc<AtomicBool>,
    ever_connected: Arc<AtomicBool>,
    generation: Arc<AtomicU64>,
}

impl LatestProjection {
    fn publish(&self, frame: ProjectionFrame) {
        let was_connected = self.connected.swap(true, Ordering::SeqCst);
        if !was_connected && self.ever_connected.swap(true, Ordering::SeqCst) {
            self.publish_lifecycle("RECONNECTING");
        }
        self.publish_lifecycle("CONNECTED");
        if let Ok(mut slot) = self.frame.lock() {
            *slot = Some(frame);
        }
    }

    fn take(&self) -> Option<ProjectionFrame> {
        self.frame.lock().ok()?.take()
    }

    fn publish_error(&self, error: String) {
        self.connected.store(false, Ordering::SeqCst);
        self.publish_lifecycle("ERROR");
        if let Ok(mut slot) = self.error.lock() {
            *slot = Some(error);
        }
    }

    fn take_error(&self) -> Option<String> {
        self.error.lock().ok()?.take()
    }

    fn mark_disconnected(&self) {
        self.connected.store(false, Ordering::SeqCst);
        self.publish_lifecycle("DISCONNECTED");
    }

    fn publish_lifecycle(&self, status: &str) {
        if status == "RECONNECTING" {
            self.generation.fetch_add(1, Ordering::SeqCst);
        }
        if let Ok(mut lifecycle) = self.lifecycle.lock() {
            lifecycle.push(status.to_owned());
        }
    }

    fn take_lifecycle(&self) -> Vec<String> {
        self.lifecycle
            .lock()
            .map(|mut lifecycle| std::mem::take(&mut *lifecycle))
            .unwrap_or_default()
    }
}

fn publish_projection_line(latest: &LatestProjection, line: &str) {
    match serde_json::from_str::<ProjectionFrame>(line) {
        Ok(frame) => match frame.normalize() {
            Ok(frame) => latest.publish(frame),
            Err(error) => latest.publish_error(error),
        },
        Err(error) => latest.publish_error(format!("invalid projection JSON: {error}")),
    }
}

fn spawn_projection_reader() -> LatestProjection {
    let latest = LatestProjection::default();
    latest.publish_lifecycle("CONNECTING");
    let writer = latest.clone();
    thread::spawn(move || {
        let stdin = io::stdin();
        for line in stdin.lock().lines() {
            let Ok(line) = line else {
                writer.mark_disconnected();
                break;
            };
            publish_projection_line(&writer, &line);
        }
        writer.mark_disconnected();
    });
    latest
}

#[cfg(unix)]
fn spawn_projection_socket(path: &str) -> Result<LatestProjection, io::Error> {
    let listener = UnixListener::bind(path)?;
    let latest = LatestProjection::default();
    latest.publish_lifecycle("CONNECTING");
    let writer = latest.clone();
    thread::spawn(move || {
        for connection in listener.incoming() {
            let Ok(stream) = connection else {
                writer.mark_disconnected();
                break;
            };
            for line in io::BufReader::new(stream).lines() {
                let Ok(line) = line else { break };
                publish_projection_line(&writer, &line);
            }
            writer.mark_disconnected();
        }
    });
    Ok(latest)
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct ApplyChanges {
    terminal: bool,
    projection: bool,
}

fn apply_available(
    core: &mut NativeTerminalCore,
    output_rx: &Receiver<Vec<u8>>,
    bridge_rx: Option<&LatestProjection>,
    projection: &mut Projection,
) -> ApplyChanges {
    let mut changed = ApplyChanges::default();
    while let Ok(bytes) = output_rx.try_recv() {
        core.feed(&bytes);
        changed.terminal = true;
    }
    if let Some(bridge_rx) = bridge_rx {
        for status in bridge_rx.take_lifecycle() {
            projection.bridge_lifecycle(&status);
            changed.projection = true;
        }
        if let Some(error) = bridge_rx.take_error() {
            projection.bridge_error(error);
            changed.projection = true;
        }
        if let Some(frame) = bridge_rx.take() {
            projection.apply(frame);
            changed.projection = true;
        }
    }
    changed
}

fn run_headless(
    mut core: NativeTerminalCore,
    mut pty: tty::Pty,
    output_rx: Receiver<Vec<u8>>,
    bridge_rx: Option<LatestProjection>,
    mut projection: Projection,
) -> Result<(), Box<dyn std::error::Error>> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        apply_available(&mut core, &output_rx, bridge_rx.as_ref(), &mut projection);
        if matches!(pty.next_child_event(), Some(ChildEvent::Exited(_))) {
            // Give both the PTY reader and a socket bridge client a bounded
            // drain window before taking the snapshot. The child can close
            // its bridge connection just before the reader thread delivers
            // the final frame.
            for _ in 0..5 {
                thread::sleep(Duration::from_millis(20));
                apply_available(&mut core, &output_rx, bridge_rx.as_ref(), &mut projection);
            }
            break;
        }
        if Instant::now() >= deadline {
            return Err("headless native terminal timed out waiting for PTY".into());
        }
        thread::sleep(Duration::from_millis(10));
    }

    let mut stdout = io::stdout().lock();
    writeln!(stdout, "{} [{}]", projection.title, projection.status)?;
    if !projection.bridge_status.is_empty() {
        writeln!(stdout, "BRIDGE {}", projection.bridge_status)?;
    }
    for line in core.snapshot() {
        writeln!(stdout, "{line}")?;
    }
    writeln!(stdout, "-- OI PROJECTION --")?;
    if let Some(action) = projection.current_action.as_ref() {
        writeln!(
            stdout,
            "ACTION {} {} {}",
            action.kind, action.target, action.detail
        )?;
    }
    if let Some(request) = projection.model_request.as_ref() {
        writeln!(
            stdout,
            "MODEL REQUEST · {}/{} [{} · {} · {}]",
            if request.provider.is_empty() {
                "—"
            } else {
                &request.provider
            },
            if request.model.is_empty() {
                "—"
            } else {
                &request.model
            },
            if request.role.is_empty() {
                "default"
            } else {
                &request.role
            },
            if request.status.is_empty() {
                "idle"
            } else {
                &request.status
            },
            if request.request_id.is_empty() {
                "—"
            } else {
                &request.request_id
            },
        )?;
    }
    write_projection_tree(&mut stdout, &projection.workspace_tree, 0)?;
    write_projection_tree(&mut stdout, &projection.runtime_tree, 0)?;
    for line in &projection.trace {
        writeln!(stdout, "TRACE {line}")?;
    }
    if !projection.view.label.is_empty() {
        writeln!(
            stdout,
            "VIEW {} [{}]",
            projection.view.label.to_ascii_uppercase(),
            if projection.view.history {
                if projection.view.history_label.is_empty() {
                    "HISTORY"
                } else {
                    &projection.view.history_label
                }
            } else if projection.view.live_label.is_empty() {
                "LIVE"
            } else {
                &projection.view.live_label
            }
        )?;
    }
    for line in projection.oi {
        writeln!(stdout, "{line}")?;
    }
    Ok(())
}

fn write_projection_tree(
    stdout: &mut impl Write,
    nodes: &[ProjectionTreeNode],
    depth: usize,
) -> io::Result<()> {
    for tree in nodes {
        let kind = if tree.kind.is_empty() {
            "node"
        } else {
            &tree.kind
        };
        let status = if tree.status.is_empty() {
            "idle"
        } else {
            &tree.status
        };
        let label = if tree.label.is_empty() {
            &tree.id
        } else {
            &tree.label
        };
        let metadata = if tree.metadata.is_null() {
            String::new()
        } else {
            format!(" {}", tree.metadata)
        };
        writeln!(
            stdout,
            "{}TREE {kind} [{status}] {label}{metadata}",
            "  ".repeat(depth)
        )?;
        write_projection_tree(stdout, &tree.children, depth + 1)?;
    }
    Ok(())
}

#[cfg(test)]
#[path = "tests/main_tests.rs"]
mod tests;
