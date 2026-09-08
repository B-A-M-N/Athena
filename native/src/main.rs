//! Native Athena terminal vertical slice.
//!
//! This executable owns a real Alacritty-backed PTY.  The first slice keeps
//! the Athena compositor deliberately small, but establishes the important
//! ownership boundary: terminal bytes and input belong to this process,
//! semantic OI content arrives through an explicit serialized projection
//! bridge, and the compositor never decides or executes anything.

use std::collections::VecDeque;
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
use serde::Deserialize;

use athena_terminal::{NativePixelLayout, NativeTerminalCore, PromptLayout, UiFontMetrics};

mod buddy;
mod input;
#[cfg(unix)]
mod x11;

const NATIVE_BRIDGE_SCHEMA_VERSION: u32 = 3;
const LEGACY_NATIVE_BRIDGE_SCHEMA_VERSION: u32 = 2;

fn default_bridge_schema_version() -> u32 {
    LEGACY_NATIVE_BRIDGE_SCHEMA_VERSION
}

#[derive(Debug, Default, Deserialize, Clone)]
struct ProjectionFrame {
    #[serde(default = "default_bridge_schema_version")]
    schema_version: u32,
    title: Option<String>,
    status: Option<String>,
    #[serde(default)]
    self_host_phase: Option<String>,
    semantic_state: Option<String>,
    #[serde(default)]
    oi: Vec<String>,
    #[serde(default)]
    instruments: Vec<serde_json::Value>,
    #[serde(default)]
    system_status: Option<String>,
    #[serde(default)]
    network_status: Option<String>,
    #[serde(default)]
    activity_status: Option<String>,
    #[serde(default)]
    entities: Option<Vec<ProjectionEntity>>,
    #[serde(default)]
    workspace_entities: Option<Vec<ProjectionEntity>>,
    #[serde(default)]
    runtime_entities: Option<Vec<ProjectionEntity>>,
    #[serde(default)]
    alerts: Option<Vec<String>>,
    #[serde(default)]
    attention_items: Option<Vec<ProjectionAttention>>,
    #[serde(default)]
    active_operation: Option<ProjectionOperation>,
    #[serde(default)]
    current_action: Option<ProjectionAction>,
    #[serde(default)]
    code_view: Option<ProjectionCodeView>,
    #[serde(default)]
    diagnostics: Option<Vec<ProjectionDiagnostic>>,
    #[serde(default)]
    verification: Option<ProjectionVerification>,
    #[serde(default)]
    progress: Option<serde_json::Value>,
    #[serde(default)]
    buddy: Option<ProjectionBuddy>,
    #[serde(default)]
    model_request: Option<ProjectionModelRequest>,
    #[serde(default)]
    workspace_tree: Option<Vec<ProjectionTreeNode>>,
    #[serde(default)]
    runtime_tree: Option<Vec<ProjectionTreeNode>>,
    #[serde(default)]
    trace: Option<Vec<String>>,
    #[serde(default)]
    stream_tail: Option<Vec<String>>,
    #[serde(default)]
    layout: Option<serde_json::Value>,
    #[serde(default)]
    view: Option<ProjectionView>,
    #[serde(default)]
    navigation: Option<ProjectionNavigation>,
}

#[derive(Debug, Default, Deserialize, Clone)]
struct ProjectionNavigation {
    #[serde(default)]
    pane: String,
    #[serde(default)]
    direction: String,
    #[serde(default)]
    amount: i32,
}

#[derive(Debug, Default, Deserialize, Clone)]
#[allow(dead_code)]
struct ProjectionAction {
    #[serde(default)]
    kind: String,
    #[serde(default)]
    label: String,
    #[serde(default)]
    target: String,
    #[serde(default)]
    detail: String,
    #[serde(default)]
    query: String,
    #[serde(default)]
    progress: String,
    #[serde(default)]
    progress_value: Option<f64>,
    #[serde(default)]
    progress_determinate: bool,
}

impl ProjectionFrame {
    fn normalize(mut self) -> Result<Self, String> {
        match self.schema_version {
            NATIVE_BRIDGE_SCHEMA_VERSION => Ok(self),
            LEGACY_NATIVE_BRIDGE_SCHEMA_VERSION => {
                self.schema_version = NATIVE_BRIDGE_SCHEMA_VERSION;
                Ok(self)
            }
            version => Err(format!(
                "unsupported native projection schema {version}; expected {NATIVE_BRIDGE_SCHEMA_VERSION}"
            )),
        }
    }
}

