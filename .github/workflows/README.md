# Reusable Workflow Usage

## code-style-reusable.yml

Sub-repos call this workflow for PR code style checks. No need to duplicate lint logic in each repo.

### Sub-repo setup

Create `.github/workflows/code-style.yml` at repo root:

```yaml
name: PR Code Check

on:
  pull_request:
    branches: [main]

jobs:
  code-style:
    uses: spacemit-robotics/scripts/.github/workflows/code-style-reusable.yml@main
    with:
      repo_path: <path-of-this-repo-in-workspace>
```

### repo_path reference

| Repo | repo_path |
|------|-----------|
| demo-repository | `demo-repository` |
| peripherals-motor | `components/peripherals/motor` |
| peripherals-lidar | `components/peripherals/lidar` |
| peripherals-imu | `components/peripherals/imu` |
| peripherals-key | `components/peripherals/key` |
| peripherals-light-sensor | `components/peripherals/light_sensor` |
| peripherals-misc-io | `components/peripherals/misc_io` |
| peripherals-nfc | `components/peripherals/nfc` |
| audio | `components/multimedia/audio` |
| build | `build` |
| target | `target` |

### Prerequisites

- Merge `code-style-reusable.yml` into **scripts** repo first
- Then add or update `code-style.yml` in each sub-repo
