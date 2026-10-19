<!--
PRs go into `dev`. Only release/* and hotfix/* branches go into `main`.
Title: type(scope): what changed    e.g.  fix(fas): reopen the COM port after unplug
Rules: https://github.com/UTATRocketry/Nova-Collected/blob/main/CONTRIBUTING.md
-->

## What and why

<!-- One or two sentences. Link the issue if there is one: "Closes #12". -->

## How was this tested?

<!-- Tick the highest level reached (CONTRIBUTING.md section 7), then describe
     what you did and what you saw. -->

- [ ] 1 Unit / Automated Test - CI
- [ ] 2 Simulator Test - testing procedure with the hardware simulators
- [ ] 3 Bench Test - testing procedure on the dev stack, with some real hardware
- [ ] 4 Integration Test - full engine test procedure, real hardware, no propellants

<!-- What you did and what you saw: -->

## Hardware impact

- [ ] None - UI, docs or tooling only
- [ ] Changes how a sensor is read, converted or shown
- [ ] Changes how an actuator is commanded, or safety rules, interlocks, timeouts or sequencing → add the `hardware-risk` label; a lead must approve
- [ ] Changes a message format, MQTT topic or FAS protocol that another repo depends on → title needs `!`, link the matching PRs:

<!-- Linked PRs in other repos: -->

## Checklist

- [ ] Targets `dev` (or `main` for a release/hotfix, or with a reason given above)
- [ ] Branch is up to date ("Update branch" button)
- [ ] No station config/calibrations, data files, secrets, `.env` files or build output committed
- [ ] README/docs updated if behaviour changed