#[derive(Debug, Default, Deserialize, Clone)]
struct ProjectionEntity {
    id: String,
    #[serde(default)]
    kind: String,
    #[serde(default)]
    label: String,
    #[serde(default)]
    status: String,
    #[serde(default)]
    parent_id: Option<String>,
    #[serde(default)]
    metadata: serde_json::Value,
}

#[derive(Debug, Default, Deserialize, Clone)]
struct ProjectionAttention {
    #[serde(default)]
    id: String,
    #[serde(default)]
    approval_id: String,
    #[serde(default)]
    kind: String,
    #[serde(default)]
    severity: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    summary: String,
    #[serde(default)]
    requires_action: bool,
    #[serde(default)]
    scopes: Vec<String>,
    #[serde(default)]
    related_object_id: Option<String>,
}

#[derive(Debug, Default, Deserialize, Clone)]
#[allow(dead_code)]
struct ProjectionOperation {
    #[serde(default)]
    id: String,
    #[serde(default)]
    capability: String,
    #[serde(default)]
    label: String,
    #[serde(default)]
    operation: String,
    #[serde(default)]
    command: String,
    #[serde(default)]
    target: String,
    #[serde(default)]
    state: String,
    #[serde(default)]
    action_kind: String,
    #[serde(default)]
    mutation_state: String,
    #[serde(default)]
    progress: String,
    #[serde(default)]
    progress_value: Option<f64>,
    #[serde(default)]
    progress_determinate: bool,
}

#[derive(Debug, Default, Deserialize, Clone)]
#[allow(dead_code)]
struct ProjectionCodeView {
    #[serde(default)]
    path: String,
    #[serde(default)]
    language: String,
    #[serde(default)]
    text: String,
    #[serde(default)]
    lines: Vec<String>,
    #[serde(default)]
    diff: Vec<String>,
    #[serde(default)]
    mutation_state: String,
    #[serde(default)]
    preview_truncated: bool,
}

#[derive(Debug, Default, Deserialize, Clone)]
#[allow(dead_code)]
struct ProjectionDiagnostic {
    #[serde(default)]
    path: String,
    #[serde(default)]
    line: Option<i64>,
    #[serde(default)]
    message: String,
    #[serde(default)]
    detail: String,
    #[serde(default)]
    expected: Option<serde_json::Value>,
    #[serde(default)]
    actual: Option<serde_json::Value>,
    #[serde(default)]
    severity: String,
}

#[derive(Debug, Default, Deserialize, Clone)]
struct ProjectionModelRequest {
    #[serde(default)]
    provider: String,
    #[serde(default)]
    model: String,
    #[serde(default)]
    role: String,
    #[serde(default)]
    request_id: String,
    #[serde(default)]
    status: String,
}

#[derive(Debug, Default, Deserialize, Clone)]
struct ProjectionTreeNode {
    #[serde(default)]
    id: String,
    #[serde(default)]
    kind: String,
    #[serde(default)]
    label: String,
    #[serde(default)]
    status: String,
    #[serde(default)]
    children: Vec<ProjectionTreeNode>,
    #[serde(default)]
    metadata: serde_json::Value,
}

#[derive(Debug, Default, Deserialize, Clone)]
struct ProjectionView {
    #[serde(default)]
    label: String,
    #[serde(default)]
    mode: String,
    #[serde(default)]
    history: bool,
    #[serde(default)]
    history_label: String,
    #[serde(default)]
    live_label: String,
}

#[derive(Debug, Default, Deserialize, Clone)]
struct ProjectionVerification {
    #[serde(default)]
    status: String,
    #[serde(default)]
    checks: Vec<serde_json::Value>,
}

#[derive(Debug, Default, Deserialize, Clone)]
struct ProjectionBuddy {
    #[serde(default)]
    state: String,
    #[serde(default)]
    anchor: String,
    #[serde(default)]
    status: String,
    #[serde(default)]
    character: String,
}

