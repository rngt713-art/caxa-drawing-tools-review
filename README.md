# CAXA Drawing Tools - Public Review Snapshot

This repository is a sanitized public snapshot prepared for architecture and
interaction review. Internal server addresses, personal names, credentials,
workstation paths, binaries, and captured CAXA network data have been removed.

The uploader combines batch PDFs and secondary-item relationships in one
hierarchical tree. Dragging an item over a possible parent highlights that row;
releasing the mouse clears the highlight and creates the relationship.

Useful files:

- `app/upload_main.py`: Tk hierarchy, drag behavior, row editing
- `app/upload_core.py`: staged upload, recovery, workflow gating
- `app/caxa/uploader.py`: CAXA document and relation operations
- `app/caxa/caxa_helper/caxa_helper.cs`: native helper
- `tests/acceptance/ui_acceptance.py`: real Tk UI checks
- `docs/ARCHITECTURE_REVIEW.md`: architecture and risk summary
- `docs/VALIDATION_20261106.md`: verification scope

The private production repository is intentionally kept separate so its commit
history and internal environment details are not published.
