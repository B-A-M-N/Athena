# Voice

## Purpose

Speech transcription and synthesis as an interface adapter.

## Ownership

Voice is a presentation/transport adapter only. It does not own sessions,
tasks, or reasoning. It never creates a second task authority and never
decides autonomy.

## Local Contracts

- Cancellation must propagate: an abandoned transcription/synthesis request
  releases its transport resources promptly.
- Privacy: raw audio and transcripts follow the same retention and access
  rules as the session they belong to. Voice may not widen visibility.
- Streaming synthesis must preserve ordering and support client disconnect
  without server-side resource leaks.
- Voice never bypasses canonical event recording: durable effects (such as
  artifact storage) still enter the canonical event/task path.

## Verification

Run voice unit tests plus the repository's architecture lint.