#[derive(Debug, Default, Clone)]
struct Projection {
    bridge_status: String,
    bridge_generation: u64,
    last_frame_sequence: u64,
    last_frame_at: Option<Instant>,
    stale: bool,
    title: String,
    status: String,
    self_host_phase: String,
    semantic_state: String,
    oi: Vec<String>,
    oi_history: VecDeque<Vec<String>>,
    history_index: Option<usize>,
    instruments: Vec<serde_json::Value>,
    system_status: String,
    network_status: String,
    activity_status: String,
    entities: Vec<ProjectionEntity>,
    workspace_entities: Vec<ProjectionEntity>,
    runtime_entities: Vec<ProjectionEntity>,
    alerts: Vec<String>,
    attention_items: Vec<ProjectionAttention>,
    attention_page: usize,
    active_operation: Option<ProjectionOperation>,
    current_action: Option<ProjectionAction>,
    code_view: Option<ProjectionCodeView>,
    diagnostics: Vec<ProjectionDiagnostic>,
    verification: ProjectionVerification,
    progress: Option<serde_json::Value>,
    buddy: Option<ProjectionBuddy>,
    model_request: Option<ProjectionModelRequest>,
    workspace_tree: Vec<ProjectionTreeNode>,
    runtime_tree: Vec<ProjectionTreeNode>,
    trace: Vec<String>,
    stream_tail: Vec<String>,
    layout: Option<serde_json::Value>,
    view: ProjectionView,
    animation: AnimationState,
}

#[derive(Debug, Default, Clone)]
struct AnimationState {
    key: String,
    entered_at_sequence: u64,
    elapsed: f32,
    transition_progress: f32,
    cursor_phase: f32,
    scan_phase: f32,
    pulse_phase: f32,
    grid_phase: f32,
    code_reveal: f32,
    activity_phase: f32,
}

impl AnimationState {
    fn observe(&mut self, key: String, frame_sequence: u64) {
        if key == self.key {
            return;
        }
        self.key = key;
        self.entered_at_sequence = frame_sequence;
        self.elapsed = 0.0;
        self.transition_progress = 0.0;
        self.cursor_phase = 0.0;
        self.scan_phase = 0.0;
        self.pulse_phase = 0.0;
        self.grid_phase = 0.0;
        self.code_reveal = 0.0;
        self.activity_phase = 0.0;
    }

    fn advance(&mut self, delta_seconds: f32) {
        if !delta_seconds.is_finite() || delta_seconds <= 0.0 {
            return;
        }
        self.elapsed += delta_seconds;
        self.transition_progress = (self.elapsed / 0.42).clamp(0.0, 1.0);
        self.cursor_phase = (self.cursor_phase + delta_seconds * 1.10).fract();
        self.scan_phase = (self.scan_phase + delta_seconds * 0.70).fract();
        self.pulse_phase = (self.pulse_phase + delta_seconds * 0.90).fract();
        self.grid_phase = (self.grid_phase + delta_seconds * 0.35).fract();
        self.code_reveal = (self.code_reveal + delta_seconds / 0.70).clamp(0.0, 1.0);
        self.activity_phase = (self.activity_phase + delta_seconds * 0.42).fract();
    }

