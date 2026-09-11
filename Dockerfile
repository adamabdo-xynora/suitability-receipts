# syntax=docker/dockerfile:1
#
# suitability-receipts — a test image with a job, and a runtime image that says
# plainly what little it is for.
#
# Two things this file is for, and how to check each:
#
#   The suite, offline, inside the image (all 334 tests, no credential):
#     docker build --target test -t suitability-receipts:test .
#     docker run --rm suitability-receipts:test uv run pytest
#     docker run --rm suitability-receipts:test uv run python -m eval
#     docker run --rm suitability-receipts:test uv run ruff check .
#     docker run --rm suitability-receipts:test uv run mypy --strict src tests
#
#   The library, as a lean runtime image (the default target):
#     docker build -t suitability-receipts .
#
# The base is python:3.12-slim, not python:3.12 and not a different minor.
# pyproject.toml sets requires-python = "==3.12.*" and uv.lock is resolved
# against exactly that, so the interpreter in the image is the interpreter the
# lockfile was solved for — the same one uv provisions locally and in
# .github/workflows/ci.yml. UV_PYTHON_DOWNLOADS=never below makes that
# load-bearing rather than incidental: uv uses the image's Python or fails
# loudly, it never quietly fetches a second one to satisfy the constraint.
#
# The lockfile, and why every install here is --locked
# ----------------------------------------------------
# uv.lock is committed, and `uv sync --locked` fails rather than updating it. A
# plain `uv sync` would resolve afresh when the lock and the manifest disagree,
# which means the image could be built from a dependency set that exists nowhere
# else — not in a developer's .venv, not in CI, not in the lock. Every sync in
# this file passes --locked, so a build either reproduces the committed
# resolution or stops.
#
# Secrets, and why none of them are in here
# -----------------------------------------
# ANTHROPIC_API_KEY is read in exactly one module,
# src/suitability_receipts/llm/client.py, and that is not a docstring promise:
# tests/test_llm_boundary.py parses every module under src/ and fails if any
# other file so much as imports `os` or `anthropic`. A Dockerfile is a second
# route into a process, so it keeps the same discipline — there is no ARG for a
# key, no ENV, no default, no file copied in that could hold one. An ARG or ENV
# secret survives in the image history; a COPYed .env is readable by anyone who
# can pull the image. .dockerignore closes the route at the other end: .env and
# .env.* never enter the build context, so no COPY — not the ones written here,
# and not one a later edit widens — can pick one up.
#
# A credential arrives one way only: `docker run -e ANTHROPIC_API_KEY ...`, read
# by the same single line of code CI would use. And nothing in the test stage
# needs one. The determination rules are pure functions over documented data:
# the seven rules, the models, the rationale verifier and the eval set all run
# with no credential present, which is why the suite below is run offline with
# no key and passes 334 tests. Only the two modules that call a model need one,
# and their tests supply a stub client rather than a key.

ARG PYTHON_IMAGE=python:3.12-slim
ARG UV_VERSION=0.12.8

# ---------------------------------------------------------------------------
# base: the interpreter and uv. No project files yet.
#
# uv arrives as a binary copied out of the `uv` stage above, pinned by the
# UV_VERSION argument to the version installed locally (0.12.8), rather than
# `pip install uv` — one fewer resolution happening at build time, and a pin
# means the tool that reads the lockfile is as fixed as the lockfile itself. CI
# uses astral-sh/setup-uv, which tracks the latest release; this pin is
# stricter, not looser.
#
# UV_PROJECT_ENVIRONMENT and the PATH entry put /app/.venv where uv already
# wants it and then make it the default interpreter, so `python3` inside any of
# these images is the project's Python and not the base image's bare one.
# ---------------------------------------------------------------------------
# uv, at the pinned version, as a stage of its own. `COPY --from=` does not
# expand build arguments — a global ARG reaches FROM and nowhere else — so the
# pin lives on a FROM line and the binary is copied out of the resulting stage.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM ${PYTHON_IMAGE} AS base
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"
WORKDIR /app

# ---------------------------------------------------------------------------
# deps: the dependency closure the lockfile pins, cached on the lockfile alone.
#
# Only pyproject.toml and uv.lock are copied, so this layer is invalidated when
# a dependency changes and reused when only source does — editing engine.py does
# not reinstall pytest. --no-install-project is what makes that possible: the
# project itself cannot be installed yet because src/ is deliberately not here.
# It is installed in the stage below, after the source arrives.
#
# Dev dependencies are included: pytest, hypothesis, mypy, ruff and mutmut. This
# stage is a toolchain, and the runtime stage does not build on it.
# ---------------------------------------------------------------------------
FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project

