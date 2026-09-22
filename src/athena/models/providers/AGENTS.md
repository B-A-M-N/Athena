# Model Providers

## Purpose

Provider modules adapt external model wire protocols to Athena's model
contract.

## Ownership

Providers own transport/schema adaptation and provider-local response parsing.

## Local Contracts

- Do not choose Athena policy, task strategy, or global retry behavior.
- Publish normalized metadata needed by model admission.
- Provider calls remain behind the inference/model authority.

## Work Guidance

Keep provider quirks local and test compatibility behavior at the normalized
contract boundary.

## Verification

Run the provider-specific unit tests and model routing tests.

## Child DOX Index

No nested contracts are currently required.