    fn channel(&self, mode: VisualMode, fallback: f32) -> f32 {
        if self.key.is_empty() {
            return fallback;
        }
        match mode {
            VisualMode::Approval | VisualMode::Failure => self.transition_progress,
            VisualMode::Read => self.cursor_phase,
            VisualMode::Search => self.scan_phase,
            VisualMode::Think => self.pulse_phase,
            VisualMode::Code => self.code_reveal,
            VisualMode::Execute | VisualMode::Generate | VisualMode::Recover => self.activity_phase,
            _ => self.elapsed,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum VisualMode {
    Idle,
    Think,
    Respond,
    Inspect,
    Read,
    Search,
    Code,
    Execute,
    Test,
    Verify,
    Generate,
    Approval,
    Recover,
    Failure,
    Success,
}

impl VisualMode {
    fn as_str(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Think => "think",
            Self::Respond => "respond",
            Self::Inspect => "inspect",
            Self::Read => "read",
            Self::Search => "search",
            Self::Code => "code",
            Self::Execute => "execute",
            Self::Test => "test",
            Self::Verify => "verify",
            Self::Generate => "generate",
            Self::Approval => "approval",
            Self::Recover => "recover",
            Self::Failure => "failure",
            Self::Success => "success",
        }
    }

    fn from_projection(projection: &Projection) -> Self {
        let raw = if !projection.view.mode.is_empty() {
            projection.view.mode.as_str()
        } else if !projection.semantic_state.is_empty() {
            projection.semantic_state.as_str()
        } else {
            projection
                .buddy
                .as_ref()
                .map(|buddy| buddy.state.as_str())
                .unwrap_or("")
        };
        match raw.to_ascii_lowercase().as_str() {
            "think" | "thinking" => Self::Think,
            "respond" => Self::Respond,
            "inspect" => Self::Inspect,
            "read" | "reading" => Self::Read,
            "search" | "searching" => Self::Search,
            "code" | "coding" => Self::Code,
            "execute" | "executing" | "tools" | "working" => Self::Execute,
            "test" | "testing" => Self::Test,
            "verify" | "verifying" => Self::Verify,
            "generate" | "generating" => Self::Generate,
            "approval" => Self::Approval,
            "recover" | "recovery" | "recovering" => Self::Recover,
            "failure" | "blocked" => Self::Failure,
            "success" | "successful" | "complete" | "completed" => Self::Success,
            _ => Self::Idle,
        }
    }

    fn is_active(self) -> bool {
        !matches!(
            self,
            Self::Idle | Self::Failure | Self::Approval | Self::Success
        )
    }

    fn is_animated(self, projection: &Projection) -> bool {
        match self {
            Self::Idle | Self::Success => false,
            // Approval and failure are entrance/settle animations. Once the
            // decision surface has settled, a static frame prevents a denied
            // or failed task from looking like it is still progressing.
            Self::Approval => projection.animation.elapsed < 0.42,
            Self::Failure => projection.animation.elapsed < 0.36,
            _ => true,
        }
    }

    fn prompt_state(self, projection: &Projection) -> &'static str {
        let status = projection.status.to_ascii_lowercase();
        if projection.bridge_status.starts_with("ERROR") || status.contains("disconnect") {
            "DISCONNECTED"
        } else if matches!(self, Self::Failure) || status.contains("fail") || status == "blocked" {
            "FAILURE"
        } else if matches!(self, Self::Approval) || status.contains("approval") {
            "APPROVAL"
        } else if self.is_active() || status.contains("work") || status.contains("execut") {
            "WORKING"
        } else {
            "READY"
        }
    }
}

#[allow(dead_code)]
impl Projection {
    fn apply(&mut self, frame: ProjectionFrame) {
        let animation_key = frame_animation_key(&frame);
        self.animation
            .observe(animation_key, self.last_frame_sequence);
        self.bridge_status = "CONNECTED".to_owned();
        self.stale = false;
        self.last_frame_sequence = self.last_frame_sequence.saturating_add(1);
        self.last_frame_at = Some(Instant::now());
        if let Some(title) = frame.title {
            self.title = title;
        }
        if let Some(status) = frame.status {
            self.status = status;
        }
        if let Some(self_host_phase) = frame.self_host_phase {
            self.self_host_phase = self_host_phase;
        }
        if let Some(semantic_state) = frame.semantic_state {
            self.semantic_state = semantic_state;
        }
        if !frame.oi.is_empty() {
            if self.oi != frame.oi {
                self.oi_history.push_back(frame.oi.clone());
                while self.oi_history.len() > 32 {
                    self.oi_history.pop_front();
                }
                self.history_index = None;
            }
            self.oi = frame.oi;
        }
        self.instruments = frame.instruments;
        if let Some(system_status) = frame.system_status {
            self.system_status = system_status;
        }
        if let Some(network_status) = frame.network_status {
            self.network_status = network_status;
        }
        if let Some(activity_status) = frame.activity_status {
            self.activity_status = activity_status;
        }
        if let Some(entities) = frame.entities {
            self.entities = entities;
        }
        self.workspace_entities = frame.workspace_entities.unwrap_or_default();
        self.runtime_entities = frame.runtime_entities.unwrap_or_default();
        if let Some(alerts) = frame.alerts {
            self.alerts = alerts;
        }
        self.attention_items = frame.attention_items.unwrap_or_default();
        self.active_operation = frame.active_operation;
        self.current_action = frame.current_action;
        self.code_view = frame.code_view;
        self.diagnostics = frame.diagnostics.unwrap_or_default();
        self.verification = frame.verification.unwrap_or_default();
        self.progress = frame.progress;
        self.buddy = frame.buddy;
        self.model_request = frame.model_request;
        self.workspace_tree = frame.workspace_tree.unwrap_or_default();
        self.runtime_tree = frame.runtime_tree.unwrap_or_default();
        self.trace = frame.trace.unwrap_or_default();
        self.stream_tail = frame.stream_tail.unwrap_or_default();
        self.layout = frame.layout;
        self.view = frame.view.unwrap_or_default();
        if let Some(navigation) = frame.navigation {
            self.apply_navigation(navigation);
        }
    }

