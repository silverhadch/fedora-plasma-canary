ARG BASE_IMAGE="ghcr.io/silverhadch/fedora-plasma-canary-kinoite:latest"

FROM scratch AS ctx
COPY build_files /

FROM ${BASE_IMAGE}

RUN --mount=type=bind,from=ctx,source=/,target=/ctx \
    --mount=type=cache,dst=/var/cache \
    --mount=type=cache,dst=/var/log \
    --mount=type=tmpfs,dst=/tmp \
    /ctx/build.sh && \
    ostree container commit

### FIX VAR/RUN SYMLINK
RUN rm -rf /var/run && ln -s /run /var/run

### LINTING
RUN bootc container lint
