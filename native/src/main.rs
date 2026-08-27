//! Native Athena terminal vertical slice.
//!
//! This executable owns a real Alacritty-backed PTY.  The first slice keeps
//! the Athena compositor deliberately small, but establishes the important
//! ownership boundary: terminal bytes and input belong to this process,
//! semantic OI content arrives through an explicit serialized projection
//! bridge, and the compositor never decides or executes anything.

use std::env;
use std::io::{self, BufRead, Read, Write};
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::thread;
use std::time::{Duration, Instant};

use alacritty_terminal::event::WindowSize;
use alacritty_terminal::tty::{self, ChildEvent, EventedPty};
use serde::Deserialize;

use athena_terminal::NativeTerminalCore;

#[cfg(unix)]
mod x11;

#[derive(Debug, Default, Deserialize, Clone)]
struct ProjectionFrame {
    title: Option<String>,
    status: Option<String>,
    #[serde(default)]
    oi: Vec<String>,
}

#[derive(Debug, Default, Clone)]
struct Projection {
    title: String,
    status: String,
    oi: Vec<String>,
}

impl Projection {
    fn apply(&mut self, frame: ProjectionFrame) {
        if let Some(title) = frame.title {
            self.title = title;
        }
        if let Some(status) = frame.status {
            self.status = status;
        }
        if !frame.oi.is_empty() {
            self.oi = frame.oi;
        }
    }
}

#[derive(Debug, Default)]
struct Args {
    headless: bool,
    bridge_stdin: bool,
    command: Option<String>,
    columns: usize,
    rows: usize,
}

fn parse_args() -> Result<Args, String> {
    let mut args = Args {
        columns: 100,
        rows: 32,
        ..Args::default()
    };
    let mut values = env::args().skip(1);
    while let Some(arg) = values.next() {
        match arg.as_str() {
            "--headless" => args.headless = true,
            "--bridge-stdin" => args.bridge_stdin = true,
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
            "--help" | "-h" => {
                println!("athena-terminal [--headless] [--bridge-stdin] [--command SHELL_CODE]");
                println!("  --headless       run the PTY/core slice without opening a window");
                println!("  --bridge-stdin   read JSON projection frames from stdin");
                return Err(String::new());
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    args.columns = args.columns.max(1);
    args.rows = args.rows.max(1);
    Ok(args)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = match parse_args() {
        Ok(args) => args,
        Err(error) if error.is_empty() => return Ok(()),
        Err(error) => return Err(error.into()),
    };

    let window_size = window_size(args.columns, args.rows);
    let mut pty_options = tty::Options::default();
    if let Some(command) = args.command {
        pty_options.shell = Some(tty::Shell::new(
            "/bin/sh".to_owned(),
            vec!["-lc".to_owned(), command],
        ));
    }
    let pty = tty::new(&pty_options, window_size, 0)?;
    let reader = pty.file().try_clone()?;
    let (output_tx, output_rx) = mpsc::channel::<Vec<u8>>();
    spawn_pty_reader(reader, output_tx);
    let bridge_rx = if args.bridge_stdin {
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
    };

    if args.headless {
        return run_headless(core, pty, output_rx, bridge_rx, projection);
    }

    #[cfg(unix)]
    {
        return x11::run(core, pty, output_rx, bridge_rx, projection).map_err(|error| error.into());
    }

    #[cfg(not(unix))]
    {
        let _ = (core, pty, output_rx, bridge_rx, projection);
        Err("native window frontend is not implemented on this target yet".into())
    }
}

fn window_size(columns: usize, rows: usize) -> WindowSize {
    WindowSize {
        num_cols: columns.min(u16::MAX as usize) as u16,
        num_lines: rows.min(u16::MAX as usize) as u16,
        cell_width: 9,
        cell_height: 18,
    }
}

fn spawn_pty_reader(mut reader: std::fs::File, output_tx: mpsc::Sender<Vec<u8>>) {
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

fn spawn_projection_reader() -> Receiver<ProjectionFrame> {
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let stdin = io::stdin();
        for line in stdin.lock().lines() {
            let Ok(line) = line else { break };
            if let Ok(frame) = serde_json::from_str::<ProjectionFrame>(&line) {
                if tx.send(frame).is_err() {
                    break;
                }
            }
        }
    });
    rx
}

fn apply_available(
    core: &mut NativeTerminalCore,
    output_rx: &Receiver<Vec<u8>>,
    bridge_rx: Option<&Receiver<ProjectionFrame>>,
    projection: &mut Projection,
) -> bool {
    let mut changed = false;
    loop {
        match output_rx.try_recv() {
            Ok(bytes) => {
                core.feed(&bytes);
                changed = true;
            }
            Err(TryRecvError::Empty | TryRecvError::Disconnected) => break,
        }
    }
    if let Some(bridge_rx) = bridge_rx {
        loop {
            match bridge_rx.try_recv() {
                Ok(frame) => {
                    projection.apply(frame);
                    changed = true;
                }
                Err(TryRecvError::Empty | TryRecvError::Disconnected) => break,
            }
        }
    }
    changed
}

fn run_headless(
    mut core: NativeTerminalCore,
    mut pty: tty::Pty,
    output_rx: Receiver<Vec<u8>>,
    bridge_rx: Option<Receiver<ProjectionFrame>>,
    mut projection: Projection,
) -> Result<(), Box<dyn std::error::Error>> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        apply_available(&mut core, &output_rx, bridge_rx.as_ref(), &mut projection);
        if matches!(pty.next_child_event(), Some(ChildEvent::Exited(_))) {
            // Give the reader a short drain window before taking the snapshot.
            thread::sleep(Duration::from_millis(20));
            apply_available(&mut core, &output_rx, bridge_rx.as_ref(), &mut projection);
            break;
        }
        if Instant::now() >= deadline {
            return Err("headless native terminal timed out waiting for PTY".into());
        }
        thread::sleep(Duration::from_millis(10));
    }

    let mut stdout = io::stdout().lock();
    writeln!(stdout, "{} [{}]", projection.title, projection.status)?;
    for line in core.snapshot() {
        writeln!(stdout, "{line}")?;
    }
    writeln!(stdout, "-- OI PROJECTION --")?;
    for line in projection.oi {
        writeln!(stdout, "{line}")?;
    }
    Ok(())
}