    fn bridge_error(&mut self, error: String) {
        self.bridge_status = format!("ERROR: {error}");
        self.stale = true;
    }

    fn bridge_lifecycle(&mut self, status: &str) {
        match status {
            "CONNECTING" => self.stale = true,
            "CONNECTED" if self.bridge_generation == 0 => self.bridge_generation = 1,
            "RECONNECTING" => {
                self.bridge_generation = self.bridge_generation.saturating_add(1);
                self.stale = true;
            }
            "DISCONNECTED" | "ERROR" => self.stale = true,
            _ => {}
        }
        if status == "ERROR" {
            self.bridge_status = "ERROR".to_owned();
        } else {
            self.bridge_status = status.to_owned();
        }
    }

    fn last_frame_age_ms(&self) -> Option<u128> {
        self.last_frame_at.map(|at| at.elapsed().as_millis())
    }

    fn advance_animation(&mut self, delta_seconds: f32) {
        self.animation.advance(delta_seconds);
    }

    fn apply_navigation(&mut self, navigation: ProjectionNavigation) {
        let pane = navigation.pane.to_ascii_lowercase();
        if !matches!(pane.as_str(), "oi" | "right" | "history") {
            return;
        }
        let direction = navigation.direction.to_ascii_lowercase();
        match direction.as_str() {
            "bottom" | "live" => {
                self.return_to_live_oi();
            }
            "up" => {
                for _ in 0..navigation.amount.clamp(1, 32) {
                    self.cycle_oi_history(-1);
                }
            }
            "down" => {
                for _ in 0..navigation.amount.clamp(1, 32) {
                    self.cycle_oi_history(1);
                }
            }
            _ => {}
        }
    }

    fn animation_phase(&self, fallback: f32) -> f32 {
        self.animation
            .channel(VisualMode::from_projection(self), fallback)
    }

    fn cycle_oi_history(&mut self, delta: i32) -> bool {
        let len = self.oi_history.len();
        if len <= 1 {
            return false;
        }
        let live = len as i32;
        // The live view is positioned immediately after the newest retained
        // frame. One wheel step toward history should therefore reveal the
        // previous frame, while one step toward live returns to the stream.
        let current = self
            .history_index
            .map_or((len - 1) as i32, |index| index as i32);
        let next = (current + delta).rem_euclid(live + 1);
        self.history_index = (next < live).then_some(next as usize);
        true
    }

    fn return_to_live_oi(&mut self) -> bool {
        let changed = self.history_index.is_some();
        self.history_index = None;
        changed
    }

    fn display_oi(&self) -> &[String] {
        self.history_index
            .and_then(|index| self.oi_history.get(index).map(Vec::as_slice))
            .unwrap_or(&self.oi)
    }

