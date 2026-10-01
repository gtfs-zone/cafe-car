# Contributing to rt.gtfs.zone

## Commit Message Guidelines

This project uses [Conventional Commits](https://www.conventionalcommits.org/) to automate versioning and changelog generation.

### Using Commitizen

Instead of `git commit`, use:

```bash
cz commit
```

This will prompt you to fill out the commit message following the conventional format. `git commit` also works, as long as the message is valid.

`cz` is [Commitizen](https://commitizen-tools.github.io/commitizen/). Install it with:

```bash
uv tool install commitizen
```

### Commit Message Format

Each commit message consists of a **type**, an optional **scope**, and a **subject**:

```
<type>(<scope>): <subject>
```

#### Types

- `feat`: A new feature (triggers minor version bump)
- `fix`: A bug fix (triggers patch version bump)
- `docs`: Documentation only changes
- `style`: Changes that don't affect the meaning of the code (white-space, formatting, etc)
- `refactor`: A code change that neither fixes a bug nor adds a feature
- `perf`: A performance improvement
- `test`: Adding missing tests or correcting existing tests
- `build`: Changes that affect the build system or external dependencies
- `ci`: Changes to CI configuration files and scripts
- `chore`: Other changes that don't modify src or test files
- `revert`: Reverts a previous commit

#### Breaking Changes

Add `BREAKING CHANGE:` in the commit body or add `!` after the type/scope to trigger a major version bump:

```bash
feat!: remove support for old API
```

### Examples

```bash
feat(alerts): add an informed entity filter
fix(ingest): reject positions with no timestamp
docs: update installation instructions
refactor: simplify the config loader
```

## Versioning

This project uses [Commitizen](https://commitizen-tools.github.io/commitizen/) (`cz`) for versioning and changelog generation.

### Creating Releases

When you're ready to release, run on `main`:

```bash
cz bump
```

This will:
- Bump the version in `pyproject.toml` based on commit history
- Update `CHANGELOG.md`
- Create a git tag

Then push the commit and the tag:

```bash
git push --follow-tags origin main
```

Pushing a version tag triggers `.github/workflows/build.yml`, which builds the image and pushes it to `ghcr.io/gtfs-zone/gtfs-zone-rt-api:vX.Y.Z`. [gtfs-zone-infra](https://github.com/gtfs-zone/gtfs-zone-infra) pins that tag; bumping the pin there is what deploys it.

## Development Workflow

1. Create a feature branch from `main`
2. Make your changes
3. Commit using `cz commit` (this ensures proper commit format)
4. Push your branch and create a pull request
5. After merge to `main`, run `cz bump` for releases when ready

## Changing gtfs-zone-db-models

The ORM models and Alembic migrations live in [gtfs-zone-db-models](https://github.com/gtfs-zone/gtfs-zone-db-models) and are not edited here. A schema change is:

1. A commit in gtfs-zone-db-models
2. A new tag in gtfs-zone-db-models, pushed to GitHub
3. A bump of the `gtfs-zone-db-models` tag in `pyproject.toml` here (and in each other consumer), then `uv lock`

## Git Hooks

Hooks are defined in `.pre-commit-config.yaml` and run by [prek](https://github.com/j178/prek). Install them once per clone:

```bash
uv tool install prek
prek install
```

- **pre-commit**: Runs the repo's checks
- **commit-msg**: Validates commit message format using commitizen

If your commit message doesn't follow the conventional format, the commit will be rejected with a helpful error message.
