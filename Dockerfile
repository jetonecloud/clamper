# Clamper in a container, for CI and for people who would rather not install
# a Python toolchain.
#
#   docker run --rm \
#     -v "$PWD:/work:ro" \
#     -v "$HOME/.aws:/home/clamper/.aws:ro" \
#     -e AWS_PROFILE \
#     ghcr.io/jetonecloud/clamper:latest \
#     plan --config /work/clamper.yml
#
# Note the `:ro` on both mounts. Clamper reads your config and your AWS
# profile; it has no reason to write to either, and mounting them read-only
# means you do not have to take our word for it.
#
# What this image can do depends entirely on the credentials you hand it.
# With the operator policy it can stop and start the resources you tagged,
# through `clamper clamp` and `clamper release`; with a read-only role it can
# run `plan`, `status`, `ledger` and `doctor` and nothing else. For CI, give
# it the read-only one. See iam/README.md.

FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# A virtualenv so the final stage can take the whole thing in one COPY,
# without pip, its caches, or the build backend coming along.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
# Only what the build backend reads. Keeping tests and docs out means a docs
# change does not invalidate the layer that installs the dependencies.
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install .


FROM python:3.12-slim

# Set by the release workflow from the tag; only the labels use it, so a
# local `docker build` with no --build-arg still produces a working image.
ARG VERSION=0.0.0-dev
ARG REVISION=unknown

LABEL org.opencontainers.image.title="Clamper" \
      org.opencontainers.image.description="Hard spending limits for AWS: opt-in, declared up front, recorded before acting, reversible with one command." \
      org.opencontainers.image.url="https://github.com/jetonecloud/clamper" \
      org.opencontainers.image.source="https://github.com/jetonecloud/clamper" \
      org.opencontainers.image.documentation="https://github.com/jetonecloud/clamper/blob/main/docs/installation.md" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="Jet1" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=build /opt/venv /opt/venv

# Nothing here needs root, and a tool that holds credentials able to stop
# infrastructure is the last thing that should ask for it. uid 1000 is the
# usual first human account, so a bind-mounted ~/.aws and a writable output
# directory line up without --user on most hosts.
RUN useradd --create-home --uid 1000 clamper \
    && mkdir /out /work \
    && chown clamper:clamper /out

USER clamper

# Where reports land with `--output-dir /out`.
WORKDIR /out

ENTRYPOINT ["clamper"]
CMD ["--help"]