    fn navigation_value(&self) -> f32 {
        if let Some(index) = self.history_index {
            let count = self.oi_history.len().saturating_sub(1).max(1);
            return index as f32 / count as f32;
        }
        let page_count = self.attention_items.len().div_ceil(3).max(1);
        if page_count > 1 {
            return self.attention_page.min(page_count - 1) as f32 / (page_count - 1) as f32;
        }
        0.5
    }
}

fn frame_animation_key(frame: &ProjectionFrame) -> String {
    let operation = frame.active_operation.as_ref();
    let view = frame.view.as_ref();
    format!(
        "{}|{}|{}|{}|{}|{}|{}|{}",
        frame.semantic_state.as_deref().unwrap_or_default(),
        view.map(|item| item.mode.as_str()).unwrap_or_default(),
        view.map(|item| item.history).unwrap_or_default(),
        operation.map(|item| item.id.as_str()).unwrap_or_default(),
        operation
            .map(|item| item.state.as_str())
            .unwrap_or_default(),
        operation
            .map(|item| item.mutation_state.as_str())
            .unwrap_or_default(),
        frame
            .verification
            .as_ref()
            .map(|item| item.status.as_str())
            .unwrap_or_default(),
        frame
            .current_action
            .as_ref()
            .map(|item| item.kind.as_str())
            .unwrap_or_default(),
    )
}

#[derive(Debug, Default)]
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
            .unwrap_or(1.0),
        ..Args::default()
    };
    let mut values = values.into_iter();
    while let Some(arg) = values.next() {
        match arg.as_str() {
            "--headless" => args.headless = true,
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
                    "athena-terminal [--headless] [--dump-layout] [--cabinet-only] [--bridge-stdin|--bridge-socket PATH] [--command SHELL_CODE] [--mascot owl|cat|bot|off] [--no-animations] [--reduced-motion] [--text-scale MULTIPLIER]"
                );
                println!("  --headless       run the PTY/core slice without opening a window");
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
                serde_json::json!([16, 17, 13, 11]),
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
mod tests {
    use super::{
        LEGACY_NATIVE_BRIDGE_SCHEMA_VERSION, NATIVE_BRIDGE_SCHEMA_VERSION, Projection,
        ProjectionFrame, ProjectionNavigation, VisualMode,
    };

    #[test]
    fn bridge_schema_accepts_current_and_normalizes_legacy() {
        let current = ProjectionFrame {
            schema_version: NATIVE_BRIDGE_SCHEMA_VERSION,
            ..ProjectionFrame::default()
        }
        .normalize()
        .expect("current schema should be accepted");
        assert_eq!(current.schema_version, NATIVE_BRIDGE_SCHEMA_VERSION);

        let legacy = ProjectionFrame {
            schema_version: LEGACY_NATIVE_BRIDGE_SCHEMA_VERSION,
            ..ProjectionFrame::default()
        }
        .normalize()
        .expect("legacy schema should be normalized");
        assert_eq!(legacy.schema_version, NATIVE_BRIDGE_SCHEMA_VERSION);
    }

    #[test]
    fn bridge_schema_rejects_unknown_versions_as_visible_errors() {
        let frame = ProjectionFrame {
            schema_version: 999,
            ..ProjectionFrame::default()
        };
        assert!(frame.normalize().is_err());
    }

    #[test]
    fn cabinet_only_is_a_parseable_deterministic_render_mode() {
        let args = super::parse_args_from(vec!["--cabinet-only".to_owned()])
            .expect("cabinet-only should parse");
        assert!(args.cabinet_only);
        assert!(args.animations);
    }

    #[test]
    fn visual_mode_covers_the_shared_action_vocabulary() {
        for value in [
            "idle", "think", "respond", "inspect", "read", "search", "code", "execute", "test",
            "verify", "generate", "approval", "recover", "failure", "success",
        ] {
            let projection = Projection {
                semantic_state: value.to_owned(),
                ..Projection::default()
            };
            assert_eq!(VisualMode::from_projection(&projection).as_str(), value);
        }
        let thinking = Projection {
            semantic_state: "thinking".to_owned(),
            ..Projection::default()
        };
        assert_eq!(VisualMode::from_projection(&thinking).as_str(), "think");
    }

    #[test]
    fn bridge_preserves_structured_scene_state() {
        let frame: ProjectionFrame = serde_json::from_str(
            r#"{
                "status":"EXECUTING",
                "self_host_phase":"REFEREE",
                "attention_items":[{"id":"approval:1","kind":"approval","severity":"warning","title":"APPROVAL REQUIRED","summary":"write workspace","requires_action":true}],
                "entities":[
                    {"id":"call-1","kind":"operation","label":"executor","status":"active"}
                ],
                "alerts":["test pulse"],
                "model_request":{"provider":"openrouter","model":"configured/model","role":"planner","request_id":"req-1","status":"active"},
                "workspace_tree":[{"id":"workspace:src","kind":"directory","label":"src","status":"active","children":[]}],
                "runtime_tree":[{"id":"call-1","kind":"operation","label":"executor","status":"active","children":[]}],
                "trace":["> NEXT STEP · executor"],
                "view":{"label":"action","mode":"execute","history":false,"history_label":"OI // HISTORY","live_label":"OI // LIVE"}
            }"#,
        )
        .expect("projection JSON should decode");
        let mut projection = Projection::default();
        projection.apply(frame);

        assert_eq!(projection.status, "EXECUTING");
        assert_eq!(projection.self_host_phase, "REFEREE");
        assert_eq!(projection.entities.len(), 1);
        assert_eq!(projection.entities[0].label, "executor");
        assert_eq!(projection.alerts, vec!["test pulse"]);
        assert_eq!(projection.attention_items.len(), 1);
        assert!(projection.attention_items[0].requires_action);
        assert_eq!(
            projection
                .model_request
                .as_ref()
                .expect("model request")
                .request_id,
            "req-1"
        );
        assert_eq!(projection.workspace_tree[0].label, "src");
        assert_eq!(projection.runtime_tree[0].label, "executor");
        assert_eq!(projection.trace, vec!["> NEXT STEP · executor"]);
        assert_eq!(projection.view.label, "action");
        assert!(!projection.view.history);
        assert!(projection.last_frame_age_ms().is_some());
    }

    #[test]
    fn animation_state_is_keyed_and_advances_deterministically() {
        let mut projection = Projection::default();
        projection.apply(ProjectionFrame {
            semantic_state: Some("think".to_owned()),
            ..ProjectionFrame::default()
        });
        projection.advance_animation(0.10);
        assert_eq!(projection.animation.entered_at_sequence, 0);
        assert!((projection.animation.elapsed - 0.10).abs() < f32::EPSILON);
        assert!(projection.animation.pulse_phase > 0.0);

        projection.apply(ProjectionFrame {
            semantic_state: Some("think".to_owned()),
            ..ProjectionFrame::default()
        });
        assert!((projection.animation.elapsed - 0.10).abs() < f32::EPSILON);

        projection.apply(ProjectionFrame {
            semantic_state: Some("search".to_owned()),
            ..ProjectionFrame::default()
        });
        assert_eq!(projection.animation.entered_at_sequence, 2);
        assert_eq!(projection.animation.elapsed, 0.0);
        projection.advance_animation(0.10);
        assert!((projection.animation.channel(VisualMode::Search, 0.0) - 0.07).abs() < 0.0001);
    }

    #[test]
    fn dagoal_temporal_modes_and_transitions_have_explicit_settle_contract() {
        let modes = [
            "idle", "think", "search", "read", "code", "execute", "test", "verify", "approval",
            "failure", "success",
        ];
        let mut projection = Projection::default();
        for (sequence, mode) in modes.into_iter().enumerate() {
            projection.apply(ProjectionFrame {
                semantic_state: Some(mode.to_owned()),
                ..ProjectionFrame::default()
            });
            let visual = VisualMode::from_projection(&projection);
            assert_eq!(visual.as_str(), mode);
            assert_eq!(projection.animation.entered_at_sequence, sequence as u64);
            assert_eq!(projection.animation.elapsed, 0.0);
            projection.advance_animation(0.10);
            assert!(projection.animation.channel(visual, 0.0) > 0.0);
            if matches!(visual, VisualMode::Approval | VisualMode::Failure) {
                projection.advance_animation(0.50);
                assert_eq!(projection.animation.transition_progress, 1.0);
                assert!(!visual.is_animated(&projection));
            }
            if matches!(visual, VisualMode::Idle | VisualMode::Success) {
                assert!(!visual.is_animated(&projection));
            }
        }

        for (from, to) in [
            ("search", "read"),
            ("read", "code"),
            ("code", "test"),
            ("test", "failure"),
            ("failure", "recover"),
            ("verify", "success"),
            ("execute", "approval"),
            ("approval", "execute"),
        ] {
            projection.apply(ProjectionFrame {
                semantic_state: Some(from.to_owned()),
                ..ProjectionFrame::default()
            });
            projection.advance_animation(0.12);
            let prior_key = projection.animation.key.clone();
            projection.apply(ProjectionFrame {
                semantic_state: Some(to.to_owned()),
                ..ProjectionFrame::default()
            });
            assert_ne!(projection.animation.key, prior_key);
            assert_eq!(projection.animation.elapsed, 0.0);
        }

        let no_animations = super::parse_args_from(vec!["--no-animations".to_owned()])
            .expect("no-animations should parse");
        assert!(!no_animations.animations);
        let reduced_motion = super::parse_args_from(vec!["--reduced-motion".to_owned()])
            .expect("reduced-motion should parse");
        assert!(reduced_motion.reduced_motion);
        let zoomed = super::parse_args_from(vec!["--text-scale".to_owned(), "1.25".to_owned()])
            .expect("text-scale should parse");
        assert!((zoomed.text_scale - 1.25).abs() < f32::EPSILON);
        assert!(super::parse_args_from(vec!["--text-scale".to_owned(), "0.5".to_owned()]).is_err());
    }

    #[test]
    fn bridge_lifecycle_marks_stale_and_advances_generation() {
        let mut projection = Projection::default();
        projection.bridge_lifecycle("CONNECTING");
        assert!(projection.stale);
        projection.bridge_lifecycle("CONNECTED");
        assert_eq!(projection.bridge_status, "CONNECTED");
        assert!(projection.stale);
        assert_eq!(projection.bridge_generation, 1);

        projection.apply(ProjectionFrame::default());
        assert!(!projection.stale);

        projection.bridge_lifecycle("RECONNECTING");
        assert!(projection.stale);
        assert_eq!(projection.bridge_generation, 2);

        projection.bridge_error("socket closed".to_owned());
        assert!(projection.stale);
        assert!(projection.bridge_status.starts_with("ERROR:"));
    }

    #[test]
    fn oi_history_is_bounded_and_encoder_navigation_is_independent_of_focus() {
        let mut projection = Projection::default();
        for index in 0..34 {
            projection.apply(ProjectionFrame {
                oi: vec![format!("frame-{index}")],
                ..ProjectionFrame::default()
            });
        }
        assert_eq!(projection.oi_history.len(), 32);
        assert_eq!(projection.display_oi(), ["frame-33".to_owned()].as_slice());
        assert!(projection.cycle_oi_history(-1));
        assert_eq!(projection.display_oi(), ["frame-32".to_owned()].as_slice());
        assert!(projection.navigation_value() < 1.0);
        assert!(projection.return_to_live_oi());
        assert_eq!(projection.display_oi(), ["frame-33".to_owned()].as_slice());
    }

    #[test]
    fn bridge_navigation_control_moves_retained_oi_history() {
        let mut projection = Projection::default();
        projection.apply(ProjectionFrame {
            oi: vec!["frame-1".to_owned()],
            ..ProjectionFrame::default()
        });
        projection.apply(ProjectionFrame {
            oi: vec!["frame-2".to_owned()],
            ..ProjectionFrame::default()
        });
        projection.apply(ProjectionFrame {
            navigation: Some(ProjectionNavigation {
                pane: "oi".to_owned(),
                direction: "up".to_owned(),
                amount: 1,
            }),
            ..ProjectionFrame::default()
        });

        assert_eq!(projection.display_oi(), ["frame-1".to_owned()].as_slice());
        projection.apply(ProjectionFrame {
            navigation: Some(ProjectionNavigation {
                pane: "oi".to_owned(),
                direction: "bottom".to_owned(),
                amount: 1,
            }),
            ..ProjectionFrame::default()
        });
        assert_eq!(projection.display_oi(), ["frame-2".to_owned()].as_slice());
    }
}

