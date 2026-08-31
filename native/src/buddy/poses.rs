#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum BuddyPose {
    Idle,
    Listening,
    Thinking,
    Inspecting,
    Searching,
    Reading,
    Coding,
    Executing,
    Testing,
    Verifying,
    Approval,
    Success,
    Failure,
    Recovering,
}

pub(crate) const REQUIRED_POSES: [BuddyPose; 14] = [
    BuddyPose::Idle,
    BuddyPose::Listening,
    BuddyPose::Thinking,
    BuddyPose::Inspecting,
    BuddyPose::Searching,
    BuddyPose::Reading,
    BuddyPose::Coding,
    BuddyPose::Executing,
    BuddyPose::Testing,
    BuddyPose::Verifying,
    BuddyPose::Approval,
    BuddyPose::Success,
    BuddyPose::Failure,
    BuddyPose::Recovering,
];

pub(crate) fn pose_for_state(state: &str) -> BuddyPose {
    match state.to_ascii_lowercase().as_str() {
        "listen" | "listening" => BuddyPose::Listening,
        "think" | "thinking" => BuddyPose::Thinking,
        "inspect" | "inspecting" => BuddyPose::Inspecting,
        "search" | "searching" => BuddyPose::Searching,
        "read" | "reading" => BuddyPose::Reading,
        "code" | "coding" => BuddyPose::Coding,
        "execute" | "executing" | "working" => BuddyPose::Executing,
        "test" | "testing" => BuddyPose::Testing,
        "verify" | "verifying" => BuddyPose::Verifying,
        "approval" => BuddyPose::Approval,
        "success" | "complete" => BuddyPose::Success,
        "failure" | "blocked" => BuddyPose::Failure,
        "recover" | "recovering" => BuddyPose::Recovering,
        _ => BuddyPose::Idle,
    }
}
