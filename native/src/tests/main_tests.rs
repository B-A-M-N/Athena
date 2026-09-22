use super::{
    LEGACY_NATIVE_BRIDGE_SCHEMA_VERSION, NATIVE_BRIDGE_SCHEMA_VERSION,
    PREVIOUS_NATIVE_BRIDGE_SCHEMA_VERSION, Projection, ProjectionFailure, ProjectionFrame,
    ProjectionNavigation, VisualMode,
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
fn bridge_schema_accepts_previous_compatible_version_explicitly() {
    let previous = ProjectionFrame {
        schema_version: PREVIOUS_NATIVE_BRIDGE_SCHEMA_VERSION,
        ..ProjectionFrame::default()
    }
    .normalize()
    .expect("previous compatible schema should be normalized");
    assert_eq!(previous.schema_version, NATIVE_BRIDGE_SCHEMA_VERSION);
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
fn animation_identity_survives_operation_state_churn() {
    let base = ProjectionFrame {
        semantic_state: Some("execute".to_owned()),
        active_operation: Some(super::ProjectionOperation {
            id: "op-1".to_owned(),
            state: "requested".to_owned(),
            ..super::ProjectionOperation::default()
        }),
        ..ProjectionFrame::default()
    };
    let same_action = ProjectionFrame {
        active_operation: Some(super::ProjectionOperation {
            state: "running".to_owned(),
            mutation_state: "applied".to_owned(),
            ..base.active_operation.clone().expect("operation")
        }),
        verification: Some(super::ProjectionVerification {
            status: "running".to_owned(),
            checks: Vec::new(),
        }),
        ..base.clone()
    };
    assert_eq!(
        super::frame_animation_key(&base),
        super::frame_animation_key(&same_action)
    );

    let changed_action = ProjectionFrame {
        active_operation: Some(super::ProjectionOperation {
            id: "op-2".to_owned(),
            ..base.active_operation.expect("operation")
        }),
        ..base
    };
    assert_eq!(
        super::frame_animation_key(&same_action),
        super::frame_animation_key(&changed_action)
    );
}

#[test]
fn animation_identity_survives_mode_changes_within_the_same_family() {
    let execute = ProjectionFrame {
        semantic_state: Some("execute".to_owned()),
        ..ProjectionFrame::default()
    };
    let verify = ProjectionFrame {
        semantic_state: Some("verify".to_owned()),
        ..ProjectionFrame::default()
    };
    assert_eq!(
        super::frame_animation_key(&execute),
        super::frame_animation_key(&verify)
    );
}

#[test]
fn failure_reason_is_projected_for_truthful_status() {
    let frame = ProjectionFrame {
        failure: Some(ProjectionFailure {
            reason: "no eligible model: context capacity unknown".to_owned(),
            stage: "model_routing".to_owned(),
            kind: "model_routing".to_owned(),
        }),
        ..ProjectionFrame::default()
    };
    let mut projection = Projection::default();
    projection.apply(frame);
    assert_eq!(projection.failure.kind, "model_routing");
    assert!(
        projection
            .failure
            .reason
            .contains("context capacity unknown")
    );
}

#[test]
fn bridge_preserves_structured_scene_state() {
    let frame: ProjectionFrame = serde_json::from_str(
            r#"{
                "status":"EXECUTING",
                "conversation":[{"id":1,"role":"user","text":"inspect workspace"},{"id":2,"role":"assistant","text":"done"}],
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
    assert_eq!(projection.conversation.len(), 2);
    assert_eq!(projection.conversation[0].role, "user");
    assert_eq!(projection.conversation[1].text, "done");
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
    let mut expected_reset = 0_u64;
    let mut last_key = String::new();
    let mut previous_key = String::new();
    for mode in modes {
        let frame = ProjectionFrame {
            semantic_state: Some(mode.to_owned()),
            ..ProjectionFrame::default()
        };
        let key = super::frame_animation_key(&frame);
        if last_key.is_empty() || key != last_key {
            expected_reset = projection.last_frame_sequence;
            last_key = key.clone();
        }
        projection.apply(frame);
        let visual = VisualMode::from_projection(&projection);
        assert_eq!(visual.as_str(), mode);
        if key != previous_key {
            assert_eq!(projection.animation.entered_at_sequence, expected_reset);
            assert_eq!(projection.animation.elapsed, 0.0);
        }
        previous_key = key;
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
        if super::frame_animation_key(&ProjectionFrame {
            semantic_state: Some(from.to_owned()),
            ..ProjectionFrame::default()
        }) != super::frame_animation_key(&ProjectionFrame {
            semantic_state: Some(to.to_owned()),
            ..ProjectionFrame::default()
        }) {
            assert_ne!(projection.animation.key, prior_key);
            assert_eq!(projection.animation.elapsed, 0.0);
        } else {
            assert_eq!(projection.animation.key, prior_key);
            assert!(projection.animation.elapsed > 0.0);
        }
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
