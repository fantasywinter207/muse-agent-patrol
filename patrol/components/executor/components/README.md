# Executor implementation

## Purpose
Provide the single bounded authorization/send/readback implementation shared by Full Tick and the independent one-shot Outbox CLI.

## Responsibilities
`delivery.py` owns the remote delivery capability, not message generation or scheduling.

## Public boundary
`index.py` exports `ActionSender`.

## Allowed dependencies
Published sibling-component boundaries only.