# ---------------------------------------------------------------------------
# test: the toolchain plus the whole repository the checks read.
#
# All three trees are needed, not just src/. tests/test_eval_set.py imports
# eval/ and asserts the case set's shape, tests/test_llm_boundary.py opens every
# file under src/ and parses it, and `uv run python -m eval` runs the 35 cases
# against the engine. README.md is copied for a duller reason: pyproject.toml
# names it in `readme`, so hatchling reads it when it builds the project's
# metadata and the install fails without it. That is also why .dockerignore
# leaves README.md in the build context when it excludes the rest of the prose.
#
# The second `uv sync --locked` installs the project into the environment the
# deps layer built. It is nearly free — every third-party wheel is already there
# — and it is what makes `python -m eval` able to import suitability_receipts
# without a PYTHONPATH trick.
#
# ruff and mypy run at build time, in the order ci.yml runs them, so a
# `--target test` build that succeeds has already proved lint and types. The
# suite and the eval set are left to `docker run`, so their output is something
# a reader sees rather than something buried in a build log. Those two commands
# leave .mypy_cache and .ruff_cache in /app, and they are left there on purpose:
# a reader who runs `uv run mypy --strict src tests` against this image gets a
# warm cache rather than a cold 4-second run, and mypy validates its own cache
# against the source it is given.
#
# Nothing here reaches the network or reads a credential. UV_NO_SYNC below is
# what guarantees the first half of that at run time: `uv run` would otherwise
# re-check the environment against the lockfile on every invocation, so the
# image would appear to work and then fail the moment it was run without a
# route to PyPI. With it set, `uv run pytest` uses the environment already
# built and nothing else — verified with `docker run --network none`.
# ---------------------------------------------------------------------------
FROM deps AS test
COPY README.md ./
COPY src/ src/
COPY tests/ tests/
COPY eval/ eval/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked
ENV UV_NO_SYNC=1
RUN uv run ruff check . \
    && uv run ruff format --check . \
    && uv run mypy --strict
CMD ["uv", "run", "pytest"]

# ---------------------------------------------------------------------------
# prod-deps: the same lockfile minus the dev group, built to be thrown away.
#
# This stage exists so that the runtime image below can copy a finished
# environment instead of building one. What that buys is what is *absent* from
# the final image, and the amounts are measured rather than assumed: the uv
# binary is 45MB, the dev toolchain is the difference between a 187MB
# environment and a 32MB one, and the source tree does not need to be there at
# all. A `FROM base AS runtime` that ran its own --no-dev sync would be one
# stage shorter and would ship uv to no purpose, since nothing in the runtime
# image resolves a dependency.
#
# --no-editable is the reason src/ can be left behind. An editable install would
# put a path pointer in site-packages and leave the runtime image depending on
# /app/src being present; --no-editable builds the wheel and installs it, so the
# package lives in site-packages as ordinary files. src/ and README.md are bind
# mounts rather than COPYs for the same reason: hatchling needs them to build
# the wheel, and a bind mount makes them available to this one command without
# committing them to a layer.
# ---------------------------------------------------------------------------
FROM base AS prod-deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=src,target=src \
    --mount=type=bind,source=README.md,target=README.md \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------------------
# runtime: the interpreter, the library, its two dependencies, and no CMD.
#
# What this image is for, stated plainly, because it is less than a reader will
# expect from a default target: there is no long-running process in this
# repository. No server, no worker, no queue, no `[project.scripts]` entry point
# in pyproject.toml. `determine` is a pure function that takes a client profile
# and a recommendation and returns a determination or a refusal; the application
# that would call it in anger is not in this repository. So the honest use of
# this image is one thing — importing the rules:
#
#   docker run --rm suitability-receipts \
#     python -c "from suitability_receipts import determine; print(determine)"
#
# and, secondarily, being the base layer for a service that embeds them. The
# venv is on PATH, so the `python3` command inherited from python:3.12-slim
# lands in an interpreter that can import the package; that inherited CMD is
# left exactly as it is. Nothing else is invented for it. A
# `CMD ["python", "-c", "import suitability_receipts"]` would be an import check
# wearing the costume of a purpose, and a service that does not exist cannot be
# started by naming it here.
#
# What it cannot do, and this is the point of the split:
#   * It cannot run the tests. pytest, hypothesis, mypy, ruff and mutmut are all
#     in the dev group, `--no-dev` leaves every one of them out, and tests/ is
#     never copied into this stage.
#   * It cannot run the eval set. eval/ is not part of the wheel — pyproject
#     packages src/suitability_receipts and nothing else — so `python -m eval`
#     finds no module here. The test image is where the 35 cases run.
#   * It cannot call a model, because it holds no credential and no way to
#     acquire one. `client_from_environment` raises MissingApiKeyError unless
#     ANTHROPIC_API_KEY is in the environment at run time, and nothing in this
#     file puts it there.
#
# If what you want from a container is evidence that the rules hold, the `test`
# target is the image that provides it. That one has a job, and the command that
# runs it is the first one at the top of this file.
#
# It runs as a non-root user it creates: nothing in the library writes to disk,
# so there is no directory that needs to be writable and no reason for the
# process to be able to write one. PYTHONDONTWRITEBYTECODE is the same thought
# — the wheels were byte-compiled during the sync, so there is nothing left to
# write and the interpreter should not try.
#
# Measured: 253MB, of which 216MB is python:3.12-slim itself and 33MB is the
# environment. The test image is 562MB, and every megabyte of the difference is
# a tool that has no business in a runtime image.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=prod-deps /app/.venv /app/.venv
RUN useradd --create-home --uid 10001 receipts
USER receipts
