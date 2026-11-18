# Changelog

What changed in this repo in each Nova release, newest first.

> [!NOTE]
> This file is **generated** by [git-cliff](https://git-cliff.org) from the PR
> titles merged into `dev`, by the `changelog` GitHub Actions workflow. Don't
> edit it by hand: change `cliff.toml` instead. Each release's test, notes and
> the other repos' versions are on the
> [Nova-Collected releases page](https://github.com/UTATRocketry/Nova-Collected/releases).

## [v2026.10.24](https://github.com/UTATRocketry/Nova-Collected/releases/tag/v2026.10.24) - 2026-10-22

### Features

- implement actuator state synchronization and add tests
- **fas_bridge:** implement logger-prefixed file naming for recordings
- **fas_bridge:** implement logger-prefixed file naming for recordings (#12)

### Fixes

- **state:** sync actuator state when the active config changes (#9)
- **tests:** update expected node values in command and sensor tests to fix failing tests
- **config:** reject backslash paths on Linux too

### Documentation

- point contributors at dev clones
- point contributors at dev clones (#6)

### Tests

- add novaMock end-to-end integration test
- add novaMock end-to-end integration test
- add novaMock end-to-end test and fix stale unit tests (#11)

### Chores

- **scripts:** read ops/nova.settings.ps1
- clean up README and add environment variables documentation
- add changelog generation workflow and initial changelog files
- **scripts:** read ops/nova.settings.ps1 (#7)
- add changelog generation workflow and initial changelog files (#8)
- allow 'tests' prefix for branch names in PR checks
- allow 'tests' prefix for branch names in PR checks (#10)
- **github:** default code owner team and release review requests

## [v2026.10.11](https://github.com/UTATRocketry/Nova-Collected/releases/tag/v2026.10.11) - 2026-09-30

### Features

- **config:** **BREAKING** Implement config history and recording metadata management

### Fixes

- **scripts:** update example and parameter name to use mock script

### Chores

- **BREAKING** Add CODEOWNERS, PR template, and CONTRIBUTING guidelines

