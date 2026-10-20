# Contributing to novaOps-back

**The rules for all Nova repos are in
[Nova-Collected/CONTRIBUTING.md](https://github.com/UTATRocketry/Nova-Collected/blob/main/CONTRIBUTING.md).
Read that first.** In short:

- `dev` collects finished work for hardware testing; `main` is what prod runs.
- Branch from `dev` as `feature/...`, `bugfix/...` or `chore/...`, e.g. `bugfix/fas-port-reconnect`.
- Open a PR **into `dev`**, titled `type(scope): what changed`, and fill in the template.
- CI green + 1 approval (a lead's if it can move hardware), then **Squash and merge**.
- Leads bring tested releases into `main`; you never merge into `main` yourself.

**Where to work:** clone this repo into the `dev/` folder of a Nova-Collected
clone, e.g. `Nova/dev/backend` (see "Getting started as a developer" in the
Nova-Collected README), or anywhere else you like. Branch from `dev` there.
Never work in the `prod/` or `pi/` submodules of Nova-Collected: they show the
released version and are overwritten when it changes.

This file only covers what is specific to this repo.

## Before you open a PR

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

CI runs the same tests on every PR.

## Simulator Test (level 2)

- `tools/novaMock.py`, `tools/nova_dummy.py`, `tools/novaSystem_dummy.py`:
  simulated novaGround and FAS traffic over MQTT
- `tools/sim_control.html`: drive the simulators from a browser

Then go through the
[testing procedure](https://github.com/UTATRocketry/Nova-Collected/blob/main/docs/development/testing-procedure.md)
against them.

## Scopes for PR titles

`api`, `config`, `mqtt`, `fas` (the bridge), `commands`, `lock`, `roles`,
`data`, `ws`, `sound`

## Things to know

- `config/*.yaml` are the **published defaults**, not the station's live
  config. The station edits its own copy from the web interface. Change these
  files only to change the defaults, in a PR of their own.
- `tools/fas_bridge.py` re-implements the FAS wire protocol from the firmware
  repo's `gs/protocol.py`. A change there needs a matching change here; link
  both PRs and mark the title `!`.