#[test]
fn bridge_buddy_character_survives_deserialize_and_apply() {
    let frame: ProjectionFrame = serde_json::from_str(
        r#"{
                "status":"IDLE",
                "buddy":{"state":"IDLE","anchor":"center","status":"ready","character":"owl"}
            }"#,
    )
    .expect("buddy JSON should decode");
    let mut projection = Projection::default();
    projection.apply(frame);

    let buddy = projection.buddy.as_ref().expect("buddy should be present");
    assert_eq!(buddy.character, "owl");
    assert_eq!(buddy.state, "IDLE");
    assert_eq!(buddy.anchor, "center");
    assert_eq!(buddy.status, "ready");
}

#[test]
fn bridge_buddy_character_fills_missing_default() {
    let frame: ProjectionFrame =
        serde_json::from_str(r#"{"status":"IDLE"}"#).expect("minimal JSON should decode");
    let mut projection = Projection::default();
    projection.apply(frame);

    assert!(projection.buddy.is_none());
}

#[test]
fn bridge_model_request_preserves_canonical_identity() {
    let frame: ProjectionFrame = serde_json::from_str(
        r#"{
                "model_request":{
                    "provider":"openrouter",
                    "model":"qwen3.6-35b",
                    "role":"planner",
                    "request_id":"req-canon-1",
                    "status":"active"
                }
            }"#,
    )
    .expect("model request JSON should decode");
    let mut projection = Projection::default();
    projection.apply(frame);

    let mr = projection.model_request.as_ref().expect("model request");
    assert_eq!(mr.provider, "openrouter");
    assert_eq!(mr.model, "qwen3.6-35b");
    assert_eq!(mr.request_id, "req-canon-1");
}
