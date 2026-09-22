//! Projection bridge DTO schema and animation state.
//!
//! These types define the stable wire contract between Python and the native
//! binary. Keep this module versioned; do not import Python business rules.

use std::collections::VecDeque;
use std::time::Instant;

use serde::Deserialize;

pub(crate) const NATIVE_BRIDGE_SCHEMA_VERSION: u32 = 5;
pub(crate) const PREVIOUS_NATIVE_BRIDGE_SCHEMA_VERSION: u32 = 4;
pub(crate) const LEGACY_NATIVE_BRIDGE_SCHEMA_VERSION: u32 = 2;

pub(crate) fn default_bridge_schema_version() -> u32 {
    LEGACY_NATIVE_BRIDGE_SCHEMA_VERSION
}

#[derive(Debug, Default, Deserialize, Clone)]
pub(crate) struct ProjectionFrame {
    #[serde(default = "default_bridge_schema_version")]
    pub(crate) schema_version: u32,
    pub(crate) title: Option<String>,
    pub(crate) status: Option<String>,
    #[serde(default)]
    pub(crate) conversation: Vec<ProjectionMessage>,
    #[serde(default)]
    pub(crate) self_host_phase: Option<String>,
    #[serde(default)]
    pub(crate) visual_mode: Option<String>,
    pub(crate) semantic_state: Option<String>,
    #[serde(default)]
    pub(crate) oi: Vec<String>,
    #[serde(default)]
    pub(crate) instruments: Vec<serde_json::Value>,
    #[serde(default)]
    pub(crate) system_status: Option<String>,
    #[serde(default)]
    pub(crate) network_status: Option<String>,
    #[serde(default)]
    pub(crate) activity_status: Option<String>,
    #[serde(default)]
    pub(crate) entities: Option<Vec<ProjectionEntity>>,
    #[serde(default)]
    pub(crate) workspace_entities: Option<Vec<ProjectionEntity>>,
    #[serde(default)]
    pub(crate) runtime_entities: Option<Vec<ProjectionEntity>>,
    #[serde(default)]
    pub(crate) alerts: Option<Vec<String>>,
    #[serde(default)]
    pub(crate) attention_items: Option<Vec<ProjectionAttention>>,
    #[serde(default)]
    pub(crate) active_operation: Option<ProjectionOperation>,
    #[serde(default)]
    pub(crate) current_action: Option<ProjectionAction>,
    #[serde(default)]
    pub(crate) code_view: Option<ProjectionCodeView>,
    #[serde(default)]
    pub(crate) diagnostics: Option<Vec<ProjectionDiagnostic>>,
    #[serde(default)]
    pub(crate) verification: Option<ProjectionVerification>,
    #[serde(default)]
    pub(crate) progress: Option<serde_json::Value>,
    #[serde(default)]
    pub(crate) buddy: Option<ProjectionBuddy>,
    #[serde(default)]
    pub(crate) model_request: Option<ProjectionModelRequest>,
    #[serde(default)]
    pub(crate) workspace_tree: Option<Vec<ProjectionTreeNode>>,
    #[serde(default)]
    pub(crate) runtime_tree: Option<Vec<ProjectionTreeNode>>,
    #[serde(default)]
    pub(crate) trace: Option<Vec<String>>,
    #[serde(default)]
    pub(crate) stream_tail: Option<Vec<String>>,
    #[serde(default)]
    pub(crate) failure: Option<ProjectionFailure>,
    #[serde(default)]
    pub(crate) layout: Option<serde_json::Value>,
    #[serde(default)]
    pub(crate) view: Option<ProjectionView>,
    #[serde(default)]
    pub(crate) navigation: Option<ProjectionNavigation>,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub(crate) struct ProjectionMessage {
    // Deliberate bridge-compatibility data: the projection DTO still carries
    // upstream message identity even though presentation order is authoritative.
    #[allow(dead_code)]
    pub(crate) id: u64,
    #[serde(default)]
    pub(crate) role: String,
    #[serde(default)]
    pub(crate) text: String,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub(crate) struct ProjectionNavigation {
    #[serde(default)]
    pub(crate) pane: String,
    #[serde(default)]
    pub(crate) direction: String,
    #[serde(default)]
    pub(crate) amount: i32,
}

#[derive(Debug, Default, Deserialize, Clone)]
#[allow(dead_code)]
pub(crate) struct ProjectionAction {
    #[serde(default)]
    pub(crate) kind: String,
    #[serde(default)]
    pub(crate) label: String,
    #[serde(default)]
    pub(crate) target: String,
    #[serde(default)]
    pub(crate) detail: String,
    #[serde(default)]
    pub(crate) query: String,
    #[serde(default)]
    pub(crate) progress: String,
    #[serde(default)]
    pub(crate) progress_value: Option<f64>,
    #[serde(default)]
    pub(crate) progress_determinate: bool,
}

impl ProjectionFrame {
    pub(crate) fn normalize(mut self) -> Result<Self, String> {
        match self.schema_version {
            NATIVE_BRIDGE_SCHEMA_VERSION => Ok(self),
            PREVIOUS_NATIVE_BRIDGE_SCHEMA_VERSION => {
                // v4 has defaults for the v5 additions, so normalize it
                // explicitly rather than treating arbitrary old frames as
                // current.
                self.schema_version = NATIVE_BRIDGE_SCHEMA_VERSION;
                Ok(self)
            }
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
#[allow(dead_code)]
pub(crate) struct ProjectionEntity {
    pub(crate) id: String,
    #[serde(default)]
    pub(crate) kind: String,
    #[serde(default)]
    pub(crate) label: String,
    #[serde(default)]
    pub(crate) status: String,
    #[serde(default)]
    pub(crate) parent_id: Option<String>,
    #[serde(default)]
    pub(crate) metadata: serde_json::Value,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub(crate) struct ProjectionAttention {
    #[serde(default)]
    pub(crate) id: String,
    #[serde(default)]
    pub(crate) approval_id: String,
    #[serde(default)]
    pub(crate) kind: String,
    #[serde(default)]
    pub(crate) severity: String,
    #[serde(default)]
    pub(crate) title: String,
    #[serde(default)]
    pub(crate) summary: String,
    #[serde(default)]
    pub(crate) requires_action: bool,
    #[serde(default)]
    pub(crate) scopes: Vec<String>,
    #[serde(default)]
    pub(crate) related_object_id: Option<String>,
}

#[derive(Debug, Default, Deserialize, Clone)]
#[allow(dead_code)]
pub(crate) struct ProjectionOperation {
    #[serde(default)]
    pub(crate) id: String,
    #[serde(default)]
    pub(crate) capability: String,
    #[serde(default)]
    pub(crate) label: String,
    #[serde(default)]
    pub(crate) operation: String,
    #[serde(default)]
    pub(crate) command: String,
    #[serde(default)]
    pub(crate) target: String,
    #[serde(default)]
    pub(crate) state: String,
    #[serde(default)]
    pub(crate) action_kind: String,
    #[serde(default)]
    pub(crate) mutation_state: String,
    #[serde(default)]
    pub(crate) progress: String,
    #[serde(default)]
    pub(crate) progress_value: Option<f64>,
    #[serde(default)]
    pub(crate) progress_determinate: bool,
}

#[derive(Debug, Default, Deserialize, Clone)]
#[allow(dead_code)]
pub(crate) struct ProjectionCodeView {
    #[serde(default)]
    pub(crate) path: String,
    #[serde(default)]
    pub(crate) language: String,
    #[serde(default)]
    pub(crate) text: String,
    #[serde(default)]
    pub(crate) lines: Vec<String>,
    #[serde(default)]
    pub(crate) diff: Vec<String>,
    #[serde(default)]
    pub(crate) mutation_state: String,
    #[serde(default)]
    pub(crate) preview_truncated: bool,
}

#[derive(Debug, Default, Deserialize, Clone)]
#[allow(dead_code)]
pub(crate) struct ProjectionDiagnostic {
    #[serde(default)]
    pub(crate) path: String,
    #[serde(default)]
    pub(crate) line: Option<i64>,
    #[serde(default)]
    pub(crate) message: String,
    #[serde(default)]
    pub(crate) detail: String,
    #[serde(default)]
    pub(crate) expected: Option<serde_json::Value>,
    #[serde(default)]
    pub(crate) actual: Option<serde_json::Value>,
    #[serde(default)]
    pub(crate) severity: String,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub(crate) struct ProjectionModelRequest {
    #[serde(default)]
    pub(crate) provider: String,
    #[serde(default)]
    pub(crate) model: String,
    #[serde(default)]
    pub(crate) role: String,
    #[serde(default)]
    pub(crate) request_id: String,
    #[serde(default)]
    pub(crate) status: String,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub(crate) struct ProjectionTreeNode {
    #[serde(default)]
    pub(crate) id: String,
    #[serde(default)]
    pub(crate) kind: String,
    #[serde(default)]
    pub(crate) label: String,
    #[serde(default)]
    pub(crate) status: String,
    #[serde(default)]
    pub(crate) children: Vec<ProjectionTreeNode>,
    #[serde(default)]
    pub(crate) metadata: serde_json::Value,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub(crate) struct ProjectionView {
    #[serde(default)]
    pub(crate) label: String,
    #[serde(default)]
    pub(crate) history: bool,
    #[serde(default)]
    pub(crate) history_label: String,
    #[serde(default)]
    pub(crate) live_label: String,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub(crate) struct ProjectionVerification {
    #[serde(default)]
    pub(crate) status: String,
    #[serde(default)]
    pub(crate) checks: Vec<serde_json::Value>,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub(crate) struct ProjectionFailure {
    #[serde(default)]
    pub(crate) reason: String,
    #[serde(default)]
    pub(crate) stage: String,
    #[serde(default)]
    pub(crate) kind: String,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub(crate) struct ProjectionBuddy {
    #[serde(default)]
    pub(crate) state: String,
    #[serde(default)]
    pub(crate) anchor: String,
    #[serde(default)]
    pub(crate) status: String,
    #[serde(default)]
    pub(crate) character: String,
}

#[derive(Debug, Default, Clone)]
pub(crate) struct Projection {
    pub(crate) bridge_status: String,
    pub(crate) bridge_generation: u64,
    pub(crate) last_frame_sequence: u64,
    pub(crate) last_frame_at: Option<Instant>,
    pub(crate) stale: bool,
    pub(crate) title: String,
    pub(crate) status: String,
    pub(crate) self_host_phase: String,
    pub(crate) visual_mode: String,
    pub(crate) semantic_state: String,
    pub(crate) oi: Vec<String>,
    pub(crate) oi_history: VecDeque<Vec<String>>,
    pub(crate) history_index: Option<usize>,
    pub(crate) instruments: Vec<serde_json::Value>,
    pub(crate) system_status: String,
    pub(crate) network_status: String,
    pub(crate) activity_status: String,
    pub(crate) entities: Vec<ProjectionEntity>,
    pub(crate) workspace_entities: Vec<ProjectionEntity>,
    pub(crate) runtime_entities: Vec<ProjectionEntity>,
    pub(crate) alerts: Vec<String>,
    pub(crate) attention_items: Vec<ProjectionAttention>,
    pub(crate) attention_page: usize,
    pub(crate) active_operation: Option<ProjectionOperation>,
    pub(crate) current_action: Option<ProjectionAction>,
    pub(crate) code_view: Option<ProjectionCodeView>,
    pub(crate) diagnostics: Vec<ProjectionDiagnostic>,
    pub(crate) verification: ProjectionVerification,
    pub(crate) progress: Option<serde_json::Value>,
    pub(crate) buddy: Option<ProjectionBuddy>,
    pub(crate) model_request: Option<ProjectionModelRequest>,
    pub(crate) workspace_tree: Vec<ProjectionTreeNode>,
    pub(crate) runtime_tree: Vec<ProjectionTreeNode>,
    pub(crate) trace: Vec<String>,
    pub(crate) stream_tail: Vec<String>,
    pub(crate) failure: ProjectionFailure,
    pub(crate) conversation: Vec<ProjectionMessage>,
    pub(crate) layout: Option<serde_json::Value>,
    pub(crate) view: ProjectionView,
    pub(crate) animation: AnimationState,
}

#[derive(Debug, Default, Clone)]
pub(crate) struct AnimationState {
    pub(crate) key: String,
    pub(crate) entered_at_sequence: u64,
    pub(crate) elapsed: f32,
    pub(crate) transition_progress: f32,
    pub(crate) cursor_phase: f32,
    pub(crate) scan_phase: f32,
    pub(crate) pulse_phase: f32,
    pub(crate) grid_phase: f32,
    pub(crate) code_reveal: f32,
    pub(crate) activity_phase: f32,
}

impl AnimationState {
    pub(crate) fn observe(&mut self, key: String, frame_sequence: u64) {
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

    pub(crate) fn advance(&mut self, delta_seconds: f32) {
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

    pub(crate) fn channel(&self, mode: VisualMode, fallback: f32) -> f32 {
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
pub(crate) enum VisualMode {
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
    pub(crate) fn as_str(self) -> &'static str {
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

    pub(crate) fn from_projection(projection: &Projection) -> Self {
        // `visual_mode` is selected by Python's canonical projection. The
        // semantic_state fallback exists only for legacy fixtures/frames.
        let raw = if !projection.visual_mode.is_empty() {
            projection.visual_mode.as_str()
        } else {
            projection.semantic_state.as_str()
        };
        Self::from_raw(raw)
    }

    pub(crate) fn from_raw(raw: &str) -> Self {
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

    pub(crate) fn is_active(self) -> bool {
        !matches!(
            self,
            Self::Idle | Self::Failure | Self::Approval | Self::Success
        )
    }

    pub(crate) fn is_animated(self, projection: &Projection) -> bool {
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

    pub(crate) fn prompt_state(self, projection: &Projection) -> &'static str {
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
    pub(crate) fn apply(&mut self, frame: ProjectionFrame) {
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
        self.conversation = frame.conversation;
        if let Some(self_host_phase) = frame.self_host_phase {
            self.self_host_phase = self_host_phase;
        }
        if let Some(visual_mode) = frame.visual_mode {
            self.visual_mode = visual_mode;
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
        self.failure = frame.failure.unwrap_or_default();
        self.layout = frame.layout;
        self.view = frame.view.unwrap_or_default();
        if let Some(navigation) = frame.navigation {
            self.apply_navigation(navigation);
        }
    }

    pub(crate) fn bridge_error(&mut self, error: String) {
        self.bridge_status = format!("ERROR: {error}");
        self.stale = true;
    }

    pub(crate) fn bridge_lifecycle(&mut self, status: &str) {
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

    pub(crate) fn last_frame_age_ms(&self) -> Option<u128> {
        self.last_frame_at.map(|at| at.elapsed().as_millis())
    }

    pub(crate) fn advance_animation(&mut self, delta_seconds: f32) {
        self.animation.advance(delta_seconds);
    }

    pub(crate) fn apply_navigation(&mut self, navigation: ProjectionNavigation) {
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

    pub(crate) fn animation_phase(&self, fallback: f32) -> f32 {
        self.animation
            .channel(VisualMode::from_projection(self), fallback)
    }

    pub(crate) fn cycle_oi_history(&mut self, delta: i32) -> bool {
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

    pub(crate) fn return_to_live_oi(&mut self) -> bool {
        let changed = self.history_index.is_some();
        self.history_index = None;
        changed
    }

    pub(crate) fn display_oi(&self) -> &[String] {
        self.history_index
            .and_then(|index| self.oi_history.get(index).map(Vec::as_slice))
            .unwrap_or(&self.oi)
    }

    pub(crate) fn navigation_value(&self) -> f32 {
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum VisualFamily {
    Idle,
    Cognition,
    Research,
    Work,
    Approval,
    Failure,
    Recovery,
    Success,
}

impl VisualMode {
    pub(crate) fn family(self) -> VisualFamily {
        match self {
            Self::Idle => VisualFamily::Idle,
            Self::Think | Self::Respond => VisualFamily::Cognition,
            Self::Inspect | Self::Search | Self::Read => VisualFamily::Research,
            Self::Code | Self::Execute | Self::Test | Self::Verify | Self::Generate => {
                VisualFamily::Work
            }
            Self::Approval => VisualFamily::Approval,
            Self::Failure => VisualFamily::Failure,
            Self::Recover => VisualFamily::Recovery,
            Self::Success => VisualFamily::Success,
        }
    }
}

pub(crate) fn frame_animation_key(frame: &ProjectionFrame) -> String {
    let mode = VisualMode::from_raw(
        frame
            .visual_mode
            .as_deref()
            .filter(|value| !value.is_empty())
            .or(frame.semantic_state.as_deref())
            .unwrap_or_default(),
    );
    // The global animation is one continuous family episode. Operation IDs
    // change as a task moves between code, execute, test, and verify; they
    // must not restart the scene's shared motion state.
    format!("{:?}", mode.family())
}
