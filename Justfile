export image_name := env("IMAGE_NAME", "ublue-kde-dx")
export repo_organization := env("REPO_ORGANIZATION", "silverhadch")
export image_desc := env("IMAGE_DESC", "Ublue KDE DX Development Environment - Custom bootc image with KDE unstable builds")
export image_keywords := env("IMAGE_KEYWORDS", "bootc,ublue,universal-blue,kde,development")
export image_logo_url := env("IMAGE_LOGO_URL", "https://avatars.githubusercontent.com/u/120078124?s=200&v=4")
export default_tag := env("DEFAULT_TAG", "latest")
export base_image := env("BASE_IMAGE", "ghcr.io/ublue-os/base-main:latest")

# Build the image
build $target_image=image_name $tag=default_tag:
    #!/usr/bin/env bash

    set -euox pipefail

    LABELS=()
    if [[ -z "$(git status -s)" ]]; then
        GIT_SHA=$(git rev-parse --short HEAD)
        LABELS+=("--label" "io.artifacthub.package.readme-url=https://raw.githubusercontent.com/{{ repo_organization }}/{{ image_name }}/${GIT_SHA}/README.md")
        LABELS+=("--label" "org.opencontainers.image.documentation=https://raw.githubusercontent.com/{{ repo_organization }}/{{ image_name }}/${GIT_SHA}/README.md")
        LABELS+=("--label" "org.opencontainers.image.source=https://github.com/{{ repo_organization }}/{{ image_name }}/${GIT_SHA}/Containerfile")
        LABELS+=("--label" "org.opencontainers.image.url=https://github.com/{{ repo_organization }}/{{ image_name }}/tree/${GIT_SHA}")
    fi

    LABELS+=("--label" "io.artifacthub.package.deprecated=false")
    LABELS+=("--label" "io.artifacthub.package.keywords={{ image_keywords }}")
    LABELS+=("--label" "io.artifacthub.package.license=Apache-2.0")
    LABELS+=("--label" "io.artifacthub.package.logo-url={{ image_logo_url }}")
    LABELS+=("--label" "io.artifacthub.package.prerelease=$([[ '{{ tag }}' == 'dev' ]] && echo true || echo false)")
    LABELS+=("--label" "org.opencontainers.image.created=$(date -u +%Y\-%m\-%d\T%H\:%M\:%S\Z)")
    LABELS+=("--label" "org.opencontainers.image.description={{ image_desc }}")
    LABELS+=("--label" "org.opencontainers.image.title={{ image_name }}")
    LABELS+=("--label" "org.opencontainers.image.vendor={{ repo_organization }}")
    LABELS+=("--label" "org.opencontainers.image.version=${tag}.$(date +%Y%m%d)")
    LABELS+=("--label" "containers.bootc=1")

    podman build \
        "${LABELS[@]}" \
        --build-arg BASE_IMAGE={{ base_image }} \
        --pull=newer \
        --tag "${target_image}:${tag}" \
        --file Containerfile \
        .

# Rechunk the image with rpm-ostree
ostree-rechunk $target_image=image_name $tag=default_tag:
    #!/usr/bin/env bash

    set -xeuo pipefail

    if [[ ! "${UID}" -eq "0" ]]; then
        echo "This needs to run as root."
        exit 1
    fi

    podman run --rm \
        --privileged \
        -v "/var/lib/containers:/var/lib/containers" \
        --entrypoint /usr/bin/rpm-ostree \
        "localhost/${target_image}:${tag}" \
        compose build-chunked-oci \
        --max-layers 127 \
        --format-version=2 \
        --bootc \
        --from "localhost/${target_image}:${tag}" \
        --output containers-storage:"localhost/${target_image}:${tag}"

# Generate default tag
[group('Utility')]
generate-default-tag $tag=default_tag:
    #!/usr/bin/env bash
    set -eoux pipefail
    echo "${tag}"

# Generate build tags
[group('Utility')]
generate-build-tags $target_image=image_name $tag=default_tag:
    #!/usr/bin/env bash
    set -eoux pipefail

    DATE=$(date +%Y%m%d)
    BUILD_TAGS=()
    if [[ -z "$(git status -s)" ]]; then
        GIT_SHA=$(git rev-parse --short HEAD)
        BUILD_TAGS+=("${tag}-${GIT_SHA}")
    fi

    BUILD_TAGS+=("${DATE}")
    BUILD_TAGS+=("${tag}")
    BUILD_TAGS+=("${tag}-${DATE}")

    echo "${BUILD_TAGS[@]}"

# Tag images
[group('Utility')]
tag-images $target_image=image_name $tag=default_tag tags="":
    #!/usr/bin/env bash
    set -eoux pipefail

    IMAGE=$(podman inspect ${target_image}:${tag} | jq -r .[].Id)
    podman untag ${IMAGE}

    for t in {{ tags }}; do
        podman tag $IMAGE "${target_image}:${t}"
    done

    podman images
